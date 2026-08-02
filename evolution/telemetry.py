"""
技能使用遥测：追踪每个技能的使用频率和时间。

数据存储在统一的 ``~/.minihermes/skills/.usage.json`` 文件中。
支持来源追踪（bundled vs agent-created），为 Curator 提供数据支持。

旧版每个技能独立的 .usage.json sidecar 文件在首次加载时自动迁移。
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from skills import USER_SKILLS_DIR

# ── Paths ───────────────────────────────────────────────────────────────────

_USAGE_PATH = USER_SKILLS_DIR / ".usage.json"
_BUNDLED_MANIFEST_PATH = USER_SKILLS_DIR / ".bundled_manifest"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── Internal helpers ────────────────────────────────────────────────────────


def _load_usage_db() -> dict:
    """Load the unified usage database, migrating old sidecar files if needed."""
    if _USAGE_PATH.is_file():
        try:
            return json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    db = {}
    _migrate_old_sidecars(db)
    return db


def _save_usage_db(db: dict):
    """Atomically write the usage database."""
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(USER_SKILLS_DIR), suffix=".tmp")
    try:
        os.write(fd, json.dumps(db, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, str(_USAGE_PATH))


def _migrate_old_sidecars(db: dict):
    """Migrate per-skill .usage.json sidecar files into the unified database.

    Only runs once — after migration, old sidecar files are deleted.
    """
    if not USER_SKILLS_DIR.is_dir():
        return
    migrated = False
    for entry in USER_SKILLS_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        sidecar = entry / ".usage.json"
        if not sidecar.is_file():
            continue
        try:
            old_data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        name = entry.name
        if name not in db:
            db[name] = {
                "use_count": old_data.get("total_uses", 0),
                "view_count": old_data.get("total_uses", 0),
                "patch_count": 0,
                "last_used_at": old_data.get("last_used"),
                "last_viewed_at": old_data.get("last_used"),
                "last_patched_at": None,
                "created_at": old_data.get("created_at", _now_iso()),
                "state": "active",
                "pinned": False,
                "archived_at": None,
            }
        # Delete old sidecar
        try:
            sidecar.unlink()
        except OSError:
            pass
        migrated = True

    if migrated:
        _save_usage_db(db)


def _ensure_record(db: dict, skill_name: str) -> dict:
    """Ensure a record exists for *skill_name*, creating one if missing."""
    if skill_name not in db:
        db[skill_name] = {
            "use_count": 0,
            "view_count": 0,
            "patch_count": 0,
            "last_used_at": None,
            "last_viewed_at": None,
            "last_patched_at": None,
            "created_at": _now_iso(),
            "state": "active",
            "pinned": False,
            "archived_at": None,
        }
    return db[skill_name]


# ── Bundled manifest ────────────────────────────────────────────────────────


def _read_bundled_names() -> set:
    """Read bundled skill names from .bundled_manifest."""
    if not _BUNDLED_MANIFEST_PATH.is_file():
        return set()
    names = set()
    for line in _BUNDLED_MANIFEST_PATH.read_text(encoding="utf-8").strip().splitlines():
        if ":" in line:
            names.add(line.split(":", 1)[0].strip())
    return names


def is_agent_created(skill_name: str) -> bool:
    """Return True if the skill was created by the agent (not bundled or hub-installed)."""
    bundled = _read_bundled_names()
    return skill_name not in bundled


def list_agent_created_skill_names() -> List[str]:
    """List all skill names that are agent-created (not bundled)."""
    bundled = _read_bundled_names()
    result = []
    if not USER_SKILLS_DIR.is_dir():
        return result
    for entry in USER_SKILLS_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if (entry / "SKILL.md").is_file():
            if entry.name not in bundled:
                result.append(entry.name)
    return result


# ── Public API ──────────────────────────────────────────────────────────────


def init_usage(skill_name: str):
    """Initialize telemetry for a newly created skill (idempotent)."""
    db = _load_usage_db()
    _ensure_record(db, skill_name)
    _save_usage_db(db)


def record_usage(skill_name: str):
    """Record a skill use (called when skill_view loads a skill)."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    now = _now_iso()
    rec["use_count"] += 1
    rec["view_count"] += 1
    if not rec.get("last_used_at"):
        rec["last_used_at"] = now
    rec["last_used_at"] = now
    rec["last_viewed_at"] = now
    _save_usage_db(db)


def bump_use(skill_name: str):
    """Record a skill use without incrementing view count (for slash-command loads)."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    now = _now_iso()
    rec["use_count"] += 1
    if not rec.get("last_used_at"):
        rec["last_used_at"] = now
    rec["last_used_at"] = now
    _save_usage_db(db)


def bump_view(skill_name: str):
    """Record a skill view (skill_view tool call)."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    now = _now_iso()
    rec["view_count"] += 1
    rec["last_viewed_at"] = now
    _save_usage_db(db)


def bump_patch(skill_name: str):
    """Record a skill patch/edit."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    now = _now_iso()
    rec["patch_count"] += 1
    rec["last_patched_at"] = now
    _save_usage_db(db)


def get_usage(skill_name: str) -> dict:
    """Get usage data for a single skill."""
    db = _load_usage_db()
    return db.get(skill_name, {})


def list_all_usage() -> Dict[str, dict]:
    """Get usage data for all known skills."""
    db = _load_usage_db()
    return db


def set_state(skill_name: str, state: str):
    """Set lifecycle state: 'active', 'stale', or 'archived'."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    rec["state"] = state
    if state == "archived":
        rec["archived_at"] = _now_iso()
    _save_usage_db(db)


def set_pinned(skill_name: str, pinned: bool):
    """Pin or unpin a skill (pinned skills are skipped by curator lifecycle transitions)."""
    db = _load_usage_db()
    rec = _ensure_record(db, skill_name)
    rec["pinned"] = pinned
    _save_usage_db(db)


def forget(skill_name: str):
    """Remove usage tracking for a skill (e.g., when skill is deleted)."""
    db = _load_usage_db()
    db.pop(skill_name, None)
    _save_usage_db(db)
