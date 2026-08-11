"""Phase 4：取消状态机与 CLI broker 测试。"""

import threading
import time
import os
import shlex
import subprocess
import sys

import pytest

from agent.runtime import (
    AgentRunCancelled,
    AgentRunContext,
    AgentRuntimeManager,
    AgentSpec,
    RunStatus,
)
from cli.approval import make_approval_callback
from cli.commands import handle_slash_command
from cli.state import AppState
from session import SessionDB
from tools.bash import bash


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_queued_background_run_can_be_cancelled_without_starting(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")
    entered = threading.Event()
    release = threading.Event()
    created = []

    def factory(spec, request, context):
        created.append(spec.kind)
        raise AssertionError("prepared jobs should not need an Agent")

    runtime = AgentRuntimeManager(db, ephemeral_factory=factory)

    def block_prepare(context):
        entered.set()
        assert release.wait(timeout=2)
        return None

    first = runtime.submit_background(
        spec=AgentSpec(kind="curator", persist_messages=False),
        request={"task": "block", "model": "test-model"},
        conversation_id="session-1",
        session_id="session-1",
        prepare=block_prepare,
    )
    assert entered.wait(timeout=2)
    second = runtime.submit_background(
        spec=AgentSpec(kind="curator", persist_messages=False),
        request={"task": "queued", "model": "test-model"},
        conversation_id="session-1",
        session_id="session-1",
        prepare=lambda context: None,
    )

    assert runtime.get_run(second.run_id)["status"] == "QUEUED"
    handled, _, _, _ = handle_slash_command(
        f"/cancel {second.run_id[:10]}",
        [],
        db,
        "session-1",
        runtime=runtime,
        conversation_id="session-1",
    )
    assert handled is True
    assert runtime.get_run(second.run_id)["status"] == "CANCELLED"

    release.set()
    assert _wait_until(
        lambda: runtime.get_run(first.run_id)["status"] == "SUCCEEDED"
    )
    assert created == []
    runtime.shutdown()
    db.close()


def test_approval_broker_cancellation_clears_only_matching_panel():
    state = AppState()
    callback = make_approval_callback(state)
    context = AgentRunContext(
        task_id="task-1",
        run_id="run-1",
        conversation_id="conversation-1",
        start_session_id="session-1",
    )
    observed = []

    def wait_for_approval():
        with pytest.raises(AgentRunCancelled):
            callback("bash", {"command": "rm temp"}, "sensitive", run_context=context)
        observed.append(True)

    worker = threading.Thread(target=wait_for_approval)
    worker.start()
    assert _wait_until(lambda: state.approval_state is not None)
    context.cancel_reason = "user_interrupt"
    context.cancel_event.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert observed == [True]
    assert state.approval_state is None

    # 不匹配的旧 Run 不能删除后来显示的面板。
    state.approval_state = {"run_id": "run-new"}
    assert state.clear_approval("run-old") is False
    assert state.approval_state["run_id"] == "run-new"


def test_bash_timeout_terminates_spawned_child_process(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    command = subprocess.list2cmdline([sys.executable, "-c", script])
    if os.name != "nt":
        command = " ".join(shlex.quote(part) for part in [sys.executable, "-c", script])

    started = time.monotonic()
    output = bash(command, timeout=0.8)

    assert output.startswith("Error: command timed out")
    assert time.monotonic() - started < 5
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    if os.name == "nt":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        assert str(child_pid) not in listing
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
