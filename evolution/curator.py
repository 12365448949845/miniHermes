"""
Curator 维护服务：定期整理技能库。

两阶段执行：
  Phase 1（确定性）：生命周期转换 — 7 天未使用 → stale，30 天 → archived
  Phase 2（LLM）：当 agent-created 技能 ≥ 5 个时，尝试合并重叠技能为类级别 umbrella

触发时机：session 结束时检查，距上次运行超过 7 天且空闲足够。
状态持久化在 ~/.minihermes/skills/.curator_state

使用来源追踪：只操作 agent-created 技能，不碰 bundled 技能。
"""

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from skills import USER_SKILLS_DIR, _parse_frontmatter
from evolution.telemetry import (
    get_usage, list_agent_created_skill_names,
    is_agent_created, set_state,
)
from tools.skill_manage import ARCHIVED_DIR

logger = logging.getLogger(__name__)

EVO_CURATOR_ENABLED = True
EVO_CURATOR_INTERVAL_DAYS = 7
EVO_CURATOR_STALE_DAYS = 7
EVO_CURATOR_ARCHIVE_DAYS = 30

STATE_FILE = USER_SKILLS_DIR / ".curator_state"


def _load_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _now_ts() -> float:
    return time.time()


def _days_since(iso_str: Optional[str]) -> float:
    if not iso_str:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return float("inf")


def _get_agent_created_skills() -> list[dict]:
    """获取所有 agent-created 技能及其元数据（使用来源追踪）。"""
    skills = []
    if not USER_SKILLS_DIR.is_dir():
        return skills

    agent_names = set(list_agent_created_skill_names())

    for entry in sorted(USER_SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if entry.name not in agent_names:
            continue

        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            usage = get_usage(entry.name)
            skills.append({
                "name": entry.name,
                "description": meta.get("description", ""),
                "status": usage.get("state", meta.get("status", "active")),
                "created_at": usage.get("created_at"),
                "last_used": usage.get("last_used_at"),
                "total_uses": usage.get("use_count", 0),
                "pinned": usage.get("pinned", False),
                "path": entry,
            })
        except OSError:
            continue
    return skills


def _update_skill_status(skill_path: Path, new_status: str):
    """更新技能 frontmatter 中的 status 字段。"""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.is_file():
        return
    content = skill_md.read_text(encoding="utf-8")
    import re
    if re.search(r'^status:\s*.+$', content, re.MULTILINE):
        content = re.sub(r'^status:\s*.+$', f"status: {new_status}", content, count=1, flags=re.MULTILINE)
    else:
        content = content.replace("---\n\n", f"status: {new_status}\n---\n\n", 1)
    skill_md.write_text(content, encoding="utf-8")


def lifecycle_transitions() -> dict:
    """
    Phase 1：确定性生命周期转换。

    只操作 agent-created 且非 pinned 的技能。
    Returns:
        {"marked_stale": int, "archived": int, "checked": int}
    """
    stale_days = EVO_CURATOR_STALE_DAYS
    archive_days = EVO_CURATOR_ARCHIVE_DAYS

    stats = {"marked_stale": 0, "archived": 0, "checked": 0}
    skills = _get_agent_created_skills()

    for skill in skills:
        if skill.get("pinned"):
            continue
        stats["checked"] += 1

        last_activity = skill["last_used"] or skill["created_at"]
        days_inactive = _days_since(last_activity)

        if days_inactive >= archive_days and skill["status"] != "archived":
            # Double-check: only archive agent-created skills
            if not is_agent_created(skill["name"]):
                continue
            ARCHIVED_DIR.mkdir(parents=True, exist_ok=True)
            dest = ARCHIVED_DIR / skill["name"]
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(skill["path"]), str(dest))
            set_state(skill["name"], "archived")
            stats["archived"] += 1
            logger.debug(f"Curator: archived '{skill['name']}' (inactive {days_inactive:.0f} days)")

        elif days_inactive >= stale_days and skill["status"] == "active":
            _update_skill_status(skill["path"], "stale")
            set_state(skill["name"], "stale")
            stats["marked_stale"] += 1
            logger.debug(f"Curator: marked '{skill['name']}' stale (inactive {days_inactive:.0f} days)")

    return stats


def consolidate(provider) -> Optional[str]:
    """
    Phase 2：LLM 驱动的技能合并（当 agent-created 技能 >= 5 个时触发）。

    Returns:
        合并 agent 的输出摘要，或 None（未触发）。
    """
    from agent.agent import Agent

    skills = _get_agent_created_skills()
    if len(skills) < 5:
        return None

    skill_list = "\n".join(
        f"- {s['name']}: {s['description']} [uses={s['total_uses']}, status={s['status']}]"
        for s in skills
    )

    prompt = (
        "You are the skill library curator. Below are the current agent-created skills.\n"
        "Your job is to identify opportunities to consolidate overlapping skills "
        "into broader class-level umbrellas.\n\n"
        "Current skills:\n"
        f"{skill_list}\n\n"
        "Actions you can take:\n"
        "1. Merge 2+ narrow skills into one broader umbrella (archive the narrow ones, create the umbrella)\n"
        "2. Patch an existing skill to absorb content from another\n"
        "3. Do nothing if the library is already well-organized\n\n"
        "Be conservative — only merge skills that clearly overlap. "
        "Prefer fewer high-quality skills over many narrow ones."
    )

    try:
        agent = Agent(
            provider=provider,
            db=None,
            clarify_callback=None,
            auto_approve=True,
            tool_filter={"include": {"skill_manage", "skill_view", "read_file"}},
            system_prompt_override=prompt,
            max_iterations_override=10,
        )
        result = agent.run_conversation(
            user_message="Review the skill library and consolidate if appropriate.",
            history=[],
            renderer=None,
            session_id=None,
        )
        return result.final_response
    except Exception as e:
        logger.debug(f"Curator consolidation error (non-fatal): {e}")
        return None


def should_run_curator() -> bool:
    """检查是否应该运行 curator（基于时间间隔）。"""
    if not EVO_CURATOR_ENABLED:
        return False

    interval_days = EVO_CURATOR_INTERVAL_DAYS
    state = _load_state()
    last_run = state.get("last_run_at")

    if last_run:
        days_since_run = (_now_ts() - last_run) / 86400
        if days_since_run < interval_days:
            return False

    return True


def run_curator(provider=None):
    """执行完整的 curator 运行（Phase 1 + Phase 2）。"""
    stats = lifecycle_transitions()

    consolidation_result = None
    if provider:
        consolidation_result = consolidate(provider)

    state = _load_state()
    state["last_run_at"] = _now_ts()
    state["run_count"] = state.get("run_count", 0) + 1
    state["last_stats"] = stats
    _save_state(state)

    logger.debug(
        f"Curator run complete: checked={stats['checked']}, "
        f"stale={stats['marked_stale']}, archived={stats['archived']}"
    )
    return stats


def maybe_run_curator(provider=None):
    """非阻塞：如果满足条件，在后台线程中运行 curator。"""
    if not should_run_curator():
        return

    def _run():
        try:
            run_curator(provider)
        except Exception as e:
            logger.debug(f"Curator error (non-fatal): {e}")

    t = threading.Thread(target=_run, daemon=True, name="curator")
    t.start()
