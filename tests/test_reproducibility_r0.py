import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.reproducibility import (
    ArtifactCleanupError,
    ArtifactPathError,
    ArtifactRetentionManager,
    ArtifactStore,
)
from session import SessionDB
from session.db import SCHEMA_VERSION


def _create_run(db: SessionDB, suffix: str = "one") -> tuple[str, str]:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id="conversation",
        session_id=None,
        parent_task_id=None,
        kind="delegate",
        title="test run",
        request_preview="test",
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=None,
        conversation_id="conversation",
        start_session_id=None,
        agent_kind="delegate",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    return task_id, run_id


def _create_tool_execution(db: SessionDB, run_id: str, suffix: str = "one") -> str:
    execution_id = f"tool-{suffix}"
    db.create_tool_execution(
        execution_id=execution_id,
        run_id=run_id,
        tool_call_id=f"call-{suffix}",
        tool_name="bash",
    )
    return execution_id


def test_schema_migration_is_idempotent_and_preserves_v2_tables(tmp_path: Path):
    db_path = tmp_path / "state.db"
    initial = SessionDB(db_path)
    initial.create_session("session-before-v3", "test-model")
    _, run_id = _create_run(initial, "before-v3")
    execution_id = _create_tool_execution(initial, run_id, "before-v3")
    initial._conn.execute("DROP TABLE execution_records")
    initial._conn.execute("DROP TABLE workspace_snapshots")
    initial._conn.execute("PRAGMA user_version=2")
    initial.close()

    migrated = SessionDB(db_path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        row[0]
        for row in migrated._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"sessions", "agent_runs", "tool_executions"} <= tables
    assert {"workspace_snapshots", "execution_records"} <= tables
    assert any(
        session["id"] == "session-before-v3"
        for session in migrated.list_sessions(limit=20)
    )
    assert migrated.get_agent_run(run_id) is not None
    assert migrated.get_tool_execution(execution_id) is not None
    migrated.close()

    reopened = SessionDB(db_path)
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    reopened.close()


