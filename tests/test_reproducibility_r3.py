"""R3 制品保留与 CLI 可观察性测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from agent.reproducibility import (
    ArtifactRetentionManager,
    ArtifactStore,
    ExecutionEvidenceRecorder,
)
from agent.runtime import AgentRuntimeManager
from cli.commands import handle_slash_command
from session import SessionDB
from session.db import SCHEMA_VERSION


def _create_run(db: SessionDB, suffix: str, *, parent_run_id: str | None = None) -> tuple[str, str]:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id="retention-test",
        session_id=None,
        parent_task_id=None,
        kind="delegate",
        title=suffix,
        request_preview=suffix,
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=parent_run_id,
        conversation_id="retention-test",
        start_session_id=None,
        agent_kind="delegate",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task_id)
    return task_id, run_id


def _finish_run(db: SessionDB, task_id: str, run_id: str, *, status: str = "SUCCEEDED") -> None:
    db.finish_agent_run(
        run_id=run_id,
        task_id=task_id,
        status=status,
        completion_reason="test_complete",
        end_session_id=None,
    )


def _create_record(
    db: SessionDB,
    store: ArtifactStore,
    *,
    suffix: str,
    run_id: str,
    snapshot_id: str | None,
    snapshot_run_id: str | None,
    tool_status: str = "SUCCEEDED",
    exit_code: int = 0,
    finish: bool = True,
) -> str:
    execution_id = f"tool-{suffix}"
    record_id = f"record-{suffix}"
    db.create_tool_execution(
        execution_id=execution_id,
        run_id=run_id,
        tool_call_id=f"call-{suffix}",
        tool_name="bash",
    )
    if snapshot_id is not None and snapshot_run_id is not None:
        snapshot_manifest = store.snapshot_relpath(snapshot_run_id, snapshot_id, "manifest.json")
        snapshot_path = store.root / snapshot_run_id / "snapshots" / snapshot_id / "manifest.json"
        if not snapshot_path.exists():
            store.write_json_atomic(snapshot_manifest, {"snapshot": snapshot_id})
        if db.get_workspace_snapshot(snapshot_id) is None:
            db.create_workspace_snapshot(
                snapshot_id=snapshot_id,
                run_id=snapshot_run_id,
                workspace_root="C:/retention-workspace",
                git_root="C:/retention-workspace",
                capture_status="PARTIAL",
                manifest_relpath=snapshot_manifest,
            )
    command_relpath = store.execution_relpath(run_id, record_id, "command.json")
    environment_relpath = store.execution_relpath(run_id, record_id, "environment.json")
    stdout_relpath = store.execution_relpath(run_id, record_id, "stdout.log")
    stderr_relpath = store.execution_relpath(run_id, record_id, "stderr.log")
    store.write_json_atomic(command_relpath, {"command": "echo retention"})
    store.write_json_atomic(environment_relpath, {})
    store.write_text_atomic(stdout_relpath, "ok\n")
    store.write_text_atomic(stderr_relpath, "")
    db.create_execution_record(
        record_id=record_id,
        run_id=run_id,
        tool_execution_id=execution_id,
        tool_name="bash",
        snapshot_id=snapshot_id,
        command_relpath=command_relpath,
        environment_relpath=environment_relpath,
        stdout_relpath=stdout_relpath,
        stderr_relpath=stderr_relpath,
    )
    if finish:
        db.finish_tool_execution(
            execution_id=execution_id,
            status=tool_status,
            attempts=1,
            retryable=False,
            error_code=None if tool_status == "SUCCEEDED" else "test_failed",
            error_message=None,
            output_preview="ok",
        )
        db.finish_execution_record(
            record_id=record_id,
            log_status="COMPLETE",
            reproducibility_status="PARTIAL",
            artifact_status="AVAILABLE",
            exit_code=exit_code,
            termination_reason="exited",
        )
    return record_id


def _age_group(db: SessionDB, snapshot_id: str, record_ids: list[str], seconds: float) -> None:
    then = time.time() - seconds
    db._conn.execute(
        "UPDATE workspace_snapshots SET created_at = ? WHERE snapshot_id = ?",
        (then, snapshot_id),
    )
    for record_id in record_ids:
        db._conn.execute(
            "UPDATE execution_records SET created_at = ?, finished_at = ? WHERE record_id = ?",
            (then, then, record_id),
        )


def test_retention_purges_expired_success_group_and_tracks_current_snapshot_status(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    task_id, run_id = _create_run(db, "success")
    record_id = _create_record(
        db, store, suffix="success", run_id=run_id,
        snapshot_id="snapshot-success", snapshot_run_id=run_id,
    )
    _finish_run(db, task_id, run_id)
    _age_group(db, "snapshot-success", [record_id], seconds=3 * 24 * 60 * 60)

    result = ArtifactRetentionManager(store, db).cleanup(
        retention_days=1,
        keep_failed_days=30,
        max_total_artifact_bytes=1024 * 1024,
    )

    assert result["purged_groups"] == 1
    assert result["purged_records"] == 1
    assert db.get_workspace_snapshot("snapshot-success")["artifact_status"] == "PURGED"
    assert db.get_execution_record(record_id)["artifact_status"] == "PURGED"
    assert not (store.root / run_id / "snapshots" / "snapshot-success").exists()
    assert not (store.root / run_id / "executions" / record_id).exists()
    inspection = ArtifactRetentionManager(store, db).inspect(
        retention_days=1, keep_failed_days=30
    )
    assert inspection["already_purged_groups"] == 1
    db.close()


def test_retention_keeps_failed_record_until_its_separate_protection_window_expires(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    task_id, run_id = _create_run(db, "failure")
    record_id = _create_record(
        db, store, suffix="failure", run_id=run_id,
        snapshot_id="snapshot-failure", snapshot_run_id=run_id,
        tool_status="FAILED", exit_code=7,
    )
    _finish_run(db, task_id, run_id, status="FAILED")
    _age_group(db, "snapshot-failure", [record_id], seconds=3 * 24 * 60 * 60)

    manager = ArtifactRetentionManager(store, db)
    inspection = manager.inspect(retention_days=1, keep_failed_days=30)
    assert inspection["blocked_reasons"] == {"failed_retention": 1}
    result = manager.cleanup(
        retention_days=1,
        keep_failed_days=30,
        max_total_artifact_bytes=1,
    )

    assert result["purged_groups"] == 0
    assert db.get_workspace_snapshot("snapshot-failure")["artifact_status"] == "AVAILABLE"
    assert db.get_execution_record(record_id)["artifact_status"] == "AVAILABLE"
    assert (store.root / run_id / "executions" / record_id).exists()
    db.close()


def test_retention_never_claims_snapshot_while_a_child_replay_is_running(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    source_task, source_run = _create_run(db, "source")
    source_record = _create_record(
        db, store, suffix="source", run_id=source_run,
        snapshot_id="snapshot-active", snapshot_run_id=source_run,
    )
    _finish_run(db, source_task, source_run)
    _age_group(db, "snapshot-active", [source_record], seconds=3 * 24 * 60 * 60)

    _, replay_run = _create_run(db, "replay", parent_run_id=source_run)
    replay_record = _create_record(
        db, store, suffix="replay", run_id=replay_run,
        snapshot_id="snapshot-active", snapshot_run_id=source_run,
        finish=False,
    )

    manager = ArtifactRetentionManager(store, db)
    inspection = manager.inspect(retention_days=1, keep_failed_days=30)
    assert inspection["blocked_reasons"] == {"active_run": 1}
    result = manager.cleanup(
        retention_days=1,
        keep_failed_days=30,
        max_total_artifact_bytes=1,
    )

    assert result["purged_groups"] == 0
    assert db.get_execution_record(source_record)["artifact_status"] == "AVAILABLE"
    assert db.get_execution_record(replay_record)["artifact_status"] == "INCOMPLETE"
    assert (store.root / source_run / "snapshots" / "snapshot-active").exists()
    db.close()


def test_capacity_pressure_only_purges_finished_success_groups(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    success_task, success_run = _create_run(db, "capacity-success")
    success_record = _create_record(
        db, store, suffix="capacity-success", run_id=success_run,
        snapshot_id="snapshot-capacity-success", snapshot_run_id=success_run,
    )
    _finish_run(db, success_task, success_run)

    failed_task, failed_run = _create_run(db, "capacity-failed")
    failed_record = _create_record(
        db, store, suffix="capacity-failed", run_id=failed_run,
        snapshot_id="snapshot-capacity-failed", snapshot_run_id=failed_run,
        tool_status="FAILED", exit_code=3,
    )
    _finish_run(db, failed_task, failed_run, status="FAILED")
    _age_group(db, "snapshot-capacity-success", [success_record], seconds=60)
    _age_group(db, "snapshot-capacity-failed", [failed_record], seconds=120)

    result = ArtifactRetentionManager(store, db).cleanup(
        retention_days=365,
        keep_failed_days=30,
        max_total_artifact_bytes=1,
    )

    assert result["purged_groups"] == 1
    assert db.get_execution_record(success_record)["artifact_status"] == "PURGED"
    assert db.get_workspace_snapshot("snapshot-capacity-success")["artifact_status"] == "PURGED"
    assert db.get_execution_record(failed_record)["artifact_status"] == "AVAILABLE"
    assert db.get_workspace_snapshot("snapshot-capacity-failed")["artifact_status"] == "AVAILABLE"
    db.close()


def test_cleanup_removes_only_old_unowned_bundles_after_a_grace_period(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    store = ArtifactStore(tmp_path / "artifacts")
    old_orphan = store.root / "run-orphan" / "executions" / "record-orphan"
    fresh_orphan = store.root / "run-fresh" / "snapshots" / "snapshot-fresh"
    old_orphan.mkdir(parents=True)
    fresh_orphan.mkdir(parents=True)
    (old_orphan / "partial.log").write_text("old", encoding="utf-8")
    (fresh_orphan / "partial.log").write_text("fresh", encoding="utf-8")
    then = time.time() - 7200
    os.utime(old_orphan, (then, then))

    result = ArtifactRetentionManager(store, db).cleanup(
        retention_days=30,
        keep_failed_days=30,
        max_total_artifact_bytes=1024 * 1024,
    )

    assert result["orphan_bundles"] == 1
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    db.close()


def test_v6_database_migrates_snapshot_artifact_status_to_current_schema(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db._conn.execute("DROP INDEX IF EXISTS idx_workspace_snapshots_artifact")
    db._conn.execute("ALTER TABLE workspace_snapshots DROP COLUMN artifact_status")
    db._conn.execute("PRAGMA user_version=6")
    db.close()

    migrated = SessionDB(db_path)
    columns = {
        row[1] for row in migrated._conn.execute(
            "PRAGMA table_info(workspace_snapshots)"
        ).fetchall()
    }
    assert "artifact_status" in columns
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    migrated.close()


def test_artifact_cli_exposes_retention_and_explicit_cleanup(tmp_path: Path, capsys):
    db = SessionDB(tmp_path / "state.db")
    evidence = ExecutionEvidenceRecorder(ArtifactStore(tmp_path / "artifacts"), db)
    runtime = AgentRuntimeManager(
        db,
        evidence_recorder=evidence,
        runtime_config={"max_concurrency": 1},
        replay_root=tmp_path / "replays",
    )
    try:
        for command in ("/artifacts retention", "/artifacts cleanup"):
            handled, _, _, _ = handle_slash_command(
                command, [], db, "session", runtime=runtime
            )
            assert handled
        output = capsys.readouterr().out
        assert "[artifact retention:" in output
        assert "[artifact cleanup:" in output
    finally:
        runtime.shutdown()
        db.close()
