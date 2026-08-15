"""W3: bounded parallel Worktree writers with serial host-side integration."""

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
    AgentRunContext,
    AgentRuntimeManager,
    DelegateBatchItem,
    RunStatus,
)
from agent.workspace_runner import (
    RunnerProbe,
    WorkspaceCommandResult,
    WorkspaceRunnerError,
)
from agent.worktree import WorkspaceManager
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
    (root / "src" / "one.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "two.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "src" / "three.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "src" / "four.py").write_text("VALUE = 4\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


class TrackingRunner:
    backend = "docker"

    def __init__(self):
        self.calls: list[dict] = []
        self.workspace_probes: list[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.block_first_verification = False
        self.fail_first_workspace_probe = False
        self.first_verification_started = threading.Event()
        self.release_first_verification = threading.Event()

    def probe(self):
        return RunnerProbe("docker", "sha256:" + "a" * 64)

    def verify_workspace(self, **kwargs):
        with self._lock:
            self.workspace_probes.append(kwargs["workspace_id"])
            probe_number = len(self.workspace_probes)
        if self.fail_first_workspace_probe and probe_number == 1:
            raise WorkspaceRunnerError("runner_workspace_probe_failed")

    def run(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            call_number = len(self.calls)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.block_first_verification and call_number == 1:
                self.first_verification_started.set()
                if not self.release_first_verification.wait(timeout=10):
                    return WorkspaceCommandResult(
                        stderr="verification release timed out",
                        termination_reason="timed_out",
                        error_code="timeout",
                    )
            return WorkspaceCommandResult(
                stdout="verification passed\n",
                exit_code=0,
                termination_reason="exited",
            )
        finally:
            with self._lock:
                self._active -= 1

    def has_active_processes(self, workspace_id):
        with self._lock:
            return self._active > 0


class TrackingWorkspaceManager(WorkspaceManager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._provision_lock = threading.Lock()
        self._active_provisions = 0
        self.max_active_provisions = 0

    def provision(self, **kwargs):
        with self._provision_lock:
            self._active_provisions += 1
            self.max_active_provisions = max(
                self.max_active_provisions, self._active_provisions
            )
        try:
            time.sleep(0.03)
            return super().provision(**kwargs)
        finally:
            with self._provision_lock:
                self._active_provisions -= 1


class FakeChildAgent:
    def __init__(self, run_context, request, action):
        self.run_context = run_context
        self.request = request
        self.action = action
        self.provider = None

    def interrupt(self):
        self.run_context.cancel_event.set()

    def run_conversation(self, **kwargs):
        response = self.action(self.request, self.run_context)
        return ConversationResult(
            final_response=response,
            reasoning="",
            messages=[],
            completion_reason="stop",
            iterations_used=1,
        )


class ActivityTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.two_active = threading.Event()

    def enter(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.two_active.set()

    def leave(self):
        with self._lock:
            self.active -= 1


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
    max_write_concurrency: int = 2,
    max_concurrency: int = 2,
    batch_timeout: float = 30,
):
    root, base = _repository(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    recorder = ExecutionEvidenceRecorder(store, db)
    runner = TrackingRunner()
    manager = TrackingWorkspaceManager(managed_root=tmp_path / "worktrees")
    holder = {"action": lambda request, context: "unused"}
    contexts: dict[str, AgentRunContext] = {}

    def factory(spec, request, run_context):
        contexts[request["task"]] = run_context
        return FakeChildAgent(run_context, request, holder["action"])

    runtime = AgentRuntimeManager(
        db,
        ephemeral_factory=factory,
        evidence_recorder=recorder,
        workspace_manager=manager,
        workspace_runner=runner,
        approval_callback=lambda *args, **kwargs: "once",
        runtime_config={
            "max_concurrency": max_concurrency,
            "delegate_batch_timeout_seconds": batch_timeout,
            "cancel_grace_seconds": 1,
            "run_timeout_seconds": {
                "delegate": 30,
                "worktree_integration": 30,
            },
            "worktree": {
                "enabled": True,
                "max_write_concurrency": max_write_concurrency,
                "runner": "docker",
                "docker_image": "fake@sha256:test",
                "preserve_failed_days": 30,
                "integration_verification_command": "python -m pytest -q",
            },
        },
    )
    return {
        "root": root,
        "base": base,
        "db": db,
        "runtime": runtime,
        "runner": runner,
        "manager": manager,
        "parent": _parent(db),
        "holder": holder,
        "contexts": contexts,
    }


def _item(root: Path, task: str, scope: str) -> DelegateBatchItem:
    request = DelegationRequest(
        task=task,
        tools={"read_file", "write_file", "bash"},
        execution_mode="worktree_write",
        write_scope=(scope,),
        verification_hint="python -m pytest -q",
    )
    payload = build_delegate_request(request, model="test")
    payload["_host_working_directory"] = str(root)
    return DelegateBatchItem(build_delegate_spec(request), payload)


def _items(root: Path, scopes: tuple[str, str] = ("src/one.py", "src/two.py")):
    return [
        _item(root, task, scope)
        for task, scope in zip(("first", "second"), scopes)
    ]


def _run_batch(env, items):
    return env["runtime"].run_delegate_batch(
        items=items,
        conversation_id="conversation",
        session_id=None,
        parent_task_id="parent-task",
        parent_run_id="parent-run",
        parent_run_context=env["parent"],
    )


def _write_for_task(request, context):
    target, value = {
        "first": ("src/one.py", "VALUE = 11\n"),
        "second": ("src/two.py", "VALUE = 22\n"),
        "third": ("src/three.py", "VALUE = 33\n"),
        "fourth": ("src/four.py", "VALUE = 44\n"),
    }[request["task"]]
    result = write_file(target, value, _workspace_context=context.workspace_context)
    assert result.startswith("Successfully")


def _close(env):
    env["runtime"].shutdown()
    env["db"].close()


def test_two_disjoint_worktree_writers_overlap_and_keep_results_ordered(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()

    def action(request, context):
        _write_for_task(request, context)
        activity.enter()
        try:
            assert activity.two_active.wait(timeout=15), "write delegates did not overlap"
            return f"done:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, _items(env["root"]))

        assert batch.parallel is True
        assert activity.max_active == 2
        assert len(env["runner"].workspace_probes) == 2
        assert env["manager"].max_active_provisions == 1
        assert [outcome.status for outcome in batch.outcomes] == [
            RunStatus.SUCCEEDED,
            RunStatus.SUCCEEDED,
        ]
        assert [outcome.result.final_response for outcome in batch.outcomes] == [
            "done:first",
            "done:second",
        ]
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert len({lease["workspace_id"] for lease in leases}) == 2
        assert len({lease["worktree_path"] for lease in leases}) == 2
        assert all(lease["lease_status"] == "PRESERVED" for lease in leases)
        assert Path(leases[0]["worktree_path"], "src", "one.py").read_text() == "VALUE = 11\n"
        assert Path(leases[1]["worktree_path"], "src", "two.py").read_text() == "VALUE = 22\n"
        assert (env["root"] / "src" / "one.py").read_text() == "VALUE = 1\n"
        assert (env["root"] / "src" / "two.py").read_text() == "VALUE = 2\n"
        assert _git(env["root"], "rev-parse", "HEAD") == env["base"]
    finally:
        _close(env)


def test_write_limit_one_forces_serial_fallback(tmp_path):
    env = _environment(tmp_path, max_write_concurrency=1)
    activity = ActivityTracker()

    def action(request, context):
        activity.enter()
        try:
            _write_for_task(request, context)
            time.sleep(0.08)
            return request["task"]
        finally:
            activity.leave()

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, _items(env["root"]))
        assert batch.parallel is False
        assert activity.max_active == 1
        assert all(outcome.status == RunStatus.SUCCEEDED for outcome in batch.outcomes)
    finally:
        _close(env)


def test_overlapping_scopes_can_modify_same_relative_file_in_parallel(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()

    def action(request, context):
        value = "VALUE = 11\n" if request["task"] == "first" else "VALUE = 111\n"
        result = write_file(
            "src/one.py", value, _workspace_context=context.workspace_context
        )
        assert result.startswith("Successfully")
        activity.enter()
        try:
            assert activity.two_active.wait(timeout=15)
            return f"same-file:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, [
            _item(env["root"], "first", "src/one.py"),
            _item(env["root"], "second", "src/one.py"),
        ])
        assert batch.parallel is True
        assert activity.max_active == 2
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert Path(leases[0]["worktree_path"], "src", "one.py").read_text() == "VALUE = 11\n"
        assert Path(leases[1]["worktree_path"], "src", "one.py").read_text() == "VALUE = 111\n"
        assert (env["root"] / "src" / "one.py").read_text() == "VALUE = 1\n"
    finally:
        _close(env)


def test_four_worktree_writers_queue_behind_two_execution_slots(tmp_path):
    env = _environment(tmp_path, max_concurrency=2)
    activity = ActivityTracker()
    release_first_wave = threading.Event()
    started_lock = threading.Lock()
    started_count = 0
    holder = {}

    def action(request, context):
        nonlocal started_count
        _write_for_task(request, context)
        activity.enter()
        with started_lock:
            started_count += 1
            ordinal = started_count
        try:
            if ordinal <= 2:
                assert release_first_wave.wait(timeout=15)
            else:
                time.sleep(0.08)
            return f"queued:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    items = [
        _item(env["root"], "first", "src/one.py"),
        _item(env["root"], "second", "src/two.py"),
        _item(env["root"], "third", "src/three.py"),
        _item(env["root"], "fourth", "src/four.py"),
    ]

    def run_batch():
        holder["batch"] = _run_batch(env, items)

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert activity.two_active.wait(timeout=15)
        deadline = time.monotonic() + 5
        child_runs = []
        while time.monotonic() < deadline:
            child_runs = [
                run for run in env["runtime"].list_runs("conversation", limit=10)
                if run["agent_kind"] == "delegate"
            ]
            if len(child_runs) == 4:
                break
            time.sleep(0.02)
        assert sorted(run["status"] for run in child_runs) == [
            "QUEUED", "QUEUED", "RUNNING", "RUNNING",
        ]

        release_first_wave.set()
        worker.join(timeout=20)
        assert not worker.is_alive()
        batch = holder["batch"]
        assert batch.parallel is True
        assert activity.max_active == 2
        assert [outcome.status for outcome in batch.outcomes] == [
            RunStatus.SUCCEEDED,
            RunStatus.SUCCEEDED,
            RunStatus.SUCCEEDED,
            RunStatus.SUCCEEDED,
        ]
        assert [outcome.result.final_response for outcome in batch.outcomes] == [
            "queued:first", "queued:second", "queued:third", "queued:fourth",
        ]
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert len({lease["workspace_id"] for lease in leases}) == 4
        assert env["manager"].max_active_provisions == 1
    finally:
        release_first_wave.set()
        if worker.is_alive():
            env["parent"].cancel_event.set()
            worker.join(timeout=3)
        _close(env)


def test_cancelling_queued_writer_does_not_start_it_or_cancel_other_tasks(tmp_path):
    env = _environment(tmp_path, max_concurrency=2)
    activity = ActivityTracker()
    release_running = threading.Event()
    started_lock = threading.Lock()
    started_tasks: set[str] = set()
    holder = {}

    def action(request, context):
        with started_lock:
            started_tasks.add(request["task"])
        activity.enter()
        try:
            if len(started_tasks) <= 2:
                assert release_running.wait(timeout=60)
            _write_for_task(request, context)
            return f"completed:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    items = [
        _item(env["root"], "first", "src/one.py"),
        _item(env["root"], "second", "src/two.py"),
        _item(env["root"], "third", "src/three.py"),
        _item(env["root"], "fourth", "src/four.py"),
    ]

    def run_batch():
        holder["batch"] = _run_batch(env, items)

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert activity.two_active.wait(timeout=60)
        deadline = time.monotonic() + 10
        queued_runs = []
        while time.monotonic() < deadline:
            child_runs = [
                run for run in env["runtime"].list_runs("conversation", limit=10)
                if run["agent_kind"] == "delegate"
            ]
            queued_runs = [run for run in child_runs if run["status"] == "QUEUED"]
            if len(child_runs) == 4 and len(queued_runs) == 2:
                break
            time.sleep(0.02)
        assert len(queued_runs) == 2
        cancelled = queued_runs[0]
        cancelled_task = env["db"].get_agent_task(cancelled["task_id"])
        assert env["runtime"].cancel(cancelled["run_id"]) == "CANCELLED"

        release_running.set()
        worker.join(timeout=60)
        assert not worker.is_alive()
        batch = holder["batch"]
        assert batch.parallel is True
        assert [outcome.status for outcome in batch.outcomes].count(
            RunStatus.CANCELLED
        ) == 1
        assert [outcome.status for outcome in batch.outcomes].count(
            RunStatus.SUCCEEDED
        ) == 3
        cancelled_outcome = next(
            outcome for outcome in batch.outcomes
            if outcome.status == RunStatus.CANCELLED
        )
        assert cancelled_outcome.run_id == cancelled["run_id"]
        assert cancelled_task["request_preview"] not in started_tasks
        assert env["db"].get_worktree_lease_for_run(cancelled["run_id"]) is None
        assert activity.max_active == 2
    finally:
        release_running.set()
        if worker.is_alive():
            env["parent"].cancel_event.set()
            worker.join(timeout=3)
        _close(env)


def test_parent_deadline_times_out_running_and_queued_writers(tmp_path):
    env = _environment(tmp_path, max_concurrency=2, batch_timeout=60)
    activity = ActivityTracker()
    started_lock = threading.Lock()
    started_tasks: set[str] = set()
    holder = {}

    def action(request, context):
        with started_lock:
            started_tasks.add(request["task"])
        activity.enter()
        try:
            while not context.is_cancelled():
                time.sleep(0.01)
            return f"timed-out:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    items = [
        _item(env["root"], "first", "src/one.py"),
        _item(env["root"], "second", "src/two.py"),
        _item(env["root"], "third", "src/three.py"),
        _item(env["root"], "fourth", "src/four.py"),
    ]

    def run_batch():
        holder["batch"] = _run_batch(env, items)

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert activity.two_active.wait(timeout=60)
        env["parent"].deadline_monotonic = time.monotonic() + 0.2
        worker.join(timeout=20)
        assert not worker.is_alive()

        batch = holder["batch"]
        assert batch.parallel is True
        assert batch.completion_reason == "deadline_exceeded"
        assert all(
            outcome.status == RunStatus.TIMED_OUT for outcome in batch.outcomes
        )
        assert len(started_tasks) == 2
        assert activity.max_active == 2
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert sum(lease is not None for lease in leases) == 2
        assert all(
            lease is None or lease["lease_status"] == "PRESERVED"
            for lease in leases
        )
    finally:
        env["parent"].cancel_event.set()
        if worker.is_alive():
            worker.join(timeout=3)
        _close(env)


def test_one_writer_failure_does_not_cancel_its_sibling(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()

    def action(request, context):
        _write_for_task(request, context)
        activity.enter()
        try:
            assert activity.two_active.wait(timeout=15)
            if request["task"] == "first":
                raise RuntimeError("intentional child failure")
            return "second survived"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, _items(env["root"]))
        assert batch.parallel is True
        assert [outcome.status for outcome in batch.outcomes] == [
            RunStatus.FAILED,
            RunStatus.SUCCEEDED,
        ]
        first_lease = env["db"].get_worktree_lease_for_run(batch.outcomes[0].run_id)
        second_lease = env["db"].get_worktree_lease_for_run(batch.outcomes[1].run_id)
        assert first_lease["lease_status"] == "FAILED"
        assert second_lease["lease_status"] == "PRESERVED"
        assert batch.outcomes[1].result.final_response == "second survived"
    finally:
        _close(env)


def test_one_writer_setup_failure_releases_ready_sibling(tmp_path):
    env = _environment(tmp_path)
    env["runner"].fail_first_workspace_probe = True

    def action(request, context):
        _write_for_task(request, context)
        return f"completed:{request['task']}"

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, _items(env["root"]))
        assert batch.parallel is True
        assert sorted(outcome.status.value for outcome in batch.outcomes) == [
            "FAILED",
            "SUCCEEDED",
        ]
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert sorted(lease["lease_status"] for lease in leases) == [
            "FAILED",
            "PRESERVED",
        ]
    finally:
        _close(env)


