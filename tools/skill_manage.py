"""
skill_manage 工具：让 agent 程序化地创建、修改、归档技能。

操作: create | edit | patch | archive | restore | list | write_file | remove_file

设计参考 hermes tools/skill_manager_tool.py。
"""

import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tools import register
from skills import USER_SKILLS_DIR, discover_skills, parse_frontmatter

# ── Constants ───────────────────────────────────────────────────────────────

_KEBAB_CASE = re.compile(r'^[a-z][a-z0-9]*(-[a-z0-9]+)*$')
_MAX_NAME_LEN = 64
_MAX_DESC_LEN = 100
_MAX_BODY_CHARS = 4000  # hardcoded, not from config
_MAX_AUTO_SKILLS = 20

ARCHIVED_DIR = USER_SKILLS_DIR / "_archived"

# Allowed subdirectories for write_file
_ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}

# Allowed file extensions for write_file (safety allowlist)
_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".sh", ".bash", ".zsh", ".js", ".ts", ".html", ".css",
    ".csv", ".xml", ".svg", ".toml",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _count_agent_skills() -> int:
    from evolution.telemetry import list_agent_created_skill_names
    return len(list_agent_created_skill_names())


def _clear_skills_cache():
    """Clear the skills system prompt cache after mutation."""
    try:
        from prompt.builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache()
    except ImportError:
        pass


def _validate_path_traversal(skill_dir: Path, file_path: str) -> Path:
    """Resolve file_path within skill_dir and validate no path traversal."""
    requested = (skill_dir / file_path).resolve()
    if skill_dir not in requested.parents and requested != skill_dir:
        raise ValueError(f"Path traversal denied: '{file_path}'")
    return requested


# ── Actions ─────────────────────────────────────────────────────────────────


def _create_skill(name: str, description: str, body: str) -> str:
    if not name:
        return "Error: name is required."
    if len(name) > _MAX_NAME_LEN:
        return f"Error: name must be <= {_MAX_NAME_LEN} characters."
    if not _KEBAB_CASE.match(name):
        return "Error: name must be kebab-case (e.g. 'python-testing', 'git-workflow')."
    if not description:
        return "Error: description is required."
    if len(description) > _MAX_DESC_LEN:
        return f"Error: description must be <= {_MAX_DESC_LEN} characters."
    if not body:
        return "Error: body is required."
    if len(body) > _MAX_BODY_CHARS:
        return f"Error: body must be <= {_MAX_BODY_CHARS} characters (got {len(body)})."

    current_count = _count_agent_skills()
    if current_count >= _MAX_AUTO_SKILLS:
        return (
            f"Error: agent-created skill limit reached ({current_count}/{_MAX_AUTO_SKILLS}). "
            f"Archive unused skills first."
        )

    skill_dir = USER_SKILLS_DIR / name
    skill_md = skill_dir / "SKILL.md"

    if skill_md.exists():
        return f"Error: skill '{name}' already exists. Use action 'patch' or 'edit' to modify it."

    now = _now_iso()
    frontmatter = yaml.dump({
        "name": name,
        "description": description,
        "source": "agent-created",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }, allow_unicode=True, default_flow_style=False).strip()

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")

    from evolution.telemetry import init_usage
    init_usage(name)

    return f"Created skill '{name}' at {skill_dir}"


def _edit_skill(name: str, body: str) -> str:
    """Overwrite the entire SKILL.md body while preserving frontmatter provenance fields."""
    if not name:
        return "Error: name is required."
    if not body:
        return "Error: body is required."
    if len(body) > _MAX_BODY_CHARS:
        return f"Error: body must be <= {_MAX_BODY_CHARS} characters (got {len(body)})."

    skill_md = USER_SKILLS_DIR / name / "SKILL.md"
    if not skill_md.is_file():
        return f"Error: skill '{name}' not found."

    content = skill_md.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(content)

    # Preserve provenance fields
    created_at = fm.get("created_at", _now_iso())
    source = fm.get("source", "agent-created")

    now = _now_iso()
    new_fm = yaml.dump({
        "name": name,
        "description": fm.get("description", ""),
        "source": source,
        "status": fm.get("status", "active"),
        "created_at": created_at,
        "updated_at": now,
    }, allow_unicode=True, default_flow_style=False).strip()

    skill_md.write_text(f"---\n{new_fm}\n---\n\n{body}", encoding="utf-8")

    from evolution.telemetry import bump_patch
    bump_patch(name)

    return f"Edited skill '{name}' successfully."


