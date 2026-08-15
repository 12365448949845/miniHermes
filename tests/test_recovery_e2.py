"""E2 repair handoff, verification linking, and terminal closure tests."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent.agent import Agent
from agent.recovery import RecoveryController
from agent.reproducibility import ArtifactStore, ExecutionEvidenceRecorder
from agent.runtime import AgentRunContext, AgentRuntimeManager
from cli.commands import handle_slash_command
from session import SessionDB
from session.db import SCHEMA_VERSION
from tools import get_tool_manager
from tools.registry import ToolExecutionContext, resolve_tool_access_policy


class _ToolMessageProvider:
    model = "test-model"

    @staticmethod
    def build_tool_result_message(*, tool_call_id: str, result: str) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        }


def _running_context(db: SessionDB, suffix: str) -> AgentRunContext:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    conversation_id = f"conversation-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id=conversation_id,
        session_id=None,
        parent_task_id=None,
        kind="main_turn",
        title=suffix,
        request_preview=suffix,
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=None,
        conversation_id=conversation_id,
        start_session_id=None,
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=10,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task_id)
    return AgentRunContext(
        task_id=task_id,
        run_id=run_id,
        conversation_id=conversation_id,
        start_session_id="",
    )


def _bash_call(command: str, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": command, "timeout": 5}),
        },
    }


def _python_command(source: str) -> str:
    return f'"{sys.executable}" -c "{source}"'


def _init_git(path: Path) -> None:
    subprocess.run(
        ["git", "init"], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "config", "user.email", "e2@example.invalid"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "E2 Test"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "base.txt"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_bash(
    db: SessionDB,
    recorder: ExecutionEvidenceRecorder,
    context: AgentRunContext,
    working_directory: Path,
    command: str,
    call_id: str,
):
    registry = get_tool_manager()
    return registry.execute_detailed(
        _bash_call(command, call_id),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=context,
            working_directory=str(working_directory),
            evidence_recorder=recorder,
        ),
    )


def _manual_repair_record(
    db: SessionDB, context: AgentRunContext, suffix: str
) -> dict:
    execution_id = f"execution-{suffix}"
    db.create_tool_execution(
        execution_id=execution_id,
        run_id=context.run_id,
        tool_call_id=f"call-{suffix}",
        tool_name="bash",
    )
    db.finish_tool_execution(
        execution_id=execution_id,
        status="FAILED",
        attempts=1,
        retryable=False,
        error_code="nonzero_exit",
        error_message="failed",
        output_preview="failed",
    )
    record, _ = db.create_initial_failure_recovery(
        recovery_id=f"recovery-{suffix}",
        run_id=context.run_id,
        node_run_id=None,
        tool_execution_id=execution_id,
        failure_class="CODE_EXECUTION",
        error_code="nonzero_exit",
        selected_action="REPAIR_REQUIRED",
        status="REPAIR_REQUIRED",
        reason={"recovery_mode": "repair_and_rerun"},
    )
    return record


def test_v10_migrates_to_v11_with_verification_column_and_unique_source(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db._conn.execute("DROP INDEX idx_execution_records_verification")
    db._conn.execute("DROP INDEX idx_recovery_tool_source")
    db._conn.execute(
        "ALTER TABLE execution_records DROP COLUMN verification_key"
    )
    db._conn.execute("PRAGMA user_version=10")
    db.close()

    migrated = SessionDB(path)
    columns = {
        row[1] for row in migrated._conn.execute(
            "PRAGMA table_info(execution_records)"
        ).fetchall()
    }
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "verification_key" in columns
    indexes = {
        row[1] for row in migrated._conn.execute(
            "PRAGMA index_list(failure_recovery_records)"
        ).fetchall()
    }
    assert "idx_recovery_tool_source" in indexes
    migrated.close()


def test_agent_receives_fixed_redacted_repair_summary_with_evidence(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "summary")
    secret = "sk-e2-secret-value-123456789"
    recorder = ExecutionEvidenceRecorder(
        ArtifactStore(tmp_path / "artifacts"),
        db,
        known_secrets=(secret,),
    )
    monkeypatch.chdir(tmp_path)
    agent = Agent(
        _ToolMessageProvider(),
        db=db,
        tool_db=db,
        system_prompt_override="test",
        evidence_recorder=recorder,
    )
    command = _python_command(
        "import sys; "
        f"print('UNTRUSTED {secret} ' + 'x' * 2000 + ' TAIL_SENTINEL'); "
        "sys.exit(7)"
    )
    messages: list[dict] = []
    history: list[dict] = []

    agent._process_tool_call(
        _bash_call(command, "summary-call"),
        None,
        messages,
        history,
        None,
        "",
        context.run_id,
        context,
    )

    tool_content = messages[-1]["content"]
    assert tool_content.startswith("REPAIR_REQUIRED\n")
    payload = json.loads(tool_content.split("\n", 1)[1])
    assert payload["kind"] == "repair_required"
    assert payload["error_code"] == "nonzero_exit"
    assert payload["diagnostic_trust"] == "untrusted_data"
    assert payload["evidence"]["record_id"]
    assert payload["evidence"]["exit_code"] == 7
    assert secret not in tool_content
    assert "[REDACTED]" in payload["diagnostic_excerpt"]
    assert "TAIL_SENTINEL" not in tool_content
    assert len(payload["diagnostic_excerpt"]) <= 500
    assert history[-1] == messages[-1]
    db.close()


def test_same_verification_after_repair_resolves_with_new_evidence(tmp_path):
    _init_git(tmp_path)
    db = SessionDB(tmp_path.parent / f"{tmp_path.name}-state.db")
    context = _running_context(db, "resolved")
    events = []
    context.event_callback = lambda name, payload: events.append((name, payload))
    recorder = ExecutionEvidenceRecorder(
        ArtifactStore(tmp_path.parent / f"{tmp_path.name}-artifacts"), db
    )
    command = _python_command(
        "from pathlib import Path; import sys; "
        "sys.exit(0 if Path('ready.flag').exists() else 7)"
    )

    failed = _run_bash(
        db, recorder, context, tmp_path, command, "verification-failed"
    )
    assert failed.execution_id
    recovery = db.get_failure_recovery_for_tool_execution(failed.execution_id)
    failed_evidence = db.get_execution_record_for_tool_execution(
        failed.execution_id
    )
    assert recovery["status"] == "REPAIR_REQUIRED"
    assert failed_evidence["verification_key"]

    (tmp_path / "ready.flag").write_text("fixed", encoding="utf-8")
    succeeded = _run_bash(
        db, recorder, context, tmp_path, command, "verification-succeeded"
    )
    succeeded_evidence = db.get_execution_record_for_tool_execution(
        succeeded.execution_id
    )
    closed = db.get_failure_recovery(recovery["recovery_id"])

    assert succeeded.status.value == "SUCCEEDED"
    assert failed_evidence["verification_key"] == succeeded_evidence["verification_key"]
    assert failed_evidence["command_sha256"] != succeeded_evidence["command_sha256"]
    assert closed["status"] == "RESOLVED"
    assert closed["result_record_id"] == succeeded_evidence["record_id"]
    assert any(name == "repair_resolved" for name, _ in events)
    db.close()


def test_repeated_failure_creates_parent_chain_and_cli_shows_evidence(
    tmp_path, capsys
):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "chain")
    recorder = ExecutionEvidenceRecorder(
        ArtifactStore(tmp_path / "artifacts"), db
    )
    command = _python_command("import sys; print('still broken'); sys.exit(7)")

    first_result = _run_bash(
        db, recorder, context, tmp_path, command, "chain-first"
    )
    second_result = _run_bash(
        db, recorder, context, tmp_path, command, "chain-second"
    )
    first = db.get_failure_recovery_for_tool_execution(first_result.execution_id)
    second = db.get_failure_recovery_for_tool_execution(second_result.execution_id)
    second_evidence = db.get_execution_record_for_tool_execution(
        second_result.execution_id
    )

    assert first["status"] == "ABANDONED"
    assert first["result_record_id"] == second_evidence["record_id"]
    assert second["status"] == "REPAIR_REQUIRED"
    assert second["parent_recovery_id"] == first["recovery_id"]

    RecoveryController(db).record_tool_failure(
        execution_id=second_result.execution_id,
        result=second_result,
        metadata=get_tool_manager().get_metadata("bash").resolve({}),
    )
    assert len(db.list_failure_recoveries(context.run_id)) == 2

    db.finish_agent_run(
        run_id=context.run_id,
        task_id=context.task_id,
        status="FAILED",
        completion_reason="budget_exhausted",
        end_session_id=None,
    )
    runtime = AgentRuntimeManager(
        db,
        runtime_config={
            "max_concurrency": 1,
            "run_timeout_seconds": {},
            "worktree": {"enabled": False},
        },
    )
    try:
        handle_slash_command(
            f"/recovery {second['recovery_id']}",
            [],
            db,
            "session",
            runtime=runtime,
        )
        output = capsys.readouterr().out
        assert f"parent recovery: {first['recovery_id']}" in output
        assert f"source evidence: {second_evidence['record_id']}" in output
    finally:
        runtime.shutdown()
        db.close()


def test_successful_verification_in_another_run_does_not_resolve(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(
        ArtifactStore(tmp_path / "artifacts"), db
    )
    command = _python_command(
        "from pathlib import Path; import sys; "
        "sys.exit(0 if Path('cross-run.flag').exists() else 7)"
    )
    first_context = _running_context(db, "cross-one")
    failed = _run_bash(
        db, recorder, first_context, tmp_path, command, "cross-failed"
    )
    recovery = db.get_failure_recovery_for_tool_execution(failed.execution_id)

    (tmp_path / "cross-run.flag").write_text("fixed elsewhere", encoding="utf-8")
    second_context = _running_context(db, "cross-two")
    succeeded = _run_bash(
        db, recorder, second_context, tmp_path, command, "cross-succeeded"
    )

    assert succeeded.status.value == "SUCCEEDED"
    assert db.get_failure_recovery(recovery["recovery_id"])["status"] == "REPAIR_REQUIRED"
    db.close()


def test_recovery_result_record_cannot_cross_run_boundary(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    source = _running_context(db, "result-source")
    recovery = _manual_repair_record(db, source, "result-source")
    other = _running_context(db, "result-other")
    db.create_tool_execution(
        execution_id="execution-result-other",
        run_id=other.run_id,
        tool_call_id="call-result-other",
        tool_name="bash",
    )
    db.finish_tool_execution(
        execution_id="execution-result-other",
        status="SUCCEEDED",
        attempts=1,
        retryable=False,
        error_code=None,
        error_message=None,
        output_preview="ok",
    )
    db.create_execution_record(
        record_id="record-result-other",
        run_id=other.run_id,
        tool_execution_id="execution-result-other",
        tool_name="bash",
        command_preview="verify",
        verification_key="a" * 64,
    )
    db.finish_execution_record(
        record_id="record-result-other",
        log_status="UNAVAILABLE",
        reproducibility_status="UNAVAILABLE",
        artifact_status="INCOMPLETE",
        exit_code=0,
        termination_reason="exited",
    )

    with pytest.raises(ValueError, match="another run or workspace"):
        db.transition_failure_recovery(
            recovery["recovery_id"],
            status="RESOLVED",
            expected_version=recovery["version"],
            result_record_id="record-result-other",
        )
    assert db.get_failure_recovery(recovery["recovery_id"])["status"] == "REPAIR_REQUIRED"
    db.close()


@pytest.mark.parametrize(
    ("run_status", "completion_reason", "expected"),
    [
        ("SUCCEEDED", "completed", "MANUAL_REQUIRED"),
        ("FAILED", "budget_exhausted", "MANUAL_REQUIRED"),
        ("FAILED", "provider_error", "MANUAL_REQUIRED"),
        ("CANCELLED", "user_interrupt", "ABANDONED"),
        ("TIMED_OUT", "deadline_exceeded", "ABANDONED"),
        ("INTERRUPTED", "process_restarted", "ABANDONED"),
    ],
)
def test_run_terminal_state_closes_unverified_repair(
    tmp_path, run_status, completion_reason, expected
):
    suffix = uuid.uuid4().hex
    db = SessionDB(tmp_path / f"{suffix}.db")
    context = _running_context(db, suffix)
    recovery = _manual_repair_record(db, context, suffix)
    if run_status in {"CANCELLED", "TIMED_OUT"}:
        db.request_agent_run_cancel(context.run_id, completion_reason)

    db.finish_agent_run(
        run_id=context.run_id,
        task_id=context.task_id,
        status=run_status,
        completion_reason=completion_reason,
        end_session_id=None,
    )

    closed = db.get_failure_recovery(recovery["recovery_id"])
    assert closed["status"] == expected
    assert closed["finished_at"] is not None
    events = db.list_agent_events(context.run_id)
    assert any(
        event["event_type"] == "repair_closed"
        and event["payload"]["status"] == expected
        for event in events
    )
    db.close()
