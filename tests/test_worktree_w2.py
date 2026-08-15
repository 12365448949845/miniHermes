"""W2：Worktree 显式验证、双重审批与本地主分支集成。"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import tools
from agent.agent import ConversationResult
from agent.delegate import DelegationRequest, build_delegate_request, build_delegate_spec
from agent.reproducibility import ArtifactStore, ExecutionEvidenceRecorder
from agent.runtime import AgentRunContext, AgentRuntimeManager, RunStatus
from agent.workspace_runner import RunnerProbe, WorkspaceCommandResult
from agent.worktree import WorkspaceIntegrationError, WorkspaceManager
from session import SessionDB
from tools.files import write_file
from tools.registry import resolve_tool_access_policy


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
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
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


class FakeRunner:
    backend = "docker"

    def __init__(self, result: WorkspaceCommandResult | None = None, action=None):
        self.result = result or WorkspaceCommandResult(
            stdout="verification passed\n",
            exit_code=0,
            termination_reason="exited",
        )
        self.action = action
        self.calls = []
        self.active = False

    def probe(self):
        return RunnerProbe("docker", "sha256:" + "a" * 64)

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.action:
            self.action(Path(kwargs["workspace_root"]), kwargs["command"])
        return self.result

    def verify_workspace(self, **kwargs):
        return None

    def has_active_processes(self, workspace_id):
        return self.active


class BlockingRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if kwargs["cancel_check"]():
                return WorkspaceCommandResult(
                    stderr="cancelled",
                    termination_reason="cancelled",
                    error_code="cancelled",
                )
            time.sleep(0.01)
        return WorkspaceCommandResult(
            stderr="test runner did not receive cancellation",
            termination_reason="timed_out",
        )


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


def _environment(
    tmp_path: Path,
    *,
    verification_command: str = "python -m pytest -q",
    approvals: list[str] | None = None,
    runner: FakeRunner | None = None,
    manager: WorkspaceManager | None = None,
):
    root, base = _repository(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db)
    runner = runner or FakeRunner()
    holder = {"action": lambda context: "unused"}
    approval_stages = []
    choices = list(approvals or ["once", "once"])

    def factory(spec, request, run_context):
        return FakeChildAgent(run_context, holder["action"])

    def approve(tool_name, args, description, run_context=None):
        approval_stages.append(args.get("stage"))
        return choices.pop(0) if choices else "deny"

    manager = manager or WorkspaceManager(managed_root=tmp_path / "worktrees")
    runtime = AgentRuntimeManager(
        db,
        ephemeral_factory=factory,
        evidence_recorder=recorder,
        workspace_manager=manager,
        workspace_runner=runner,
        approval_callback=approve,
        runtime_config={
            "max_concurrency": 2,
            "delegate_batch_timeout_seconds": 10,
            "cancel_grace_seconds": 1,
            "run_timeout_seconds": {"delegate": 30, "worktree_integration": 30},
            "worktree": {
                "enabled": True,
                "runner": "docker",
                "docker_image": "fake@sha256:test",
                "preserve_failed_days": 30,
                "integration_verification_command": verification_command,
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
        "approval_stages": approval_stages,
    }


def _candidate(env, value="VALUE = 2\n", *, cancel_reason: str | None = None):
    def action(context):
        assert write_file(
            "src/app.py", value, _workspace_context=context.workspace_context
        ).startswith("Successfully")
        if cancel_reason:
            context.cancel_reason = cancel_reason
            context.cancel_event.set()
        return "updated"

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
    return outcome, env["db"].get_worktree_lease_for_run(outcome.run_id)


def _close(env):
    env["runtime"].shutdown()
    env["db"].close()


def test_verified_candidate_is_merged_with_identical_tree_and_evidence(tmp_path):
    env = _environment(tmp_path)
    try:
        outcome, lease = _candidate(env)
        assert outcome.status == RunStatus.SUCCEEDED

        result = env["runtime"].integrate_worktree(lease["workspace_id"])

        assert result["status"] == "MERGED"
        assert result["expected_merge_tree_hash"] == result["final_merge_tree_hash"]
        assert result["verification_record_id"]
        assert result["result_artifact_hash"]
        assert env["approval_stages"] == ["candidate_commit", "final_apply"]
        assert _git(env["root"], "rev-parse", "HEAD") == result["final_merge_commit"]
        assert _git(env["root"], "rev-parse", "HEAD^{tree}") == result["final_merge_tree_hash"]
        assert (env["root"] / "src" / "app.py").read_text() == "VALUE = 2\n"
        assert result["lease"]["lease_status"] == "MERGED"
        assert result["lease"]["cleanup_status"] == "SUCCEEDED"
        assert not Path(lease["worktree_path"]).exists()
        execution = env["db"].get_execution_record(result["verification_record_id"])
        assert execution["run_id"] == result["integration_run_id"]
        assert execution["workspace_id"] == lease["workspace_id"]
    finally:
        _close(env)


def test_missing_verification_command_fails_before_any_integration_record(tmp_path):
    env = _environment(tmp_path, verification_command="")
    try:
        _, lease = _candidate(env)
        with pytest.raises(WorkspaceIntegrationError) as caught:
            env["runtime"].integrate_worktree(lease["workspace_id"])
        assert caught.value.reason_code == "integration_verification_command_required"
        assert env["db"].list_worktree_integrations(workspace_id=lease["workspace_id"]) == []
        assert env["db"].get_worktree_lease(lease["workspace_id"])["lease_status"] == "PRESERVED"
    finally:
        _close(env)


def test_dirty_main_workspace_is_rejected_and_candidate_is_preserved(tmp_path):
    env = _environment(tmp_path)
    try:
        _, lease = _candidate(env)
        (env["root"] / "local.txt").write_text("user change\n", encoding="utf-8")
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "workspace_dirty"
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert (env["root"] / "local.txt").read_text() == "user change\n"
        assert env["approval_stages"] == []
    finally:
        _close(env)


def test_candidate_tampering_is_rejected_before_commit(tmp_path):
    env = _environment(tmp_path)
    try:
        _, lease = _candidate(env)
        Path(lease["worktree_path"], "src", "app.py").write_text(
            "TAMPERED\n", encoding="utf-8"
        )
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "candidate_changed_after_finalization"
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


@pytest.mark.parametrize(
    "choices,expected_code,expected_approvals",
    [
        (["deny"], "candidate_commit_denied", ["candidate_commit"]),
        (["once", "deny"], "final_apply_denied", ["candidate_commit", "final_apply"]),
    ],
)
def test_both_integration_approval_gates_fail_closed(
    tmp_path, choices, expected_code, expected_approvals
):
    env = _environment(tmp_path, approvals=choices)
    try:
        _, lease = _candidate(env)
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "DENIED"
        assert result["failure_code"] == expected_code
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert env["approval_stages"] == expected_approvals
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


def test_conflict_and_verification_failure_leave_main_unchanged(tmp_path):
    env = _environment(tmp_path)
    try:
        _, lease = _candidate(env, "CANDIDATE\n")
        (env["root"] / "src" / "app.py").write_text("MAIN\n", encoding="utf-8")
        _git(env["root"], "add", "src/app.py")
        _git(env["root"], "commit", "-m", "main change")
        main_head = _git(env["root"], "rev-parse", "HEAD")
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "CONFLICT"
        assert result["failure_code"] == "integration_conflict"
        assert _git(env["root"], "rev-parse", "HEAD") == main_head
        assert (env["root"] / "src" / "app.py").read_text() == "MAIN\n"
        assert result["lease"]["lease_status"] == "PRESERVED"
    finally:
        _close(env)

    failed_runner = FakeRunner(result=WorkspaceCommandResult(
        stderr="tests failed\n",
        exit_code=1,
        termination_reason="exited",
    ))
    env = _environment(tmp_path / "verification", runner=failed_runner)
    try:
        _, lease = _candidate(env)
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["failure_code"] == "integration_verification_failed"
        assert result["verification_record_id"]
        assert result["expected_merge_tree_hash"]
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
        assert result["lease"]["lease_status"] == "PRESERVED"
    finally:
        _close(env)


def test_main_head_change_after_verification_is_rejected(tmp_path):
    env = _environment(tmp_path)
    try:
        _, lease = _candidate(env)

        original = env["runtime"]._approval_callback

        def advance_main(tool_name, args, description, run_context=None):
            choice = original(tool_name, args, description, run_context=run_context)
            if args.get("stage") == "final_apply":
                (env["root"] / "advanced.txt").write_text("advanced\n", encoding="utf-8")
                _git(env["root"], "add", "advanced.txt")
                _git(env["root"], "commit", "-m", "external advance")
            return choice

        env["runtime"]._approval_callback = advance_main
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "main_head_changed"
        assert (env["root"] / "advanced.txt").read_text() == "advanced\n"
        assert result["lease"]["lease_status"] == "PRESERVED"
    finally:
        _close(env)


def test_non_succeeded_source_run_is_not_integration_eligible(tmp_path):
    env = _environment(tmp_path / "cancelled")
    try:
        outcome, lease = _candidate(env, cancel_reason="user_interrupt")
        assert outcome.status == RunStatus.CANCELLED
        with pytest.raises(WorkspaceIntegrationError) as caught:
            env["runtime"].integrate_worktree(lease["workspace_id"])
        assert caught.value.reason_code == "source_run_not_succeeded"
        assert env["db"].list_worktree_integrations(workspace_id=lease["workspace_id"]) == []
    finally:
        _close(env)


def test_detached_main_and_broken_approval_callback_fail_closed(tmp_path):
    env = _environment(tmp_path / "detached")
    try:
        _, lease = _candidate(env)
        _git(env["root"], "checkout", "--detach")
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "source_branch_required"
        assert result["lease"]["lease_status"] == "PRESERVED"
    finally:
        _close(env)

    env = _environment(tmp_path / "approval")
    try:
        _, lease = _candidate(env)

        def broken_approval(*args, **kwargs):
            raise RuntimeError("approval UI unavailable")

        env["runtime"]._approval_callback = broken_approval
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "FAILED"
        assert result["failure_code"] == "integration_approval_failed"
        assert result["lease"]["lease_status"] == "FAILED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)

def test_missing_git_identity_fails_before_approval(tmp_path):
    env = _environment(tmp_path / "identity")
    try:
        _, lease = _candidate(env)
        _git(env["root"], "config", "user.name", "")
        _git(env["root"], "config", "user.email", "")
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "git_identity_missing"
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert env["approval_stages"] == []
    finally:
        _close(env)


@pytest.mark.parametrize(
    "runner_result,expected_status,expected_code",
    [
        (
            WorkspaceCommandResult(
                stderr="timed out", termination_reason="timed_out"
            ),
            "VERIFICATION_FAILED",
            "integration_verification_timed_out",
        ),
        (
            WorkspaceCommandResult(
                stderr="cancelled", termination_reason="cancelled"
            ),
            "CANCELLED",
            "cancelled",
        ),
        (
            WorkspaceCommandResult(
                stderr="runner unavailable",
                termination_reason="spawn_error",
                error_code="runner_unavailable",
            ),
            "VERIFICATION_FAILED",
            "runner_unavailable",
        ),
    ],
)
def test_verification_timeout_cancel_and_runner_failure_are_recorded(
    tmp_path, runner_result, expected_status, expected_code
):
    env = _environment(tmp_path, runner=FakeRunner(result=runner_result))
    try:
        _, lease = _candidate(env)
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == expected_status
        assert result["failure_code"] == expected_code
        assert result["verification_record_id"]
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


def test_runtime_cancel_reaches_active_integration_runner(tmp_path):
    runner = BlockingRunner()
    env = _environment(tmp_path, runner=runner)
    holder = {}
    try:
        _, lease = _candidate(env)

        def integrate():
            holder["result"] = env["runtime"].integrate_worktree(
                lease["workspace_id"]
            )

        worker = threading.Thread(target=integrate)
        worker.start()
        assert runner.started.wait(timeout=10)
        records = env["db"].list_worktree_integrations(
            workspace_id=lease["workspace_id"]
        )
        assert len(records) == 1
        assert env["runtime"].cancel(
            records[0]["integration_run_id"], reason="user_interrupt"
        ) == "CANCEL_REQUESTED"
        worker.join(timeout=10)
        assert not worker.is_alive()
        result = holder["result"]
        assert result["status"] == "CANCELLED"
        assert result["failure_code"] in {"cancelled", "user_interrupt"}
        assert result["lease"]["lease_status"] == "PRESERVED"
        run = env["db"].get_agent_run(result["integration_run_id"])
        assert run["status"] == "CANCELLED"
    finally:
        _close(env)


def test_verifier_cannot_change_tracked_merge_content(tmp_path):
    def mutate(workspace: Path, command: str):
        (workspace / "src" / "app.py").write_text("VERIFIER MUTATION\n", encoding="utf-8")

    env = _environment(tmp_path, runner=FakeRunner(action=mutate))
    try:
        _, lease = _candidate(env)
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["failure_code"] == "verification_modified_tracked_files"
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


class CleanupFailOnceManager(WorkspaceManager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fail_candidate_cleanup = True

    def cleanup_merged_candidate(self, *, db, runner, workspace_id):
        if self.fail_candidate_cleanup:
            self.fail_candidate_cleanup = False
            db.set_worktree_cleanup_status(
                workspace_id,
                cleanup_status="FAILED",
                failure_message="injected cleanup failure",
            )
            raise WorkspaceIntegrationError("injected_cleanup_failure")
        return super().cleanup_merged_candidate(
            db=db, runner=runner, workspace_id=workspace_id
        )


class MergeTreeMismatchManager(WorkspaceManager):
    def apply_integration_to_main(self, *, workspace, runner):
        replacement = ("0" if workspace.expected_merge_tree_hash[0] != "0" else "1")
        mismatched = replace(
            workspace,
            expected_merge_tree_hash=(
                replacement + workspace.expected_merge_tree_hash[1:]
            ),
        )
        return super().apply_integration_to_main(
            workspace=mismatched, runner=runner
        )


def test_merged_cleanup_can_retry_without_repeating_merge(tmp_path):
    manager = CleanupFailOnceManager(managed_root=tmp_path / "worktrees")
    env = _environment(tmp_path, manager=manager)
    try:
        _, lease = _candidate(env)
        first = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert first["status"] == "MERGED"
        assert first["lease"]["cleanup_status"] == "FAILED"
        final_head = _git(env["root"], "rev-parse", "HEAD")
        integration_count = len(env["db"].list_worktree_integrations())

        second = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert second["status"] == "MERGED"
        assert second["lease"]["cleanup_status"] == "SUCCEEDED"
        assert _git(env["root"], "rev-parse", "HEAD") == final_head
        assert len(env["db"].list_worktree_integrations()) == integration_count
    finally:
        _close(env)


def test_final_merge_tree_mismatch_is_aborted_before_commit(tmp_path):
    manager = MergeTreeMismatchManager(managed_root=tmp_path / "worktrees")
    env = _environment(tmp_path, manager=manager)
    try:
        _, lease = _candidate(env)
        result = env["runtime"].integrate_worktree(lease["workspace_id"])
        assert result["status"] == "PRECONDITION_FAILED"
        assert result["failure_code"] == "final_merge_tree_mismatch"
        assert result["lease"]["lease_status"] == "PRESERVED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
        assert _git(env["root"], "status", "--porcelain") == ""
    finally:
        _close(env)


def test_runtime_restart_reconciles_integration_without_touching_git(tmp_path):
    env = _environment(tmp_path)
    reopened = None
    runtime = None
    try:
        _, lease = _candidate(env)
        db = env["db"]
        task = env["runtime"].create_task(
            conversation_id="conversation",
            session_id=None,
            parent_task_id=lease["task_id"],
            kind="worktree_integration",
            request="interrupted integration",
        )
        integration_run_id = "integration-run"
        db.create_agent_run(
            run_id=integration_run_id,
            task_id=task["task_id"],
            parent_run_id=lease["run_id"],
            conversation_id="conversation",
            start_session_id=None,
            agent_kind="worktree_integration",
            model="",
            tool_policy_json="{}",
            approval_mode="interactive",
            max_iterations=0,
            timeout_seconds=30,
        )
        db.start_agent_run(integration_run_id, task["task_id"])
        db.start_worktree_integration(
            integration_id="integration-restart",
            workspace_id=lease["workspace_id"],
            integration_run_id=integration_run_id,
            source_main_commit=env["base"],
            verification_command_hash=hashlib.sha256(b"test").hexdigest(),
        )
        env["runtime"].shutdown()
        db.close()

        reopened = SessionDB(tmp_path / "state.db")
        runtime = AgentRuntimeManager(
            reopened,
            ephemeral_factory=lambda spec, request, run_context: None,
            evidence_recorder=ExecutionEvidenceRecorder(env["store"], reopened),
            workspace_manager=env["manager"],
            workspace_runner=env["runner"],
            approval_callback=lambda *args, **kwargs: "deny",
            runtime_config={"run_timeout_seconds": {}, "worktree": {"enabled": False}},
        )
        record = reopened.get_worktree_integration("integration-restart")
        assert record["status"] == "INTERRUPTED"
        assert record["failure_code"] == "runtime_restart"
        assert reopened.get_worktree_lease(lease["workspace_id"])["lease_status"] == "PRESERVED"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
        assert Path(lease["worktree_path"]).is_dir()
    finally:
        if runtime is not None:
            runtime.shutdown()
        if reopened is not None:
            reopened.close()
        elif env["db"]._conn:
            try:
                _close(env)
            except Exception:
                pass


def test_v12_database_migrates_and_interrupted_integration_is_reconciled(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 13
    db._conn.execute("DROP TABLE worktree_integration_records")
    db._conn.execute("PRAGMA user_version=12")
    db.close()

    reopened = SessionDB(path)
    try:
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 13
        columns = {
            row[1] for row in reopened._conn.execute(
                "PRAGMA table_info(worktree_integration_records)"
            ).fetchall()
        }
        assert {"integration_id", "expected_merge_tree_hash", "temp_cleanup_status"} <= columns
    finally:
        reopened.close()
