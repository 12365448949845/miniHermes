"""skill_view 工具：让 agent 按需加载 skill 的完整指令。

返回结构化 JSON（hermes 兼容），包含：
  - 预处理后的正文
  - 附属文件列表（references/templates/scripts/assets）
  - 环境变量需求
  - 平台兼容性

支持 file_path 参数读取技能目录内的附属文件。
"""

import json
import os
from pathlib import Path

from tools import register
from skills import discover_skills, load_skill_structured
from skills.preprocessing import preprocess_skill_content

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": (
            "Load a skill's full instructions by name. "
            "Use this when a task matches an available skill listed in the system prompt. "
            "Returns structured JSON with content, linked files, env requirements, and setup status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to load (e.g. 'code-review')"
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Optional: relative path to a supporting file within the skill directory "
                        "(e.g. 'references/api.md', 'templates/report.tmpl'). "
                        "Omit to load the main SKILL.md content."
                    ),
                },
                "preprocess": {
                    "type": "boolean",
                    "description": "Whether to apply template variable substitution. Default: true.",
                },
            },
            "required": ["name"]
        }
    }
}


@register(_SCHEMA)
def skill_view(name: str, file_path: str = "", preprocess: bool = True) -> str:
    """Load a skill and return structured JSON."""
    return _skill_view_impl(name, file_path=file_path, preprocess=preprocess)


def _skill_view_impl(
    name: str,
    file_path: str = "",
    preprocess: bool = True,
    session_id: str = "",
) -> str:
    """Core implementation — also callable from other modules."""
    # Resolve skill
    info = load_skill_structured(name)
    if info is None:
        available = [s["name"] for s in discover_skills()]
        return json.dumps({
            "success": False,
            "error": f"Skill '{name}' not found.",
            "available_skills": available,
            "hint": "Use one of the available skills listed above, or check the name for typos.",
        }, ensure_ascii=False)

    # Check platform compatibility
    if not info.get("platform_compatible", True):
        return json.dumps({
            "success": False,
            "error": f"Skill '{name}' is not compatible with the current platform.",
            "platform_compatible": False,
        }, ensure_ascii=False)

    # If requesting a supporting file
    if file_path:
        return _serve_supporting_file(info, file_path)

    # Check required environment variables
    env_vars = info.get("required_env_vars", [])
    setup_needed = False
    setup_note = None
    missing_vars = []

    if env_vars:
        for ev in env_vars:
            if isinstance(ev, dict):
                ev_name = ev.get("name", "")
                ev_optional = ev.get("optional", False)
            else:
                ev_name = str(ev)
                ev_optional = False

            if ev_name and ev_name not in os.environ:
                missing_vars.append(ev_name)
                if not ev_optional:
                    setup_needed = True

        if missing_vars:
            setup_note = (
                f"This skill requires environment variables: {', '.join(missing_vars)}. "
                f"Set them before using this skill."
            )

    # Preprocess content
    content = info["content"]
    if preprocess:
        content = preprocess_skill_content(
            content,
            skill_dir=info["skill_dir"],
            session_id=session_id,
        )

    # Record usage
    try:
        from evolution.telemetry import record_usage
        record_usage(name)
    except ImportError:
        pass

    return json.dumps({
        "success": True,
        "name": info["name"],
        "description": info["description"],
        "content": content,
        "path": info["path"],
        "skill_dir": info["skill_dir"],
        "linked_files": info["linked_files"],
        "required_env_vars": env_vars,
        "setup_needed": setup_needed,
        "setup_note": setup_note,
        "setup_skipped": False,
        "readiness_status": "setup_needed" if setup_needed else "available",
        "platform_compatible": info["platform_compatible"],
        "category": info["category"],
    }, ensure_ascii=False)


def _serve_supporting_file(info: dict, file_path: str) -> str:
    """Serve a supporting file from within a skill directory.

    Security: validates no path traversal (file_path must stay within skill_dir).
    """
    skill_dir = Path(info["skill_dir"])
    requested = (skill_dir / file_path).resolve()

    # Path traversal check
    if skill_dir not in requested.parents and requested != skill_dir:
        return json.dumps({
            "success": False,
            "error": f"Path traversal denied: '{file_path}' is outside the skill directory.",
            "available_files": info.get("linked_files", {}),
        }, ensure_ascii=False)

    # Don't serve SKILL.md through this path (use main skill_view for that)
    if requested.name == "SKILL.md" and requested.parent == skill_dir:
        return json.dumps({
            "success": False,
            "error": "Use skill_view without file_path to load the main SKILL.md content.",
        }, ensure_ascii=False)

    if not requested.is_file():
        return json.dumps({
            "success": False,
            "error": f"File '{file_path}' not found in skill '{info['name']}'.",
            "available_files": info.get("linked_files", {}),
        }, ensure_ascii=False)

    try:
        content = requested.read_text(encoding="utf-8")
    except OSError as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to read '{file_path}': {e}",
        }, ensure_ascii=False)

    return json.dumps({
        "success": True,
        "name": info["name"],
        "file_path": file_path,
        "content": content,
        "size": len(content),
    }, ensure_ascii=False)
