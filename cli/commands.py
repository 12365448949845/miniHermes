"""斜杠命令定义与处理。"""

import json
import sys
import time
from datetime import datetime
from typing import Optional
import uuid

from renderer.renderer import _cprint, _DIM, _RST
from renderer import print_resumed_history
from session import SessionDB
from skills import discover_skills, load_skill, load_skill_structured
import config as cfg


SLASH_COMMANDS: dict[str, str] = {
    "/clear":     "Clear conversation history",
    "/compress":  "Manually trigger context compression",
    "/plan":      "Enter plan mode (read-only analysis, then execute)",
    "/init":      "Scan project and generate minihermes.md",
    "/history":   "Show current conversation length",
    "/sessions":  "List recent sessions",
    "/agents":    "List recent Agent runs",
    "/agent":     "Show one Agent run",
    "/artifacts": "Show execution artifacts, retention, or cleanup",
    "/recoveries": "List failure classifications for an Agent run",
    "/recovery":  "Show one failure recovery audit record",
    "/worktrees": "List isolated Worktree candidates",
    "/worktree":  "Show one Worktree candidate",
    "/integrate-worktree": "Verify and merge one Worktree candidate",
    "/discard-worktree": "Permanently discard one Worktree candidate",
    "/replay":    "Replay one recorded bash command in an isolated copy",
    "/cancel":    "Request cancellation of an Agent run",
    "/resume":    "Resume a previous session",
    "/title":     "Set title for current session",
    "/sysprompt": "Print current system prompt (debug)",
    "/help":      "Show available commands",
    "/setup":     "Interactive configuration setup",
    "/exit":      "Exit MiniHermes",
    "/quit":      "Exit MiniHermes",
}


_INIT_INSTRUCTION = """[/init] Please analyze this codebase and create a minihermes.md file at the current working directory's root.

What to include:
1. Build/run/test commands used in this project
2. High-level architecture (the "big picture" that requires reading multiple files to understand)
3. Key conventions or patterns that aren't obvious from a single file

Guidelines:
- Read the README.md if present, plus key config files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.)
- Use list_dir and read_file to explore structure; sample a few core source files
- Keep it concise — focus on what a future agent needs to be productive quickly
- Do NOT include obvious instructions ("write tests", "handle errors", "follow security best practices")
- Do NOT enumerate every file or directory — list only what matters
- If a CLAUDE.md / AGENTS.md / .hermes.md / README.md already exists, read it first and adapt key insights

Begin the file with this exact header:

# minihermes.md

This file provides project context to miniHermes when working in this repository.

When done, write the file using the write_file tool with path="minihermes.md", then briefly tell me what you captured.
"""


def generate_session_id() -> str:
    """生成唯一 session id。"""
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{timestamp_str}_{short_uuid}"


def register_skill_commands():
    """启动时扫描 skills 并追加到 SLASH_COMMANDS。"""
    for skill in discover_skills():
        cmd = f"/{skill['name']}"
        if cmd not in SLASH_COMMANDS:
            SLASH_COMMANDS[cmd] = f"[skill] {skill['description']}"


