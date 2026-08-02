"""
Skills discovery and loading module.

Skills are Markdown instruction templates loaded from multiple locations:
1. ~/.minihermes/skills/ — user-global skills (including built-in synced on first start)
2. {cwd}/.minihermes/skills/ — project-level skills (auto-discovered)
3. External directories (hardcoded constant, relative paths resolved against MINIHERMES_HOME)

When a skill name exists in multiple locations, the user-global version takes priority.

Frontmatter is YAML-based (hermes-compatible), with a regex fallback for malformed YAML.
"""

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import MINIHERMES_HOME

USER_SKILLS_DIR = MINIHERMES_HOME / "skills"
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "_builtin_skills"

# ── Hardcoded constants (no config.yaml exposure) ──────────────────────────

EXTERNAL_DIRS: List[str] = []
DISABLED_SKILLS: set = set()

# ── Platform mapping ───────────────────────────────────────────────────────

_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

_EXCLUDED_SKILL_DIRS = frozenset({".git", ".github", ".hub", "_archived"})

# ── Lazy YAML loader ───────────────────────────────────────────────────────

_yaml_load_fn = None


def _yaml_load(content: str):
    """Parse YAML with lazy import — prefers CSafeLoader, falls back to SafeLoader."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


# ── Frontmatter parsing ────────────────────────────────────────────────────


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses yaml with CSafeLoader preference for full YAML support (nested metadata,
    lists, etc.) with a fallback to simple key:value regex for robustness.

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = _yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # Fallback: simple key:value regex for malformed YAML
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# Backward-compatible alias — old code imports _parse_frontmatter
_parse_frontmatter = parse_frontmatter


# ── Platform matching ───────────────────────────────────────────────────────


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Skills declare platform requirements via a top-level ``platforms`` list::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = _PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


# ── Directory helpers ───────────────────────────────────────────────────────


def get_external_skills_dirs() -> List[Path]:
    """Return validated external skill directory paths.

    Each entry in EXTERNAL_DIRS is expanded (``~`` and ``${VAR}``) and resolved.
    Relative paths are resolved against MINIHERMES_HOME.
    Only directories that actually exist are returned.
    """
    seen: set = set()
    result: List[Path] = []
    local_skills = USER_SKILLS_DIR.resolve()

    for entry in EXTERNAL_DIRS:
        entry = str(entry).strip()
        if not entry:
            continue
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        if not p.is_absolute():
            p = (MINIHERMES_HOME / p).resolve()
        else:
            p = p.resolve()
        if p == local_skills:
            continue
        if p in seen:
            continue
        seen.add(p)
        if p.is_dir():
            result.append(p)

    return result


def _get_skills_dirs() -> List[Path]:
    """Return all skill directories in priority order (user > project > external)."""
    dirs = [USER_SKILLS_DIR]
    project_dir = Path.cwd() / ".minihermes" / "skills"
    if project_dir.is_dir():
        dirs.append(project_dir)
    dirs.extend(get_external_skills_dirs())
    return dirs


# ── File iteration ──────────────────────────────────────────────────────────


def iter_skill_index_files(skills_dir: Path, filename: str = "SKILL.md"):
    """Walk *skills_dir* recursively yielding sorted paths matching *filename*.

    Excludes ``.git``, ``.github``, ``.hub``, ``_archived`` directories.
    Supports nested category directories (e.g. ``skills/devops/git-workflow/SKILL.md``).
    """
    if not skills_dir.is_dir():
        return
    matches = []
    for root, dirs, files in os.walk(skills_dir, followlinks=True):
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


# ── Condition extraction ────────────────────────────────────────────────────


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation fields from parsed frontmatter.

    Skills can declare when they should be shown/hidden via::

        metadata:
          hermes:
            fallback_for_tools: [primary_tool]
            requires_tools: [required_tool]
            fallback_for_toolsets: [primary_toolset]
            requires_toolsets: [required_toolset]
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    hermes = metadata.get("hermes") or {}
    if not isinstance(hermes, dict):
        hermes = {}
    return {
        "fallback_for_toolsets": _normalize_list(hermes.get("fallback_for_toolsets", [])),
        "requires_toolsets": _normalize_list(hermes.get("requires_toolsets", [])),
        "fallback_for_tools": _normalize_list(hermes.get("fallback_for_tools", [])),
        "requires_tools": _normalize_list(hermes.get("requires_tools", [])),
    }


def extract_skill_config_vars(frontmatter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract config variable declarations from parsed frontmatter.

    Skills declare config.yaml settings they need via::

        metadata:
          hermes:
            config:
              - key: wiki.path
                description: Path to the knowledge base directory
                default: "~/wiki"
                prompt: Wiki directory path
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return []
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        return []
    raw = hermes.get("config")
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or key in seen:
            continue
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        entry: Dict[str, Any] = {"key": key, "description": desc}
        default = item.get("default")
        if default is not None:
            entry["default"] = default
        prompt_text = item.get("prompt")
        if isinstance(prompt_text, str) and prompt_text.strip():
            entry["prompt"] = prompt_text.strip()
        else:
            entry["prompt"] = desc
        seen.add(key)
        result.append(entry)
    return result


def _normalize_list(values) -> List[str]:
    """Normalize a value that may be a string, list, or None into a list of strings."""
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(v).strip() for v in values if str(v).strip()]
    return []


# ── Description extraction ──────────────────────────────────────────────────


def _extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a description from parsed frontmatter, truncating to ~60 chars."""
    raw_desc = frontmatter.get("description", "")
    if not raw_desc:
        return ""
    desc = str(raw_desc).strip().strip("'\"")
    if len(desc) > 60:
        return desc[:57] + "..."
    return desc


