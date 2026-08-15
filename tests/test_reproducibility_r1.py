"""R1 bash 证据记录：旁路采集、脱敏和 Graph 节点关联。"""

import json
import sys
from pathlib import Path

from agent.graph_runner import GraphRunner
from agent.reproducibility import ArtifactStore, ExecutionEvidenceRecorder
from agent.runtime import AgentRunContext
from session import SessionDB
from tools import get_tool_manager
from tools.registry import ToolExecutionContext, resolve_tool_access_policy


def _prepare_graph_run(db: SessionDB, suffix: str = "one") -> AgentRunContext:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    conversation_id = f"conversation-{suffix}"
    session_id = f"session-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id=conversation_id,
        session_id=session_id,
        parent_task_id=None,
        kind="main_turn",
        title="R1 evidence test",
        request_preview="capture bash evidence",
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=None,
        conversation_id=conversation_id,
        start_session_id=session_id,
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=3,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task_id)
    graph_context = GraphRunner(db).start_main_turn(
        task_id=task_id,
        agent_run_id=run_id,
        conversation_id=conversation_id,
        session_id=session_id,
    )
    return AgentRunContext(
        task_id=task_id,
        run_id=run_id,
        conversation_id=conversation_id,
        start_session_id=session_id,
        workflow_run_id=graph_context.workflow_run_id,
        node_run_id=graph_context.node_run_id,
    )


def _bash_call(command: str, *, timeout: float = 5, call_id: str = "bash-call") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": command, "timeout": timeout}),
        },
    }


def _run_bash(
    db: SessionDB,
    recorder: ExecutionEvidenceRecorder,
    run_context: AgentRunContext,
    command: str,
    *,
    timeout: float = 5,
    cancel_check=None,
    working_directory: Path,
):
    registry = get_tool_manager()
    return registry.execute_detailed(
        _bash_call(command, timeout=timeout),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=run_context,
            cancel_check=cancel_check,
            working_directory=str(working_directory),
            evidence_recorder=recorder,
        ),
    )


def _single_record(db: SessionDB, run_context: AgentRunContext) -> dict:
    records = db.list_execution_records(run_context.run_id)
    assert len(records) == 1
    return records[0]


def _python_command(source: str) -> str:
    return f'"{sys.executable}" -c "{source}"'


