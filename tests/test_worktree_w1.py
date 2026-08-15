"""Worktree W1：串行写入、严格 Runner、范围审计与失败保留。"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

import tools
from agent.agent import ConversationResult
from agent.delegate import DelegationRequest, build_delegate_request, build_delegate_spec
from agent.reproducibility import ArtifactStore, ExecutionEvidenceRecorder
from agent.runtime import (
    AgentRunCancelled,
    AgentRunContext,
    AgentRuntimeManager,
    AgentSpec,
    RunStatus,
)
from agent.workspace_runner import (
    DockerWorkspaceCommandRunner,
    RunnerProbe,
    WorkspaceCommandResult,
    WorkspaceRunnerError,
)
from agent.worktree import WorkspaceManager
from session import SessionDB
from tools.files import read_file, write_file
from tools.registry import ToolExecutionContext, resolve_tool_access_policy


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
    (root / "tests" / "test_app.py").write_text("def test_value(): pass\n", encoding="utf-8")
    _git(root, "add", "src/app.py", "tests/test_app.py")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


class FakeRunner:
    backend = "docker"

    def __init__(
        self,
        action=None,
        result: WorkspaceCommandResult | None = None,
        probe_error: str | None = None,
    ):
        self.action = action
        self.result = result or WorkspaceCommandResult(
            stdout="ok\n", exit_code=0, termination_reason="exited"
        )
        self.calls = []
        self.probe_error = probe_error

    def probe(self):
        if self.probe_error:
            raise WorkspaceRunnerError(self.probe_error)
        return RunnerProbe("docker", "sha256:" + "a" * 64)

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.action:
            self.action(Path(kwargs["workspace_root"]), kwargs["command"])
        return self.result

    def verify_workspace(self, **kwargs):
        return None

    def has_active_processes(self, workspace_id):
        return False


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


def _parent(db: SessionDB):
    names = tools.get_tool_manager().get_names()
    policy = resolve_tool_access_policy(None, names)
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


def _runtime(
    tmp_path: Path,
    root: Path,
    runner,
    action,
    *,
    enabled: bool = True,
):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db)

    def factory(spec, request, run_context):
        return FakeChildAgent(run_context, action)

    runtime = AgentRuntimeManager(
        db,
        ephemeral_factory=factory,
        evidence_recorder=recorder,
        workspace_manager=WorkspaceManager(managed_root=tmp_path / "worktrees"),
        workspace_runner=runner,
        runtime_config={
            "max_concurrency": 2,
            "delegate_batch_timeout_seconds": 10,
            "cancel_grace_seconds": 1,
            "run_timeout_seconds": {"delegate": 30},
            "worktree": {
                "enabled": enabled,
                "runner": "docker",
                "docker_image": "fake@sha256:test",
                "preserve_failed_days": 30,
            },
        },
    )
    parent = _parent(db)
    request = DelegationRequest(
        task="update app",
        tools={"read_file", "write_file", "bash"},
        execution_mode="worktree_write",
        write_scope=("src/",),
        verification_hint="python -m pytest -q",
    )
    payload = build_delegate_request(request, model="test")
    payload["_host_working_directory"] = str(root)
    return db, runtime, parent, request, payload


def _run(runtime, parent, request, payload):
    return runtime.run_ephemeral(
        spec=build_delegate_spec(request),
        request=payload,
        conversation_id="conversation",
        parent_task_id="parent-task",
        parent_run_id="parent-run",
        parent_run_context=parent,
    )


@pytest.mark.parametrize("write_tool", ["write_file", "bash"])
def test_delegate_request_requires_worktree_for_host_write_tools(write_tool: str):
    with pytest.raises(ValueError, match="require execution_mode=worktree_write"):
        DelegationRequest(task="unsafe", tools={write_tool})

    default_spec = build_delegate_spec(DelegationRequest(task="inspect only"))
    assert {"write_file", "bash"} <= set(default_spec.tool_policy["exclude"])


@pytest.mark.parametrize("write_tool", ["write_file", "bash"])
def test_runtime_rejects_direct_delegate_write_policy_bypass(
    tmp_path: Path, write_tool: str
):
    db = SessionDB(tmp_path / "state.db")
    parent = _parent(db)
    child_started = False

    def factory(spec, request, run_context):
        nonlocal child_started
        child_started = True
        raise AssertionError("unsafe child must not start")

    runtime = AgentRuntimeManager(
        db,
        ephemeral_factory=factory,
        runtime_config={"run_timeout_seconds": {}, "worktree": {"enabled": False}},
    )
    try:
        outcome = runtime.run_ephemeral(
            spec=AgentSpec(
                kind="delegate",
                tool_policy={"include": {write_tool}},
                persist_messages=False,
            ),
            request={"task": "unsafe", "execution_mode": None},
            conversation_id="conversation",
            parent_task_id="parent-task",
            parent_run_id="parent-run",
            parent_run_context=parent,
        )
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "delegate_write_requires_worktree"
        assert child_started is False
        assert db.get_agent_run(outcome.run_id)["status"] == "FAILED"
    finally:
        runtime.shutdown()
        db.close()


def test_serial_worktree_write_preserves_candidate_and_main_workspace(tmp_path: Path):
    root, base = _repository(tmp_path)
    original = (root / "src" / "app.py").read_bytes()

    def child_action(context):
        assert write_file(
            "src/app.py", "VALUE = 2\n", _workspace_context=context.workspace_context
        ).startswith("Successfully")
        return "updated"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.SUCCEEDED, outcome
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert lease["lease_status"] == "PRESERVED"
        assert lease["base_commit"] == base
        assert Path(lease["worktree_path"], "src", "app.py").read_text() == "VALUE = 2\n"
        assert (root / "src" / "app.py").read_bytes() == original
        assert _git(root, "rev-parse", "HEAD") == base
        assert lease["diff_hash"] and lease["change_manifest_hash"]

        detail = runtime.inspect_worktree(lease["workspace_id"])
        assert [item["path"] for item in detail["current_changes"]] == ["src/app.py"]
        discarded = runtime.discard_worktree(lease["workspace_id"])
        assert discarded["lease_status"] == "REJECTED"
        assert discarded["cleanup_status"] == "SUCCEEDED"
        assert not Path(lease["worktree_path"]).exists()
        assert (root / "src" / "app.py").read_bytes() == original
    finally:
        runtime.shutdown()
        db.close()


def test_file_tools_use_frozen_workspace_root_and_scope(tmp_path: Path):
    root, _ = _repository(tmp_path)

    def child_action(context):
        workspace = context.workspace_context
        assert "VALUE = 1" in read_file("src/app.py", _workspace_context=workspace)
        outside = write_file(
            "tests/test_app.py", "changed\n", _workspace_context=workspace
        )
        absolute = write_file(
            str(root / "src" / "app.py"), "changed\n", _workspace_context=workspace
        )
        protected = write_file(
            ".git/config", "changed\n", _workspace_context=workspace
        )
        assert "outside the frozen" in outside
        assert "absolute_path_forbidden" in absolute
        assert "protected_path" in protected
        return "checked"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.SUCCEEDED, outcome
        assert (root / "tests" / "test_app.py").read_text().startswith("def test_value")
    finally:
        runtime.shutdown()
        db.close()


def test_scope_violation_from_runner_fails_lease_and_stops_later_tools(tmp_path: Path):
    root, _ = _repository(tmp_path)

    def escape(workspace: Path, command: str):
        (workspace / "outside.txt").write_text("escaped\n", encoding="utf-8")

    runner = FakeRunner(action=escape)

    def child_action(context):
        call = {
            "id": "call-bash",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"modify"}'},
        }
        result = tools.execute_detailed(
            call,
            ToolExecutionContext(
                policy=context.tool_policy,
                run_context=context,
                db=context.workspace_context.db,
                working_directory=str(context.workspace_context.workspace_root),
                workspace_context=context.workspace_context,
                evidence_recorder=ExecutionEvidenceRecorder(
                    context.workspace_context.artifact_store,
                    context.workspace_context.db,
                ),
            ),
        )
        assert result.error_code == "scope_violation"
        blocked = write_file(
            "src/app.py", "VALUE = 3\n", _workspace_context=context.workspace_context
        )
        assert "scope_violation" in blocked
        return "violation observed"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, runner, child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "scope_violation", outcome
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert lease["lease_status"] == "FAILED"
        assert lease["failure_code"] == "scope_violation"
        assert Path(lease["worktree_path"], "outside.txt").is_file()
        assert not (root / "outside.txt").exists()
        records = db.list_execution_records(outcome.run_id)
        assert len(records) == 1
        assert records[0]["workspace_id"] == lease["workspace_id"]
        assert records[0]["log_status"] == "COMPLETE"
    finally:
        runtime.shutdown()
        db.close()


def test_ignored_out_of_scope_file_is_still_audited(tmp_path: Path):
    root, _ = _repository(tmp_path)
    (root / ".gitignore").write_text("*.cache\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore cache files")

    def create_ignored_file(workspace: Path, command: str):
        (workspace / "hidden.cache").write_text("not really hidden\n", encoding="utf-8")

    runner = FakeRunner(action=create_ignored_file)

    def child_action(context):
        result = context.workspace_context.execute_command(
            "create ignored output", timeout=10
        )
        assert result.error_code == "scope_violation"
        return "ignored violation observed"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, runner, child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "scope_violation"
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert lease["failure_code"] == "scope_violation"
        detail = runtime.inspect_worktree(lease["workspace_id"])
        ignored = next(
            item for item in detail["current_changes"]
            if item["path"] == "hidden.cache"
        )
        assert ignored["status"] == "ignored_untracked"
        assert ignored["git_status"] == "!!"
        assert ignored["sha256"] and ignored["size"] > 0
        assert not (root / "hidden.cache").exists()
    finally:
        runtime.shutdown()
        db.close()


def test_final_audit_can_turn_apparent_success_into_failed_run(tmp_path: Path):
    root, _ = _repository(tmp_path)

    def child_action(context):
        Path(
            context.workspace_context.workspace_root, "late-outside.txt"
        ).write_text("late violation\n", encoding="utf-8")
        return "apparently successful"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "scope_violation"
        run = db.get_agent_run(outcome.run_id)
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert run["status"] == "FAILED"
        assert run["error_code"] == "scope_violation"
        assert lease["lease_status"] == "FAILED"
        assert lease["failure_code"] == "scope_violation"
        assert Path(lease["worktree_path"], "late-outside.txt").is_file()
        assert not (root / "late-outside.txt").exists()
    finally:
        runtime.shutdown()
        db.close()


def test_serial_write_gate_wait_can_be_cancelled(tmp_path: Path):
    root, _ = _repository(tmp_path)
    db, runtime, parent, _, _ = _runtime(
        tmp_path, root, FakeRunner(), lambda context: "unused"
    )
    runtime._worktree_write_gate.acquire()

    def cancel_waiter():
        time.sleep(0.15)
        parent.cancel_reason = "parent_cancelled"
        parent.cancel_event.set()

    canceller = threading.Thread(target=cancel_waiter)
    canceller.start()
    try:
        with pytest.raises(AgentRunCancelled) as exc_info:
            runtime._acquire_worktree_write_gate(parent)
        assert exc_info.value.completion_reason == "parent_cancelled"
    finally:
        runtime._worktree_write_gate.release()
        canceller.join(timeout=1)
        runtime.shutdown()
        db.close()


@pytest.mark.parametrize(
    "flag", ["--assume-unchanged", "--skip-worktree"]
)
def test_git_gate_rejects_index_visibility_flags(tmp_path: Path, flag: str):
    root, _ = _repository(tmp_path)
    _git(root, "update-index", flag, "src/app.py")

    inspection = WorkspaceManager().inspect_git_workspace(root)

    assert inspection.eligible is False
    assert "index_visibility_flags_unsupported" in {
        failure.reason_code for failure in inspection.failures
    }


@pytest.mark.parametrize("enabled,error_code", [(False, "worktree_disabled")])
def test_explicit_worktree_gate_failure_never_falls_back_to_main(
    tmp_path: Path, enabled: bool, error_code: str
):
    root, _ = _repository(tmp_path)
    called = False

    def child_action(context):
        nonlocal called
        called = True
        return "must not run"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action, enabled=enabled
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == error_code
        assert called is False
        assert db.get_worktree_lease_for_run(outcome.run_id) is None
        assert _git(root, "status", "--porcelain") == ""
    finally:
        runtime.shutdown()
        db.close()


def test_dirty_git_gate_rejects_before_child_and_preserves_user_change(tmp_path: Path):
    root, _ = _repository(tmp_path)
    (root / "src" / "app.py").write_text("USER CHANGE\n", encoding="utf-8")
    called = False

    def child_action(context):
        nonlocal called
        called = True
        return "must not run"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "workspace_dirty"
        assert called is False
        assert (root / "src" / "app.py").read_text() == "USER CHANGE\n"
    finally:
        runtime.shutdown()
        db.close()


def test_runner_probe_and_parent_permission_fail_before_worktree_creation(tmp_path: Path):
    root, _ = _repository(tmp_path)

    def child_action(context):
        raise AssertionError("child must not start")

    db, runtime, parent, request, payload = _runtime(
        tmp_path,
        root,
        FakeRunner(probe_error="docker_image_unavailable"),
        child_action,
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.error_code == "docker_image_unavailable"
        assert db.get_worktree_lease_for_run(outcome.run_id) is None
    finally:
        runtime.shutdown()
        db.close()

    root, _ = _repository(tmp_path / "second")
    db, runtime, parent, request, payload = _runtime(
        tmp_path / "second", root, FakeRunner(), child_action
    )
    parent.tool_policy = resolve_tool_access_policy(
        {"include": {"read_file"}}, tools.get_tool_manager().get_names()
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.error_code == "parent_write_permission_missing"
        assert db.get_worktree_lease_for_run(outcome.run_id) is None
    finally:
        runtime.shutdown()
        db.close()


def test_docker_runtime_failure_preserves_diagnostics_and_fails_candidate(tmp_path: Path):
    root, _ = _repository(tmp_path)
    runner = FakeRunner(result=WorkspaceCommandResult(
        stderr="daemon unavailable",
        termination_reason="spawn_error",
        error_code="docker_unavailable",
    ))

    def child_action(context):
        from tools.bash import bash

        return bash(
            "python -m pytest -q",
            _workspace_context=context.workspace_context,
        )

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, runner, child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == RunStatus.FAILED
        assert outcome.error_code == "runner_failed"
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert lease["lease_status"] == "FAILED"
        assert lease["failure_code"] == "runner_failed"
        assert lease["diff_hash"] and lease["change_manifest_hash"]
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        runtime.shutdown()
        db.close()


def test_docker_runner_argv_has_strict_isolation_and_immutable_image(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_temp = tmp_path / "runtime"
    workspace.mkdir()
    task_temp.mkdir()
    (task_temp / "home").mkdir()
    (task_temp / "tmp").mkdir()
    (task_temp / "git-sentinel").write_text("hidden\n", encoding="utf-8")
    runner = DockerWorkspaceCommandRunner(
        "local-image", container_user="12345:12345", pids_limit=64, memory_limit="512m"
    )
    runner._probe_result = RunnerProbe("docker", "sha256:" + "b" * 64)

    argv, _ = runner.build_command(
        workspace_id="workspace-one",
        workspace_root=workspace,
        task_temp_root=task_temp,
        command="python -m pytest -q",
    )
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--read-only" in argv
    assert "--user 12345:12345" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "/workspace/.git,readonly" in joined
    assert "HOME=/tmp/minihermes/home" in argv
    assert "sha256:" + "b" * 64 in argv
    assert "-p" not in argv and "--publish" not in argv
    assert "docker.sock" not in joined


def test_runtime_restart_preserves_orphaned_running_worktree(tmp_path: Path):
    root, _ = _repository(tmp_path)
    db_path = tmp_path / "state.db"
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
    manager = WorkspaceManager(managed_root=tmp_path / "worktrees")
    context = manager.provision(
        db=db,
        runner=FakeRunner(),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        task_id="child-task",
        run_id="child-run",
        parent_run_id="parent-run",
        working_directory=root,
        write_scope=["src/"],
    )
    manager.start(context)
    workspace_path = context.workspace_root
    db.close()

    reopened = SessionDB(db_path)
    runtime = AgentRuntimeManager(
        reopened,
        ephemeral_factory=lambda spec, request, run_context: None,
        workspace_manager=manager,
        runtime_config={"run_timeout_seconds": {}, "worktree": {"enabled": False}},
    )
    try:
        lease = reopened.get_worktree_lease(context.workspace_id)
        assert lease["lease_status"] == "PRESERVED"
        assert workspace_path.is_dir()
        assert reopened.get_agent_run("child-run")["status"] == "INTERRUPTED"
    finally:
        runtime.shutdown()
        reopened.close()


@pytest.mark.parametrize(
    "reason,expected_status",
    [
        ("user_interrupt", RunStatus.CANCELLED),
        ("deadline_exceeded", RunStatus.TIMED_OUT),
    ],
)
def test_cancelled_or_timed_out_candidate_is_preserved_with_final_artifacts(
    tmp_path: Path, reason: str, expected_status: RunStatus
):
    root, _ = _repository(tmp_path)

    def child_action(context):
        context.cancel_reason = reason
        context.cancel_event.set()
        return "cancelled"

    db, runtime, parent, request, payload = _runtime(
        tmp_path, root, FakeRunner(), child_action
    )
    try:
        outcome = _run(runtime, parent, request, payload)
        assert outcome.status == expected_status
        lease = db.get_worktree_lease_for_run(outcome.run_id)
        assert lease["lease_status"] == "PRESERVED"
        assert lease["diff_hash"] and lease["change_manifest_hash"]
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        runtime.shutdown()
        db.close()
