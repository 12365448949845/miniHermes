"""
Security scanner for agent-created and externally-sourced skills.

Port of hermes tools/skills_guard.py core threat patterns.
Controlled by module-level constant ``GUARD_AGENT_CREATED`` (default: False).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ── Hardcoded constant ──────────────────────────────────────────────────────

GUARD_AGENT_CREATED = False  # Set to True to enable security scanning


# ── Threat patterns ─────────────────────────────────────────────────────────

THREAT_PATTERNS = [
    # Exfiltration
    (re.compile(r'curl\s+\S+\s+-[dD].*\$?\w+', re.IGNORECASE), "exfiltration_curl"),
    (re.compile(r'nc\s+\S+\s+\d+\s*<\s*', re.IGNORECASE), "exfiltration_netcat"),
    (re.compile(r'scp\s+\S+\s+\S+@', re.IGNORECASE), "exfiltration_scp"),
    (re.compile(r'\.post\(\s*[\'"].*webhook', re.IGNORECASE), "exfiltration_webhook"),
    (re.compile(r'sendmail\s', re.IGNORECASE), "exfiltration_sendmail"),
    # Injection
    (re.compile(r'eval\s*\(', re.IGNORECASE), "injection_eval"),
    (re.compile(r'exec\s*\(', re.IGNORECASE), "injection_exec"),
    (re.compile(r'__import__\s*\(\s*[\'"]os[\'"]', re.IGNORECASE), "injection_import_os"),
    (re.compile(r'subprocess\.(call|Popen|run)', re.IGNORECASE), "injection_subprocess"),
    (re.compile(r'os\.system\s*\(', re.IGNORECASE), "injection_os_system"),
    # Destructive
    (re.compile(r'rm\s+-rf\s+/', re.IGNORECASE), "destructive_rm_rf_root"),
    (re.compile(r'rm\s+-rf\s+\$', re.IGNORECASE), "destructive_rm_rf_var"),
    (re.compile(r':\s*\(\)\s*\{\s*:\|:&\s*\};:', re.IGNORECASE), "destructive_fork_bomb"),
    (re.compile(r'dd\s+if=/dev/(zero|random|urandom)', re.IGNORECASE), "destructive_dd_overwrite"),
    (re.compile(r'mkfs\.', re.IGNORECASE), "destructive_mkfs"),
    (re.compile(r'>\s*/dev/sd[a-z]', re.IGNORECASE), "destructive_raw_disk"),
    # Persistence
    (re.compile(r'crontab\s+-', re.IGNORECASE), "persistence_cron"),
    (re.compile(r'@reboot', re.IGNORECASE), "persistence_reboot"),
    (re.compile(r'systemctl\s+enable\s+', re.IGNORECASE), "persistence_systemd"),
    (re.compile(r'launchctl\s+load', re.IGNORECASE), "persistence_launchctl"),
    (re.compile(r'~/\.bashrc', re.IGNORECASE), "persistence_bashrc"),
    (re.compile(r'~/\.zshrc', re.IGNORECASE), "persistence_zshrc"),
    # Network
    (re.compile(r'wget\s+\S+\s+-O\s+\S+\s*\|\s*sh', re.IGNORECASE), "network_pipe_to_shell"),
    (re.compile(r'curl\s+\S+\s*\|\s*(ba)?sh', re.IGNORECASE), "network_pipe_to_shell"),
    (re.compile(r'chmod\s+\+x\s+\S+\s*&&\s*\.\/', re.IGNORECASE), "network_exec_chmod"),
    # Obfuscation
    (re.compile(r'base64\s+-d\b.*\|', re.IGNORECASE), "obfuscation_base64_pipe"),
    (re.compile(r'\$\{[!^]\w+\}', re.IGNORECASE), "obfuscation_brace_expansion"),
    # Credential exposure
    (re.compile(r'(password|passwd|secret|token|api_key|apikey)\s*=\s*[\'\"][^\'\"]{8,}', re.IGNORECASE), "credential_hardcoded"),
    (re.compile(r'export\s+(AWS_|GITHUB_TOKEN|NPM_TOKEN|DOCKER_)', re.IGNORECASE), "credential_export"),
    # Supply chain
    (re.compile(r'pip\s+install\s+\S+\s*&&', re.IGNORECASE), "supply_chain_pip_chained"),
    (re.compile(r'npm\s+install\s+-g\s+', re.IGNORECASE), "supply_chain_npm_global"),
    (re.compile(r'gem\s+install\s+', re.IGNORECASE), "supply_chain_gem"),
]

# Invisible unicode characters (zero-width, BOM, etc.)
_INVISIBLE_CHARS = {
    '​', '‌', '‍', '‎', '‏',
    '⁠', '⁡', '⁢', '⁣', '⁤',
    '﻿', '­', '͏', '؜',
    'ᅟ', 'ᅠ', '឴', '឵',
    '᠎', ' ', ' ', '‪', '‫',
    '‬', '‭', '‮', '⁦', '⁧',
    '⁨', '⁩',
}


# ── Structural limits ───────────────────────────────────────────────────────

MAX_SKILL_FILES = 50
MAX_FILE_SIZE = 100_000  # bytes


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class ScanFinding:
    pattern_id: str
    line: int
    snippet: str


@dataclass
class ScanResult:
    verdict: str  # "safe", "suspicious", "dangerous"
    findings: List[ScanFinding] = field(default_factory=list)
    structural_issues: List[str] = field(default_factory=list)
    has_invisible_chars: bool = False


# ── Scanner ─────────────────────────────────────────────────────────────────


def scan_skill(skill_dir: Path, source: str = "agent-created") -> ScanResult:
    """Scan all files in a skill directory for security threats.

    Args:
        skill_dir: Path to the skill directory.
        source: Provenance source (``"agent-created"``, ``"bundled"``, ``"hub"``).

    Returns:
        ScanResult with verdict and findings.
    """
    findings: List[ScanFinding] = []
    structural: List[str] = []
    has_invisible = False

    if not skill_dir.is_dir():
        return ScanResult(verdict="safe")

    all_files = list(skill_dir.rglob("*"))
    files = [f for f in all_files if f.is_file()]

    # Structural checks
    if len(files) > MAX_SKILL_FILES:
        structural.append(f"Too many files: {len(files)} > {MAX_SKILL_FILES}")

    for f in files:
        # Size check
        try:
            size = f.stat().st_size
            if size > MAX_FILE_SIZE:
                structural.append(f"File too large: {f.name} ({size} bytes)")
        except OSError:
            continue

        # Symlink escape check
        if f.is_symlink():
            try:
                resolved = f.resolve()
                if skill_dir not in resolved.parents and resolved != skill_dir:
                    structural.append(f"Symlink escapes skill dir: {f.name} -> {resolved}")
            except OSError:
                pass

        # Content scan
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Invisible char check
        for char in _INVISIBLE_CHARS:
            if char in content:
                has_invisible = True
                findings.append(ScanFinding(
                    pattern_id="invisible_unicode",
                    line=0,
                    snippet=f"U+{ord(char):04X} in {f.name}",
                ))
                break

        # Threat pattern check
        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            for pattern, pid in THREAT_PATTERNS:
                if pattern.search(line):
                    snippet = line.strip()[:120]
                    findings.append(ScanFinding(pattern_id=pid, line=i, snippet=snippet))

    # Determine verdict
    danger_count = len([f for f in findings
                        if f.pattern_id.startswith(("destructive_", "persistence_", "exfiltration_"))])
    suspicious_count = len([f for f in findings
                            if f.pattern_id.startswith(("injection_", "network_", "obfuscation_",
                                                        "credential_", "supply_chain_"))])

    if danger_count > 0 or structural:
        verdict = "dangerous"
    elif suspicious_count > 3 or has_invisible:
        verdict = "suspicious"
    else:
        verdict = "safe"

    return ScanResult(
        verdict=verdict,
        findings=findings,
        structural_issues=structural,
        has_invisible_chars=has_invisible,
    )


def format_scan_report(result: ScanResult) -> str:
    """Format a ScanResult as a human-readable report."""
    lines = [f"Security scan verdict: **{result.verdict.upper()}**"]
    if result.findings:
        lines.append(f"\nFindings ({len(result.findings)}):")
        for f in result.findings:
            lines.append(f"  [{f.pattern_id}] line {f.line}: {f.snippet}")
    if result.structural_issues:
        lines.append(f"\nStructural issues ({len(result.structural_issues)}):")
        for s in result.structural_issues:
            lines.append(f"  - {s}")
    if result.has_invisible_chars:
        lines.append("\n⚠ Contains invisible Unicode characters.")
    return "\n".join(lines)


def should_block(result: ScanResult) -> bool:
    """Return True if the skill should be blocked from installation."""
    return result.verdict == "dangerous"