def _patch_skill(name: str, old_string: str, new_string: str) -> str:
    if not name:
        return "Error: name is required."
    if not old_string:
        return "Error: old_string is required for patch."
    if not new_string:
        return "Error: new_string is required for patch."
    if old_string == new_string:
        return "Error: old_string and new_string are identical."

    skill_md = USER_SKILLS_DIR / name / "SKILL.md"
    if not skill_md.is_file():
        return f"Error: skill '{name}' not found."

    content = skill_md.read_text(encoding="utf-8")
    if old_string not in content:
        return f"Error: old_string not found in skill '{name}'."

    occurrences = content.count(old_string)
    if occurrences > 1:
        return f"Error: old_string appears {occurrences} times — provide a more specific match."

    new_content = content.replace(old_string, new_string, 1)

    # Update updated_at timestamp
    now = _now_iso()
    if "updated_at:" in content:
        new_content = re.sub(r'updated_at:\s*.+', f"updated_at: {now}", new_content, count=1)
    else:
        new_content = new_content.replace("---\n\n", f"updated_at: {now}\n---\n\n", 1)

    skill_md.write_text(new_content, encoding="utf-8")

    from evolution.telemetry import bump_patch
    bump_patch(name)

    return f"Patched skill '{name}' successfully."


def _archive_skill(name: str) -> str:
    if not name:
        return "Error: name is required."

    skill_dir = USER_SKILLS_DIR / name
    if not skill_dir.is_dir():
        return f"Error: skill '{name}' not found."

    # Check provenance — only agent-created skills can be archived by agent
    from evolution.telemetry import is_agent_created
    if not is_agent_created(name):
        return f"Error: skill '{name}' is not agent-created and cannot be archived by the agent."

    ARCHIVED_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVED_DIR / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(skill_dir), str(dest))

    from evolution.telemetry import set_state
    set_state(name, "archived")

    return f"Archived skill '{name}' to {dest}"


def _restore_skill(name: str) -> str:
    """Restore an archived skill back to the active skills directory."""
    if not name:
        return "Error: name is required."

    archived_path = ARCHIVED_DIR / name
    if not archived_path.is_dir():
        return f"Error: skill '{name}' not found in archive."

    dest_path = USER_SKILLS_DIR / name
    if dest_path.exists():
        return f"Error: skill '{name}' already exists in active skills."

    shutil.move(str(archived_path), str(dest_path))

    from evolution.telemetry import set_state
    set_state(name, "active")

    return f"Restored skill '{name}' from archive."


def _write_skill_file(name: str, file_path: str, content: str) -> str:
    """Create or overwrite a file within a skill's supporting directories."""
    if not name:
        return "Error: name is required."
    if not file_path:
        return "Error: file_path is required."
    if not content:
        return "Error: content is required."

    skill_dir = USER_SKILLS_DIR / name
    if not skill_dir.is_dir():
        return f"Error: skill '{name}' not found."

    # Validate subdirectory
    parts = Path(file_path).parts
    if not parts:
        return "Error: file_path must not be empty."
    subdir = parts[0]
    if subdir not in _ALLOWED_SUBDIRS:
        return f"Error: file_path must start with one of: {', '.join(sorted(_ALLOWED_SUBDIRS))}."

    # Validate extension
    ext = Path(file_path).suffix.lower()
    if ext and ext not in _ALLOWED_EXTENSIONS:
        return f"Error: file extension '{ext}' is not allowed in skills."

    # Path traversal check
    try:
        target = _validate_path_traversal(skill_dir, file_path)
    except ValueError as e:
        return f"Error: {e}"

    # Don't allow overwriting SKILL.md
    if target.name == "SKILL.md" and target.parent == skill_dir:
        return "Error: use 'edit' or 'patch' to modify SKILL.md, not write_file."

    # Size limit
    if len(content) > 50_000:
        return f"Error: file content too large ({len(content)} chars, max 50000)."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return f"Wrote file '{file_path}' in skill '{name}' ({len(content)} chars)."