def test_cancelling_one_writer_does_not_cancel_its_sibling(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()
    first_stopped = threading.Event()
    holder = {}

    def action(request, context):
        activity.enter()
        try:
            if request["task"] == "first":
                while not context.is_cancelled():
                    time.sleep(0.01)
                first_stopped.set()
                return "first cancelled"
            assert first_stopped.wait(timeout=15)
            _write_for_task(request, context)
            return "second completed"
        finally:
            activity.leave()

    env["holder"]["action"] = action

    def run_batch():
        holder["batch"] = _run_batch(env, _items(env["root"]))

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert activity.two_active.wait(timeout=60)
        first_context = env["contexts"]["first"]
        assert env["runtime"].cancel(first_context.run_id) == "CANCEL_REQUESTED"
        worker.join(timeout=10)
        assert not worker.is_alive()
        batch = holder["batch"]
        assert [outcome.status for outcome in batch.outcomes] == [
            RunStatus.CANCELLED,
            RunStatus.SUCCEEDED,
        ]
        assert batch.outcomes[1].result.final_response == "second completed"
    finally:
        if worker.is_alive():
            env["parent"].cancel_event.set()
            worker.join(timeout=2)
        _close(env)


def test_one_writer_timeout_does_not_cancel_its_sibling(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()

    def action(request, context):
        activity.enter()
        try:
            assert activity.two_active.wait(timeout=15)
            if request["task"] == "first":
                context.deadline_monotonic = time.monotonic() + 0.2
                while not context.is_cancelled():
                    time.sleep(0.01)
                return "first timed out"
            time.sleep(0.35)
            _write_for_task(request, context)
            return "second completed"
        finally:
            activity.leave()

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, _items(env["root"]))
        assert [outcome.status for outcome in batch.outcomes] == [
            RunStatus.TIMED_OUT,
            RunStatus.SUCCEEDED,
        ]
        assert batch.outcomes[1].result.final_response == "second completed"
    finally:
        _close(env)


def test_parent_cancel_reaches_all_parallel_worktree_writers(tmp_path):
    env = _environment(tmp_path)
    activity = ActivityTracker()
    holder = {}

    def action(request, context):
        activity.enter()
        try:
            assert activity.two_active.wait(timeout=15)
            while not context.is_cancelled():
                time.sleep(0.01)
            return f"stopped:{request['task']}"
        finally:
            activity.leave()

    env["holder"]["action"] = action

    def run_batch():
        holder["batch"] = _run_batch(env, _items(env["root"]))

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert activity.two_active.wait(timeout=15)
        env["parent"].cancel_reason = "parent_cancelled"
        env["parent"].cancel_event.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        batch = holder["batch"]
        assert batch.completion_reason == "parent_cancelled"
        assert all(outcome.status == RunStatus.CANCELLED for outcome in batch.outcomes)
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        assert all(lease["lease_status"] == "PRESERVED" for lease in leases)
    finally:
        if worker.is_alive():
            env["parent"].cancel_event.set()
            worker.join(timeout=2)
        _close(env)


def test_parallel_candidates_are_integrated_serially_against_latest_main(tmp_path):
    env = _environment(tmp_path)

    def action(request, context):
        _write_for_task(request, context)
        return f"candidate:{request['task']}"

    env["holder"]["action"] = action
    results = {}
    try:
        batch = _run_batch(env, _items(env["root"]))
        assert batch.parallel is True
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]
        env["runner"].block_first_verification = True

        first_thread = threading.Thread(
            target=lambda: results.setdefault(
                "first",
                env["runtime"].integrate_worktree(leases[0]["workspace_id"]),
            )
        )
        second_thread = threading.Thread(
            target=lambda: results.setdefault(
                "second",
                env["runtime"].integrate_worktree(leases[1]["workspace_id"]),
            )
        )
        first_thread.start()
        assert env["runner"].first_verification_started.wait(timeout=5)
        second_thread.start()
        time.sleep(0.2)
        assert len(env["runner"].calls) == 1
        env["runner"].release_first_verification.set()
        first_thread.join(timeout=15)
        second_thread.join(timeout=15)
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()

        first = results["first"]
        second = results["second"]
        assert first["status"] == "MERGED"
        assert second["status"] == "MERGED"
        assert env["runner"].max_active == 1
        assert len(env["runner"].calls) == 2
        assert second["source_main_commit"] == first["final_merge_commit"]
        assert (env["root"] / "src" / "one.py").read_text() == "VALUE = 11\n"
        assert (env["root"] / "src" / "two.py").read_text() == "VALUE = 22\n"
        assert _git(env["root"], "rev-parse", "HEAD") == second["final_merge_commit"]
    finally:
        env["runner"].release_first_verification.set()
        _close(env)