def test_execution_record_requires_its_own_tool_execution_and_tracks_purge(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    snapshot = db.create_workspace_snapshot(
        snapshot_id="snapshot-one",
        run_id=run_id,
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="REPLAYABLE",
        manifest_relpath="run-one/snapshots/snapshot-one/manifest.json",
        base_tree_relpath="run-one/snapshots/snapshot-one/base.tar.gz",
        capture_fingerprint="stable",
    )
    assert snapshot["capture_status"] == "REPLAYABLE"

    record = db.create_execution_record(
        record_id="record-one",
        run_id=run_id,
        tool_execution_id=tool_execution_id,
        tool_name="bash",
        command_preview="pytest -q",
        snapshot_id="snapshot-one",
        command_relpath="run-one/executions/record-one/command.json",
        working_directory_rel=".",
        environment_relpath="run-one/executions/record-one/environment.json",
        stdout_relpath="run-one/executions/record-one/stdout.log",
        stderr_relpath="run-one/executions/record-one/stderr.log",
    )
    assert record["artifact_status"] == "INCOMPLETE"

    complete = db.finish_execution_record(
        record_id="record-one",
        log_status="COMPLETE",
        reproducibility_status="REPLAYABLE",
        artifact_status="AVAILABLE",
        exit_code=0,
        termination_reason="exited",
    )
    assert complete["reproducibility_status"] == "REPLAYABLE"
    assert db.purge_snapshot_references("snapshot-one") == 1
    purged = db.get_execution_record("record-one")
    assert purged["artifact_status"] == "PURGED"
    assert purged["reproducibility_status"] == "REPLAYABLE"
    db.close()


def test_replayable_record_rejects_incomplete_evidence(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    db.create_workspace_snapshot(
        snapshot_id="snapshot-two",
        run_id=run_id,
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="PARTIAL",
    )
    db.create_execution_record(
        record_id="record-two",
        run_id=run_id,
        tool_execution_id=tool_execution_id,
        tool_name="bash",
        snapshot_id="snapshot-two",
    )
    with pytest.raises(ValueError, match="REPLAYABLE"):
        db.finish_execution_record(
            record_id="record-two",
            log_status="COMPLETE",
            reproducibility_status="REPLAYABLE",
        )
    db.close()


def test_execution_record_cannot_attach_a_snapshot_already_marked_purged(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db, "purged-snapshot")
    first_execution = _create_tool_execution(db, run_id, "purged-snapshot-one")
    second_execution = _create_tool_execution(db, run_id, "purged-snapshot-two")
    db.create_workspace_snapshot(
        snapshot_id="snapshot-purged",
        run_id=run_id,
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="PARTIAL",
    )
    db.create_execution_record(
        record_id="record-purged-source",
        run_id=run_id,
        tool_execution_id=first_execution,
        tool_name="bash",
        snapshot_id="snapshot-purged",
    )
    db.purge_snapshot_references("snapshot-purged")
    with pytest.raises(RuntimeError, match="purged snapshot"):
        db.create_execution_record(
            record_id="record-purged-target",
            run_id=run_id,
            tool_execution_id=second_execution,
            tool_name="bash",
            snapshot_id="snapshot-purged",
        )
    db.close()


@pytest.mark.parametrize("relative_path", ["../outside", "/absolute", "C:/drive", "a\\b", "a/../../b"])
def test_artifact_store_rejects_path_escapes(tmp_path: Path, relative_path: str):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactPathError):
        store.write_text_atomic(relative_path, "blocked")


def test_artifact_store_uses_atomic_final_path_and_removes_only_its_temp_files(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    relative_path = store.execution_relpath("run-one", "record-one", "result.json")
    target = store.write_json_atomic(relative_path, {"status": "ok"})
    assert target.is_file()
    assert '"status": "ok"' in target.read_text(encoding="utf-8")

    stale = target.parent / ".minihermes-tmp-stale"
    stale.write_text("partial", encoding="utf-8")
    os.utime(stale, (1, 1))
    unrelated = target.parent / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    assert store.cleanup_stale_temporary_files(older_than_seconds=0) == 1
    assert not stale.exists()
    assert unrelated.exists()
    assert target.exists()


def test_artifact_store_rejects_project_and_git_metadata_roots(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ArtifactPathError):
        ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactPathError):
        ArtifactStore(tmp_path.parent / ".git")


def test_artifact_store_rejects_nested_git_and_managed_worktree_roots(tmp_path: Path, monkeypatch):
    import agent.reproducibility as reproducibility

    repo = tmp_path / "other-repository"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ArtifactPathError):
        ArtifactStore(repo / "artifacts")
    with pytest.raises(ArtifactPathError):
        ArtifactStore(repo / ".git" / "artifacts")

    monkeypatch.setattr(reproducibility, "MINIHERMES_HOME", tmp_path / "minihermes-home")
    with pytest.raises(ArtifactPathError):
        ArtifactStore(tmp_path / "minihermes-home" / "worktrees" / "candidate")


