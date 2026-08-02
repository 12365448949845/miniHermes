# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MiniHermes is a Python-based AI coding assistant CLI. It provides an interactive terminal interface backed by OpenAI-compatible LLM APIs with tool execution, memory persistence, and skills.

## Build & Run

```bash
uv sync                    # Install dependencies
python main.py             # Run the CLI
bash build_wheel.sh        # Build wheel for distribution
pip install dist/minihermes-*.whl  # Install globally → `minihermes` command
```

Configuration lives at `~/.minihermes/config.yaml` (created by setup wizard from `config/config.yaml` template).

### Testing

```bash
uv sync --group test       # Install test deps (pytest, pytest-mock)
pytest                     # Run tests (no test suite exists yet)
```

No automated test suite currently exists in the project.

## Directory Structure

```
main.py                       # Entry point
agent/        agent.py        # Conversation loop orchestrator
              delegate.py     # Sub-agent delegation
approval/     engine.py       # Security approval engine
provider/     provider.py     # OpenAI SDK wrapper (streaming, retry, reasoning)
context/      context.py      # ConversationContext (tokens, budget)
              compressor.py   # 5-phase context compression
              token_utils.py  # Shared token estimation
prompt/       builder.py      # 12-layer system prompt assembly
config/       config.py       # Config class + legacy accessor functions
              setup_wizard.py # Interactive setup wizard
              config.yaml     # Default config template
tools/        registry.py     # ToolRegistry (registration, schema query, execution)
              __init__.py     # Public API + eager imports for @register side effects
              retry.py        # Tool execution retry with backoff
              approval.py     # Approval pattern data (HARDLINE, DANGEROUS, SENSITIVE)
              bash.py         # Shell command execution
              files.py        # File read/write/list
              search.py       # Exa AI web search
              code_execution.py  # Cloud sandbox code execution
              memory.py       # Cross-session persistent memory
              delegate.py     # Delegate task schema (execution in agent/delegate.py)
              todo.py         # Task planning
              clarify.py      # User clarification
              image_gen.py    # Image generation via Pollinations.ai
              process_tool.py # System process listing
              session_search.py  # FTS5 session search
              skills_tool.py  # Skill loading
              skill_manage.py # Skill lifecycle management
              web_extract.py  # Web page content extraction
session/      db.py           # SQLite WAL persistence
skills/       manager.py      # Skill discovery, YAML frontmatter parsing, index building
              preprocessing.py  # Template var substitution, inline shell expansion
              guard.py          # Security scanner for agent-created skills
              __init__.py       # Public API exports
cli/          conversation.py # Background conversation loop thread
              state.py        # AppState (shared mutable state between threads)
              commands.py     # Slash command handlers
              plan.py         # Plan mode
              context_ref.py  # @file reference preprocessor
              layout.py       # prompt_toolkit layout builder
              keybindings.py  # Keyboard shortcuts
              styles.py       # UI color styles
              completers.py   # Tab completion
              approval.py     # Approval UI rendering
              clarify.py      # Clarify UI rendering
renderer/     renderer.py     # StreamRenderer, SubagentRenderer, Rich adapters
evolution/    nudge.py        # Background memory/skill review triggers
              curator.py      # Skill lifecycle management
              telemetry.py    # Usage telemetry
_builtin_skills/              # Built-in skill templates
```

## Message Flow

```
User Input → CLI (prompt_toolkit) → Agent.run_conversation()
  → Provider.stream() [LLM call with tool schemas]
  → Tool Execution [if tool_calls returned, loop until done or budget exhausted]
  → ContextCompressor [if approaching token limits]
  → SessionDB [persist to SQLite WAL]
  → StreamRenderer [output to terminal]
```

## CLI Interface

### Keyboard Shortcuts

| Action | Key |
| --- | --- |
| Send message | `Enter` |
| Newline (multi-line) | `Shift+Enter` / `Cmd+Enter` |
| Interrupt AI response | `Ctrl+C` |
| Exit | `/exit` / `/quit` / `Ctrl+D` |

### Slash Commands

| Command | Description |
| --- | --- |
| `/help` | Show all available commands |
| `/clear` | Clear conversation, start new session |
| `/compress` | Force context compression next turn |
| `/plan [desc]` | Enter plan mode (read-only analysis → plan → approval → execute) |
| `/init` | Scan CWD and generate `minihermes.md` context file |
| `/history` | Show current session ID, turns, message count |
| `/sessions` | List recent sessions with titles and timestamps |
| `/resume <id>` | Restore a previous session (follows compression chain) |
| `/title <name>` | Set current session title |
| `/sysprompt` | Print full system prompt (debug) |
| `/setup` | Runtime config wizard (API key, model, etc.) |
| `/exit` `/quit` `/q` | Exit |
| `/<skill-name>` | Load and execute a skill (e.g. `/code-review`) |

