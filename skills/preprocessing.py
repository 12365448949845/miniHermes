"""
Skill content preprocessing: template variable substitution and inline shell expansion.

Hardcoded constants control behavior (no config.yaml exposure).
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# ── Hardcoded constants ─────────────────────────────────────────────────────

TEMPLATE_VARS_ENABLED = True
INLINE_SHELL_ENABLED = False
INLINE_SHELL_TIMEOUT = 10  # seconds
INLINE_SHELL_MAX_OUTPUT = 4000  # chars


# ── Template variable substitution ──────────────────────────────────────────


# Known template variables
_TEMPLATE_VARS = {
    "MINIHERMES_SKILL_DIR": None,   # resolved at call time
    "MINIHERMES_SESSION_ID": None,  # resolved at call time
}

_TEMPLATE_RE = re.compile(r'\$\{([A-Z_][A-Z0-9_]*)\}')


def substitute_template_vars(
    content: str,
    skill_dir: str = "",
    session_id: str = "",
) -> str:
    """Replace ``${MINIHERMES_SKILL_DIR}`` and ``${MINIHERMES_SESSION_ID}`` tokens.

    Unresolved tokens are left in place (no error).
    """
    if not TEMPLATE_VARS_ENABLED:
        return content

    replacements = {
        "MINIHERMES_SKILL_DIR": skill_dir,
        "MINIHERMES_SESSION_ID": session_id,
    }

    def _replace(m: re.Match) -> str:
        varname = m.group(1)
        value = replacements.get(varname)
        if value is not None:
            return value
        return m.group(0)  # leave unresolved tokens in place

    return _TEMPLATE_RE.sub(_replace, content)


# ── Inline shell expansion ──────────────────────────────────────────────────


_INLINE_SHELL_RE = re.compile(r'`!([^`]+)`')


def expand_inline_shell(
    content: str,
    skill_dir: str = "",
    timeout: int = INLINE_SHELL_TIMEOUT,
) -> str:
    """Expand `` `!cmd` `` patterns by executing the command via shell.

    Output is capped at ``INLINE_SHELL_MAX_OUTPUT`` chars.
    Failures are replaced with an error marker.
    """
    if not INLINE_SHELL_ENABLED:
        return content

    def _expand(m: re.Match) -> str:
        cmd = m.group(1).strip()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=skill_dir or None,
            )
            output = (result.stdout + result.stderr).strip()
            if not output and result.returncode != 0:
                return f"[shell: exit {result.returncode}]"
            if len(output) > INLINE_SHELL_MAX_OUTPUT:
                output = output[:INLINE_SHELL_MAX_OUTPUT] + "\n...[truncated]"
            return output
        except subprocess.TimeoutExpired:
            return f"[shell: timeout after {timeout}s]"
        except Exception as e:
            return f"[shell error: {e}]"

    return _INLINE_SHELL_RE.sub(_expand, content)


# ── Combined preprocessing ──────────────────────────────────────────────────


def preprocess_skill_content(
    content: str,
    skill_dir: str = "",
    session_id: str = "",
) -> str:
    """Apply all preprocessing steps to skill content.

    Order: template vars → inline shell.
    """
    content = substitute_template_vars(content, skill_dir=skill_dir, session_id=session_id)
    content = expand_inline_shell(content, skill_dir=skill_dir)
    return content
