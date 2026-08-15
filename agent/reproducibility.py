"""可复现执行的制品基础设施。

本模块不执行 Shell、不创建快照，也不调用模型。它只负责 R0 所需的
受控制品目录、相对路径校验、原子写入和安全清理，供后续证据记录器复用。
"""

from __future__ import annotations

import json
import gzip
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from config import MINIHERMES_HOME


ARTIFACT_FORMAT_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TEMPORARY_PREFIX = ".minihermes-tmp-"
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"),
)
_TRUNCATION_MARKER = "\n\n... [EVIDENCE LOG TRUNCATED] ...\n\n"
_SNAPSHOT_MANIFEST_VERSION = 1
_SNAPSHOT_GIT_TIMEOUT_SECONDS = 30
_MAX_SNAPSHOT_FILES = 10_000
_SENSITIVE_PATH_PARTS = frozenset({
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "image_tmp",
})
_SENSITIVE_FILENAMES = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)|.*\.(?:pem|p12|pfx|key)|"
    r"(?:credentials?|secrets?)(?:\..*)?)$"
)
_BINARY_SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        rb"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*['\"]?"
        rb"[A-Za-z0-9_-]{12,}"
    ),
)


class ArtifactPathError(ValueError):
    """制品路径不在系统管理根目录内，或可能通过链接逃逸。"""


class ArtifactCleanupError(RuntimeError):
    """制品已被标记不可用，但磁盘清理尚未完成，可安全重试。"""

    def __init__(self, snapshot_id: str, references_marked_purged: int, cause: Exception):
        self.snapshot_id = snapshot_id
        self.references_marked_purged = references_marked_purged
        self.cause = cause
        super().__init__(
            f"snapshot {snapshot_id} was marked PURGED but its artifact bundle "
            f"could not be removed: {cause}"
        )


class SnapshotCaptureError(RuntimeError):
    """Git 工作区不能安全封存时的受控错误。"""

    def __init__(self, reason_code: str, message: str = ""):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class ReplaySetupError(RuntimeError):
    """快照制品不能安全材料化，重放不得启动 Shell。"""

    def __init__(self, reason_code: str, message: str = ""):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class WorkspaceSnapshotCapture:
    """一次命令启动前的工作区封存结果。"""

    snapshot_id: str | None
    git_root: Path | None
    working_directory_rel: str | None
    capture_status: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ReplaySource:
    """已经校验完整性、可用于材料化的历史 bash 记录。"""

    record: dict[str, Any]
    snapshot: dict[str, Any]
    command: str
    working_directory_rel: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ReplayMaterialization:
    """隔离重放目录及其实际工作目录。"""

    replay_root: Path
    workspace_root: Path
    working_directory: Path
    source: ReplaySource


@dataclass
class BashEvidenceCapture:
    """一次 bash 执行的私有记录句柄，不暴露给模型或工具 schema。"""

    recorder: "ExecutionEvidenceRecorder"
    record_id: str
    run_id: str
    failure_reporter: object | None = None
    command_redacted: bool = False
    completed: bool = False

    def complete(
        self,
        *,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        termination_reason: str,
    ) -> None:
        if self.completed:
            return
        self.recorder._finish_bash_capture(
            self,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            termination_reason=termination_reason,
        )
        self.completed = True

    def mark_unavailable(self, reason: str) -> None:
        if self.completed:
            return
        self.recorder._mark_capture_unavailable(self, reason)
        self.completed = True

    def finish_without_execution(self, *, replay_status: str, reason: str) -> None:
        """审批或材料化失败时收尾预登记记录，明确 Shell 从未启动。"""
        if self.completed:
            return
        self.recorder._finish_preexecution_capture(
            self, replay_status=replay_status, reason=reason
        )
        self.completed = True

    def report_failure(self, stage: str, error: Exception) -> None:
        if callable(self.failure_reporter):
            self.failure_reporter(stage, error)