# ── Category detection ──────────────────────────────────────────────────────


def _detect_category(skill_md_path: Path, skills_dir: Path) -> Optional[str]:
    """Detect the category from the directory structure.

    If the skill is at ``skills_dir/category-name/skill-name/SKILL.md``,
    returns ``"category-name"``. Returns None for flat skills.
    """
    try:
        rel = skill_md_path.relative_to(skills_dir)
    except ValueError:
        return None
    parts = rel.parts
    # Expected: category/skill-name/SKILL.md  or  skill-name/SKILL.md
    if len(parts) >= 3:
        return parts[0]
    return None


# ── Core public API ─────────────────────────────────────────────────────────


def discover_skills() -> List[dict]:
    """Scan all skills directories, return metadata list. User skills win on name conflict.

    Returns:
        List of dicts with keys: name, description, path, skill_dir, category, platforms
    """
    skills: List[dict] = []
    seen_names: set[str] = set()

    for skills_dir in _get_skills_dirs():
        if not skills_dir.is_dir():
            continue

        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue

            fm, _ = parse_frontmatter(content)
            name = fm.get("name") or skill_file.parent.name

            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            if name in seen_names:
                continue

            # Check disabled
            if name in DISABLED_SKILLS:
                continue

            # Check platform compatibility
            if not skill_matches_platform(fm):
                continue

            seen_names.add(name)
            skills.append({
                "name": name,
                "description": _extract_skill_description(fm),
                "path": skill_file,
                "skill_dir": skill_file.parent,
                "category": _detect_category(skill_file, skills_dir),
                "platforms": fm.get("platforms", []),
            })

    return skills


def load_skill(name: str) -> Optional[str]:
    """Load a skill's full instruction body. Searches in priority order.

    Returns the body text after the frontmatter, or None if not found.
    """
    for skills_dir in _get_skills_dirs():
        if not skills_dir.is_dir():
            continue

        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue

            fm, body = parse_frontmatter(content)
            skill_name = fm.get("name") or skill_file.parent.name
            if not isinstance(skill_name, str):
                continue
            if skill_name.strip() == name:
                return body.strip()

    return None


