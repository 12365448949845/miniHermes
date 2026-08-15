"""Worktree W0：只读 Git 门禁、路径范围和 lease 数据模型。"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.worktree import (
    WorkspaceManager,
    WorkspaceValidationError,
    normalize_write_scope,
    parse_git_version,
)
from session import SessionDB
from session.db import SCHEMA_VERSION


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


def _repository(tmp_path: Path, name: str = "repo") -> tuple[Path, str]:
    root = tmp_path / name
    root.mkdir()
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


def _runtime_runs(db: SessionDB, suffix: str = "one") -> tuple[str, str, str]:
    parent_task = f"parent-task-{suffix}"
    parent_run = f"parent-run-{suffix}"
    child_task = f"child-task-{suffix}"
    child_run = f"child-run-{suffix}"
    db.create_agent_task(
        task_id=parent_task,
        conversation_id="worktree-test",
        session_id=None,
        parent_task_id=None,
        kind="main_turn",
        title="parent",
        request_preview="parent",
    )
    db.create_agent_run(
        run_id=parent_run,
        task_id=parent_task,
        parent_run_id=None,
        conversation_id="worktree-test",
        start_session_id=None,
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    db.create_agent_task(
        task_id=child_task,
        conversation_id="worktree-test",
        session_id=None,
        parent_task_id=parent_task,
        kind="delegate",
        title="child",
        request_preview="child",
    )
    db.create_agent_run(
        run_id=child_run,
        task_id=child_task,
        parent_run_id=parent_run,
        conversation_id="worktree-test",
        start_session_id=None,
        agent_kind="delegate",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="deny_sensitive",
        max_iterations=1,
        timeout_seconds=None,
    )
    return child_task, child_run, parent_run


def _lease(
    db: SessionDB,
    root: Path,
    base_commit: str,
    managed_root: Path,
    *,
    suffix: str = "one",
) -> dict:
    task_id, run_id, parent_run_id = _runtime_runs(db, suffix)
    workspace_id = f"workspace-{suffix}"
    return db.create_worktree_lease(
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        git_root=str(root),
        worktree_path=str(managed_root / workspace_id),
        branch_name=f"minihermes/worktree/{workspace_id}",
        base_commit=base_commit,
        write_scope=["src/", "tests/test_app.py"],
        runner_backend="docker",
        runner_image_digest="sha256:test",
    )


def test_git_gate_accepts_a_clean_primary_repository(tmp_path: Path):
    root, head = _repository(tmp_path)
    result = WorkspaceManager().inspect_git_workspace(root / "src")

    assert result.eligible is True
    assert result.failures == ()
    assert result.git_root == root.resolve()
    assert result.head_commit == head
    assert result.git_version >= (2, 20, 0)


def test_git_gate_reports_dirty_busy_and_in_progress_states(tmp_path: Path):
    root, head = _repository(tmp_path)
    (root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="ascii")

    result = WorkspaceManager().inspect_git_workspace(root, repository_busy=True)
    reasons = {failure.reason_code for failure in result.failures}

    assert result.eligible is False
    assert {"workspace_dirty", "git_operation_in_progress", "repository_busy"} <= reasons


def test_git_gate_reports_non_repository_and_missing_git(tmp_path: Path, monkeypatch):
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    # 即使 pytest 的 basetemp 位于另一个仓库内，也要阻止 Git 向父目录查找。
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(ordinary.parent))
    assert WorkspaceManager().inspect_git_workspace(ordinary).primary_reason == "not_git_repository"
    assert WorkspaceManager(
        git_executable="git-command-that-does-not-exist"
    ).inspect_git_workspace(ordinary).primary_reason == "git_unavailable"


def test_git_gate_rejects_submodules_lfs_and_nested_repositories(tmp_path: Path):
    submodule_root, head = _repository(tmp_path, "submodule-repo")
    _git(
        submodule_root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{head},vendor/dependency",
    )
    submodule_reasons = {
        item.reason_code
        for item in WorkspaceManager().inspect_git_workspace(submodule_root).failures
    }
    assert "submodule_unsupported" in submodule_reasons

    lfs_root, _ = _repository(tmp_path, "lfs-repo")
    (lfs_root / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", encoding="utf-8")
    _git(lfs_root, "add", ".gitattributes")
    _git(lfs_root, "commit", "-m", "lfs attributes")
    lfs_reasons = {
        item.reason_code
        for item in WorkspaceManager().inspect_git_workspace(lfs_root).failures
    }
    assert "git_lfs_unsupported" in lfs_reasons

    nested_root, _ = _repository(tmp_path, "nested-repo")
    nested = nested_root / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init")
    _git(nested, "config", "user.name", "Nested Test")
    _git(nested, "config", "user.email", "nested@example.invalid")
    (nested / "file.txt").write_text("nested\n", encoding="utf-8")
    _git(nested, "add", "file.txt")
    _git(nested, "commit", "-m", "nested")
    nested_reasons = {
        item.reason_code
        for item in WorkspaceManager().inspect_git_workspace(nested_root).failures
    }
    assert "nested_repository_unsupported" in nested_reasons


def test_write_scope_is_canonical_and_rejects_unsafe_forms(tmp_path: Path):
    root, _ = _repository(tmp_path)
    assert normalize_write_scope(
        ["src/pkg/", "tests/test_app.py", "src/", "src/"],
        workspace_root=root,
    ) == ("src/", "tests/test_app.py")
    assert normalize_write_scope(["generated", "generated/"]) == ("generated/",)

    unsafe = (
        "",
        "../outside.py",
        "/absolute.py",
        "C:/drive.py",
        "src\\windows.py",
        ".git/config",
        ".minihermes/state",
        "src/*.py",
        "src//app.py",
        "src",
    )
    for value in unsafe:
        with pytest.raises(WorkspaceValidationError):
            normalize_write_scope([value], workspace_root=root)


def test_workspace_path_resolution_enforces_root_and_frozen_scope(tmp_path: Path):
    root, _ = _repository(tmp_path)
    manager = WorkspaceManager()
    scope = manager.validate_write_scope(["src/"], workspace_root=root)

    assert manager.resolve_workspace_path(
        root, "src/app.py", write_scope=scope, require_write=True
    ) == (root / "src" / "app.py").resolve()
    assert manager.resolve_workspace_path(root, ".", allow_root=True) == root.resolve()
    with pytest.raises(WorkspaceValidationError, match="outside the frozen"):
        manager.resolve_workspace_path(
            root, "tests/test_app.py", write_scope=scope, require_write=True
        )
    for value in ("../outside.py", "/absolute.py", ".git/config"):
        with pytest.raises(WorkspaceValidationError):
            manager.resolve_workspace_path(root, value)


def test_workspace_path_resolution_rejects_symlink_escape_and_scope_bypass(tmp_path: Path):
    root, _ = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "src" / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    manager = WorkspaceManager()
    with pytest.raises(WorkspaceValidationError, match="escapes"):
        manager.resolve_workspace_path(root, "src/escape/file.py")

    secret = root / "secret"
    secret.mkdir()
    internal = root / "src" / "internal"
    internal.symlink_to(secret, target_is_directory=True)
    with pytest.raises(WorkspaceValidationError, match="resolved target"):
        manager.resolve_workspace_path(
            root,
            "src/internal/file.py",
            write_scope=["src/"],
            require_write=True,
        )


def test_worktree_lease_state_machine_and_evidence_links(tmp_path: Path):
    root, head = _repository(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    lease = _lease(db, root, head, tmp_path / "managed")

    assert lease["lease_status"] == "PROVISIONING"
    assert lease["cleanup_status"] == "PENDING"
    assert lease["write_scope"] == ["src/", "tests/test_app.py"]
    with pytest.raises(RuntimeError, match="illegal worktree lease transition"):
        db.transition_worktree_lease(lease["workspace_id"], status="MERGED")

    db.transition_worktree_lease(lease["workspace_id"], status="READY")
    db.transition_worktree_lease(lease["workspace_id"], status="RUNNING")
    preserved = db.transition_worktree_lease(
        lease["workspace_id"], status="PRESERVED"
    )
    assert preserved["lease_status"] == "PRESERVED"

    run_id = lease["run_id"]
    db.create_tool_execution(
        execution_id="tool-worktree",
        run_id=run_id,
        tool_call_id="call-worktree",
        tool_name="bash",
    )
    snapshot = db.create_workspace_snapshot(
        snapshot_id="snapshot-worktree",
        run_id=run_id,
        workspace_id=lease["workspace_id"],
        workspace_root=lease["worktree_path"],
        git_root=lease["worktree_path"],
        capture_status="PARTIAL",
    )
    record = db.create_execution_record(
        record_id="record-worktree",
        run_id=run_id,
        workspace_id=lease["workspace_id"],
        tool_execution_id="tool-worktree",
        tool_name="bash",
        snapshot_id=snapshot["snapshot_id"],
    )
    assert snapshot["workspace_id"] == lease["workspace_id"]
    assert record["workspace_id"] == lease["workspace_id"]
    assert db.get_worktree_lease_for_run(run_id)["workspace_id"] == lease["workspace_id"]
    db.close()


def test_worktree_lease_is_unique_per_run_and_rejects_cross_run_evidence(tmp_path: Path):
    root, head = _repository(tmp_path)
    db = SessionDB(tmp_path / "state.db")
    first = _lease(db, root, head, tmp_path / "managed", suffix="first")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_worktree_lease(
            workspace_id="workspace-duplicate",
            task_id=first["task_id"],
            run_id=first["run_id"],
            parent_run_id=first["parent_run_id"],
            git_root=str(root),
            worktree_path=str(tmp_path / "managed" / "workspace-duplicate"),
            branch_name="minihermes/worktree/workspace-duplicate",
            base_commit=head,
            write_scope=["src/"],
            runner_backend="docker",
        )

    second = _lease(db, root, head, tmp_path / "managed", suffix="second")
    with pytest.raises(ValueError, match="does not belong"):
        db.create_workspace_snapshot(
            snapshot_id="snapshot-cross-run",
            run_id=second["run_id"],
            workspace_id=first["workspace_id"],
            workspace_root=first["worktree_path"],
            git_root=first["worktree_path"],
        )
    db.close()


def test_v7_database_migrates_to_worktree_schema_without_losing_evidence(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    task_id, run_id, _ = _runtime_runs(db, "legacy")
    db.create_tool_execution(
        execution_id="tool-legacy-worktree",
        run_id=run_id,
        tool_call_id="call-legacy-worktree",
        tool_name="bash",
    )
    db.create_workspace_snapshot(
        snapshot_id="snapshot-legacy-worktree",
        run_id=run_id,
        workspace_root="C:/legacy",
        git_root="C:/legacy",
    )
    db.create_execution_record(
        record_id="record-legacy-worktree",
        run_id=run_id,
        tool_execution_id="tool-legacy-worktree",
        tool_name="bash",
        snapshot_id="snapshot-legacy-worktree",
    )
    db._conn.execute("DROP INDEX IF EXISTS idx_execution_records_verification")
    db._conn.execute("DROP INDEX IF EXISTS idx_execution_records_workspace")
    db._conn.execute("DROP INDEX IF EXISTS idx_workspace_snapshots_workspace")
    db._conn.execute("ALTER TABLE execution_records DROP COLUMN workspace_id")
    db._conn.execute("ALTER TABLE workspace_snapshots DROP COLUMN workspace_id")
    db._conn.execute("DROP TABLE worktree_leases")
    db._conn.execute("PRAGMA user_version=7")
    db.close()

    migrated = SessionDB(db_path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.get_agent_task(task_id) is not None
    assert migrated.get_agent_run(run_id) is not None
    assert migrated.get_workspace_snapshot("snapshot-legacy-worktree") is not None
    assert migrated.get_execution_record("record-legacy-worktree") is not None
    assert migrated.get_workspace_snapshot("snapshot-legacy-worktree")["workspace_id"] is None
    assert migrated.get_execution_record("record-legacy-worktree")["workspace_id"] is None
    assert migrated._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'worktree_leases'"
    ).fetchone()[0] == "worktree_leases"
    migrated.close()


def test_parse_git_version_handles_platform_suffixes():
    assert parse_git_version("git version 2.46.0.windows.1") == (2, 46, 0)
    assert parse_git_version("git version 2.39") == (2, 39, 0)
    with pytest.raises(WorkspaceValidationError, match="git_version_unrecognized"):
        parse_git_version("unknown")