class ExecutionEvidenceRecorder:
    """bash 证据旁路：快照失败只降级记录，不能改变命令本身的结果。"""

    def __init__(
        self,
        store: "ArtifactStore",
        session_db,
        *,
        max_log_bytes_per_stream: int = 20 * 1024 * 1024,
        max_snapshot_bytes: int = 200 * 1024 * 1024,
        known_secrets: tuple[str, ...] | list[str] = (),
    ):
        self.store = store
        self.db = session_db
        try:
            limit = int(max_log_bytes_per_stream)
        except (TypeError, ValueError):
            limit = 20 * 1024 * 1024
        self.max_log_bytes_per_stream = min(max(limit, 1), 100 * 1024 * 1024)
        try:
            snapshot_limit = int(max_snapshot_bytes)
        except (TypeError, ValueError):
            snapshot_limit = 200 * 1024 * 1024
        self.max_snapshot_bytes = min(
            max(snapshot_limit, 1024), 10 * 1024 * 1024 * 1024
        )
        self._known_secrets = tuple(
            sorted(
                {str(value) for value in known_secrets if isinstance(value, str) and len(value) >= 4},
                key=len,
                reverse=True,
            )
        )

    @classmethod
    def from_config(
        cls,
        store: "ArtifactStore",
        session_db,
        reproducibility_config: dict,
        *,
        secrets_config: dict | None = None,
    ) -> "ExecutionEvidenceRecorder":
        """从证据配置取限制，同时从完整配置提取待脱敏的已知密钥。"""
        known_secrets = tuple(_extract_config_secrets(secrets_config or {}))
        return cls(
            store,
            session_db,
            max_log_bytes_per_stream=(reproducibility_config or {}).get(
                "max_log_bytes_per_stream", 20 * 1024 * 1024
            ),
            max_snapshot_bytes=(reproducibility_config or {}).get(
                "max_snapshot_bytes", 200 * 1024 * 1024
            ),
            known_secrets=known_secrets,
        )

    def start_bash(
        self,
        *,
        run_id: str,
        tool_execution_id: str,
        command: str,
        working_directory: str | Path,
        node_run_id: str | None = None,
        workspace_id: str | None = None,
        failure_reporter=None,
        replayed_from_record_id: str | None = None,
    ) -> BashEvidenceCapture:
        cwd = Path(working_directory).resolve(strict=False)
        snapshot_capture = WorkspaceSnapshotter(
            self.store,
            self.db,
            max_snapshot_bytes=self.max_snapshot_bytes,
            known_secrets=self._known_secrets,
        ).capture(run_id=run_id, working_directory=cwd)
        return self._create_bash_capture(
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            command=command,
            working_directory=cwd,
            snapshot_capture=snapshot_capture,
            node_run_id=node_run_id,
            workspace_id=workspace_id,
            replayed_from_record_id=replayed_from_record_id,
            failure_reporter=failure_reporter,
        )

    def _finish_bash_capture(
        self,
        capture: BashEvidenceCapture,
        *,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        termination_reason: str,
    ) -> None:
        stdout_text, stdout_truncated, stdout_redacted = self._prepare_log(stdout)
        stderr_text, stderr_truncated, stderr_redacted = self._prepare_log(stderr)
        stdout_relpath = self.store.execution_relpath(capture.run_id, capture.record_id, "stdout.log")
        stderr_relpath = self.store.execution_relpath(capture.run_id, capture.record_id, "stderr.log")
        result_relpath = self.store.execution_relpath(capture.run_id, capture.record_id, "result.json")
        try:
            self.store.write_text_atomic(stdout_relpath, stdout_text)
            self.store.write_text_atomic(stderr_relpath, stderr_text)
            self.store.write_json_atomic(result_relpath, {
                "exit_code": exit_code,
                "termination_reason": termination_reason,
            })
            record = self.db.get_execution_record(capture.record_id) or {}
            if stdout_truncated or stderr_truncated:
                log_status = "TRUNCATED"
            elif stdout_redacted or stderr_redacted:
                log_status = "REDACTED"
            else:
                log_status = "COMPLETE"
            snapshot = (
                self.db.get_workspace_snapshot(record["snapshot_id"])
                if record.get("snapshot_id") else None
            )
            reproducibility_status = (
                "REPLAYABLE"
                if (
                    log_status == "COMPLETE"
                    and snapshot is not None
                    and snapshot.get("capture_status") == "REPLAYABLE"
                    and record.get("working_directory_rel") is not None
                    and record.get("command_sha256")
                    and not capture.command_redacted
                )
                else "PARTIAL"
            )
            is_replay = bool(record.get("replayed_from_record_id"))
            replay_status = "NOT_REQUESTED"
            if is_replay:
                replay_status = {
                    "cancelled": "REPLAY_CANCELLED",
                    "timed_out": "REPLAY_TIMED_OUT",
                }.get(
                    termination_reason,
                    "REPLAY_SUCCEEDED"
                    if exit_code == 0 and termination_reason == "exited"
                    else "REPLAY_COMMAND_FAILED",
                )
            self.db.finish_execution_record(
                record_id=capture.record_id,
                log_status=log_status,
                reproducibility_status=reproducibility_status,
                artifact_status="AVAILABLE",
                exit_code=exit_code,
                termination_reason=termination_reason,
                replay_status=replay_status,
            )
        except Exception as exc:
            self._mark_capture_unavailable(capture, f"write_failed:{type(exc).__name__}")
            raise

    def _mark_capture_unavailable(self, capture: BashEvidenceCapture, reason: str) -> None:
        try:
            self.db.finish_execution_record(
                record_id=capture.record_id,
                log_status="UNAVAILABLE",
                reproducibility_status="UNAVAILABLE",
                artifact_status="INCOMPLETE",
                termination_reason=self._sanitize(reason)[:200],
            )
        except Exception:
            # 记录器决不能反向掩盖 bash 的真实错误；调用方只接收最初的采集异常。
            pass

    def _finish_preexecution_capture(
        self,
        capture: BashEvidenceCapture,
        *,
        replay_status: str,
        reason: str,
    ) -> None:
        """不启动命令也要保留可查询的重放审计记录。"""
        try:
            self.db.finish_execution_record(
                record_id=capture.record_id,
                log_status="UNAVAILABLE",
                reproducibility_status="UNAVAILABLE",
                artifact_status="INCOMPLETE",
                termination_reason=self._sanitize(reason)[:200],
                replay_status=replay_status,
            )
        except Exception:
            pass

    def _prepare_log(self, value: str) -> tuple[str, bool, bool]:
        sanitized = self._sanitize(value)
        text, truncated = _truncate_text_bytes(
            sanitized, self.max_log_bytes_per_stream
        )
        return text, truncated, text != str(value or "")

    def _sanitize(self, value: str) -> str:
        text = str(value or "").replace("\x00", "")
        for secret in self._known_secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text

    def sanitize_preview(self, value: str, limit: int = 500) -> str:
        """供审计预览复用证据制品的同一套已知密钥脱敏规则。"""
        return self._sanitize(value)[:limit]

    def _environment_summary(
        self,
        working_directory: Path,
        *,
        working_directory_rel: str | None = None,
        git_root: Path | None = None,
    ) -> dict[str, object]:
        return {
            "working_directory": self._sanitize(str(working_directory)),
            "working_directory_rel": working_directory_rel,
            "git_root": self._sanitize(str(git_root)) if git_root else None,
            "platform": os.name,
            "python_version": os.sys.version.split()[0],
            "shell": "cmd.exe" if os.name == "nt" else "/bin/sh",
            "environment_variable_names": sorted(
                name for name in os.environ if not _looks_sensitive_name(name)
            ),
            "environment_variable_count": len(os.environ),
        }

    def prepare_replay_bash(
        self,
        *,
        run_id: str,
        tool_execution_id: str,
        command: str,
        working_directory: str | Path,
        snapshot_id: str,
        working_directory_rel: str,
        replayed_from_record_id: str,
        failure_reporter=None,
    ) -> BashEvidenceCapture:
        """为非 LLM 重放预先建立证据记录，审批拒绝也能留下审计链。"""
        snapshot = self.db.get_workspace_snapshot(snapshot_id)
        if (
            not snapshot
            or snapshot.get("capture_status") != "REPLAYABLE"
            or snapshot.get("artifact_status") != "AVAILABLE"
        ):
            raise ReplaySetupError("source_snapshot_unavailable")
        capture = WorkspaceSnapshotCapture(
            snapshot_id=snapshot_id,
            git_root=Path(snapshot["git_root"]),
            working_directory_rel=working_directory_rel,
            capture_status="REPLAYABLE",
        )
        return self._create_bash_capture(
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            command=command,
            working_directory=Path(working_directory).resolve(strict=False),
            snapshot_capture=capture,
            node_run_id=None,
            replayed_from_record_id=replayed_from_record_id,
            failure_reporter=failure_reporter,
        )

    def _create_bash_capture(
        self,
        *,
        run_id: str,
        tool_execution_id: str,
        command: str,
        working_directory: Path,
        snapshot_capture: WorkspaceSnapshotCapture,
        node_run_id: str | None,
        workspace_id: str | None = None,
        replayed_from_record_id: str | None,
        failure_reporter,
    ) -> BashEvidenceCapture:
        """登记命令证据；快照器和重放共用同一条不可变记录路径。"""
        record_id = uuid.uuid4().hex
        command_relpath = self.store.execution_relpath(run_id, record_id, "command.json")
        environment_relpath = self.store.execution_relpath(run_id, record_id, "environment.json")
        stdout_relpath = self.store.execution_relpath(run_id, record_id, "stdout.log")
        stderr_relpath = self.store.execution_relpath(run_id, record_id, "stderr.log")
        capture = BashEvidenceCapture(self, record_id, run_id, failure_reporter=failure_reporter)
        sanitized_command = self._sanitize(command)
        verification_working_directory = (
            {
                "kind": "relative",
                "value": snapshot_capture.working_directory_rel,
            }
            if snapshot_capture.working_directory_rel is not None
            else {
                "kind": "absolute",
                "value": self._sanitize(str(working_directory)),
            }
        )
        self.db.create_execution_record(
            record_id=record_id,
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            tool_name="bash",
            workspace_id=workspace_id,
            command_preview=sanitized_command[:500],
            command_sha256=_sha256(_canonical_json_bytes({
                "command": sanitized_command,
                "working_directory_rel": snapshot_capture.working_directory_rel,
                "snapshot_id": snapshot_capture.snapshot_id,
            })),
            verification_key=_sha256(_canonical_json_bytes({
                "command": sanitized_command,
                "working_directory": verification_working_directory,
            })),
            snapshot_id=snapshot_capture.snapshot_id,
            command_relpath=command_relpath,
            working_directory_rel=snapshot_capture.working_directory_rel,
            environment_relpath=environment_relpath,
            stdout_relpath=stdout_relpath,
            stderr_relpath=stderr_relpath,
            node_run_id=node_run_id,
            replayed_from_record_id=replayed_from_record_id,
        )
        try:
            capture.command_redacted = sanitized_command != str(command or "")
            self.store.write_json_atomic(command_relpath, {
                "command": sanitized_command,
                "working_directory": self._sanitize(str(working_directory)),
                "working_directory_rel": snapshot_capture.working_directory_rel,
                "snapshot_id": snapshot_capture.snapshot_id,
            })
            self.store.write_json_atomic(
                environment_relpath,
                self._environment_summary(
                    working_directory,
                    working_directory_rel=snapshot_capture.working_directory_rel,
                    git_root=snapshot_capture.git_root,
                ),
            )
        except Exception as exc:
            self._mark_capture_unavailable(capture, f"setup_failed:{type(exc).__name__}")
            capture.completed = True
            raise
        return capture