## Core Layers

- **Entry point** (`main.py`): Wires Config, Provider, Agent, SessionDB, builds CLI app, starts background conversation thread.
- **Agent** (`agent/agent.py`): Orchestrates the conversation loop with ConversationContext (budget, token tracking). Delegates tool dispatch to ToolRegistry, approval to ApprovalEngine, compression to ContextCompressor. Main method: `run_conversation()`.
- **ApprovalEngine** (`approval/engine.py`): Two-tier security policy — HARDLINE (7 patterns, unrejectable: `rm /`, `mkfs`, `dd of=/dev/`, fork bomb, shutdown/reboot, `kill -9 1`, `kill -1`) and DANGEROUS (19 patterns requiring user confirmation: `rm`, `kill`, `chmod 777`, `git push --force`, `git reset --hard`, `curl | sh`, `sudo`, `DROP TABLE`, writes to `/etc/` / `~/.ssh/` / `.env`, etc.). Manages session whitelist for "allow for session" approvals.
- **ConversationContext** (`context/context.py`): State container for a single conversation run — token estimation, iteration budget, compression triggers, evolution counters.
- **Provider** (`provider/provider.py`): OpenAI SDK wrapper supporting streaming with `on_delta`/`on_thinking` callbacks, tool calling, and reasoning modes (deepseek-reasoner, o-series). API-level retry with jittered exponential backoff.
- **ToolRegistry** (`tools/registry.py`): Decorator-based tool registration (`@register(schema)`) with schema filtering and execution dispatch. Each instance maintains an independent registry.
- **CLI** (`cli/`): prompt_toolkit Application with streaming renderer, slash commands, approval flows, clarification modals. Conversation runs in a **background daemon thread** consuming from `AppState.input_queue`; the **main thread** runs the UI event loop.
- **Skills** (`skills/`): Markdown instruction templates with YAML frontmatter. Discovered from `~/.minihermes/skills/` (global, including built-in synced on first start), `./.minihermes/skills/` (project-local), and external directories. Two-layer cache (LRU + disk snapshot) avoids filesystem scans on every prompt build. Supports conditional activation (hide/show based on available tools), platform matching, supporting files (references/templates/scripts/assets), template variable substitution, and provenance tracking (bundled vs agent-created). See [Skill System](#skill-system) below.
- **Context Compression** (`context/compressor.py`): 5-phase strategy — boundary determination (HEAD/MIDDLE/TAIL), tool output pruning (>500 chars → one-line summary), LLM summary (12-section structured template), tool pair sanitization (fix orphaned tool_call/result pairs), assembly + session splitting. Anti-thrashing with 60s cooldown between compressions.
- **Session** (`session/db.py`): SQLite with WAL mode for multi-turn conversation persistence. FTS5 full-text search.
- **Evolution** (`evolution/`): Background nudge triggers every **10 user turns** (memory review) and every **10 tool iterations** (skill review). Curator runs every 7 days at session exit. Telemetry tracks usage patterns.
- **Prompt** (`prompt/builder.py`): Multi-layer system prompt builder. ~6 active layers: Layer 1 Identity (SOUL.md), Layer 7 Memory snapshot, Layer 9 Context files, Layer 10 Timestamp, Layer 11 Environment detection, Layer 12 Platform guidance. Invisible-char and prompt-injection scanning.

## Sub-agent Delegation

`agent/delegate.py` allows the main agent to spawn sub-agents for self-contained tasks, with their own iteration budget and tool access. The tool schema is registered in `tools/delegate.py`; actual execution is intercepted by `Agent._execute_tool()`.

Sub-agents have: independent iteration budget (default 50), auto-approved tool calls (no user prompts), no DB persistence, and restricted tool access (cannot delegate further or use clarify).

## Plan Mode

`/plan` launches a two-phase workflow managed by `cli/conversation.py:_execute_plan_mode()`:

1. **Analysis phase**: A temporary Agent is spawned with `tool_filter={"include": PLAN_ALLOWED_TOOLS}` (read-only tools only: `read_file`, `list_dir`, `bash`, `web_search`, etc.). It analyzes the codebase and produces a structured implementation plan.
2. **Approval phase**: The plan is rendered in a formatted panel. User chooses Execute / Cancel.
3. **Execution phase**: If approved, the plan text is injected as the next user message with the prefix `"Execute the following approved implementation plan..."` so the main Agent executes it.

Plans are saved to `.minihermes/plans/` for later reference. The plan agent runs with `auto_approve=True` and `max_iterations_override=50`.

## Memory System

Dual-track persistent storage at `~/.minihermes/memory/`:

| File | Purpose |
| --- | --- |
| `MEMORY.md` | Environment facts, project conventions, cross-session knowledge |
| `USER.md` | User preferences, background info, communication style |

Two states:

- **Frozen snapshot**: Loaded at session start and injected into system prompt (Layer 7). Does not change during a session.
- **Live state**: Modified via the `memory` tool — changes persist immediately and take effect next session.

Memories follow a structured format: frontmatter with `name`, `description`, `metadata` (type: user/feedback/project/reference), and body with **Why:** and **How to apply:** lines. Related memories linked via `[[name]]` wikilinks.

## Context Files

When building the system prompt (Layer 9), the project root is scanned for context files in this priority order:

```
minihermes.md → .hermes.md → HERMES.md → AGENTS.md → CLAUDE.md → .cursorrules
```

First match wins. `/init` generates `minihermes.md` by scanning the codebase.

`@file:` references in user input are preprocessed by `cli/context_ref.py`:

- `@file:path.py` — entire file
- `@file:path.py:42` — single line
- `@file:path.py:10-25` — line range
- `@file:"path with spaces/file"` — quoted paths

Resolved relative to CWD, injected as a code block with token count.

## Skill System

Skills are reusable Markdown instruction templates with YAML frontmatter, stored as directory structures under the skills directories. The design follows Hermes' skill architecture with two-layer caching, conditional activation, provenance tracking, and security scanning.

### Directory Structure

```
skill-name/
├── SKILL.md          # Required: main instructions with YAML frontmatter
├── references/       # Optional: supporting reference documents
├── templates/        # Optional: output templates
├── scripts/          # Optional: executable scripts
└── assets/           # Optional: static resources
```

### Frontmatter Format (YAML)

```yaml
---
name: skill-name                    # Required, kebab-case, max 64 chars
description: Brief description      # Required, max 1024 chars
version: 1.0.0                      # Optional
platforms: [macos, linux]           # Optional — restrict to specific OS
metadata:
  hermes:
    tags: [tag1, tag2]
    related_skills: [other-skill]
    fallback_for_tools: [primary_tool]    # Hide when tool IS available
    requires_tools: [required_tool]       # Hide when tool is NOT available
    fallback_for_toolsets: [toolset]      # Hide when toolset IS available
    requires_toolsets: [toolset]          # Hide when toolset is NOT available
required_environment_variables:          # Structured env var declarations
  - name: API_KEY
    prompt: Enter your API key
    optional: false
---
# Skill body (markdown)
```

### Skill Sources & Priority

| Source | Path | Priority |
|--------|------|----------|
| User global | `~/.minihermes/skills/<name>/SKILL.md` | Highest |
| Project local | `./.minihermes/skills/<name>/SKILL.md` | Medium |
| External dirs | configured via `EXTERNAL_DIRS` constant | Low |
| Built-in | synced from `_builtin_skills/` to user dir on first start | — |

Name conflicts: first discovered wins (user > project > external).

### Tool Interface

| Tool | Purpose |
|------|---------|
| `skill_view(name, file_path?, preprocess?)` | Load a skill's full instructions. Returns structured JSON with content, linked files, env requirements, platform compatibility, setup status. Supports `file_path` for reading supporting files. |
| `skill_manage` | CRUD operations: `create`, `edit` (full rewrite), `patch` (find-and-replace), `archive`, `restore`, `list`, `write_file`, `remove_file`. |

### skill_view Response Structure

```json
{
  "success": true,
  "name": "skill-name",
  "description": "...",
  "content": "<preprocessed body>",
  "path": "/absolute/path/to/SKILL.md",
  "skill_dir": "/absolute/path/to/skill/",
  "linked_files": {
    "references": ["references/api.md"],
    "templates": ["templates/report.tmpl"],
    "scripts": ["scripts/helper.py"],
    "assets": ["assets/logo.png"]
  },
  "required_env_vars": [{"name": "API_KEY", "prompt": "...", "optional": false}],
  "setup_needed": false,
  "platform_compatible": true,
  "category": "optional-category",
  "readiness_status": "available"
}
```

### Invocation Paths

1. **Tool call**: Agent calls `skill_view("skill-name")` → loads SKILL.md, preprocesses content, returns JSON
2. **Slash command**: User types `/skill-name` → CLI calls `load_skill_structured()`, builds rich activation message
3. **System prompt**: `build_skills_index()` generates an index block listing available skills; agent sees this and decides to call `skill_view`

### Preprocessing

`skills/preprocessing.py` applies two transformations (controlled by module-level constants):

1. **Template variable substitution** (`TEMPLATE_VARS_ENABLED = True`): `${MINIHERMES_SKILL_DIR}` → skill directory path, `${MINIHERMES_SESSION_ID}` → current session ID
2. **Inline shell expansion** (`INLINE_SHELL_ENABLED = False`): `` `!cmd` `` → command output (disabled by default for safety)

### Caching

Two-layer cache in `prompt/builder.py` eliminates filesystem scans on every system prompt build:

1. **In-process LRU** (`OrderedDict`, max 8 entries): Fastest, keyed by (skills_dirs, tools, toolsets)
2. **Disk snapshot** (`.skills_prompt_snapshot.json`): Validated by mtime/size manifest, survives restarts
3. **Filesystem scan**: Fallback, writes both caches on completion

Cache is invalidated automatically after skill mutations (create/edit/patch/archive/restore/sync) via `clear_skills_system_prompt_cache()`.

### Conditional Activation

Skills can declare visibility rules in `metadata.hermes` frontmatter:

- `fallback_for_tools`: Skill is a fallback — hide when the primary tool IS available
- `requires_tools`: Skill needs this tool — hide when it's NOT available
- `fallback_for_toolsets` / `requires_toolsets`: Same logic for toolsets

No conditions declared → always shown (backward compatible default).

### Provenance Tracking

Skills are classified by origin for safe lifecycle management:

| Origin | How Identified | Curator Policy |
|--------|---------------|----------------|
| Bundled | Listed in `.bundled_manifest` (SHA-256 hash per entry) | Never archived by curator |
| Agent-created | Everything not in bundled manifest | Subject to stale/archive lifecycle |
| Hub-installed | Listed in `.hub/lock.json` (future) | Never archived by curator |

`sync_builtin_skills()` writes `.bundled_manifest` on each run, tracking `name:sha256_hash` per skill.

### Telemetry

Unified usage database at `~/.minihermes/skills/.usage.json` (migrated from per-skill `.usage.json` sidecars on first load):

```json
{
  "skill-name": {
    "use_count": 5,
    "view_count": 5,
    "patch_count": 2,
    "last_used_at": "2026-06-27T...",
    "last_viewed_at": "2026-06-27T...",
    "last_patched_at": "2026-06-27T...",
    "created_at": "2026-06-20T...",
    "state": "active",
    "pinned": false
  }
}
```

Key functions: `bump_use()`, `bump_view()`, `bump_patch()`, `set_state()`, `set_pinned()`, `is_agent_created()`, `list_agent_created_skill_names()`.

### Lifecycle (Curator)

`evolution/curator.py` runs in the background (every 7 days, at session exit):

- **Phase 1 (deterministic)**: Agent-created, non-pinned skills: 7 days unused → `stale`, 30 days → archive to `_archived/`
- **Phase 2 (LLM)**: When ≥ 5 agent-created skills exist, spawns a sub-agent to merge overlapping skills into broader umbrellas

Pinned skills (`set_pinned(name, True)`) are excluded from all lifecycle transitions.

### Security Guard

`skills/guard.py` scans agent-created skills for threats (controlled by `GUARD_AGENT_CREATED = False`):

- **30+ threat patterns**: exfiltration, injection, destructive commands, persistence, network pipe-to-shell, obfuscation, credential exposure, supply chain
- **Structural checks**: file count limits, size limits, symlink escape detection
- **Invisible unicode detection**: zero-width characters, BOM, etc.
- **Verdicts**: `safe` / `suspicious` / `dangerous` (dangerous → blocked from install)

### Backward Compatibility

All old APIs preserved: `discover_skills()` returns same shape, `load_skill()` returns plain text, `build_skills_index()` returns same format, `_parse_frontmatter` still works, old `.usage.json` sidecars auto-migrated.

## Key Conventions

- All tools follow OpenAI function calling schema, registered via the `@register` decorator. New tool modules must be imported in `tools/__init__.py` to trigger registration.
- Tool execution retry is always enabled for `bash`, `web_extract`, `web_search` — up to 2 retries on timeout/transient errors. Bash timeout doubles on each retry (max 120s).
- All tool outputs go through `truncate_output()` (50K char limit, head 40% + tail 60%).
- Config merging: user config at `~/.minihermes/config.yaml` is merged over the default template at `config/config.yaml`. A `Config` class provides injectable access with lazy loading. Missing top-level keys are auto-filled on load.
- The build script (`build_wheel.sh`) copies source modules into `minihermes_cli/app/` before packaging. Only `minihermes_cli/` is included in the wheel. The package list in `build_wheel.sh` must stay in sync with the project structure.
- The CLI runs two threads: main thread (prompt_toolkit UI event loop) and daemon thread (conversation loop consuming from `AppState.input_queue`). `AppState` (`cli/state.py`) is the shared mutable state.
- Primary language in commits and comments is Chinese.

## Tech Stack

- **Python ≥3.11**, packaged with hatchling
- **OpenAI SDK** for LLM integration (any OpenAI-compatible endpoint)
- **prompt_toolkit** for terminal UI
- **Rich** for formatted console output
- **SQLite** (WAL mode) for session persistence
- **Exa AI SDK** for web search
- **e2b-code-interpreter** for cloud code sandbox
- **PyYAML** for configuration