def test_artifact_store_rejects_symlinked_parent_escape(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_parent = store.root / "run-one"
    try:
        escaped_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(ArtifactPathError):
        store.write_text_atomic("run-one/result.json", "blocked")


def test_artifact_store_refuses_cleanup_after_root_is_replaced_by_a_symlink(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write_text_atomic(store.run_manifest_relpath("run-one"), "{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_root = store.root
    moved_root = tmp_path / "moved-v1"
    original_root.rename(moved_root)
    try:
        original_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(ArtifactPathError):
        store.remove_run("run-one")
    with pytest.raises(ArtifactPathError):
        store.cleanup_stale_temporary_files(older_than_seconds=0)


def test_artifact_store_refuses_cleanup_through_a_windows_junction(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = store.root / "run-one"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run_dir), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"NTFS junctions are unavailable: {result.stderr or result.stdout}")

    sentinel = outside / ".minihermes-tmp-sentinel"
    sentinel.write_text("must remain", encoding="utf-8")
    with pytest.raises(ArtifactPathError):
        store.remove_run("run-one")
    assert store.cleanup_stale_temporary_files(older_than_seconds=0) == 0
    assert sentinel.exists()


def test_artifact_store_rejects_a_junctioned_artifact_root(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "artifact-link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"NTFS junctions are unavailable: {result.stderr or result.stdout}")

    with pytest.raises(ArtifactPathError, match="symlink or junction"):
        ArtifactStore(junction / "artifacts")


def test_retention_purge_removes_managed_snapshot_and_marks_references(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    store = ArtifactStore(tmp_path / "artifacts")
    manifest_relpath = store.snapshot_relpath(run_id, "snapshot-three", "manifest.json")
    store.write_json_atomic(manifest_relpath, {"snapshot": "three"})
    db.create_workspace_snapshot(
        snapshot_id="snapshot-three",
        run_id=run_id,
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="PARTIAL",
        manifest_relpath=manifest_relpath,
    )
    db.create_execution_record(
        record_id="record-three",
        run_id=run_id,
        tool_execution_id=tool_execution_id,
        tool_name="bash",
        snapshot_id="snapshot-three",
    )

    retention = ArtifactRetentionManager(store, db)
    assert retention.purge_snapshot("snapshot-three") == 1
    assert not (store.root / run_id / "snapshots" / "snapshot-three").exists()
    assert db.get_execution_record("record-three")["artifact_status"] == "PURGED"
    db.close()


def test_unknown_or_cross_run_tool_execution_is_rejected(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, first_run = _create_run(db, "one")
    _, second_run = _create_run(db, "two")
    execution_id = _create_tool_execution(db, first_run)
    with pytest.raises(ValueError, match="does not belong"):
        db.create_execution_record(
            record_id="record-cross-run",
            run_id=second_run,
            tool_execution_id=execution_id,
            tool_name="bash",
        )
    with pytest.raises(KeyError, match="unknown tool"):
        db.create_execution_record(
            record_id="record-unknown-tool",
            run_id=first_run,
            tool_execution_id="missing-tool",
            tool_name="bash",
        )
    db.close()


def test_artifact_paths_must_belong_to_their_snapshot_or_execution_bundle(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    with pytest.raises(ValueError, match="does not belong"):
        db.create_workspace_snapshot(
            snapshot_id="snapshot-owned",
            run_id=run_id,
            workspace_root="C:/project",
            git_root="C:/project",
            manifest_relpath="other-run/snapshots/snapshot-owned/manifest.json",
        )
    with pytest.raises(ValueError, match="does not belong"):
        db.create_execution_record(
            record_id="record-owned",
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            tool_name="bash",
            command_relpath="other-run/executions/record-owned/command.json",
        )
    db.close()


def test_incomplete_execution_survives_restart_without_being_marked_complete(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    db.create_execution_record(
        record_id="record-interrupted",
        run_id=run_id,
        tool_execution_id=tool_execution_id,
        tool_name="bash",
    )
    db.close()

    reopened = SessionDB(db_path)
    record = reopened.get_execution_record("record-interrupted")
    assert record["artifact_status"] == "INCOMPLETE"
    assert record["log_status"] == "UNAVAILABLE"
    assert record["reproducibility_status"] == "UNAVAILABLE"
    assert record["finished_at"] is None
    reopened.close()


def test_execution_record_rejects_mismatched_or_unsupported_tool(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    execution_id = _create_tool_execution(db, run_id)
    with pytest.raises(ValueError, match="matching bash"):
        db.create_execution_record(
            record_id="record-mismatched-tool",
            run_id=run_id,
            tool_execution_id=execution_id,
            tool_name="read_file",
        )
    db.close()


def test_retention_purge_reports_delete_failure_but_keeps_records_unavailable(tmp_path: Path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _, run_id = _create_run(db)
    tool_execution_id = _create_tool_execution(db, run_id)
    store = ArtifactStore(tmp_path / "artifacts")
    db.create_workspace_snapshot(
        snapshot_id="snapshot-delete-failure",
        run_id=run_id,
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="PARTIAL",
    )
    db.create_execution_record(
        record_id="record-delete-failure",
        run_id=run_id,
        tool_execution_id=tool_execution_id,
        tool_name="bash",
        snapshot_id="snapshot-delete-failure",
    )
    retention = ArtifactRetentionManager(store, db)
    monkeypatch.setattr(
        store,
        "remove_snapshot_bundle",
        lambda *_args: (_ for _ in ()).throw(OSError("disk is busy")),
    )

    with pytest.raises(ArtifactCleanupError, match="marked PURGED"):
        retention.purge_snapshot("snapshot-delete-failure")
    assert db.get_execution_record("record-delete-failure")["artifact_status"] == "PURGED"
    db.close()