def load_skill_structured(name: str) -> Optional[Dict[str, Any]]:
    """Load a skill with full structured metadata. Like load_skill() but returns a dict.

    Returns None if not found.
    """
    for skills_dir in _get_skills_dirs():
        if not skills_dir.is_dir():
            continue

        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue

            fm, body = parse_frontmatter(content)
            skill_name = fm.get("name") or skill_file.parent.name
            if not isinstance(skill_name, str):
                continue
            if skill_name.strip() != name:
                continue

            name_str = skill_name.strip()
            skill_dir = skill_file.parent

            # Enumerate linked files
            linked = {"references": [], "templates": [], "scripts": [], "assets": []}
            for subdir in ("references", "templates", "scripts", "assets"):
                sub_path = skill_dir / subdir
                if sub_path.is_dir():
                    linked[subdir] = sorted(
                        str(f.relative_to(skill_dir))
                        for f in sub_path.rglob("*")
                        if f.is_file()
                    )

            # Extract env var requirements
            required_env_vars = fm.get("required_environment_variables", [])
            if isinstance(required_env_vars, dict):
                required_env_vars = [required_env_vars]
            if not isinstance(required_env_vars, list):
                required_env_vars = []

            # Platform check
            platform_ok = skill_matches_platform(fm)

            return {
                "name": name_str,
                "description": _extract_skill_description(fm),
                "content": body.strip(),
                "raw_content": body.strip(),
                "path": str(skill_file),
                "skill_dir": str(skill_dir),
                "linked_files": linked,
                "required_env_vars": required_env_vars,
                "platform_compatible": platform_ok,
                "category": _detect_category(skill_file, skills_dir),
                "frontmatter": fm,
            }

    return None


def build_skills_index() -> str:
    """Generate skills index text for system prompt injection."""
    skills = discover_skills()
    if not skills:
        return ""

    lines = [
        "## Available Skills",
        "If a task matches a skill below, load it with the skill_view tool before proceeding.",
        "",
    ]

    # Group by category
    categorized: Dict[str, List[dict]] = {}
    uncategorized: List[dict] = []
    for s in skills:
        cat = s.get("category")
        if cat:
            categorized.setdefault(cat, []).append(s)
        else:
            uncategorized.append(s)

    for cat_name in sorted(categorized.keys()):
        lines.append(f"  {cat_name}:")
        for s in categorized[cat_name]:
            lines.append(f"    - {s['name']}: {s['description']}")

    if uncategorized:
        if categorized:
            lines.append("  uncategorized:")
        for s in uncategorized:
            if categorized:
                lines.append(f"    - {s['name']}: {s['description']}")
            else:
                lines.append(f"- {s['name']}: {s['description']}")

    return "\n".join(lines)


# ── Built-in skill sync ─────────────────────────────────────────────────────


def sync_builtin_skills() -> bool:
    """Copy built-in skills to user skills directory. Skips if already exists (idempotent).

    Also writes/updates ``.bundled_manifest`` for provenance tracking.
    Returns True if any skills were synced.
    """
    if not _BUILTIN_SKILLS_DIR.is_dir():
        return False

    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    synced = False
    manifest_entries: List[str] = []

    # Read existing manifest
    manifest_path = USER_SKILLS_DIR / ".bundled_manifest"
    existing_manifest: Dict[str, str] = {}
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding="utf-8").strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                existing_manifest[k.strip()] = v.strip()

    for entry in sorted(_BUILTIN_SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue

        # Compute hash
        content = skill_md.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode()).hexdigest()[:16]
        manifest_entries.append(f"{entry.name}:{sha}")

        dest_dir = USER_SKILLS_DIR / entry.name
        dest_file = dest_dir / "SKILL.md"

        if dest_file.exists():
            # Check if bundled version changed
            prev_hash = existing_manifest.get(entry.name, "")
            if prev_hash == sha:
                continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(content, encoding="utf-8")
        synced = True

    # Write manifest
    manifest_path.write_text("\n".join(manifest_entries) + "\n", encoding="utf-8")

    return synced
