"""E3：Worktree 受控撤销、冲突关闭和清理重试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import tools
from agent.agent import ConversationResult
from agent.delegate import DelegationRequest, build_delegate_request, build_delegate_spec
from agent.reproducibility import ArtifactStore, ExecutionEvidenceRecorder
from agent.runtime import AgentRunContext, AgentRuntimeManager, RunStatus
from agent.workspace_runner import RunnerProbe, WorkspaceCommandResult, WorkspaceRunnerError
from agent.worktree import WorkspaceManager, WorkspaceOperationError
from session import SessionDB
from tools.files import write_file
from tools.registry import resolve_tool_access_policy


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "MiniHermes Test")
    _git(root, "config", "user.email", "minihermes@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "def test_value(): pass\n", encoding="utf-8"
    )
    _git(root, "add", "src/app.py", "tests/test_app.py")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


class FakeRunner:
    backend = "docker"

    def __init__(self):
        self.active = False
        self.state_error = False

    def probe(self):
        return RunnerProbe("docker", "sha256:" + "a" * 64)

    def run(self, **kwargs):
        return WorkspaceCommandResult(
            stdout="ok\n", exit_code=0, termination_reason="exited"
        )

    def verify_workspace(self, **kwargs):
        return None

    def has_active_processes(self, workspace_id):
        if self.state_error:
            raise WorkspaceRunnerError("runner_state_unavailable")
        return self.active


class FakeChildAgent:
    def __init__(self, run_context, action):
        self.run_context = run_context
        self.action = action
        self.provider = None

    def interrupt(self):
        self.run_context.cancel_event.set()

    def run_conversation(self, **kwargs):
        response = self.action(self.run_context)
        return ConversationResult(
            final_response=response,
            reasoning="",
            messages=[],
            completion_reason="stop",
            iterations_used=1,
        )


class CleanupFailOnceManager(WorkspaceManager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fail_cleanup = True

    def _git_bytes(self, cwd, *args):
        if self.fail_cleanup and args[:2] == ("worktree", "remove"):
            raise WorkspaceOperationError("injected_cleanup_failure")
        return super()._git_bytes(cwd, *args)


class FailingRollbackWrites:
    def __init__(self, delegate):
        self.delegate = delegate

    def read_bytes(self, relative_path):
        return self.delegate.read_bytes(relative_path)

    def write_json_atomic(self, relative_path, payload):
        if "/rollback-" in relative_path:
            raise OSError("injected artifact failure")
        return self.delegate.write_json_atomic(relative_path, payload)


def _parent(db: SessionDB) -> AgentRunContext:
    policy = resolve_tool_access_policy(None, tools.get_tool_manager().get_names())
    db.create_agent_task(
        task_id="parent-task",
        conversation_id="conversation",
        session_id=None,
        parent_task_id=None,
        kind="main_turn",
        title="parent",
        request_preview="parent",
    )
    db.create_agent_run(
        run_id="parent-run",
        task_id="parent-task",
        parent_run_id=None,
        conversation_id="conversation",
        start_session_id=None,
        agent_kind="main_turn",
        model="test",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=10,
        timeout_seconds=None,
    )
    db.start_agent_run("parent-run", "parent-task")
    return AgentRunContext(
        task_id="parent-task",
        run_id="parent-run",
        conversation_id="conversation",
        start_session_id="",
        tool_policy=policy,
    )


def _environment(tmp_path: Path, *, manager=None):
    root, base = _repository(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db)
    runner = FakeRunner()
    holder = {"action": lambda context: "unused"}

    def factory(spec, request, run_context):
        return FakeChildAgent(run_context, holder["action"])

    manager = manager or WorkspaceManager(managed_root=tmp_path / "worktrees")
    runtime = AgentRuntimeManager(
        db,
        ephemeral_factory=factory,
        evidence_recorder=recorder,
        workspace_manager=manager,
        workspace_runner=runner,
        runtime_config={
            "max_concurrency": 2,
            "delegate_batch_timeout_seconds": 10,
            "cancel_grace_seconds": 1,
            "run_timeout_seconds": {"delegate": 30},
            "worktree": {
                "enabled": True,
                "runner": "docker",
                "docker_image": "fake@sha256:test",
                "preserve_failed_days": 30,
            },
        },
    )
    return {
        "root": root,
        "base": base,
        "db": db,
        "store": store,
        "recorder": recorder,
        "runner": runner,
        "manager": manager,
        "runtime": runtime,
        "parent": _parent(db),
        "holder": holder,
    }


def _run_candidate(env, action):
    env["holder"]["action"] = action
    request = DelegationRequest(
        task="update app",
        tools={"read_file", "write_file", "bash"},
        execution_mode="worktree_write",
        write_scope=("src/",),
        verification_hint="python -m pytest -q",
    )
    payload = build_delegate_request(request, model="test")
    payload["_host_working_directory"] = str(env["root"])
    outcome = env["runtime"].run_ephemeral(
        spec=build_delegate_spec(request),
        request=payload,
        conversation_id="conversation",
        parent_task_id="parent-task",
        parent_run_id="parent-run",
        parent_run_context=env["parent"],
    )
    lease = env["db"].get_worktree_lease_for_run(outcome.run_id)
    return outcome, lease


def _write_candidate(context, value="VALUE = 2\n"):
    assert write_file(
        "src/app.py", value, _workspace_context=context.workspace_context
    ).startswith("Successfully")
    return "updated"


def _latest_rollback(db: SessionDB, run_id: str) -> dict:
    records = [
        record for record in db.list_failure_recoveries(run_id)
        if record["source_kind"] == "RUN" and record["selected_action"] == "ROLLBACK"
    ]
    assert records
    return records[0]


def _close(env):
    env["runtime"].shutdown()
    env["db"].close()


def test_verified_rollback_restores_checkpoint_then_cleans_candidate(tmp_path):
    env = _environment(tmp_path)
    original = (env["root"] / "src" / "app.py").read_bytes()
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        discarded = env["runtime"].discard_worktree(lease["workspace_id"])

        recovery = _latest_rollback(env["db"], outcome.run_id)
        artifact = json.loads(
            env["store"].read_bytes(recovery["result_artifact_relpath"])
        )
        assert recovery["status"] == "ROLLED_BACK"
        assert artifact["status"] == "ROLLED_BACK"
        assert artifact["details"]["remaining_changes"] == []
        assert discarded["lease_status"] == "REJECTED"
        assert discarded["cleanup_status"] == "SUCCEEDED"
        assert not Path(lease["worktree_path"]).exists()
        assert (env["root"] / "src" / "app.py").read_bytes() == original
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


def test_external_candidate_edit_causes_conflict_without_overwrite(tmp_path):
    env = _environment(tmp_path)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        candidate = Path(lease["worktree_path"], "src", "app.py")
        candidate.write_text("EXTERNAL EDIT\n", encoding="utf-8")

        with pytest.raises(WorkspaceOperationError) as exc_info:
            env["runtime"].discard_worktree(lease["workspace_id"])
        recovery = _latest_rollback(env["db"], outcome.run_id)
        assert exc_info.value.reason_code == "candidate_changed_after_finalization"
        assert recovery["status"] == "ROLLBACK_CONFLICT"
        assert recovery["result_reason_code"] == "candidate_changed_after_finalization"
        assert candidate.read_text(encoding="utf-8") == "EXTERNAL EDIT\n"
        assert env["db"].get_worktree_lease(lease["workspace_id"])["lease_status"] == "PRESERVED"
    finally:
        _close(env)


def test_tampered_candidate_artifact_is_conflict_and_is_preserved(tmp_path):
    env = _environment(tmp_path)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        manifest = env["store"].resolve_relative_path(
            lease["change_manifest_relpath"], create_parent=False
        )
        manifest.write_bytes(manifest.read_bytes() + b"tampered")

        with pytest.raises(WorkspaceOperationError) as exc_info:
            env["runtime"].discard_worktree(lease["workspace_id"])
        recovery = _latest_rollback(env["db"], outcome.run_id)
        assert exc_info.value.reason_code == "candidate_artifact_hash_mismatch"
        assert recovery["status"] == "ROLLBACK_CONFLICT"
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        _close(env)


def test_scope_violation_is_never_auto_rolled_back(tmp_path):
    env = _environment(tmp_path)

    def write_outside_scope(context):
        Path(context.workspace_context.workspace_root, "outside.txt").write_text(
            "outside\n", encoding="utf-8"
        )
        return "done"

    try:
        outcome, lease = _run_candidate(env, write_outside_scope)
        assert outcome.status == RunStatus.FAILED
        assert not [
            item for item in env["db"].list_failure_recoveries(outcome.run_id)
            if item["source_kind"] == "RUN"
        ]
        with pytest.raises(WorkspaceOperationError):
            env["runtime"].discard_worktree(lease["workspace_id"])
        recovery = _latest_rollback(env["db"], outcome.run_id)
        assert recovery["status"] == "ROLLBACK_CONFLICT"
        assert Path(lease["worktree_path"], "outside.txt").is_file()
    finally:
        _close(env)


@pytest.mark.parametrize(
    "runner_field,reason_code",
    [("active", "runner_still_active"), ("state_error", "runner_state_unavailable")],
)
def test_runner_must_be_provably_stopped_before_rollback(
    tmp_path, runner_field, reason_code
):
    env = _environment(tmp_path)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        setattr(env["runner"], runner_field, True)
        with pytest.raises(WorkspaceOperationError) as exc_info:
            env["runtime"].discard_worktree(lease["workspace_id"])
        recovery = _latest_rollback(env["db"], outcome.run_id)
        assert exc_info.value.reason_code == reason_code
        assert recovery["status"] == "ROLLBACK_SKIPPED"
        assert recovery["result_reason_code"] == reason_code
        assert Path(lease["worktree_path"], "src", "app.py").read_text() == "VALUE = 2\n"
    finally:
        _close(env)


def test_preflight_artifact_failure_changes_nothing(tmp_path):
    env = _environment(tmp_path)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        candidate = Path(lease["worktree_path"], "src", "app.py")
        env["recorder"].store = FailingRollbackWrites(env["store"])
        with pytest.raises(WorkspaceOperationError) as exc_info:
            env["runtime"].discard_worktree(lease["workspace_id"])
        recovery = _latest_rollback(env["db"], outcome.run_id)
        assert exc_info.value.reason_code == "rollback_artifact_unavailable"
        assert recovery["status"] == "ROLLBACK_SKIPPED"
        assert recovery["result_artifact_relpath"] is None
        assert candidate.read_text() == "VALUE = 2\n"
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        _close(env)


def test_cleanup_failure_keeps_rolled_back_record_and_can_retry(tmp_path):
    manager = CleanupFailOnceManager(managed_root=tmp_path / "worktrees")
    env = _environment(tmp_path, manager=manager)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        with pytest.raises(WorkspaceOperationError) as exc_info:
            env["runtime"].discard_worktree(lease["workspace_id"])
        assert exc_info.value.reason_code == "injected_cleanup_failure"
        recovery = _latest_rollback(env["db"], outcome.run_id)
        failed_cleanup = env["db"].get_worktree_lease(lease["workspace_id"])
        assert recovery["status"] == "ROLLED_BACK"
        assert failed_cleanup["lease_status"] == "REJECTED"
        assert failed_cleanup["cleanup_status"] == "FAILED"
        assert Path(lease["worktree_path"]).is_dir()

        manager.fail_cleanup = False
        cleaned = env["runtime"].discard_worktree(lease["workspace_id"])
        assert cleaned["cleanup_status"] == "SUCCEEDED"
        assert not Path(lease["worktree_path"]).exists()
        assert _latest_rollback(env["db"], outcome.run_id)["recovery_id"] == recovery["recovery_id"]
    finally:
        _close(env)


def test_merged_candidate_cannot_enter_rollback(tmp_path):
    env = _environment(tmp_path)
    try:
        outcome, lease = _run_candidate(env, _write_candidate)
        env["db"].transition_worktree_lease(lease["workspace_id"], status="INTEGRATING")
        env["db"].transition_worktree_lease(lease["workspace_id"], status="MERGED")
        with pytest.raises(RuntimeError, match="cannot be rolled back"):
            env["runtime"].discard_worktree(lease["workspace_id"])
        assert not [
            item for item in env["db"].list_failure_recoveries(outcome.run_id)
            if item["source_kind"] == "RUN"
        ]
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        _close(env)


@pytest.mark.parametrize(
    "reason,expected_status",
    [("user_interrupt", RunStatus.CANCELLED), ("deadline_exceeded", RunStatus.TIMED_OUT)],
)
def test_cancelled_and_timed_out_candidates_can_be_explicitly_discarded(
    tmp_path, reason, expected_status
):
    env = _environment(tmp_path)

    def cancel_after_write(context):
        _write_candidate(context)
        context.cancel_reason = reason
        context.cancel_event.set()
        return "stopped"

    try:
        outcome, lease = _run_candidate(env, cancel_after_write)
        assert outcome.status == expected_status
        discarded = env["runtime"].discard_worktree(lease["workspace_id"])
        assert discarded["cleanup_status"] == "SUCCEEDED"
        assert _latest_rollback(env["db"], outcome.run_id)["status"] == "ROLLED_BACK"
    finally:
        _close(env)


def test_runtime_restart_backfills_candidate_evidence_for_later_discard(tmp_path):
    root, _ = _repository(tmp_path)
    db_path = tmp_path / "state.db"
    store = ArtifactStore(tmp_path / "artifacts")
    runner = FakeRunner()
    manager = WorkspaceManager(managed_root=tmp_path / "worktrees")
    db = SessionDB(db_path)
    _parent(db)
    db.create_agent_task(
        task_id="child-task",
        conversation_id="conversation",
        session_id=None,
        parent_task_id="parent-task",
        kind="delegate",
        title="child",
        request_preview="child",
    )
    db.create_agent_run(
        run_id="child-run",
        task_id="child-task",
        parent_run_id="parent-run",
        conversation_id="conversation",
        start_session_id=None,
        agent_kind="delegate",
        model="test",
        tool_policy_json="{}",
        approval_mode="deny_sensitive",
        max_iterations=10,
        timeout_seconds=None,
    )
    db.start_agent_run("child-run", "child-task")
    context = manager.provision(
        db=db,
        runner=runner,
        artifact_store=store,
        task_id="child-task",
        run_id="child-run",
        parent_run_id="parent-run",
        working_directory=root,
        write_scope=["src/"],
    )
    manager.start(context)
    Path(context.workspace_root, "src", "app.py").write_text(
        "VALUE = 9\n", encoding="utf-8"
    )
    db.close()

    reopened = SessionDB(db_path)
    recorder = ExecutionEvidenceRecorder(store, reopened)
    runtime = AgentRuntimeManager(
        reopened,
        ephemeral_factory=lambda spec, request, run_context: None,
        evidence_recorder=recorder,
        workspace_manager=manager,
        workspace_runner=runner,
        runtime_config={"run_timeout_seconds": {}, "worktree": {"enabled": False}},
    )
    try:
        lease = reopened.get_worktree_lease(context.workspace_id)
        assert lease["lease_status"] == "PRESERVED"
        assert lease["diff_hash"] and lease["change_manifest_hash"]
        discarded = runtime.discard_worktree(context.workspace_id)
        assert discarded["cleanup_status"] == "SUCCEEDED"
        assert _latest_rollback(reopened, "child-run")["status"] == "ROLLED_BACK"
    finally:
        runtime.shutdown()
        reopened.close()


def test_discarding_one_candidate_does_not_touch_another(tmp_path):
    env = _environment(tmp_path)
    try:
        first_outcome, first = _run_candidate(
            env, lambda context: _write_candidate(context, "VALUE = 2\n")
        )
        second_outcome, second = _run_candidate(
            env, lambda context: _write_candidate(context, "VALUE = 3\n")
        )
        second_path = Path(second["worktree_path"], "src", "app.py")
        env["runtime"].discard_worktree(first["workspace_id"])
        assert not Path(first["worktree_path"]).exists()
        assert second_path.read_text() == "VALUE = 3\n"
        assert env["db"].get_worktree_lease(second["workspace_id"])["lease_status"] == "PRESERVED"
        assert _latest_rollback(env["db"], first_outcome.run_id)["status"] == "ROLLED_BACK"
        assert not [
            item for item in env["db"].list_failure_recoveries(second_outcome.run_id)
            if item["source_kind"] == "RUN"
        ]
    finally:
        _close(env)


def test_v11_database_migrates_incrementally_to_v12(tmp_path, monkeypatch):
    import session.db as db_module

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 11)
    old = db_module.SessionDB(db_path)
    old.close()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(failure_recovery_records)"
            )
        }
        assert "result_artifact_hash" not in columns

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 12)
    upgraded = db_module.SessionDB(db_path)
    try:
        columns = {
            row[1] for row in upgraded._conn.execute(
                "PRAGMA table_info(failure_recovery_records)"
            ).fetchall()
        }
        assert {
            "result_artifact_relpath", "result_artifact_hash", "result_reason_code"
        } <= columns
        assert upgraded._conn.execute("PRAGMA user_version").fetchone()[0] == 12
    finally:
        upgraded.close()