def _remove_skill_file(name: str, file_path: str) -> str:
    """Delete a file from a skill's supporting directories."""
    if not name:
        return "Error: name is required."
    if not file_path:
        return "Error: file_path is required."

    skill_dir = USER_SKILLS_DIR / name
    if not skill_dir.is_dir():
        return f"Error: skill '{name}' not found."

    try:
        target = _validate_path_traversal(skill_dir, file_path)
    except ValueError as e:
        return f"Error: {e}"

    # Don't allow removing SKILL.md through this path
    if target.name == "SKILL.md" and target.parent == skill_dir:
        return "Error: use 'archive' to remove a skill entirely. Cannot delete SKILL.md via remove_file."

    if not target.is_file():
        return f"Error: file '{file_path}' not found in skill '{name}'."

    try:
        target.unlink()
    except OSError as e:
        return f"Error: failed to delete '{file_path}': {e}"

    return f"Removed file '{file_path}' from skill '{name}'."


def _list_skills() -> str:
    """List all skills with metadata as structured JSON."""
    from evolution.telemetry import get_usage, is_agent_created

    skills = discover_skills()
    if not skills:
        return json.dumps({"success": True, "skills": []}, ensure_ascii=False)

    result = []
    for s in skills:
        name = s["name"]
        usage = get_usage(name)
        source = "bundled" if not is_agent_created(name) else "agent-created"
        result.append({
            "name": name,
            "description": s["description"],
            "source": source,
            "status": usage.get("state", "active"),
            "use_count": usage.get("use_count", 0),
            "patch_count": usage.get("patch_count", 0),
            "category": s.get("category"),
        })

    return json.dumps({"success": True, "skills": result}, ensure_ascii=False)


# ── Schema & Registration ───────────────────────────────────────────────────

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_manage",
        "description": (
            "Create, modify, or archive reusable skills. "
            "Skills are class-level umbrellas — create broad, reusable skills "
            "(e.g. 'python-testing') not narrow one-offs (e.g. 'fix-pytest-fixture-in-foo'). "
            "Only create a skill when the pattern would clearly recur."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "patch", "archive", "restore", "list", "write_file", "remove_file"],
                    "description": (
                        "create: new skill | edit: full rewrite body | patch: find-and-replace | "
                        "archive: move to _archived/ | restore: move back from _archived/ | "
                        "list: show all skills | write_file: create/update supporting file | "
                        "remove_file: delete supporting file"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Skill name in kebab-case (e.g. 'python-testing'). Required for most actions.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description (max 100 chars). Required for create.",
                },
                "body": {
                    "type": "string",
                    "description": "Skill body content in markdown. Required for create and edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to find in existing skill. Required for patch.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text. Required for patch.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Relative path within the skill directory (e.g. 'references/api.md'). Required for write_file and remove_file.",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write. Required for write_file.",
                },
            },
            "required": ["action"],
        },
    },
}


@register(_SCHEMA)
def skill_manage(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "",
    content: str = "",
) -> str:
    result = _skill_manage_impl(
        action, name=name, description=description, body=body,
        old_string=old_string, new_string=new_string,
        file_path=file_path, content=content,
    )
    # Clear skills cache after mutations
    if action in ("create", "edit", "patch", "archive", "restore", "write_file", "remove_file"):
        _clear_skills_cache()
    return result


def _skill_manage_impl(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "",
    content: str = "",
) -> str:
    if action == "create":
        return _create_skill(name, description, body)
    if action == "edit":
        return _edit_skill(name, body)
    if action == "patch":
        return _patch_skill(name, old_string, new_string)
    if action == "archive":
        return _archive_skill(name)
    if action == "restore":
        return _restore_skill(name)
    if action == "list":
        return _list_skills()
    if action == "write_file":
        return _write_skill_file(name, file_path, content)
    if action == "remove_file":
        return _remove_skill_file(name, file_path)
    return (
        f"Error: unknown action '{action}'. "
        f"Use: create | edit | patch | archive | restore | list | write_file | remove_file."
    )