def handle_slash_command(
    cmd: str, history: list, db: SessionDB, session_id: str,
    agent=None, runtime=None, conversation_id: str | None = None,
) -> tuple[bool, list, str, Optional[str]]:
    """
    处理斜杠命令。

    Returns:
        (handled, history, session_id, override_message)
        handled=True 表示命令已处理完毕（主循环应 continue）
        override_message 非 None 时替换原始输入后继续交给 agent
    """
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        db.end_session(session_id, end_reason="user_exit")
        print("Bye!")
        sys.exit(0)

    if command == "/compress":
        print("[manual compression triggered — will execute on next LLM call]")
        return True, history, session_id, None

    if command == "/clear":
        print("\033[2J\033[H", end="")
        print("[history cleared — starting new session]")
        db.end_session(session_id, end_reason="clear")
        new_id = generate_session_id()
        from provider.provider import MODEL_NAME
        model_name = cfg.get_model_config().get("name") or MODEL_NAME
        db.create_session(new_id, model_name,
                          model_config=json.dumps(cfg.get_model_config(), ensure_ascii=False))
        return True, [], new_id, None

    if command == "/history":
        user_turns = sum(1 for m in history if m["role"] == "user")
        print(f"[session: {session_id} | {user_turns} user turns, {len(history)} total messages]")
        return True, history, session_id, None

    if command == "/sessions":
        sessions = db.list_sessions(limit=15)
        if not sessions:
            print("[no sessions found]")
        else:
            print(f"{'ID':<14} {'Messages':>4}  {'Time':<20} Title")
            print("─" * 60)
            for s in sessions:
                t = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
                title = s["title"] or ""
                active = " ◀" if s["id"] == session_id else ""
                print(f"{s['id']:<14} {s['message_count']:>4}  {t:<20} {title}{active}")
        return True, history, session_id, None

    if command == "/agents":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        runs = runtime.list_runs(conversation_id=conversation_id, limit=20)
        if not runs:
            print("[no Agent runs found for this conversation]")
            return True, history, session_id, None
        print(f"{'RUN ID':<32} {'KIND':<12} {'STATUS':<18} {'TIME':>8}  TASK")
        print("─" * 96)
        now = time.time()
        for run in runs:
            started = run.get("started_at") or run.get("created_at") or now
            finished = run.get("finished_at") or now
            duration = max(0.0, finished - started)
            task = runtime.get_task(run["task_id"]) or {}
            title = (task.get("title") or "")[:40]
            print(
                f"{run['run_id']:<32} {run['agent_kind']:<12} "
                f"{run['status']:<18} {duration:>7.1f}s  {title}"
            )
        return True, history, session_id, None

    if command == "/recoveries":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        run_id = None
        run_ref = arg.strip()
        if run_ref:
            run = runtime.get_run(run_ref)
            if run is None:
                matches = [
                    item for item in runtime.list_runs(limit=200)
                    if item["run_id"].startswith(run_ref)
                ]
                if len(matches) == 1:
                    run = matches[0]
                elif len(matches) > 1:
                    print(f"[run id prefix is ambiguous: {run_ref}]")
                    return True, history, session_id, None
            if run is None:
                print(f"[Agent run not found: {run_ref}]")
                return True, history, session_id, None
            run_id = run["run_id"]
        elif conversation_id:
            recent = runtime.list_runs(
                conversation_id=conversation_id, limit=1
            )
            run_id = recent[0]["run_id"] if recent else None
        records = runtime.list_recoveries(run_id=run_id, limit=50)
        if not records:
            suffix = f" for run {run_id}" if run_id else ""
            print(f"[no recovery records found{suffix}]")
            return True, history, session_id, None
        print(f"{'RECOVERY ID':<32} {'CLASS':<18} {'ACTION':<16} STATUS")
        print("─" * 92)
        for record in records:
            print(
                f"{record['recovery_id']:<32} "
                f"{record['failure_class']:<18} "
                f"{record['selected_action']:<16} {record['status']}"
            )
        return True, history, session_id, None

    if command == "/recovery":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        recovery_ref = arg.strip()
        if not recovery_ref:
            print("[usage: /recovery <recovery_id>]")
            return True, history, session_id, None
        record = runtime.get_recovery(recovery_ref)
        if record is None:
            matches = runtime.find_recoveries_by_prefix(recovery_ref, limit=3)
            if len(matches) == 1:
                record = matches[0]
            elif len(matches) > 1:
                print(f"[recovery id prefix is ambiguous: {recovery_ref}]")
                return True, history, session_id, None
        if record is None:
            print(f"[recovery record not found: {recovery_ref}]")
            return True, history, session_id, None
        print(f"[Recovery {record['recovery_id']}]")
        print(
            f"run: {record['run_id']}  source: {record['source_kind']}  "
            f"status: {record['status']}"
        )
        print(
            f"failure: {record['failure_class']}/{record['error_code']}  "
            f"decision: {record['selected_action']}"
        )
        if record.get("tool_execution_id"):
            print(f"tool execution: {record['tool_execution_id']}")
            evidence = runtime.get_execution_record_for_tool_execution(
                record["tool_execution_id"]
            )
            if evidence:
                print(
                    f"source evidence: {evidence['record_id']}  "
                    f"logs={evidence['log_status']}  "
                    f"artifacts={evidence['artifact_status']}"
                )
            attempts = runtime.list_tool_retry_attempts(
                record["tool_execution_id"]
            )
            for attempt in attempts:
                wait = attempt.get("wait_status", "NOT_SCHEDULED")
                delay = attempt.get("retry_delay_seconds")
                wait_detail = wait
                if delay is not None:
                    wait_detail += f"({delay:.2f}s)"
                detail = attempt["status"]
                if attempt.get("error_code"):
                    detail += f"/{attempt['error_code']}"
                print(
                    f"  attempt {attempt['attempt_number']}: {detail}  "
                    f"retryable={attempt['retryable']} wait={wait_detail}"
                )
        if record.get("parent_recovery_id"):
            print(f"parent recovery: {record['parent_recovery_id']}")
        if record.get("result_record_id"):
            print(f"result evidence: {record['result_record_id']}")
        if record.get("workspace_id"):
            print(f"workspace: {record['workspace_id']}")
        print(
            f"attempts: {record['attempt_number']}/{record['max_attempts']}  "
            f"version: {record['version']}"
        )
        reason = record.get("reason") or {}
        print(
            "policy: "
            f"audit_only={reason.get('audit_only', False)} "
            f"registered_error={reason.get('registered_error', False)} "
            f"retry_eligible={reason.get('retry_eligible', False)}"
        )
        return True, history, session_id, None

    if command == "/worktrees":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        leases = runtime.list_worktrees(limit=50)
        if not leases:
            print("[no Worktree candidates found]")
            return True, history, session_id, None
        print(f"{'WORKSPACE ID':<32} {'STATUS':<13} {'CLEANUP':<10} SCOPE")
        print("─" * 96)
        for lease in leases:
            scope = ", ".join(lease.get("write_scope") or ())[:35]
            print(
                f"{lease['workspace_id']:<32} {lease['lease_status']:<13} "
                f"{lease['cleanup_status']:<10} {scope}"
            )
        return True, history, session_id, None

    if command in {"/worktree", "/integrate-worktree", "/discard-worktree"}:
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        workspace_ref = arg.strip()
        if not workspace_ref:
            print(f"[usage: {command} <workspace_id>]")
            return True, history, session_id, None
        lease = runtime.get_worktree(workspace_ref)
        if lease is None:
            matches = [
                item for item in runtime.list_worktrees(limit=200)
                if item["workspace_id"].startswith(workspace_ref)
            ]
            if len(matches) == 1:
                lease = matches[0]
            elif len(matches) > 1:
                print(f"[workspace id prefix is ambiguous: {workspace_ref}]")
                return True, history, session_id, None
        if lease is None:
            print(f"[Worktree candidate not found: {workspace_ref}]")
            return True, history, session_id, None
        try:
            detail = runtime.inspect_worktree(lease["workspace_id"])
        except Exception as exc:
            print(f"[Worktree inspection failed: {exc}]")
            return True, history, session_id, None
        print(f"[Worktree {detail['workspace_id']}]")
        print(
            f"status: {detail['lease_status']}  cleanup: {detail['cleanup_status']}  "
            f"run: {detail['run_id']}"
        )
        print(f"base: {detail['base_commit']}  branch: {detail['branch_name']}")
        print(f"scope: {', '.join(detail.get('write_scope') or ())}")
        changes = detail.get("current_changes") or []
        print(f"changes: {len(changes)}")
        for change in changes[:20]:
            print(f"  {change['status']:<10} {change['path']}")
        if len(changes) > 20:
            print(f"  ... {len(changes) - 20} more")
        for violation in detail.get("current_violations") or []:
            print(f"violation: {violation}")
        latest_integration = detail.get("latest_integration")
        if latest_integration:
            print(
                "latest integration: "
                f"{latest_integration['integration_id']} "
                f"status={latest_integration['status']}"
            )
        if command == "/integrate-worktree":
            print("[starting explicit verification and two-step approval]")
            try:
                integrated = runtime.integrate_worktree(detail["workspace_id"])
                lease_result = integrated.get("lease") or {}
                print(
                    f"[Worktree integration {integrated.get('status', 'UNKNOWN')}: "
                    f"{integrated.get('integration_id', detail['workspace_id'])}]"
                )
                if integrated.get("final_merge_commit"):
                    print(f"merge commit: {integrated['final_merge_commit']}")
                print(
                    f"candidate: {lease_result.get('lease_status', 'UNKNOWN')}  "
                    f"cleanup: {lease_result.get('cleanup_status', 'UNKNOWN')}"
                )
            except Exception as exc:
                print(f"[Worktree integration failed to start: {exc}]")
            return True, history, session_id, None
        if command == "/discard-worktree":
            print("[discarding this unmerged candidate permanently]")
            try:
                discarded = runtime.discard_worktree(detail["workspace_id"])
                print(
                    f"[Worktree discarded: {discarded['workspace_id']} "
                    f"cleanup={discarded['cleanup_status']}]"
                )
            except Exception as exc:
                print(f"[Worktree discard failed: {exc}]")
        return True, history, session_id, None

    if command == "/agent":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        run_ref = arg.strip()
        if not run_ref:
            print("[usage: /agent <run_id>]")
            return True, history, session_id, None
        run = runtime.get_run(run_ref)
        if run is None:
            matches = [
                item for item in runtime.list_runs(limit=200)
                if item["run_id"].startswith(run_ref)
            ]
            if len(matches) == 1:
                run = matches[0]
            elif len(matches) > 1:
                print(f"[run id prefix is ambiguous: {run_ref}]")
                return True, history, session_id, None
        if run is None:
            print(f"[Agent run not found: {run_ref}]")
            return True, history, session_id, None

        task = runtime.get_task(run["task_id"]) or {}
        print(f"[Agent run {run['run_id']}]")
        print(f"kind: {run['agent_kind']}  status: {run['status']}  attempt: {run['attempt']}")
        print(f"task: {task.get('title') or run['task_id']}")
        if run.get("parent_run_id"):
            print(f"parent run: {run['parent_run_id']}")
        print(
            f"iterations: {run['iterations_used']}/{run['max_iterations']}  "
            f"provider calls: {run['provider_attempts']}  "
            f"tokens: {run['prompt_tokens']} in / {run['completion_tokens']} out"
        )
        if run.get("completion_reason"):
            print(f"completion: {run['completion_reason']}")
        if run.get("error_message"):
            print(f"error: {run['error_message']}")
        events = runtime.list_events(run["run_id"])
        if events:
            event_names = " → ".join(event["event_type"] for event in events)
            print(f"events: {event_names}")
        tool_executions = runtime.list_tool_executions(run["run_id"])
        if tool_executions:
            print("tools:")
            for execution in tool_executions:
                detail = execution["status"]
                if execution.get("error_code"):
                    detail += f"/{execution['error_code']}"
                print(
                    f"  {execution['tool_name']}: {detail}  "
                    f"attempts={execution['attempts']} "
                    f"execution={execution['execution_id']}"
                )
                for attempt in runtime.list_tool_retry_attempts(
                    execution["execution_id"]
                ):
                    wait = attempt.get("wait_status", "NOT_SCHEDULED")
                    delay = attempt.get("retry_delay_seconds")
                    if delay is not None:
                        wait += f"({delay:.2f}s)"
                    attempt_detail = attempt["status"]
                    if attempt.get("error_code"):
                        attempt_detail += f"/{attempt['error_code']}"
                    print(
                        f"    #{attempt['attempt_number']} {attempt_detail} "
                        f"retryable={attempt['retryable']} wait={wait}"
                    )
        records = runtime.list_execution_records(run["run_id"])
        if records:
            print("execution records:")
            for record in records:
                snapshot_detail = ""
                if record.get("snapshot_id"):
                    snapshot = runtime.get_workspace_snapshot(record["snapshot_id"])
                    snapshot_status = (
                        snapshot.get("artifact_status", "AVAILABLE")
                        if snapshot else "MISSING"
                    )
                    snapshot_detail = (
                        f" snapshot={record['snapshot_id']}({snapshot_status})"
                    )
                print(
                    f"  {record['record_id']}: {record['reproducibility_status']} "
                    f"artifact={record['artifact_status']} replay={record['replay_status']}"
                    f"{snapshot_detail}"
                )
        return True, history, session_id, None

    if command == "/artifacts":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        run_ref = arg.strip()
        if run_ref in {"retention", "cleanup"}:
            try:
                if run_ref == "retention":
                    summary = runtime.inspect_execution_artifact_retention()
                    reasons = summary.get("blocked_reasons", {})
                    reason_text = ", ".join(
                        f"{name}={count}" for name, count in sorted(reasons.items())
                    ) or "none"
                    print(
                        "[artifact retention: "
                        f"groups={summary['groups']} eligible={summary['eligible_groups']} "
                        f"blocked={summary['blocked_groups']} "
                        f"already_purged={summary['already_purged_groups']} "
                        f"reasons={reason_text}]"
                    )
                else:
                    summary = runtime.cleanup_execution_artifacts()
                    errors = summary.get("errors", [])
                    error_text = ", ".join(errors[:3]) if errors else "none"
                    print(
                        "[artifact cleanup: "
                        f"purged_groups={summary['purged_groups']}/"
                        f"{summary['claimed_groups']} "
                        f"records={summary['purged_records']} "
                        f"orphan_bundles={summary.get('orphan_bundles', 0)} "
                        f"deleted_bytes={summary['deleted_bytes']} "
                        f"remaining_bytes={summary['remaining_bytes']} "
                        f"errors={error_text}]"
                    )
            except Exception as exc:
                print(f"[artifact retention unavailable: {exc}]")
            return True, history, session_id, None
        if not run_ref:
            print("[usage: /artifacts <run_id> | retention | cleanup]")
            return True, history, session_id, None
        run = runtime.get_run(run_ref)
        if run is None:
            matches = [item for item in runtime.list_runs(limit=200) if item["run_id"].startswith(run_ref)]
            if len(matches) == 1:
                run = matches[0]
            elif len(matches) > 1:
                print(f"[run id prefix is ambiguous: {run_ref}]")
                return True, history, session_id, None
        if run is None:
            print(f"[Agent run not found: {run_ref}]")
            return True, history, session_id, None
        records = runtime.list_execution_records(run["run_id"])
        if not records:
            print("[no execution artifacts for this run]")
            return True, history, session_id, None
        print(f"[execution artifacts for {run['run_id']}]")
        snapshots = runtime.list_workspace_snapshots(run["run_id"])
        if snapshots:
            print("snapshots:")
            for snapshot in snapshots:
                print(
                    f"  {snapshot['snapshot_id']}  "
                    f"capture={snapshot['capture_status']} "
                    f"artifact={snapshot.get('artifact_status', 'AVAILABLE')}"
                )
        for record in records:
            snapshot_detail = ""
            if record.get("snapshot_id"):
                snapshot = runtime.get_workspace_snapshot(record["snapshot_id"])
                snapshot_status = (
                    snapshot.get("artifact_status", "AVAILABLE")
                    if snapshot else "MISSING"
                )
                snapshot_detail = f" snapshot={record['snapshot_id']}({snapshot_status})"
            print(
                f"{record['record_id']}  {record['tool_name']}  "
                f"{record['reproducibility_status']}  artifact={record['artifact_status']}  "
                f"replay={record['replay_status']}{snapshot_detail}"
            )
        return True, history, session_id, None

    if command == "/replay":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        record_ref = arg.strip()
        if not record_ref:
            print("[usage: /replay <record_id>]")
            return True, history, session_id, None
        record = runtime.get_execution_record(record_ref)
        if record is None:
            matches = runtime.find_execution_records_by_prefix(record_ref)
            if len(matches) == 1:
                record = matches[0]
            elif len(matches) > 1:
                print(f"[execution record id prefix is ambiguous: {record_ref}]")
                return True, history, session_id, None
        if record is None:
            print(f"[execution record not found: {record_ref}]")
            return True, history, session_id, None
        try:
            outcome = runtime.replay_execution(
                record["record_id"], conversation_id=conversation_id
            )
        except Exception as exc:
            print(f"[replay unavailable: {exc}]")
            return True, history, session_id, None
        print(
            f"[replay {outcome.status.value.lower()}: run={outcome.run_id} "
            f"reason={outcome.completion_reason}]"
        )
        return True, history, session_id, None

    if command == "/cancel":
        if runtime is None:
            print("[Agent Runtime is not available]")
            return True, history, session_id, None
        run_ref = arg.strip()
        if not run_ref:
            print("[usage: /cancel <run_id>]")
            return True, history, session_id, None
        run = runtime.get_run(run_ref)
        if run is None:
            matches = [
                item for item in runtime.list_runs(limit=200)
                if item["run_id"].startswith(run_ref)
            ]
            if len(matches) == 1:
                run = matches[0]
            elif len(matches) > 1:
                print(f"[run id prefix is ambiguous: {run_ref}]")
                return True, history, session_id, None
        if run is None:
            print(f"[Agent run not found: {run_ref}]")
            return True, history, session_id, None
        status = runtime.cancel(run["run_id"])
        print(f"[cancel request for {run['run_id']}: {status}]")
        return True, history, session_id, None

    if command == "/resume":
        target_id = arg.strip()
        if not target_id:
            sessions = db.list_sessions(limit=5)
            candidates = [s for s in sessions if s["id"] != session_id and s["message_count"] > 0]
            if not candidates:
                print("[no previous session to resume]")
                return True, history, session_id, None
            target_id = candidates[0]["id"]

        # 解析压缩链路：走到最新的有消息的 session
        resolved_id = db.resolve_resume_session_id(target_id)

        all_msgs = db.get_messages(resolved_id)
        if not all_msgs:
            print(f"[session {resolved_id} has no messages]")
            return True, history, session_id, None

        db.end_session(session_id, end_reason="resumed")
        if resolved_id != target_id:
            print(f"[session {target_id} was compressed → following to {resolved_id}]")
        print(f"[resumed session {resolved_id} with {len(all_msgs)} messages]")
        print_resumed_history(all_msgs)
        llm_msgs = db.get_messages_for_llm(resolved_id)
        return True, llm_msgs, resolved_id, None

    if command == "/title":
        if not arg:
            print("[usage: /title <name>]")
        else:
            db.set_title(session_id, arg)
            print(f"[session titled: {arg.strip()[:100]}]")
        return True, history, session_id, None

    if command == "/sysprompt":
        if agent is None or not getattr(agent, "system_prompt", None):
            print("[no system prompt available]")
            return True, history, session_id, None
        sp = agent.system_prompt
        char_count = len(sp)
        token_estimate = char_count // 4
        print(f"{_DIM}─── system prompt ({char_count} chars, ~{token_estimate} tokens) ───{_RST}")
        print(sp)
        print(f"{_DIM}─── end of system prompt ───{_RST}")
        return True, history, session_id, None

    if command == "/help":
        print(
            "/clear       — clear history & start new session\n"
            "/compress    — manually trigger context compression\n"
            "/history     — show current session info\n"
            "/sessions    — list recent sessions\n"
            "/agents      — list recent Agent runs\n"
            "/agent <id>  — show one Agent run\n"
            "/artifacts <run_id> — show recorded command artifacts\n"
            "/artifacts retention — show protected/expired artifact groups\n"
            "/artifacts cleanup — purge only expired, unprotected artifacts\n"
            "/recoveries [run_id] — list failure classifications and decisions\n"
            "/recovery <id> — show one recovery audit record\n"
            "/worktrees    — list isolated write candidates\n"
            "/worktree <id> — inspect one candidate and its changed files\n"
            "/integrate-worktree <id> — verify and merge one candidate locally\n"
            "/discard-worktree <id> — permanently remove an unmerged candidate\n"
            "/replay <record_id> — replay one recorded bash command\n"
            "/resume [id] — resume a previous session\n"
            "/title <name>— name the current session\n"
            "/sysprompt   — print current system prompt (debug)\n"
            "/setup       — interactive configuration setup\n"
            "/init        — scan project and generate minihermes.md\n"
            "/exit        — exit MiniHermes\n"
            "Ctrl+C       — interrupt current response\n"
            "Ctrl+D       — exit\n"
            "Shift+Enter / Cmd+Enter — new line (multiline input)"
        )
        return True, history, session_id, None

    if command == "/plan":
        override_msg = f"__PLAN_MODE__:{arg}"
        return False, history, session_id, override_msg

    if command == "/init":
        from pathlib import Path
        target = Path.cwd() / "minihermes.md"
        if target.exists():
            print(f"[minihermes.md already exists at {target}. Delete it first to regenerate.]")
            return True, history, session_id, None
        return False, history, session_id, _INIT_INSTRUCTION

    if command == "/setup":
        # /setup 的实际处理在 conversation.py 中通过 run_in_terminal 完成
        # 如果走到这里说明不在正常对话循环上下文中
        from config.setup_wizard import run_setup_cli
        try:
            run_setup_cli()  # 无 event loop，直接运行（首次向导等场景）
        except Exception as e:
            print(f"[Setup error: {e}]")
        return True, history, session_id, None

    # 尝试匹配 skill（优先用结构化数据，降级到旧版 load_skill）
    skill_name = command.lstrip("/")
    skill_info = load_skill_structured(skill_name)
    if skill_info:
        # Build rich activation message
        lines = [
            f"[IMPORTANT: The user has invoked the '{skill_name}' skill. "
            f"Follow the instructions below unless the user asks otherwise.]",
            "",
        ]
        # Category hint
        if skill_info.get("category"):
            lines.insert(0, f"[Skill category: {skill_info['category']}]")

        # Supporting files hint
        linked = skill_info.get("linked_files", {})
        has_linked = any(v for v in linked.values())
        if has_linked:
            lines.append(f"[This skill has supporting files at {skill_info['skill_dir']}:]")
            for subdir, files in linked.items():
                if files:
                    file_list = ", ".join(files[:5])
                    if len(files) > 5:
                        file_list += f" (+{len(files) - 5} more)"
                    lines.append(f"  {subdir}/: {file_list}")
            lines.append(f"[Use skill_view('{skill_name}', file_path='...') to load a specific file.]")
            lines.append("")

        # Platform warning
        if not skill_info.get("platform_compatible", True):
            lines.append("[WARNING: This skill may not be fully compatible with your current platform.]")
            lines.append("")

        # Setup warning
        if skill_info.get("setup_needed"):
            lines.append(f"[SETUP NEEDED: {skill_info.get('setup_note', 'Some environment variables are missing.')}]")
            lines.append("")

        # Main content
        lines.append(skill_info["content"])

        # User instruction
        if arg:
            lines.append(f"\n\n[User request: {arg}]")

        override_msg = "\n".join(lines)
        return False, history, session_id, override_msg

    # Fallback: try old-school load_skill for backward compat
    skill_content = load_skill(skill_name)
    if skill_content:
        msg = f"[Skill '{skill_name}' loaded. Follow these instructions:]\n\n{skill_content}"
        if arg:
            msg += f"\n\n[User request: {arg}]"
        return False, history, session_id, msg

    print(f"[unknown command: {command}. Type /help for available commands]")
    return True, history, session_id, None