class WorkspaceSnapshotter:
    """把普通 Git 工作区封存在制品库中，不写入用户仓库。"""

    def __init__(
        self,
        store: ArtifactStore,
        session_db,
        *,
        max_snapshot_bytes: int,
        known_secrets: tuple[str, ...] | list[str] = (),
    ):
        self.store = store
        self.db = session_db
        self.max_snapshot_bytes = max(1024, int(max_snapshot_bytes))
        self._known_secret_bytes = tuple(
            secret.encode("utf-8")
            for secret in known_secrets
            if isinstance(secret, str) and secret
        )

    def capture(self, *, run_id: str, working_directory: Path) -> WorkspaceSnapshotCapture:
        """稳定封存一次命令输入；不支持的工作区只返回降级状态。"""
        try:
            return self._capture(run_id=run_id, working_directory=working_directory)
        except SnapshotCaptureError as exc:
            return WorkspaceSnapshotCapture(
                None, None, None, "UNAVAILABLE", exc.reason_code
            )
        except ReplaySetupError as exc:
            return WorkspaceSnapshotCapture(
                None, None, None, "UNAVAILABLE", exc.reason_code
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return WorkspaceSnapshotCapture(
                None, None, None, "UNAVAILABLE", f"snapshot_error:{type(exc).__name__}"
            )

    def _capture(self, *, run_id: str, working_directory: Path) -> WorkspaceSnapshotCapture:
        git_root = self._find_git_root(working_directory)
        if _has_link_component(git_root):
            raise SnapshotCaptureError("linked_workspace")
        relative_cwd = _safe_relative_directory(working_directory, git_root)
        self._reject_unsupported_repository(git_root)

        for attempt in range(2):
            before = self._fingerprint(git_root)
            reusable = self._find_reusable(git_root, before)
            if reusable:
                if before["fingerprint"] == self._fingerprint(git_root)["fingerprint"]:
                    return WorkspaceSnapshotCapture(
                        reusable["snapshot_id"], git_root, relative_cwd, "REPLAYABLE"
                    )
                if attempt == 0:
                    continue
                raise SnapshotCaptureError("workspace_changed_during_capture")
            base_commit = before["base_commit"]
            base_tar = self._git_bytes(git_root, "archive", "--format=tar", base_commit)
            if len(base_tar) > self.max_snapshot_bytes:
                raise SnapshotCaptureError("snapshot_too_large")
            self._validate_tar_bytes(base_tar, label="base")
            self._reject_sensitive_tar_content(base_tar)
            patch = self._git_bytes(git_root, "diff", "--binary", base_commit)
            if len(patch) > self.max_snapshot_bytes:
                raise SnapshotCaptureError("snapshot_too_large")
            self._reject_sensitive_bytes(patch, "sensitive_tracked_diff")
            untracked = self._build_untracked_archive(git_root, before["untracked_paths"])
            base_gzip = _gzip_bytes(base_tar)
            total_size = len(base_gzip) + len(patch) + len(untracked)
            if total_size > self.max_snapshot_bytes:
                raise SnapshotCaptureError("snapshot_too_large")
            after = self._fingerprint(git_root)
            if before["fingerprint"] != after["fingerprint"]:
                if attempt == 0:
                    continue
                raise SnapshotCaptureError("workspace_changed_during_capture")
            return self._persist_snapshot(
                run_id=run_id,
                git_root=git_root,
                base_commit=base_commit,
                fingerprint=before["fingerprint"],
                base_gzip=base_gzip,
                patch=patch,
                untracked=untracked,
                working_directory_rel=relative_cwd,
            )
        raise SnapshotCaptureError("workspace_changed_during_capture")

    def _find_git_root(self, working_directory: Path) -> Path:
        if not working_directory.is_dir():
            raise SnapshotCaptureError("working_directory_unavailable")
        output = self._git_text(working_directory, "rev-parse", "--show-toplevel")
        root = Path(output.strip()).resolve(strict=False)
        if not root.is_dir() or _is_within(root, root / ".git"):
            raise SnapshotCaptureError("not_a_git_worktree")
        return root

    def _reject_unsupported_repository(self, git_root: Path) -> None:
        if not (git_root / ".git").is_dir():
            raise SnapshotCaptureError("linked_git_worktree_unsupported")
        # 160000 表示子模块；这些内容不在普通 archive 的可重放契约内。
        index = self._git_bytes(git_root, "ls-files", "--stage", "-z")
        for entry in index.split(b"\0"):
            if entry.startswith(b"160000 "):
                raise SnapshotCaptureError("submodule_unsupported")
            if entry.startswith(b"120000 "):
                raise SnapshotCaptureError("symlink_unsupported")
        attributes = git_root / ".gitattributes"
        if attributes.is_file() and b"filter=lfs" in attributes.read_bytes().lower():
            raise SnapshotCaptureError("git_lfs_unsupported")

    def _fingerprint(self, git_root: Path) -> dict[str, Any]:
        base_commit = self._git_text(git_root, "rev-parse", "HEAD").strip()
        status = self._git_bytes(git_root, "status", "--porcelain=v1", "-z")
        patch = self._git_bytes(git_root, "diff", "--binary", base_commit)
        changed_paths = _parse_nul_paths(
            self._git_bytes(git_root, "diff", "--name-only", "-z", base_commit)
        )
        for changed_path in changed_paths:
            self._reject_sensitive_path(changed_path)
        if b" mode 120000" in patch:
            raise SnapshotCaptureError("symlink_unsupported")
        untracked_raw = self._git_bytes(
            git_root, "ls-files", "--others", "--exclude-standard", "-z"
        )
        untracked_paths = _parse_nul_paths(untracked_raw)
        untracked_content_hash = self._untracked_content_hash(
            git_root, untracked_paths
        )
        payload = {
            "base_commit": base_commit,
            "status_sha256": _sha256(status),
            "patch_sha256": _sha256(patch),
            "untracked_sha256": untracked_content_hash,
        }
        return {
            **payload,
            "fingerprint": _sha256(_canonical_json_bytes(payload)),
            "untracked_paths": untracked_paths,
        }

    def _untracked_content_hash(self, git_root: Path, paths: list[str]) -> str:
        digest = hashlib.sha256()
        total = 0
        count = 0
        for relative in paths:
            for path, archive_name in _iter_safe_untracked_files(git_root, relative):
                count += 1
                if count > _MAX_SNAPSHOT_FILES:
                    raise SnapshotCaptureError("snapshot_too_large")
                payload = path.read_bytes()
                total += len(payload)
                if total > self.max_snapshot_bytes:
                    raise SnapshotCaptureError("snapshot_too_large")
                digest.update(archive_name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(payload).digest())
        return digest.hexdigest()

    def _find_reusable(self, git_root: Path, fingerprint: dict[str, Any]) -> dict | None:
        finder = getattr(self.db, "find_replayable_workspace_snapshot", None)
        if not callable(finder):
            return None
        snapshot = finder(str(git_root), fingerprint["fingerprint"])
        if not snapshot:
            return None
        try:
            _load_verified_snapshot_manifest(self.store, snapshot)
        except ReplaySetupError:
            return None
        return snapshot

    def _persist_snapshot(
        self,
        *,
        run_id: str,
        git_root: Path,
        base_commit: str,
        fingerprint: str,
        base_gzip: bytes,
        patch: bytes,
        untracked: bytes,
        working_directory_rel: str,
    ) -> WorkspaceSnapshotCapture:
        snapshot_id = uuid.uuid4().hex
        base_relpath = self.store.snapshot_relpath(run_id, snapshot_id, "base.tar.gz")
        patch_relpath = self.store.snapshot_relpath(run_id, snapshot_id, "tracked.patch")
        untracked_relpath = self.store.snapshot_relpath(run_id, snapshot_id, "untracked.tar.gz")
        manifest_relpath = self.store.snapshot_relpath(run_id, snapshot_id, "manifest.json")
        artifacts = {
            "base_tree": _artifact_descriptor(base_relpath, base_gzip),
            "tracked_patch": _artifact_descriptor(patch_relpath, patch),
            "untracked": _artifact_descriptor(untracked_relpath, untracked),
        }
        manifest = {
            "snapshot_manifest_version": _SNAPSHOT_MANIFEST_VERSION,
            "snapshot_id": snapshot_id,
            "base_commit": base_commit,
            "git_root": str(git_root),
            "capture_fingerprint": fingerprint,
            "artifacts": artifacts,
        }
        try:
            self.store.write_bytes_atomic(base_relpath, base_gzip)
            self.store.write_bytes_atomic(patch_relpath, patch)
            self.store.write_bytes_atomic(untracked_relpath, untracked)
            self.store.write_json_atomic(manifest_relpath, manifest)
            self.db.create_workspace_snapshot(
                snapshot_id=snapshot_id,
                run_id=run_id,
                workspace_root=str(git_root),
                git_root=str(git_root),
                base_commit=base_commit,
                state_hash=fingerprint,
                capture_status="REPLAYABLE",
                manifest_relpath=manifest_relpath,
                base_tree_relpath=base_relpath,
                patch_relpath=patch_relpath,
                untracked_relpath=untracked_relpath,
                capture_fingerprint=fingerprint,
            )
        except Exception as exc:
            try:
                self.store.remove_snapshot_bundle(run_id, snapshot_id)
            except Exception:
                pass
            raise SnapshotCaptureError("snapshot_persist_failed", str(exc)) from exc
        return WorkspaceSnapshotCapture(
            snapshot_id, git_root, working_directory_rel, "REPLAYABLE"
        )

    def _build_untracked_archive(self, git_root: Path, paths: list[str]) -> bytes:
        buffer = io.BytesIO()
        total = 0
        count = 0
        with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for relative in paths:
                for path, archive_name in _iter_safe_untracked_files(git_root, relative):
                    data = path.read_bytes()
                    count += 1
                    total += len(data)
                    if count > _MAX_SNAPSHOT_FILES or total > self.max_snapshot_bytes:
                        raise SnapshotCaptureError("snapshot_too_large")
                    self._reject_sensitive_path(archive_name)
                    self._reject_sensitive_bytes(data, "sensitive_untracked_content")
                    member = tarfile.TarInfo(archive_name)
                    member.size = len(data)
                    member.mode = 0o644
                    member.mtime = 0
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    archive.addfile(member, io.BytesIO(data))
        return buffer.getvalue()

    def _validate_tar_bytes(self, payload: bytes, *, label: str) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                _validate_tar_members(archive, label=label)
        except (tarfile.TarError, OSError, ValueError) as exc:
            raise SnapshotCaptureError(f"invalid_{label}_archive", str(exc)) from exc

    def _reject_sensitive_tar_content(self, payload: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in _validate_tar_members(archive, label="base"):
                if not member.isfile():
                    continue
                self._reject_sensitive_path(member.name)
                source = archive.extractfile(member)
                self._reject_sensitive_bytes(
                    source.read() if source else b"", "sensitive_base_content"
                )

    def _reject_sensitive_path(self, relative: str) -> None:
        parts = _safe_archive_parts(relative)
        if any(part.lower() in _SENSITIVE_PATH_PARTS for part in parts):
            raise SnapshotCaptureError("sensitive_or_unsupported_path")
        if parts and _SENSITIVE_FILENAMES.fullmatch(parts[-1]):
            raise SnapshotCaptureError("sensitive_path")

    def _reject_sensitive_bytes(self, payload: bytes, reason: str) -> None:
        for secret in self._known_secret_bytes:
            if secret in payload:
                raise SnapshotCaptureError(reason)
        if any(pattern.search(payload) for pattern in _BINARY_SECRET_PATTERNS):
            raise SnapshotCaptureError(reason)

    @staticmethod
    def _git_text(cwd: Path, *args: str) -> str:
        return WorkspaceSnapshotter._git_bytes(cwd, *args).decode("utf-8", errors="strict")

    @staticmethod
    def _git_bytes(cwd: Path, *args: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                check=False,
                timeout=_SNAPSHOT_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotCaptureError("git_unavailable", str(exc)) from exc
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SnapshotCaptureError("git_command_failed", message[:200])
        return completed.stdout


class ReplayMaterializer:
    """只用已封存制品建立一次独立重放目录，不访问原仓库历史。"""

    def __init__(self, store: ArtifactStore, session_db, *, replay_root: str | Path | None = None):
        self.store = store
        self.db = session_db
        self.replay_root = Path(replay_root).expanduser() if replay_root else MINIHERMES_HOME / "replays"

    def load_source(self, record_id: str) -> ReplaySource:
        record = self.db.get_execution_record(record_id)
        if not record:
            raise ReplaySetupError("record_not_found")
        if (
            record.get("reproducibility_status") != "REPLAYABLE"
            or record.get("artifact_status") != "AVAILABLE"
            or not record.get("snapshot_id")
            or not record.get("working_directory_rel")
            or not record.get("command_relpath")
        ):
            raise ReplaySetupError("record_not_replayable")
        snapshot = self.db.get_workspace_snapshot(record["snapshot_id"])
        if (
            not snapshot
            or snapshot.get("capture_status") != "REPLAYABLE"
            or snapshot.get("artifact_status") != "AVAILABLE"
        ):
            raise ReplaySetupError("snapshot_unavailable")
        manifest = _load_verified_snapshot_manifest(self.store, snapshot)
        try:
            command_doc = json.loads(self.store.read_bytes(record["command_relpath"]).decode("utf-8"))
            command = command_doc["command"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplaySetupError("command_artifact_invalid", str(exc)) from exc
        if not isinstance(command, str) or not command:
            raise ReplaySetupError("command_artifact_invalid")
        cwd = _safe_replay_relative_directory(record["working_directory_rel"])
        canonical_command = {
            "command": command,
            "working_directory_rel": command_doc.get("working_directory_rel"),
            "snapshot_id": command_doc.get("snapshot_id"),
        }
        if (
            command_doc.get("working_directory_rel") != cwd
            or command_doc.get("snapshot_id") != snapshot.get("snapshot_id")
            or record.get("command_sha256") != _sha256(_canonical_json_bytes(canonical_command))
        ):
            raise ReplaySetupError("command_artifact_mismatch")
        return ReplaySource(record, snapshot, command, cwd, manifest)

    def materialize(self, source: ReplaySource) -> ReplayMaterialization:
        workspace_parent = self._create_replay_directory(source.record["record_id"])
        workspace = workspace_parent / "workspace"
        workspace.mkdir()
        artifacts = source.manifest["artifacts"]
        try:
            self._extract_archive(artifacts["base_tree"]["path"], workspace, compressed=True)
            self._apply_patch(artifacts["tracked_patch"]["path"], workspace)
            self._extract_archive(artifacts["untracked"]["path"], workspace, compressed=True)
            working_directory = workspace.joinpath(*PurePosixPath(source.working_directory_rel).parts)
            if not working_directory.is_dir() or not _is_within(working_directory.resolve(), workspace.resolve()):
                raise ReplaySetupError("replay_working_directory_missing")
            _write_json_atomic(
                workspace_parent / "replay.json",
                {
                    "source_record_id": source.record["record_id"],
                    "source_snapshot_id": source.snapshot["snapshot_id"],
                    "working_directory_rel": source.working_directory_rel,
                },
            )
        except ReplaySetupError:
            raise
        except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
            raise ReplaySetupError("replay_materialization_failed", str(exc)) from exc
        return ReplayMaterialization(workspace_parent, workspace, working_directory, source)

    def _create_replay_directory(self, record_id: str) -> Path:
        root = self.replay_root.resolve(strict=False)
        if _has_link_component(root):
            raise ReplaySetupError("replay_root_linked")
        root.mkdir(parents=True, exist_ok=True)
        if _has_link_component(root) or _is_link_or_reparse_point(root):
            raise ReplaySetupError("replay_root_linked")
        candidate = root / f"{_require_identifier(record_id, 'record_id')}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        candidate.mkdir()
        if _is_link_or_reparse_point(candidate) or not _is_within(candidate.resolve(), root.resolve()):
            raise ReplaySetupError("replay_root_escaped")
        return candidate

    def _extract_archive(self, relpath: str, destination: Path, *, compressed: bool) -> None:
        payload = self.store.read_bytes(relpath)
        mode = "r:gz" if compressed else "r:"
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as archive:
                for member in _validate_tar_members(archive, label="replay"):
                    parts = _safe_archive_parts(member.name)
                    target = destination.joinpath(*parts)
                    if not _is_within(target.resolve(strict=False), destination.resolve()):
                        raise ReplaySetupError("archive_path_escape")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        os.chmod(target, member.mode & 0o777)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if _has_link_component(target.parent):
                        raise ReplaySetupError("archive_path_linked")
                    content = archive.extractfile(member)
                    if content is None:
                        raise ReplaySetupError("archive_member_unreadable")
                    with target.open("xb") as output:
                        shutil.copyfileobj(content, output)
                    os.chmod(target, member.mode & 0o777)
        except ReplaySetupError:
            raise
        except (tarfile.TarError, OSError) as exc:
            raise ReplaySetupError("archive_extract_failed", str(exc)) from exc

    def _apply_patch(self, relpath: str, workspace: Path) -> None:
        patch_path = workspace.parent / ".tracked.patch"
        try:
            payload = self.store.read_bytes(relpath)
            if not payload:
                return
            patch_path.write_bytes(payload)
            completed = subprocess.run(
                ["git", "apply", "--no-index", "--binary", str(patch_path)],
                cwd=workspace,
                capture_output=True,
                check=False,
                timeout=_SNAPSHOT_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReplaySetupError("patch_apply_unavailable", str(exc)) from exc
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError:
                pass
        if completed.returncode != 0:
            raise ReplaySetupError(
                "patch_apply_failed",
                completed.stderr.decode("utf-8", errors="replace")[:300],
            )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _gzip_bytes(payload: bytes) -> bytes:
    return gzip.compress(payload, mtime=0)


def _artifact_descriptor(path: str, payload: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(payload), "sha256": _sha256(payload)}


def _safe_archive_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ReplaySetupError("archive_member_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in ("", ".", "..") for part in path.parts):
        raise ReplaySetupError("archive_member_path_invalid")
    return path.parts


def _validate_tar_members(archive: tarfile.TarFile, *, label: str) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > _MAX_SNAPSHOT_FILES:
        raise ReplaySetupError(f"{label}_archive_too_many_files")
    total = 0
    for member in members:
        _safe_archive_parts(member.name)
        if not (member.isdir() or member.isfile()):
            raise ReplaySetupError(f"{label}_archive_special_member")
        if member.isfile():
            total += max(0, int(member.size))
    if total > 10 * 1024 * 1024 * 1024:
        raise ReplaySetupError(f"{label}_archive_too_large")
    return members


def _safe_relative_directory(working_directory: Path, git_root: Path) -> str:
    try:
        relative = working_directory.resolve(strict=False).relative_to(git_root.resolve(strict=False))
    except ValueError as exc:
        raise SnapshotCaptureError("working_directory_outside_git_root") from exc
    return "." if not relative.parts else PurePosixPath(*relative.parts).as_posix()


def _safe_replay_relative_directory(value: str) -> str:
    if value == ".":
        return value
    parts = _safe_archive_parts(value)
    return PurePosixPath(*parts).as_posix()


def _parse_nul_paths(payload: bytes) -> list[str]:
    paths: list[str] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SnapshotCaptureError("untracked_path_encoding_invalid") from exc
        _safe_archive_parts(value)
        paths.append(value)
    return paths


def _iter_safe_untracked_files(git_root: Path, relative: str):
    parts = _safe_archive_parts(relative)
    target = git_root.joinpath(*parts)
    if not _is_within(target.resolve(strict=False), git_root.resolve(strict=False)):
        raise SnapshotCaptureError("untracked_path_escape")
    if _is_link_or_reparse_point(target):
        raise SnapshotCaptureError("symlink_unsupported")
    if target.is_file():
        yield target, PurePosixPath(*parts).as_posix()
        return
    if not target.is_dir():
        raise SnapshotCaptureError("untracked_file_unavailable")
    for parent, directories, filenames in os.walk(target, followlinks=False):
        parent_path = Path(parent)
        if _is_link_or_reparse_point(parent_path):
            raise SnapshotCaptureError("symlink_unsupported")
        for name in directories:
            if _is_link_or_reparse_point(parent_path / name):
                raise SnapshotCaptureError("symlink_unsupported")
        for filename in filenames:
            candidate = parent_path / filename
            if _is_link_or_reparse_point(candidate) or not candidate.is_file():
                raise SnapshotCaptureError("symlink_unsupported")
            relative_name = candidate.relative_to(git_root)
            yield candidate, PurePosixPath(*relative_name.parts).as_posix()


def _load_verified_snapshot_manifest(store: "ArtifactStore", snapshot: dict[str, Any]) -> dict[str, Any]:
    required_paths = {
        "base_tree": snapshot.get("base_tree_relpath"),
        "tracked_patch": snapshot.get("patch_relpath"),
        "untracked": snapshot.get("untracked_relpath"),
    }
    if not snapshot.get("manifest_relpath") or not all(required_paths.values()):
        raise ReplaySetupError("snapshot_manifest_missing")
    try:
        manifest = json.loads(store.read_bytes(snapshot["manifest_relpath"]).decode("utf-8"))
    except (ArtifactPathError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplaySetupError("snapshot_manifest_invalid", str(exc)) from exc
    if (
        manifest.get("snapshot_manifest_version") != _SNAPSHOT_MANIFEST_VERSION
        or manifest.get("snapshot_id") != snapshot.get("snapshot_id")
        or manifest.get("capture_fingerprint") != snapshot.get("capture_fingerprint")
        or not isinstance(manifest.get("artifacts"), dict)
    ):
        raise ReplaySetupError("snapshot_manifest_mismatch")
    for key, expected_path in required_paths.items():
        descriptor = manifest["artifacts"].get(key)
        if not isinstance(descriptor, dict) or descriptor.get("path") != expected_path:
            raise ReplaySetupError("snapshot_manifest_mismatch")
        try:
            payload = store.read_bytes(expected_path)
        except ArtifactPathError as exc:
            raise ReplaySetupError("snapshot_artifact_missing", str(exc)) from exc
        if descriptor.get("size") != len(payload) or descriptor.get("sha256") != _sha256(payload):
            raise ReplaySetupError("snapshot_artifact_hash_mismatch")
    return manifest


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = path.with_name(f"{_TEMPORARY_PREFIX}{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _extract_config_secrets(value, key_hint: str = "") -> set[str]:
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            secrets.update(_extract_config_secrets(item, str(key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            secrets.update(_extract_config_secrets(item, key_hint))
    elif isinstance(value, str) and re.search(r"(?i)(api[_-]?key|token|password|secret)", key_hint):
        if value.strip():
            secrets.add(value.strip())
    return secrets


def _looks_sensitive_name(name: str) -> bool:
    return bool(re.search(r"(?i)(api[_-]?key|token|password|secret|credential)", name))


def _truncate_text_bytes(value: str, limit: int) -> tuple[str, bool]:
    data = value.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return value, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    if limit <= len(marker):
        return data[:limit].decode("utf-8", errors="replace"), True
    available = max(0, limit - len(marker))
    head_size = available * 2 // 5
    tail_size = available - head_size
    head = data[:head_size].decode("utf-8", errors="ignore")
    tail = data[-tail_size:].decode("utf-8", errors="ignore")
    return head + _TRUNCATION_MARKER + tail, True


def _require_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ArtifactPathError(f"invalid {field}")
    return value


def validate_artifact_relpath(value: str) -> str:
    """返回规范化 POSIX 相对路径，拒绝 Windows 驱动器和路径穿越。"""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ArtifactPathError("artifact path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in ("", ".", "..") for part in path.parts):
        raise ArtifactPathError(f"artifact path escapes root: {value!r}")
    return path.as_posix()


class ArtifactStore:
    """版本化制品根目录的最小文件系统接口。"""

    def __init__(self, artifact_root: str | Path | None = None):
        root = Path(artifact_root).expanduser() if artifact_root else (
            MINIHERMES_HOME / "artifacts"
        )
        if not root.is_absolute():
            raise ArtifactPathError("artifact_root must be an absolute path")
        self._configured_root = root
        if _has_link_component(root):
            raise ArtifactPathError("artifact_root must not contain a symlink or junction")
        self._base_root = root.resolve(strict=False)
        project_root = Path.cwd().resolve(strict=False)
        if _is_within(self._base_root, project_root):
            raise ArtifactPathError("artifact_root must not be inside the current project")
        if _is_git_related_path(self._base_root):
            raise ArtifactPathError("artifact_root must not be inside a Git worktree or metadata directory")
        managed_worktrees_root = (MINIHERMES_HOME / "worktrees").resolve(strict=False)
        if _is_within(self._base_root, managed_worktrees_root):
            raise ArtifactPathError("artifact_root must not be inside the managed worktree root")
        self.root = self._base_root / f"v{ARTIFACT_FORMAT_VERSION}"
        self._ensure_root()

    @classmethod
    def from_config(cls, config: dict | None = None) -> "ArtifactStore":
        raw = (config or {}).get("artifact_root", "")
        if raw:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                raise ArtifactPathError("configured artifact_root must be absolute")
            return cls(path)
        return cls()

    def snapshot_relpath(self, run_id: str, snapshot_id: str, filename: str) -> str:
        return self._bundle_relpath(run_id, "snapshots", snapshot_id, filename)

    def execution_relpath(self, run_id: str, record_id: str, filename: str) -> str:
        return self._bundle_relpath(run_id, "executions", record_id, filename)

    def run_manifest_relpath(self, run_id: str) -> str:
        return f"{_require_identifier(run_id, 'run_id')}/run-manifest.json"

    def write_bytes_atomic(self, relative_path: str, data: bytes) -> Path:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        target = self.resolve_relative_path(relative_path, create_parent=True)
        self._atomic_replace(target, data)
        return target

    def write_text_atomic(self, relative_path: str, content: str) -> Path:
        if not isinstance(content, str):
            raise TypeError("content must be text")
        return self.write_bytes_atomic(relative_path, content.encode("utf-8"))

    def write_json_atomic(self, relative_path: str, payload: dict[str, Any]) -> Path:
        document = {
            "artifact_format_version": ARTIFACT_FORMAT_VERSION,
            **payload,
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        return self.write_text_atomic(relative_path, encoded)

    def read_bytes(self, relative_path: str) -> bytes:
        path = self.resolve_relative_path(relative_path, create_parent=False)
        if _is_link_or_reparse_point(path) or not path.is_file():
            raise ArtifactPathError(f"artifact is not a regular file: {relative_path}")
        return path.read_bytes()

    def resolve_relative_path(self, relative_path: str, *, create_parent: bool) -> Path:
        rel = validate_artifact_relpath(relative_path)
        target = self.root.joinpath(*PurePosixPath(rel).parts)
        parent = target.parent
        if create_parent:
            self._ensure_directory_chain(parent)
        self._assert_root_intact()
        self._assert_real_directory_chain(parent)
        if target.exists() and _is_link_or_reparse_point(target):
            raise ArtifactPathError(f"artifact target is a symlink: {relative_path!r}")
        return target

    def remove_run(self, run_id: str) -> bool:
        """只删除系统按 run_id 建立的顶层目录，不接受任意递归路径。"""
        self._assert_root_intact()
        target = self.root / _require_identifier(run_id, "run_id")
        if not target.exists():
            return False
        if (
            _is_link_or_reparse_point(target)
            or not target.is_dir()
            or target.parent != self.root
        ):
            raise ArtifactPathError("refusing to remove unmanaged artifact directory")
        shutil.rmtree(target)
        return True

    def remove_snapshot_bundle(self, run_id: str, snapshot_id: str) -> bool:
        self._assert_root_intact()
        target = self.root / _require_identifier(run_id, "run_id") / "snapshots" / _require_identifier(
            snapshot_id, "snapshot_id"
        )
        expected_parent = self.root / run_id / "snapshots"
        if not target.exists():
            return False
        self._assert_real_directory_chain(expected_parent)
        if (
            _is_link_or_reparse_point(target)
            or not target.is_dir()
            or target.parent != expected_parent
        ):
            raise ArtifactPathError("refusing to remove unmanaged snapshot directory")
        shutil.rmtree(target)
        return True

    def remove_execution_bundle(self, run_id: str, record_id: str) -> bool:
        """删除一条执行记录自己的制品目录，不接受任意递归路径。"""
        self._assert_root_intact()
        target = self.root / _require_identifier(run_id, "run_id") / "executions" / _require_identifier(
            record_id, "record_id"
        )
        expected_parent = self.root / run_id / "executions"
        if not target.exists():
            return False
        self._assert_real_directory_chain(expected_parent)
        if (
            _is_link_or_reparse_point(target)
            or not target.is_dir()
            or target.parent != expected_parent
        ):
            raise ArtifactPathError("refusing to remove unmanaged execution directory")
        shutil.rmtree(target)
        return True

    def bundle_size_bytes(self, run_id: str, category: str, item_id: str) -> int:
        """统计一个已验证制品目录的常规文件大小，拒绝链接和路径逃逸。"""
        if category not in {"snapshots", "executions"}:
            raise ArtifactPathError("unsupported artifact bundle category")
        self._assert_root_intact()
        target = self.root / _require_identifier(run_id, "run_id") / category / _require_identifier(
            item_id, "item_id"
        )
        expected_parent = self.root / run_id / category
        if not target.exists():
            return 0
        self._assert_real_directory_chain(expected_parent)
        if (
            _is_link_or_reparse_point(target)
            or not target.is_dir()
            or target.parent != expected_parent
        ):
            raise ArtifactPathError("artifact bundle is not a managed directory")
        return self._regular_directory_size_bytes(target)

    @staticmethod
    def _regular_directory_size_bytes(target: Path) -> int:
        """验证目录树仅包含普通文件后返回总大小。"""
        total = 0
        for parent, dirnames, filenames in os.walk(target, followlinks=False):
            parent_path = Path(parent)
            if _is_link_or_reparse_point(parent_path):
                raise ArtifactPathError("artifact bundle contains a link")
            safe_dirs = [
                name for name in dirnames
                if not _is_link_or_reparse_point(parent_path / name)
            ]
            if len(safe_dirs) != len(dirnames):
                raise ArtifactPathError("artifact bundle contains a link")
            dirnames[:] = safe_dirs
            for filename in filenames:
                candidate = parent_path / filename
                if _is_link_or_reparse_point(candidate) or not candidate.is_file():
                    raise ArtifactPathError("artifact bundle contains a non-regular file")
                total += candidate.stat().st_size
        return total

    def total_size_bytes(self) -> int:
        """统计整个受控制品根；发现链接时拒绝继续清理。"""
        self._assert_root_intact()
        total = 0
        for parent, dirnames, filenames in os.walk(self.root, followlinks=False):
            parent_path = Path(parent)
            if _is_link_or_reparse_point(parent_path):
                raise ArtifactPathError("artifact root contains a link")
            safe_dirs = [
                name for name in dirnames
                if not _is_link_or_reparse_point(parent_path / name)
            ]
            if len(safe_dirs) != len(dirnames):
                raise ArtifactPathError("artifact root contains a link")
            dirnames[:] = safe_dirs
            for filename in filenames:
                candidate = parent_path / filename
                if _is_link_or_reparse_point(candidate) or not candidate.is_file():
                    raise ArtifactPathError("artifact root contains a non-regular file")
                total += candidate.stat().st_size
        return total

    def cleanup_stale_temporary_files(self, older_than_seconds: float) -> int:
        """清理本模块留下的临时文件，不触碰完成制品或用户文件。"""
        self._assert_root_intact()
        age = max(0.0, float(older_than_seconds))
        deadline = time.time() - age
        removed = 0
        for parent, dirnames, filenames in os.walk(self.root, followlinks=False):
            parent_path = Path(parent)
            if (
                _is_link_or_reparse_point(parent_path)
                or not _is_within(parent_path.resolve(strict=False), self.root)
            ):
                continue
            dirnames[:] = [
                name
                for name in dirnames
                if not _is_link_or_reparse_point(parent_path / name)
            ]
            for filename in filenames:
                if not filename.startswith(_TEMPORARY_PREFIX):
                    continue
                candidate = parent_path / filename
                try:
                    if (
                        _is_link_or_reparse_point(candidate)
                        or candidate.stat().st_mtime > deadline
                    ):
                        continue
                    candidate.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def cleanup_orphan_bundles(
        self,
        *,
        known_snapshot_bundles: set[tuple[str, str]],
        known_execution_bundles: set[tuple[str, str]],
        older_than_seconds: float,
    ) -> int:
        """清理崩溃在数据库登记前留下的旧制品目录。

        只识别固定 ``<run>/(snapshots|executions)/<item>`` 布局，且必须等到
        宽限期后才处理。任何链接、异常文件类型或未知目录结构都会中止清理，
        不以“尽力删除”穿透文件系统边界。
        """
        self._assert_root_intact()
        deadline = time.time() - max(0.0, float(older_than_seconds))
        removed = 0
        candidates: list[Path] = []
        for run_dir in self.root.iterdir():
            if _is_link_or_reparse_point(run_dir):
                raise ArtifactPathError("artifact root contains a link")
            if not run_dir.is_dir():
                continue
            if not _IDENTIFIER.fullmatch(run_dir.name):
                continue
            for category, known in (
                ("snapshots", known_snapshot_bundles),
                ("executions", known_execution_bundles),
            ):
                category_dir = run_dir / category
                if not category_dir.exists():
                    continue
                if _is_link_or_reparse_point(category_dir) or not category_dir.is_dir():
                    raise ArtifactPathError("artifact category is not a managed directory")
                for bundle_dir in category_dir.iterdir():
                    if _is_link_or_reparse_point(bundle_dir):
                        raise ArtifactPathError("artifact bundle contains a link")
                    if not bundle_dir.is_dir() or not _IDENTIFIER.fullmatch(bundle_dir.name):
                        continue
                    if (run_dir.name, bundle_dir.name) in known:
                        continue
                    if bundle_dir.stat().st_mtime > deadline:
                        continue
                    candidates.append(bundle_dir)

        for bundle_dir in candidates:
            # 再次确认父级和链接边界，防止扫描后目录被替换。
            category_dir = bundle_dir.parent
            run_dir = category_dir.parent
            if (
                category_dir.name not in {"snapshots", "executions"}
                or _is_link_or_reparse_point(run_dir)
                or _is_link_or_reparse_point(category_dir)
                or _is_link_or_reparse_point(bundle_dir)
                or bundle_dir.parent != category_dir
                or category_dir.parent != run_dir
                or run_dir.parent != self.root
                or not _is_within(bundle_dir.resolve(strict=False), self.root)
            ):
                raise ArtifactPathError("orphan artifact bundle escaped managed root")
            self._regular_directory_size_bytes(bundle_dir)
            shutil.rmtree(bundle_dir)
            removed += 1
        return removed

    def _bundle_relpath(self, run_id: str, category: str, item_id: str, filename: str) -> str:
        safe_run_id = _require_identifier(run_id, "run_id")
        safe_item_id = _require_identifier(item_id, "item_id")
        safe_filename = validate_artifact_relpath(filename)
        if len(PurePosixPath(safe_filename).parts) != 1:
            raise ArtifactPathError("bundle filename must not contain a directory")
        return f"{safe_run_id}/{category}/{safe_item_id}/{safe_filename}"

    def _ensure_root(self) -> None:
        self._ensure_directory_chain(self.root)

    def _ensure_directory_chain(self, directory: Path) -> None:
        if not _is_within(directory.resolve(strict=False), self._base_root):
            raise ArtifactPathError("artifact directory escapes base root")
        current = self._base_root
        current.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse_point(current):
            raise ArtifactPathError("artifact base root must not be a symlink")
        relative = directory.relative_to(self._base_root)
        for part in relative.parts:
            current = current / part
            if current.exists():
                if _is_link_or_reparse_point(current) or not current.is_dir():
                    raise ArtifactPathError("artifact directory is not a real directory")
                continue
            current.mkdir()
            if (
                _is_link_or_reparse_point(current)
                or not _is_within(current.resolve(strict=False), self._base_root)
            ):
                raise ArtifactPathError("artifact directory escaped base root")

    def _atomic_replace(self, target: Path, data: bytes) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix=_TEMPORARY_PREFIX,
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._assert_root_intact()
            if (
                _is_link_or_reparse_point(temporary)
                or _is_link_or_reparse_point(target.parent)
                or not _is_within(target.parent.resolve(strict=False), self.root)
            ):
                raise ArtifactPathError("artifact directory changed during write")
            os.replace(temporary, target)
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _assert_root_intact(self) -> None:
        if _has_link_component(self._configured_root):
            raise ArtifactPathError("artifact root must not contain a symlink or junction")
        if _is_link_or_reparse_point(self._base_root) or _is_link_or_reparse_point(self.root):
            raise ArtifactPathError("artifact root must not be a symlink")
        if not _is_within(self.root.resolve(strict=False), self._base_root):
            raise ArtifactPathError("artifact root escaped its base directory")

    def _assert_real_directory_chain(self, directory: Path) -> None:
        self._assert_root_intact()
        try:
            relative = directory.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactPathError("artifact directory is outside the managed root") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if _is_link_or_reparse_point(current) or not current.is_dir():
                raise ArtifactPathError("artifact directory is not a real directory")
            if not _is_within(current.resolve(strict=False), self.root):
                raise ArtifactPathError("artifact directory escaped the managed root")


class ArtifactRetentionManager:
    """R3 制品保留：数据库先行、文件删除后置的保守清理协调器。"""

    def __init__(self, store: ArtifactStore, session_db):
        self.store = store
        self.db = session_db

    def purge_snapshot(self, snapshot_id: str) -> int:
        snapshot = self.db.get_workspace_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(f"unknown workspace snapshot: {snapshot_id}")
        # 先保守地把引用标记为不可用。进程即使在删除文件前崩溃，也不会
        # 让后续重放误以为材料仍完整；残留目录可以由下一次显式清理处理。
        affected = self.db.purge_snapshot_references(snapshot_id)
        try:
            self.store.remove_snapshot_bundle(snapshot["run_id"], snapshot_id)
        except Exception as exc:
            # 数据库已经保守地标记为 PURGED，重试只会再次尝试受限目录删除。
            raise ArtifactCleanupError(snapshot_id, affected, exc) from exc
        return affected

    def cleanup_temporary_files(self, older_than_seconds: float = 3600) -> int:
        return self.store.cleanup_stale_temporary_files(older_than_seconds)

    def inspect(self, *, retention_days: int, keep_failed_days: int) -> dict[str, object]:
        """只读展示哪些制品目前可清理，供 CLI 使用。"""
        now = time.time()
        groups = self.db.inspect_artifact_retention_groups(
            normal_before=now - _retention_seconds(retention_days),
            failed_before=now - _retention_seconds(keep_failed_days),
        )
        summary = self._retention_summary(groups)
        summary["used_bytes"] = self.store.total_size_bytes()
        return summary

    def cleanup(
        self,
        *,
        retention_days: int,
        keep_failed_days: int,
        max_total_artifact_bytes: int,
    ) -> dict[str, object]:
        """清理到期制品，容量压力下只额外清理成功记录。

        `max_total_artifact_bytes` 是软上限：受保护的失败记录、正在运行的
        重放或任何有活动引用的制品一律不因空间压力删除。
        """
        now = time.time()
        normal_before = now - _retention_seconds(retention_days)
        failed_before = now - _retention_seconds(keep_failed_days)
        prior_groups = self.db.inspect_artifact_retention_groups(
            normal_before=normal_before,
            failed_before=failed_before,
        )
        retry_groups: list[dict] = []
        retry_errors: list[str] = []
        for group in prior_groups:
            if not group["already_purged"]:
                continue
            try:
                if self._group_size(group) > 0:
                    retry_groups.append(group)
            except Exception as exc:
                retry_errors.append(
                    f"retry_size:{group['group_type']}:{group['group_id']}:{type(exc).__name__}"
                )
        retry_outcome = self._delete_claimed_groups(retry_groups)
        groups = self.db.claim_expired_artifact_retention_groups(
            normal_before=normal_before,
            failed_before=failed_before,
        )
        outcome = self._delete_claimed_groups(groups)
        outcome["reclaimed_purged_groups"] = retry_outcome["purged_groups"]
        outcome["reclaimed_purged_bytes"] = retry_outcome["deleted_bytes"]
        outcome["errors"] = retry_errors + retry_outcome["errors"] + outcome["errors"]
        try:
            owners = self.db.list_artifact_bundle_owners()
            outcome["orphan_bundles"] = self.store.cleanup_orphan_bundles(
                known_snapshot_bundles=owners["snapshots"],
                known_execution_bundles=owners["executions"],
                older_than_seconds=3600,
            )
        except Exception as exc:
            outcome["orphan_bundles"] = 0
            outcome["errors"].append(f"orphan_cleanup:{type(exc).__name__}")
        try:
            current_size = self.store.total_size_bytes()
        except Exception as exc:
            outcome["errors"].append(f"size_check:{type(exc).__name__}")
            return self._finalize_cleanup_outcome(outcome)

        limit = max(0, int(max_total_artifact_bytes))
        if limit and current_size > limit:
            candidates = self.db.inspect_artifact_retention_groups(
                normal_before=normal_before,
                failed_before=failed_before,
            )
            capacity_candidates = [
                group for group in candidates
                if not group["failed"]
                and not group["already_purged"]
                and group["blocked_reason"] in {None, "retention"}
            ]
            capacity_candidates.sort(
                key=lambda item: (item["last_activity_at"], item["group_type"], item["group_id"])
            )
            keys: set[tuple[str, str]] = set()
            for group in capacity_candidates:
                if current_size <= limit:
                    break
                try:
                    size = self._group_size(group)
                except Exception as exc:
                    outcome["errors"].append(
                        f"size:{group['group_type']}:{group['group_id']}:{type(exc).__name__}"
                    )
                    continue
                if size <= 0:
                    continue
                keys.add((group["group_type"], group["group_id"]))
                current_size -= size
            claimed = self.db.claim_capacity_artifact_retention_groups(keys)
            capacity_outcome = self._delete_claimed_groups(claimed)
            self._merge_cleanup_outcomes(outcome, capacity_outcome)

        return self._finalize_cleanup_outcome(outcome)

    def _group_size(self, group: dict) -> int:
        total = 0
        if group["snapshot_id"] and group.get("snapshot_run_id"):
            total += self.store.bundle_size_bytes(
                group["snapshot_run_id"], "snapshots", group["snapshot_id"]
            )
        for record_id, run_id in group["record_locations"]:
            total += self.store.bundle_size_bytes(run_id, "executions", record_id)
        return total

    def _delete_claimed_groups(self, groups: list[dict]) -> dict[str, object]:
        outcome: dict[str, object] = {
            "claimed_groups": 0,
            "purged_groups": 0,
            "purged_records": 0,
            "deleted_bytes": 0,
            "orphan_bundles": 0,
            "errors": [],
        }
        for group in groups:
            outcome["claimed_groups"] += 1
            try:
                size = self._group_size(group)
                if group["snapshot_id"] and group.get("snapshot_run_id"):
                    self.store.remove_snapshot_bundle(
                        group["snapshot_run_id"], group["snapshot_id"]
                    )
                for record_id, run_id in group["record_locations"]:
                    self.store.remove_execution_bundle(run_id, record_id)
                outcome["purged_groups"] += 1
                outcome["purged_records"] += len(group["record_ids"])
                outcome["deleted_bytes"] += size
            except Exception as exc:
                outcome["errors"].append(
                    f"delete:{group['group_type']}:{group['group_id']}:{type(exc).__name__}"
                )
        return outcome

    @staticmethod
    def _merge_cleanup_outcomes(target: dict[str, object], source: dict[str, object]) -> None:
        for key in ("claimed_groups", "purged_groups", "purged_records", "deleted_bytes"):
            target[key] += source[key]
        target["errors"].extend(source["errors"])

    def _finalize_cleanup_outcome(self, outcome: dict[str, object]) -> dict[str, object]:
        try:
            outcome["remaining_bytes"] = self.store.total_size_bytes()
        except Exception as exc:
            outcome["remaining_bytes"] = None
            outcome["errors"].append(f"size_final:{type(exc).__name__}")
        return outcome

    @staticmethod
    def _retention_summary(groups: list[dict]) -> dict[str, object]:
        return {
            "groups": len(groups),
            "eligible_groups": sum(1 for group in groups if group["eligible"]),
            "blocked_groups": sum(1 for group in groups if group["blocked_reason"]),
            "already_purged_groups": sum(1 for group in groups if group["already_purged"]),
            "blocked_reasons": {
                reason: sum(1 for group in groups if group["blocked_reason"] == reason)
                for reason in sorted({group["blocked_reason"] for group in groups if group["blocked_reason"]})
            },
        }


def _retention_seconds(days: int) -> float:
    try:
        return max(1, int(days)) * 24 * 60 * 60
    except (TypeError, ValueError):
        return 30 * 24 * 60 * 60


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse_point(path: Path) -> bool:
    """在 Windows 也把 junction 等重解析点视为不安全链接。"""
    try:
        return path.is_symlink() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        )
    except OSError:
        return True


def _has_link_component(path: Path) -> bool:
    """检查用户配置路径中的既有组件，拒绝通过链接间接指定制品根。"""
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor and path.parts else path.parts
    for part in parts:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError:
            # 后续组件不可能已存在，后续 mkdir 会按真实目录逐层创建。
            return False
        except OSError:
            return True
        if _is_link_or_reparse_point(current):
            return True
    return False


def _is_git_related_path(path: Path) -> bool:
    """拒绝 Git 元数据树及任何已存在 Git 工作区内的制品根。"""
    if any(part.lower() == ".git" for part in path.parts):
        return True
    current = path
    while True:
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent
