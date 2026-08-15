"""R2 Git 快照与单条命令重放的闭环测试。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.reproducibility import (
    ArtifactStore,
    ExecutionEvidenceRecorder,
    ReplayMaterializer,
    _canonical_json_bytes,
    _sha256,
)
from agent.runtime import AgentRunContext, AgentRuntimeManager, RunStatus
from approval import ApprovalEngine, ApprovalMode
from session import SessionDB
from tools import get_tool_manager
from tools.registry import ToolExecutionContext, resolve_tool_access_policy


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "MiniHermes Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo / "subdir").mkdir()
    (repo / "subdir" / "keep.txt").write_text("base nested\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _prepare_run(db: SessionDB, suffix: str = "one") -> AgentRunContext:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id="conversation-r2",
        session_id="session-r2",
        parent_task_id=None,
        kind="main_turn",
        title="R2 test",
        request_preview="capture",
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=None,
        conversation_id="conversation-r2",
        start_session_id="session-r2",
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=5,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task_id)
    return AgentRunContext(
        task_id=task_id,
        run_id=run_id,
        conversation_id="conversation-r2",
        start_session_id="session-r2",
    )


def _bash_call(command: str, call_id: str = "bash-r2") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": command, "timeout": 10}),
        },
    }


def _run_bash(
    db: SessionDB, recorder: ExecutionEvidenceRecorder, context: AgentRunContext,
    repo: Path, command: str,
):
    registry = get_tool_manager()
    return registry.execute_detailed(
        _bash_call(command),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=context,
            working_directory=str(repo),
            evidence_recorder=recorder,
        ),
    )


def _finish_source_run(db: SessionDB, context: AgentRunContext):
    db.finish_agent_run(
        run_id=context.run_id,
        task_id=context.task_id,
        status="SUCCEEDED",
        completion_reason="completed",
        end_session_id=context.start_session_id,
    )


def _single_record(db: SessionDB, context: AgentRunContext) -> dict:
    records = db.list_execution_records(context.run_id)
    assert len(records) == 1
    return records[0]


def _hash_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and ".git" not in candidate.parts:
            digest.update(candidate.relative_to(path).as_posix().encode("utf-8"))
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _runtime(db: SessionDB, recorder: ExecutionEvidenceRecorder, approval_callback=None):
    return AgentRuntimeManager(
        db,
        evidence_recorder=recorder,
        approval_engine=ApprovalEngine(),
        approval_callback=approval_callback,
        runtime_config={
            "max_concurrency": 1,
            "run_timeout_seconds": {"replay": 30},
        },
        replay_root=recorder.store.root.parent / "replays",
    )


def test_snapshot_replay_restores_tracked_and_untracked_files_without_touching_source(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    original_hash = _hash_tree(repo)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'tracked.txt\').read_text() == \'changed\\n\'; assert Path(\'untracked.txt\').read_text() == \'untracked\\n\'"'
    result = _run_bash(db, recorder, context, repo, command)
    assert result.status.value == "SUCCEEDED"
    record = _single_record(db, context)
    assert record["reproducibility_status"] == "REPLAYABLE"
    snapshot = db.get_workspace_snapshot(record["snapshot_id"])
    assert snapshot["capture_status"] == "REPLAYABLE"
    assert snapshot["base_tree_relpath"]
    assert record["working_directory_rel"] == "."
    _finish_source_run(db, context)

    runtime = _runtime(db, recorder)
    outcome = runtime.replay_execution(record["record_id"], conversation_id="conversation-r2")
    assert outcome.status == RunStatus.SUCCEEDED
    replay_run = db.get_agent_run(outcome.run_id)
    assert replay_run["parent_run_id"] == context.run_id
    replay_records = db.list_execution_records(outcome.run_id)
    assert len(replay_records) == 1
    assert replay_records[0]["replayed_from_record_id"] == record["record_id"]
    assert replay_records[0]["replay_status"] == "REPLAY_SUCCEEDED"
    assert db.get_execution_record(record["record_id"])["replay_status"] == "REPLAY_SUCCEEDED"
    assert _hash_tree(repo) == original_hash
    runtime.shutdown()
    db.close()


def test_materialization_does_not_need_original_repository_or_git_objects(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo snapshot")
    record = _single_record(db, context)
    assert record["reproducibility_status"] == "REPLAYABLE"
    materializer = ReplayMaterializer(recorder.store, db, replay_root=tmp_path / "replays")
    source = materializer.load_source(record["record_id"])
    repo.rename(tmp_path / "source-repository-removed")
    materialized = materializer.materialize(source)
    assert (materialized.workspace_root / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    db.close()


def test_sensitive_or_unstable_snapshots_are_downgraded_without_leaking(tmp_path: Path):
    repo = _repo(tmp_path)
    secret = "known-secret-value-12345"
    (repo / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(
        ArtifactStore(tmp_path / "artifacts"), db, known_secrets=(secret,)
    )
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo done")
    record = _single_record(db, context)
    assert record["reproducibility_status"] == "PARTIAL"
    assert record["snapshot_id"] is None
    assert secret not in recorder.store.read_bytes(record["command_relpath"]).decode("utf-8")
    assert db.list_workspace_snapshots(context.run_id) == []
    db.close()


def test_replay_rechecks_approval_and_hardline_never_starts_shell(tmp_path: Path):
    repo = _repo(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    command = "rm source-file-that-does-not-exist"
    _run_bash(db, recorder, context, repo, command)
    source = _single_record(db, context)
    _finish_source_run(db, context)
    sentinel = repo / "replay-target.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    approvals = []

    def deny_callback(*args, **kwargs):
        approvals.append(args[0])
        return "deny"

    runtime = _runtime(db, recorder, approval_callback=deny_callback)
    outcome = runtime.replay_execution(source["record_id"])
    assert outcome.status == RunStatus.CANCELLED
    assert approvals == ["bash"]
    assert sentinel.exists()
    replay_record = db.list_execution_records(outcome.run_id)[0]
    assert replay_record["replay_status"] == "REPLAY_DENIED"
    assert db.get_execution_record(source["record_id"])["replay_status"] == "REPLAY_DENIED"
    runtime.shutdown()
    db.close()


def test_tampered_snapshot_is_recorded_as_unavailable_without_running_shell(tmp_path: Path):
    repo = _repo(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo source")
    source = _single_record(db, context)
    snapshot = db.get_workspace_snapshot(source["snapshot_id"])
    recorder.store.write_bytes_atomic(snapshot["patch_relpath"], b"tampered")
    _finish_source_run(db, context)

    runtime = _runtime(db, recorder)
    outcome = runtime.replay_execution(source["record_id"])
    assert outcome.status == RunStatus.FAILED
    assert outcome.completion_reason == "replay_unavailable"
    assert db.get_execution_record(source["record_id"])["replay_status"] == "REPLAY_UNAVAILABLE"
    replay_records = db.list_execution_records(outcome.run_id)
    assert len(replay_records) == 1
    assert replay_records[0]["replay_status"] == "REPLAY_UNAVAILABLE"
    assert db.list_tool_executions(outcome.run_id)[0]["status"] == "FAILED"
    runtime.shutdown()
    db.close()


def test_tampered_command_artifact_is_recorded_as_unavailable_without_running_shell(tmp_path: Path):
    repo = _repo(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo source")
    source = _single_record(db, context)
    command_doc = json.loads(recorder.store.read_bytes(source["command_relpath"]).decode("utf-8"))
    command_doc["command"] = "echo should-not-run > unexpected.txt"
    recorder.store.write_json_atomic(source["command_relpath"], command_doc)
    _finish_source_run(db, context)

    runtime = _runtime(db, recorder)
    outcome = runtime.replay_execution(source["record_id"])
    assert outcome.status == RunStatus.FAILED
    assert outcome.completion_reason == "replay_unavailable"
    assert not (repo / "unexpected.txt").exists()
    assert db.get_execution_record(source["record_id"])["replay_status"] == "REPLAY_UNAVAILABLE"
    runtime.shutdown()
    db.close()


def test_hardline_replay_is_blocked_without_approval_callback(tmp_path: Path):
    repo = _repo(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo source")
    source = _single_record(db, context)
    command_doc = json.loads(recorder.store.read_bytes(source["command_relpath"]).decode("utf-8"))
    command_doc["command"] = "shutdown"
    recorder.store.write_json_atomic(source["command_relpath"], command_doc)
    db._conn.execute(
        "UPDATE execution_records SET command_sha256 = ? WHERE record_id = ?",
        (
            _sha256(_canonical_json_bytes({
                "command": command_doc["command"],
                "working_directory_rel": command_doc["working_directory_rel"],
                "snapshot_id": command_doc["snapshot_id"],
            })),
            source["record_id"],
        ),
    )
    _finish_source_run(db, context)
    called = []

    def callback(*args, **kwargs):
        called.append(True)
        return "once"

    runtime = _runtime(db, recorder, approval_callback=callback)
    outcome = runtime.replay_execution(source["record_id"])
    assert outcome.status == RunStatus.CANCELLED
    assert not called
    assert db.get_execution_record(source["record_id"])["replay_status"] == "REPLAY_DENIED"
    runtime.shutdown()
    db.close()


def test_cancelled_replay_does_not_start_shell_or_leave_running_records(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    recorder = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    context = _prepare_run(db)
    _run_bash(db, recorder, context, repo, "echo source")
    source = _single_record(db, context)
    _finish_source_run(db, context)
    runtime = _runtime(db, recorder)

    def cancel_when_registered(run_id: str, task_id: str):
        original_start(run_id, task_id)
        runtime.cancel(run_id)

    original_start = db.start_agent_run
    monkeypatch.setattr(db, "start_agent_run", cancel_when_registered)
    outcome = runtime.replay_execution(source["record_id"])
    assert outcome.status == RunStatus.CANCELLED
    assert outcome.completion_reason == "user_interrupt"
    assert db.get_execution_record(source["record_id"])["replay_status"] == "REPLAY_CANCELLED"
    tool_executions = db.list_tool_executions(outcome.run_id)
    replay_records = db.list_execution_records(outcome.run_id)
    assert len(tool_executions) == 1
    assert tool_executions[0]["status"] == "CANCELLED"
    assert len(replay_records) == 1
    assert replay_records[0]["replay_status"] == "REPLAY_CANCELLED"
    runtime.shutdown()
    db.close()