def test_same_file_candidates_merge_first_and_preserve_second_conflict(tmp_path):
    env = _environment(tmp_path)

    def action(request, context):
        value = "VALUE = 11\n" if request["task"] == "first" else "VALUE = 111\n"
        result = write_file(
            "src/one.py", value, _workspace_context=context.workspace_context
        )
        assert result.startswith("Successfully")
        return f"same-file:{request['task']}"

    env["holder"]["action"] = action
    try:
        batch = _run_batch(env, [
            _item(env["root"], "first", "src/one.py"),
            _item(env["root"], "second", "src/one.py"),
        ])
        assert batch.parallel is True
        leases = [
            env["db"].get_worktree_lease_for_run(outcome.run_id)
            for outcome in batch.outcomes
        ]

        first = env["runtime"].integrate_worktree(leases[0]["workspace_id"])
        second = env["runtime"].integrate_worktree(leases[1]["workspace_id"])

        assert first["status"] == "MERGED"
        assert second["status"] == "CONFLICT"
        assert second["failure_code"] == "integration_conflict"
        assert second["lease"]["lease_status"] == "PRESERVED"
        assert (env["root"] / "src" / "one.py").read_text() == "VALUE = 11\n"
        assert _git(env["root"], "rev-parse", "HEAD") == first["final_merge_commit"]
    finally:
        _close(env)