def test_bash_evidence_keeps_streams_separate_redacts_and_links_graph_node(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    run_context = _prepare_graph_run(db)
    secret = "test-secret-value-1234"
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(
        store, db, max_log_bytes_per_stream=4096, known_secrets=(secret,)
    )
    command = _python_command(
        f"import sys; print('stdout {secret}'); print('stderr {secret}', file=sys.stderr)"
    )

    result = _run_bash(
        db, recorder, run_context, command, working_directory=tmp_path
    )

    assert result.status.value == "SUCCEEDED"
    assert secret in result.output
    record = _single_record(db, run_context)
    assert record["node_run_id"] == run_context.node_run_id
    assert record["exit_code"] == 0
    assert record["termination_reason"] == "exited"
    assert record["log_status"] == "REDACTED"
    assert record["reproducibility_status"] == "PARTIAL"
    assert record["artifact_status"] == "AVAILABLE"
    assert record["working_directory_rel"] is None
    assert secret not in record["command_preview"]

    command_artifact = store.read_bytes(record["command_relpath"]).decode("utf-8")
    stdout = store.read_bytes(record["stdout_relpath"]).decode("utf-8")
    stderr = store.read_bytes(record["stderr_relpath"]).decode("utf-8")
    environment = store.read_bytes(record["environment_relpath"]).decode("utf-8")
    result_artifact = store.read_bytes(
        store.execution_relpath(run_context.run_id, record["record_id"], "result.json")
    ).decode("utf-8")
    for content in (command_artifact, stdout, stderr, environment, result_artifact):
        assert secret not in content
    assert "stdout [REDACTED]" in stdout
    assert "stderr [REDACTED]" in stderr
    assert '"termination_reason": "exited"' in result_artifact
    assert json.loads(command_artifact)["working_directory"] == str(tmp_path)
    assert json.loads(environment)["working_directory"] == str(tmp_path)
    assert db.get_tool_execution(record["tool_execution_id"])["output_preview"]
    assert secret not in db.get_tool_execution(record["tool_execution_id"])["output_preview"]
    db.close()


def test_bash_evidence_records_timeout_cancellation_and_spawn_failure(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db, max_log_bytes_per_stream=4096)

    timeout_context = _prepare_graph_run(db, "timeout")
    timeout_result = _run_bash(
        db,
        recorder,
        timeout_context,
        _python_command("import time; time.sleep(2)"),
        timeout=0.05,
        working_directory=tmp_path,
    )
    assert timeout_result.error_code == "timeout"
    timeout_record = _single_record(db, timeout_context)
    assert timeout_record["termination_reason"] == "timed_out"
    assert timeout_record["exit_code"] is None
    assert timeout_record["reproducibility_status"] == "PARTIAL"

    cancelled_context = _prepare_graph_run(db, "cancelled")
    checks = {"count": 0}

    def cancel_after_start():
        checks["count"] += 1
        return checks["count"] >= 3

    cancelled_result = _run_bash(
        db,
        recorder,
        cancelled_context,
        _python_command("import time; time.sleep(2)"),
        cancel_check=cancel_after_start,
        working_directory=tmp_path,
    )
    assert cancelled_result.status.value == "CANCELLED"
    cancelled_record = _single_record(db, cancelled_context)
    assert cancelled_record["termination_reason"] == "cancelled"
    assert cancelled_record["exit_code"] is None

    spawn_context = _prepare_graph_run(db, "spawn")
    spawn_result = _run_bash(
        db,
        recorder,
        spawn_context,
        "echo this command cannot start in its configured cwd",
        working_directory=tmp_path / "missing-directory",
    )
    assert spawn_result.status.value == "FAILED"
    spawn_record = _single_record(db, spawn_context)
    assert spawn_record["termination_reason"] == "spawn_error"
    assert spawn_record["exit_code"] is None
    db.close()


def test_bash_evidence_records_nonzero_exit_and_enforces_per_stream_log_limit(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    run_context = _prepare_graph_run(db, "nonzero")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db, max_log_bytes_per_stream=160)
    result = _run_bash(
        db,
        recorder,
        run_context,
        _python_command(
            "import sys; print('x' * 1000); print('y' * 1000, file=sys.stderr); sys.exit(7)"
        ),
        working_directory=tmp_path,
    )

    assert result.status.value == "FAILED"
    assert result.error_code == "nonzero_exit"
    assert result.attempts == 1
    record = _single_record(db, run_context)
    assert record["exit_code"] == 7
    assert record["termination_reason"] == "exited"
    assert record["log_status"] == "TRUNCATED"
    assert record["reproducibility_status"] == "PARTIAL"
    assert len(store.read_bytes(record["stdout_relpath"])) <= 160
    assert len(store.read_bytes(record["stderr_relpath"])) <= 160
    assert b"EVIDENCE LOG TRUNCATED" in store.read_bytes(record["stdout_relpath"])
    assert b"EVIDENCE LOG TRUNCATED" in store.read_bytes(record["stderr_relpath"])
    db.close()


def test_evidence_recorder_reads_log_limit_from_reproducibility_config(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder.from_config(
        ArtifactStore(tmp_path / "artifacts"),
        db,
        {"max_log_bytes_per_stream": 1234},
        secrets_config={"model": {"api_key": "known-api-key-1234"}},
    )

    assert recorder.max_log_bytes_per_stream == 1234
    assert recorder.sanitize_preview("known-api-key-1234") == "[REDACTED]"
    db.close()


def test_bash_evidence_is_not_created_before_policy_approval_and_capture_failure_is_observable(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    run_context = _prepare_graph_run(db, "policy")
    registry = get_tool_manager()
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    denied = registry.execute_detailed(
        _bash_call("echo denied"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy({"include": set()}, registry.get_names()),
            db=db,
            run_context=run_context,
            working_directory=str(tmp_path),
            evidence_recorder=recorder,
        ),
    )
    assert denied.status.value == "DENIED"
    assert db.list_execution_records(run_context.run_id) == []

    cancelled_context = _prepare_graph_run(db, "before-start")
    cancelled = registry.execute_detailed(
        _bash_call("echo not-started"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=cancelled_context,
            cancel_check=lambda: True,
            working_directory=str(tmp_path),
            evidence_recorder=recorder,
        ),
    )
    assert cancelled.status.value == "CANCELLED"
    assert db.list_execution_records(cancelled_context.run_id) == []

    racing_cancel_context = _prepare_graph_run(db, "cancel-race")
    checks = {"count": 0}

    def cancel_before_invocation():
        checks["count"] += 1
        return checks["count"] >= 2

    racing_cancel = registry.execute_detailed(
        _bash_call("echo not-started-race"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=racing_cancel_context,
            cancel_check=cancel_before_invocation,
            working_directory=str(tmp_path),
            evidence_recorder=recorder,
        ),
    )
    assert racing_cancel.status.value == "CANCELLED"
    assert db.list_execution_records(racing_cancel_context.run_id) == []

    events = []
    failing_context = _prepare_graph_run(db, "capture-failure")
    failing_context.event_callback = lambda event_type, payload: events.append(
        (event_type, payload)
    )

    class FailingRecorder:
        def start_bash(self, **_kwargs):
            raise OSError("artifact volume unavailable")

    succeeded = registry.execute_detailed(
        _bash_call("echo still-runs"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=failing_context,
            working_directory=str(tmp_path),
            evidence_recorder=FailingRecorder(),
        ),
    )
    assert succeeded.status.value == "SUCCEEDED"
    assert any(
        event_type == "evidence_capture_failed" and payload["stage"] == "start"
        for event_type, payload in events
    )
    assert db.list_execution_records(failing_context.run_id) == []
    schema = next(
        item for item in registry.get_schemas()
        if item["function"]["name"] == "bash"
    )
    assert set(schema["function"]["parameters"]["properties"]) == {"command", "timeout"}
    db.close()


def test_execution_record_rejects_node_from_a_different_agent_run(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    first = _prepare_graph_run(db, "first")
    second = _prepare_graph_run(db, "second")
    db.create_tool_execution(
        execution_id="tool-cross-node",
        run_id=second.run_id,
        tool_call_id="call-cross-node",
        tool_name="bash",
    )

    try:
        db.create_execution_record(
            record_id="record-cross-node",
            run_id=second.run_id,
            tool_execution_id="tool-cross-node",
            tool_name="bash",
            node_run_id=first.node_run_id,
        )
    except ValueError as exc:
        assert "does not belong" in str(exc)
    else:
        raise AssertionError("cross-run graph node association was accepted")
    db.close()
