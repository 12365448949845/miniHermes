"""Worktree 门禁、路径边界、候选生命周期与范围审计。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence


MINIMUM_GIT_VERSION = (2, 20, 0)
GIT_PROBE_TIMEOUT_SECONDS = 10.0
WORKTREE_BRANCH_PREFIX = "minihermes/worktree/"
WORKTREE_WRITE_TOOLS = frozenset({
    "read_file", "list_dir", "write_file", "bash", "skill_view",
})
# Delegate 获得这些宿主工作区能力时必须进入 worktree_write；普通 Delegate
# 即使串行运行也不能直接修改主项目。父 Run 也必须拥有同一组能力。
WORKTREE_MUTATING_DELEGATE_TOOLS = frozenset({"write_file", "bash"})
WORKTREE_REQUIRED_PARENT_TOOLS = WORKTREE_MUTATING_DELEGATE_TOOLS
_DEFAULT_WORKTREE_ROOT = Path.home() / ".minihermes" / "worktrees"

_FORBIDDEN_PATH_PARTS = frozenset({".git", ".minihermes"})
_GLOB_CHARACTERS = frozenset("*?[]{}")
_GIT_VERSION = re.compile(r"\bgit version (\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_LFS_ATTRIBUTE = re.compile(rb"(?:^|\s)filter\s*(?:=|\s)\s*lfs(?:\s|$)", re.IGNORECASE)
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class WorkspaceValidationError(ValueError):
    """工作区请求违反固定路径或范围边界。"""

    def __init__(self, reason_code: str, message: str = ""):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class WorkspaceOperationError(RuntimeError):
    """Worktree 创建、审计或清理没有满足受控操作条件。"""

    def __init__(self, reason_code: str, message: str = ""):
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class WorkspaceIntegrationError(WorkspaceOperationError):
    """显式集成失败；调用方应保留候选并记录失败原因。"""

    def __init__(
        self,
        reason_code: str,
        message: str = "",
        *,
        conflicts: Sequence[str] = (),
    ):
        super().__init__(reason_code, message)
        self.conflicts = tuple(conflicts)


class WorktreeRollbackError(WorkspaceOperationError):
    """受控回滚被跳过或检测到冲突，并携带已保存的证据引用。"""

    def __init__(
        self,
        reason_code: str,
        message: str = "",
        *,
        status: str = "ROLLBACK_CONFLICT",
        artifact_relpath: str | None = None,
        artifact_hash: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(reason_code, message)
        self.status = status
        self.artifact_relpath = artifact_relpath
        self.artifact_hash = artifact_hash
        self.details = details or {}


@dataclass(frozen=True)
class GitGateFailure:
    reason_code: str
    message: str


@dataclass(frozen=True)
class GitWorkspaceInspection:
    """一次只读 Git 门禁检查的完整结果。"""

    eligible: bool
    working_directory: Path
    git_root: Path | None
    git_dir: Path | None
    head_commit: str | None
    git_version: tuple[int, int, int] | None
    failures: tuple[GitGateFailure, ...]

    @property
    def primary_reason(self) -> str | None:
        return self.failures[0].reason_code if self.failures else None


@dataclass(frozen=True)
class CandidateAudit:
    base_commit: str
    branch_name: str
    tracked_diff: bytes
    changes: tuple[dict, ...]
    violations: tuple[str, ...] = ()

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(change["path"] for change in self.changes)


@dataclass(frozen=True)
class WorktreeRollbackResult:
    status: str
    reason_code: str | None
    artifact_relpath: str | None
    artifact_hash: str | None


@dataclass(frozen=True)
class IntegrationWorkspace:
    """一次临时集成 Worktree 的受控路径和已验证 tree。"""

    integration_id: str
    workspace_id: str
    git_root: Path
    workspace_root: Path
    task_temp_root: Path
    source_main_commit: str
    source_branch_name: str
    candidate_commit: str
    expected_merge_tree_hash: str


@dataclass
class WorkspaceExecutionContext:
    """一次写入 Delegate 的冻结工作区能力；永不进入模型可见参数。"""

    manager: "WorkspaceManager"
    db: object
    runner: object
    artifact_store: object
    workspace_id: str
    run_id: str
    workspace_root: Path
    task_temp_root: Path
    write_scope: tuple[str, ...]
    base_commit: str
    branch_name: str
    failure_code: str | None = None
    failure_message: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def resolve_path(
        self, requested_path: str, *, require_write: bool = False,
        allow_root: bool = False,
    ) -> Path:
        self.raise_if_failed()
        return self.manager.resolve_workspace_path(
            self.workspace_root,
            requested_path,
            write_scope=self.write_scope,
            require_write=require_write,
            allow_root=allow_root,
        )

    def raise_if_failed(self) -> None:
        if self.failure_code:
            raise WorkspaceOperationError(
                self.failure_code,
                f"{self.failure_code}: "
                f"{self.failure_message or 'Worktree candidate is no longer executable'}",
            )

    def execute_command(
        self,
        command: str,
        *,
        timeout: float,
        cancel_check=None,
    ):
        """执行严格命令并立即审计，范围违规后永久停止该 lease。"""
        from agent.workspace_runner import WorkspaceCommandResult

        with self._lock:
            try:
                self.raise_if_failed()
            except WorkspaceOperationError as exc:
                return WorkspaceCommandResult(
                    stderr=str(exc), error_code=exc.reason_code
                )
            result = self.runner.run(
                workspace_id=self.workspace_id,
                workspace_root=self.workspace_root,
                task_temp_root=self.task_temp_root,
                command=command,
                cwd_relative=".",
                timeout=timeout,
                cancel_check=cancel_check,
            )
            if result.error_code in {
                "docker_unavailable", "docker_spawn_failed",
                "docker_image_unavailable", "workspace_mount_unavailable",
                "git_sentinel_unavailable", "task_temp_unavailable",
            }:
                self._fail_with_current_audit(
                    "runner_failed", result.stderr or result.error_code
                )
                return result
            try:
                audit = self.manager.audit_candidate(self)
            except Exception as exc:
                self._fail_with_current_audit(
                    "candidate_audit_failed", f"{type(exc).__name__}: {exc}"
                )
                return WorkspaceCommandResult(
                    stdout=result.stdout,
                    stderr=(result.stderr + "\nCandidate audit failed").strip(),
                    exit_code=result.exit_code,
                    termination_reason=result.termination_reason,
                    error_code="candidate_audit_failed",
                )
            if audit.violations:
                refs = self.manager.persist_candidate_audit(self, audit)
                message = "; ".join(audit.violations)[:500]
                self._mark_failed("scope_violation", message, **refs)
                return WorkspaceCommandResult(
                    stdout=result.stdout,
                    stderr=(result.stderr + f"\nScope violation: {message}").strip(),
                    exit_code=result.exit_code,
                    termination_reason=result.termination_reason,
                    error_code="scope_violation",
                )
            return result

    def _fail_with_current_audit(self, code: str, message: str) -> None:
        refs = {}
        try:
            refs = self.manager.persist_candidate_audit(
                self, self.manager.audit_candidate(self)
            )
        except Exception:
            pass
        self._mark_failed(code, message, **refs)

    def _mark_failed(self, code: str, message: str, **artifact_refs) -> None:
        if self.failure_code:
            return
        self.failure_code = code
        self.failure_message = str(message)[:500]
        lease = self.db.get_worktree_lease(self.workspace_id)
        if lease and lease["lease_status"] in {"PROVISIONING", "READY", "RUNNING"}:
            self.db.transition_worktree_lease(
                self.workspace_id,
                status="FAILED",
                failure_code=code,
                failure_message=self.failure_message,
                **artifact_refs,
            )


class _GitProbeError(WorkspaceOperationError):
    def __init__(self, reason_code: str, message: str = ""):
        super().__init__(reason_code, message)


class WorkspaceManager:
    """宿主侧唯一允许管理 Git Worktree 生命周期的组件。"""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        minimum_git_version: tuple[int, int, int] = MINIMUM_GIT_VERSION,
        probe_timeout_seconds: float = GIT_PROBE_TIMEOUT_SECONDS,
        managed_root: str | Path | None = None,
    ):
        self.git_executable = git_executable
        self.minimum_git_version = minimum_git_version
        self.probe_timeout_seconds = max(0.1, float(probe_timeout_seconds))
        configured_root = Path(managed_root).expanduser() if managed_root else _DEFAULT_WORKTREE_ROOT
        if not configured_root.is_absolute():
            raise WorkspaceValidationError(
                "managed_root_must_be_absolute"
            )
        self.managed_root = configured_root.resolve(strict=False)

    def provision(
        self,
        *,
        db,
        runner,
        artifact_store,
        task_id: str,
        run_id: str,
        parent_run_id: str,
        working_directory: str | Path,
        write_scope: Sequence[str],
        preserve_failed_days: int = 30,
    ) -> WorkspaceExecutionContext:
        """通过全部门禁后创建一个真实 Worktree 和 READY lease。"""
        if artifact_store is None:
            raise WorkspaceOperationError(
                "evidence_unavailable",
                "Worktree writes require the reproducibility artifact store",
            )
        inspection = self.inspect_git_workspace(working_directory)
        if not inspection.eligible:
            failure = inspection.failures[0]
            raise WorkspaceOperationError(failure.reason_code, failure.message)
        assert inspection.git_root is not None and inspection.head_commit is not None
        git_root = inspection.git_root
        try:
            frozen_scope = self.validate_write_scope(
                write_scope, workspace_root=git_root
            )
        except WorkspaceValidationError as exc:
            raise WorkspaceOperationError(exc.reason_code, str(exc)) from exc
        try:
            probe = runner.probe()
        except Exception as exc:
            reason = getattr(exc, "reason_code", "runner_probe_failed")
            raise WorkspaceOperationError(reason, str(exc)) from exc
        if getattr(probe, "backend", None) != "docker":
            raise WorkspaceOperationError("strict_docker_runner_required")

        workspace_id = uuid.uuid4().hex
        repository_root = self._repository_managed_root(git_root)
        workspace_path = repository_root / workspace_id
        task_temp = repository_root / ".runtime" / workspace_id
        branch_name = WORKTREE_BRANCH_PREFIX + workspace_id
        self._validate_managed_paths(git_root, workspace_path, task_temp)
        preserve_until = time.time() + min(
            max(int(preserve_failed_days), 1), 3650
        ) * 86400
        lease = db.create_worktree_lease(
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            git_root=str(git_root),
            worktree_path=str(workspace_path),
            branch_name=branch_name,
            base_commit=inspection.head_commit,
            write_scope=frozen_scope,
            runner_backend="docker",
            runner_image_digest=probe.image_digest,
            preserve_until=preserve_until,
        )
        try:
            self._ensure_managed_directory(repository_root)
            self._ensure_managed_directory(task_temp)
            self._ensure_managed_directory(task_temp / "home")
            self._ensure_managed_directory(task_temp / "tmp")
            sentinel = task_temp / "git-sentinel"
            sentinel.write_text(
                "Git metadata is intentionally unavailable in this container.\n",
                encoding="utf-8",
            )
            self._git_bytes(
                git_root,
                "worktree", "add", "-b", branch_name,
                str(workspace_path), inspection.head_commit,
            )
            if (
                not workspace_path.is_dir()
                or _is_link_or_reparse_point(workspace_path)
                or not (workspace_path / ".git").is_file()
            ):
                raise WorkspaceOperationError(
                    "worktree_materialization_invalid"
                )
            actual_head = self._git_text(
                workspace_path, "rev-parse", "--verify", "HEAD"
            ).strip().lower()
            actual_branch = self._git_text(
                workspace_path, "branch", "--show-current"
            ).strip()
            if actual_head != inspection.head_commit or actual_branch != branch_name:
                raise WorkspaceOperationError("worktree_identity_mismatch")
            verifier = getattr(runner, "verify_workspace", None)
            if not callable(verifier):
                raise WorkspaceOperationError("runner_workspace_probe_unavailable")
            verifier(
                workspace_id=workspace_id,
                workspace_root=workspace_path.resolve(strict=True),
                task_temp_root=task_temp.resolve(strict=True),
            )
            if self._git_bytes(
                workspace_path,
                "status", "--porcelain=v1", "-z", "--untracked-files=all",
            ):
                raise WorkspaceOperationError("runner_workspace_probe_left_changes")
            db.transition_worktree_lease(workspace_id, status="READY")
        except Exception as exc:
            reason = getattr(exc, "reason_code", "worktree_provision_failed")
            current = db.get_worktree_lease(workspace_id)
            if current and current["lease_status"] == "PROVISIONING":
                db.transition_worktree_lease(
                    workspace_id,
                    status="FAILED",
                    failure_code=reason,
                    failure_message=str(exc)[:500],
                )
            raise WorkspaceOperationError(reason, str(exc)) from exc

        return WorkspaceExecutionContext(
            manager=self,
            db=db,
            runner=runner,
            artifact_store=artifact_store,
            workspace_id=workspace_id,
            run_id=run_id,
            workspace_root=workspace_path.resolve(strict=True),
            task_temp_root=task_temp.resolve(strict=True),
            write_scope=frozen_scope,
            base_commit=inspection.head_commit,
            branch_name=branch_name,
        )

    def start(self, context: WorkspaceExecutionContext) -> None:
        lease = context.db.get_worktree_lease(context.workspace_id)
        if not lease or lease["lease_status"] != "READY":
            raise WorkspaceOperationError("worktree_not_ready")
        context.db.transition_worktree_lease(context.workspace_id, status="RUNNING")

    def finalize(self, context: WorkspaceExecutionContext, run_status: str) -> dict:
        """保存最终候选证据，并按 Agent 终态关闭 lease。"""
        with context._lock:
            lease = context.db.get_worktree_lease(context.workspace_id)
            if not lease:
                raise WorkspaceOperationError("worktree_lease_missing")
            if lease["lease_status"] == "FAILED":
                return lease
            try:
                audit = self.audit_candidate(context)
                refs = self.persist_candidate_audit(context, audit)
            except Exception as exc:
                context._mark_failed(
                    "candidate_finalize_failed", f"{type(exc).__name__}: {exc}"
                )
                return context.db.get_worktree_lease(context.workspace_id)
            if audit.violations:
                context._mark_failed(
                    "scope_violation", "; ".join(audit.violations), **refs
                )
                return context.db.get_worktree_lease(context.workspace_id)
            if lease["lease_status"] == "READY":
                return context.db.transition_worktree_lease(
                    context.workspace_id,
                    status="FAILED",
                    failure_code="agent_not_started",
                    failure_message="Worktree was provisioned but the child Agent did not start",
                    **refs,
                )
            if lease["lease_status"] != "RUNNING":
                return lease
            if run_status in {"SUCCEEDED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"}:
                return context.db.transition_worktree_lease(
                    context.workspace_id, status="PRESERVED", **refs
                )
            return context.db.transition_worktree_lease(
                context.workspace_id,
                status="FAILED",
                failure_code="agent_failed",
                failure_message=f"Agent run ended as {run_status}",
                **refs,
            )

    def audit_candidate(self, context: WorkspaceExecutionContext) -> CandidateAudit:
        """以 lease 基线审计 tracked、untracked、删除和重命名。"""
        root = context.workspace_root.resolve(strict=True)
        self._assert_context_identity(context, root)
        status = self._git_bytes(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        changes, violations = _parse_candidate_status(status, root)
        visibility = self._git_bytes(root, "ls-files", "-v", "-z")
        if _index_has_hidden_paths(visibility):
            violations.append(
                "Git index contains assume-unchanged or skip-worktree paths"
            )
        ignored = self._git_bytes(
            root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
        )
        known_paths = {change["path"] for change in changes}
        for raw_path in ignored.split(b"\0"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", errors="surrogateescape")
                normalized, _ = _normalize_relative_path(path, scope=False)
                change, unsafe = _candidate_file_record(
                    root, normalized, "ignored_untracked", "!!"
                )
            except (UnicodeError, WorkspaceValidationError):
                violations.append("unsafe ignored path in Git index")
                continue
            if normalized not in known_paths:
                changes.append(change)
                known_paths.add(normalized)
            if unsafe:
                violations.append(unsafe)
        for change in changes:
            path = change["path"]
            if not _path_matches_scope(path, context.write_scope):
                violations.append(f"path outside write_scope: {path}")
            old_path = change.get("old_path")
            if old_path and not _path_matches_scope(old_path, context.write_scope):
                violations.append(f"renamed path outside write_scope: {old_path}")
        tracked_diff = self._git_bytes(
            root,
            "diff", "--binary", "--full-index", "--no-ext-diff",
            context.base_commit, "--",
        )
        return CandidateAudit(
            context.base_commit,
            context.branch_name,
            tracked_diff,
            tuple(sorted(changes, key=lambda item: (item["path"], item["status"]))),
            tuple(sorted(set(violations))),
        )

    def persist_candidate_audit(
        self, context: WorkspaceExecutionContext, audit: CandidateAudit
    ) -> dict[str, str]:
        store = context.artifact_store
        prefix = f"{context.run_id}/worktrees/{context.workspace_id}"
        diff_relpath = f"{prefix}/candidate.diff"
        manifest_relpath = f"{prefix}/change-manifest.json"
        diff_path = store.write_bytes_atomic(diff_relpath, audit.tracked_diff)
        manifest_path = store.write_json_atomic(manifest_relpath, {
            "workspace_id": context.workspace_id,
            "run_id": context.run_id,
            "base_commit": audit.base_commit,
            "branch_name": audit.branch_name,
            "write_scope": list(context.write_scope),
            "changes": list(audit.changes),
            "violations": list(audit.violations),
            "tracked_diff_sha256": _sha256(audit.tracked_diff),
        })
        return {
            "diff_relpath": diff_relpath,
            "diff_hash": _sha256(diff_path.read_bytes()),
            "change_manifest_relpath": manifest_relpath,
            "change_manifest_hash": _sha256(manifest_path.read_bytes()),
        }

    def prepare_candidate_commit(
        self,
        *,
        db,
        artifact_store,
        workspace_id: str,
        integration_id: str,
    ) -> dict[str, object]:
        """校验冻结候选，并用临时 index 内容生成不可变 commit 对象。

        候选分支不会移动。所有暂存只服务于 ``write-tree``，完成后恢复为
        基线 index，同时保留候选工作区里的文件内容。
        """
        lease = db.get_worktree_lease(workspace_id)
        if not lease:
            raise WorkspaceIntegrationError("worktree_lease_missing")
        if lease["lease_status"] not in {"PRESERVED", "INTEGRATING"}:
            raise WorkspaceIntegrationError("worktree_not_integration_ready")
        context = self._context_from_lease(lease)
        context.db = db
        context.artifact_store = artifact_store
        root = context.workspace_root
        if not root.is_dir():
            raise WorkspaceIntegrationError("candidate_worktree_missing")
        audit, _manifest = self._verify_frozen_candidate(
            context=context, lease=lease, artifact_store=artifact_store
        )
        cached = self._git_completed(
            root, "diff", "--cached", "--quiet", lease["base_commit"], "--"
        )
        if cached.returncode != 0:
            raise WorkspaceIntegrationError(
                "candidate_index_dirty",
                cached.stderr.decode("utf-8", errors="replace")[:300],
            )
        if not audit.changes:
            raise WorkspaceIntegrationError("candidate_empty")
        paths = tuple(sorted({
            path
            for change in audit.changes
            for path in (change.get("path"), change.get("old_path"))
            if path
        }))
        if not paths:
            raise WorkspaceIntegrationError("candidate_paths_missing")

        try:
            self._git_bytes(root, "add", "-f", "--", *paths)
            tree_hash = self._git_text(root, "write-tree").strip().lower()
            if not _GIT_OBJECT_ID.fullmatch(tree_hash):
                raise WorkspaceIntegrationError("candidate_tree_invalid")
            message = f"MiniHermes: integrate Worktree candidate {workspace_id}"
            commit_hash = self._git_bytes_input(
                root,
                message.encode("utf-8"),
                "commit-tree", tree_hash, "-p", lease["base_commit"],
                "-m", message,
            ).decode("ascii", errors="replace").strip().lower()
            if not _GIT_OBJECT_ID.fullmatch(commit_hash):
                raise WorkspaceIntegrationError("candidate_commit_invalid")
            committed_tree = self._git_text(
                root, "rev-parse", f"{commit_hash}^{{tree}}"
            ).strip().lower()
            if committed_tree != tree_hash:
                raise WorkspaceIntegrationError("candidate_commit_tree_mismatch")
            return {
                "candidate_commit": commit_hash,
                "candidate_tree_hash": tree_hash,
                "changes": list(audit.changes),
            }
        finally:
            try:
                base_paths, new_paths = self._partition_rollback_paths(
                    root, lease["base_commit"], paths
                )
                for batch in _path_batches(base_paths):
                    self._git_bytes(
                        root,
                        "restore", f"--source={lease['base_commit']}", "--staged",
                        "--", *batch,
                    )
                for batch in _path_batches(new_paths):
                    self._git_bytes(
                        root, "rm", "--cached", "-f", "--ignore-unmatch", "--", *batch
                    )
            except Exception as exc:
                raise WorkspaceIntegrationError(
                    "candidate_index_restore_failed", str(exc)
                ) from exc

    def prepare_integration_workspace(
        self,
        *,
        lease: dict,
        integration_id: str,
        candidate_commit: str,
        source_main_commit: str,
        runner,
    ) -> IntegrationWorkspace:
        """从主分支基线创建 detached 临时 Worktree 并完成无提交合并。"""
        if not _GIT_OBJECT_ID.fullmatch(candidate_commit or ""):
            raise WorkspaceIntegrationError("candidate_commit_invalid")
        root = Path(lease["git_root"]).resolve(strict=True)
        source_inspection = self.inspect_git_workspace(root)
        if not source_inspection.eligible:
            failure = source_inspection.failures[0]
            raise WorkspaceIntegrationError(failure.reason_code, failure.message)
        if source_inspection.head_commit != source_main_commit:
            raise WorkspaceIntegrationError("main_head_changed")
        source_branch = self._git_text(root, "branch", "--show-current").strip()
        if not source_branch:
            raise WorkspaceIntegrationError("source_branch_required")
        candidate_type = self._git_text(
            root, "cat-file", "-t", candidate_commit
        ).strip()
        if candidate_type != "commit":
            raise WorkspaceIntegrationError("candidate_commit_unavailable")

        managed_root = self._repository_managed_root(root)
        integration_root = managed_root / ".integration"
        temp_root = managed_root / ".integration-runtime"
        workspace_path = integration_root / integration_id
        task_temp = temp_root / integration_id
        self._validate_managed_paths(root, workspace_path, task_temp)
        self._ensure_managed_directory(integration_root)
        self._ensure_managed_directory(temp_root)
        self._ensure_managed_directory(task_temp)
        self._ensure_managed_directory(task_temp / "home")
        self._ensure_managed_directory(task_temp / "tmp")
        (task_temp / "git-sentinel").write_text(
            "Git metadata is intentionally unavailable in this container.\n",
            encoding="utf-8",
        )
        materialized = False
        try:
            self._git_bytes(
                root, "worktree", "add", "--detach", str(workspace_path),
                source_main_commit,
            )
            materialized = True
            if (
                not workspace_path.is_dir()
                or _is_link_or_reparse_point(workspace_path)
                or not (workspace_path / ".git").is_file()
            ):
                raise WorkspaceIntegrationError("integration_worktree_invalid")
            actual_head = self._git_text(
                workspace_path, "rev-parse", "--verify", "HEAD"
            ).strip().lower()
            if actual_head != source_main_commit:
                raise WorkspaceIntegrationError("integration_worktree_identity_mismatch")
            verifier = getattr(runner, "verify_workspace", None)
            if not callable(verifier):
                raise WorkspaceIntegrationError("runner_workspace_probe_unavailable")
            verifier(
                workspace_id=lease["workspace_id"],
                workspace_root=workspace_path.resolve(strict=True),
                task_temp_root=task_temp.resolve(strict=True),
            )
            merged = self._git_completed(
                workspace_path,
                "merge", "--no-commit", "--no-ff", "--no-edit", candidate_commit,
            )
            if merged.returncode != 0:
                conflicts = self._integration_conflicts(workspace_path)
                self._abort_merge(workspace_path)
                if conflicts:
                    raise WorkspaceIntegrationError(
                        "integration_conflict",
                        "merge conflicts: " + ", ".join(conflicts[:20]),
                        conflicts=conflicts,
                    )
                raise WorkspaceIntegrationError(
                    "integration_merge_failed",
                    merged.stderr.decode("utf-8", errors="replace")[:500],
                )
            expected_tree = self._git_text(workspace_path, "write-tree").strip().lower()
            if not _GIT_OBJECT_ID.fullmatch(expected_tree):
                raise WorkspaceIntegrationError("integration_tree_invalid")
            return IntegrationWorkspace(
                integration_id=integration_id,
                workspace_id=lease["workspace_id"],
                git_root=root,
                workspace_root=workspace_path.resolve(strict=True),
                task_temp_root=task_temp.resolve(strict=True),
                source_main_commit=source_main_commit,
                source_branch_name=source_branch,
                candidate_commit=candidate_commit,
                expected_merge_tree_hash=expected_tree,
            )
        except Exception as exc:
            cleanup_succeeded = True
            if materialized:
                try:
                    self._abort_merge(workspace_path)
                except Exception:
                    cleanup_succeeded = False
                try:
                    self._git_bytes(root, "worktree", "remove", "--force", str(workspace_path))
                except Exception:
                    cleanup_succeeded = False
            try:
                shutil.rmtree(task_temp)
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_succeeded = False
            if isinstance(exc, WorkspaceOperationError):
                exc.temp_cleanup_succeeded = cleanup_succeeded
                raise
            normalized = WorkspaceIntegrationError(
                getattr(exc, "reason_code", "integration_workspace_prepare_failed"),
                str(exc),
            )
            normalized.temp_cleanup_succeeded = cleanup_succeeded
            raise normalized from exc

    def assert_git_identity(self, git_root: str | Path) -> None:
        """在任何暂存或合并前确认 Git 能创建候选和最终提交。"""
        root = Path(git_root).resolve(strict=True)
        branch = self._git_completed(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode != 0 or not branch.stdout.strip():
            raise WorkspaceIntegrationError(
                "source_branch_required",
                "explicit integration requires the primary workspace to be on a branch",
            )
        for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            completed = self._git_completed(root, "var", variable)
            if completed.returncode != 0 or not completed.stdout.strip():
                raise WorkspaceIntegrationError(
                    "git_identity_missing",
                    completed.stderr.decode("utf-8", errors="replace")[:300]
                    or f"{variable} is unavailable",
                )

    def validate_integration_verification(
        self, workspace: IntegrationWorkspace
    ) -> None:
        """验证命令不得改写待合并的 tracked 内容或 index tree。"""
        if not workspace.workspace_root.is_dir():
            raise WorkspaceIntegrationError("integration_worktree_missing")
        unstaged = self._git_completed(workspace.workspace_root, "diff", "--quiet", "--")
        if unstaged.returncode == 1:
            raise WorkspaceIntegrationError("verification_modified_tracked_files")
        if unstaged.returncode != 0:
            raise WorkspaceIntegrationError("verification_diff_failed")
        actual_tree = self._git_text(
            workspace.workspace_root, "write-tree"
        ).strip().lower()
        if actual_tree != workspace.expected_merge_tree_hash:
            raise WorkspaceIntegrationError("verification_changed_merge_tree")
        if self._integration_conflicts(workspace.workspace_root):
            raise WorkspaceIntegrationError("verification_left_conflicts")

    def apply_integration_to_main(
        self,
        *,
        workspace: IntegrationWorkspace,
        runner,
    ) -> tuple[str, str]:
        """再次检查主工作区后执行受控 no-commit merge，并证明 tree 一致。"""
        inspection = self.inspect_git_workspace(workspace.git_root)
        if not inspection.eligible:
            failure = inspection.failures[0]
            raise WorkspaceIntegrationError(failure.reason_code, failure.message)
        if inspection.head_commit != workspace.source_main_commit:
            raise WorkspaceIntegrationError("main_head_changed")
        current_branch = self._git_text(
            workspace.git_root, "branch", "--show-current"
        ).strip()
        if current_branch != workspace.source_branch_name:
            raise WorkspaceIntegrationError("main_branch_changed")
        if getattr(runner, "has_active_processes", None) is not None:
            try:
                if runner.has_active_processes(workspace.workspace_id):
                    raise WorkspaceIntegrationError("verification_runner_still_active")
            except WorkspaceIntegrationError:
                raise
            except Exception as exc:
                raise WorkspaceIntegrationError("runner_state_unavailable", str(exc)) from exc
        merged = self._git_completed(
            workspace.git_root,
            "merge", "--no-commit", "--no-ff", "--no-edit", workspace.candidate_commit,
        )
        if merged.returncode != 0:
            conflicts = self._integration_conflicts(workspace.git_root)
            self._abort_merge(workspace.git_root)
            raise WorkspaceIntegrationError(
                "final_merge_conflict" if conflicts else "final_merge_failed",
                ", ".join(conflicts[:20]) if conflicts else merged.stderr.decode(
                    "utf-8", errors="replace"
                )[:500],
                conflicts=conflicts,
            )
        try:
            actual_tree = self._git_text(workspace.git_root, "write-tree").strip().lower()
            if actual_tree != workspace.expected_merge_tree_hash:
                raise WorkspaceIntegrationError("final_merge_tree_mismatch")
            message = f"MiniHermes: merge Worktree candidate {workspace.workspace_id}"
            self._git_bytes(
                workspace.git_root,
                "commit", "--no-verify", "--no-gpg-sign", "-m", message,
            )
            final_commit = self._git_text(
                workspace.git_root, "rev-parse", "--verify", "HEAD"
            ).strip().lower()
            final_tree = self._git_text(
                workspace.git_root, "rev-parse", "HEAD^{tree}"
            ).strip().lower()
            if final_tree != workspace.expected_merge_tree_hash:
                raise WorkspaceIntegrationError("final_commit_tree_mismatch")
            return final_commit, final_tree
        except Exception:
            # 只有本次 merge 已知仍未提交时才允许 abort；不触碰已存在的用户现场。
            merge_head = self._git_path(workspace.git_root, "MERGE_HEAD")
            if merge_head is not None and merge_head.exists():
                try:
                    self._abort_merge(workspace.git_root)
                except Exception:
                    pass
            raise

    def cleanup_integration_workspace(
        self, *, workspace: IntegrationWorkspace, runner
    ) -> None:
        """清理受控临时集成 Worktree；失败由上层单独记账。"""
        checker = getattr(runner, "has_active_processes", None)
        if callable(checker):
            try:
                if checker(workspace.workspace_id):
                    raise WorkspaceIntegrationError("verification_runner_still_active")
            except WorkspaceIntegrationError:
                raise
            except Exception as exc:
                raise WorkspaceIntegrationError("runner_state_unavailable", str(exc)) from exc
        managed_root = self._repository_managed_root(workspace.git_root)
        expected = managed_root / ".integration" / workspace.integration_id
        expected_temp = managed_root / ".integration-runtime" / workspace.integration_id
        if workspace.workspace_root.resolve(strict=False) != expected.resolve(strict=False):
            raise WorkspaceIntegrationError("integration_path_identity_mismatch")
        if workspace.task_temp_root.resolve(strict=False) != expected_temp.resolve(strict=False):
            raise WorkspaceIntegrationError("integration_temp_identity_mismatch")
        if expected.exists():
            if _is_link_or_reparse_point(expected) or not expected.is_dir():
                raise WorkspaceIntegrationError("integration_path_unmanaged")
            self._git_bytes(workspace.git_root, "worktree", "remove", "--force", str(expected))
        if expected_temp.exists():
            if _is_link_or_reparse_point(expected_temp) or not expected_temp.is_dir():
                raise WorkspaceIntegrationError("integration_temp_unmanaged")
            shutil.rmtree(expected_temp)

    def cleanup_merged_candidate(self, *, db, runner, workspace_id: str) -> dict:
        """合并成功后删除候选；不能把 MERGED 候选退回 REJECTED。"""
        lease = db.get_worktree_lease(workspace_id)
        if not lease:
            raise WorkspaceIntegrationError("worktree_lease_missing")
        if lease["lease_status"] != "MERGED":
            raise WorkspaceIntegrationError("worktree_not_merged")
        if lease["cleanup_status"] == "SUCCEEDED":
            return lease
        self._assert_runner_stopped(runner, workspace_id)
        root = Path(lease["git_root"]).resolve(strict=True)
        expected = self._repository_managed_root(root) / workspace_id
        actual = Path(lease["worktree_path"]).resolve(strict=False)
        if actual != expected or lease["branch_name"] != WORKTREE_BRANCH_PREFIX + workspace_id:
            raise WorkspaceIntegrationError("worktree_cleanup_identity_mismatch")
        try:
            if actual.exists():
                if _is_link_or_reparse_point(actual) or not actual.is_dir():
                    raise WorkspaceIntegrationError("unmanaged_worktree_path")
                self._git_bytes(root, "worktree", "remove", "--force", str(actual))
            if self._git_ref_exists(root, lease["branch_name"]):
                branch_head = self._git_text(
                    root, "rev-parse", "--verify", lease["branch_name"]
                ).strip().lower()
                if branch_head != lease["base_commit"]:
                    raise WorkspaceIntegrationError("worktree_branch_head_changed")
                self._git_bytes(root, "branch", "-d", lease["branch_name"])
            task_temp = expected.parent / ".runtime" / workspace_id
            if task_temp.exists():
                if _is_link_or_reparse_point(task_temp) or not task_temp.is_dir():
                    raise WorkspaceIntegrationError("unmanaged_task_temp_path")
                shutil.rmtree(task_temp)
            return db.set_worktree_cleanup_status(
                workspace_id, cleanup_status="SUCCEEDED"
            )
        except Exception as exc:
            try:
                db.set_worktree_cleanup_status(
                    workspace_id,
                    cleanup_status="FAILED",
                    failure_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            raise

    def inspect_candidate(self, lease: dict) -> CandidateAudit:
        """只读查看已保存候选的当前状态，重新计算而不信任旧摘要。"""
        context = self._context_from_lease(lease)
        return self.audit_candidate(context)

    def persist_rollback_result(
        self,
        *,
        artifact_store,
        lease: dict,
        recovery_id: str,
        status: str,
        reason_code: str | None,
        details: dict | None = None,
    ) -> WorktreeRollbackResult:
        relpath = (
            f"{lease['run_id']}/worktrees/{lease['workspace_id']}/"
            f"rollback-{recovery_id}.json"
        )
        artifact_store.write_json_atomic(relpath, {
            "recovery_id": recovery_id,
            "workspace_id": lease["workspace_id"],
            "run_id": lease["run_id"],
            "status": status,
            "reason_code": reason_code,
            "base_commit": lease["base_commit"],
            "branch_name": lease["branch_name"],
            "write_scope": list(lease["write_scope"]),
            "candidate_diff_relpath": lease.get("diff_relpath"),
            "candidate_diff_hash": lease.get("diff_hash"),
            "candidate_manifest_relpath": lease.get("change_manifest_relpath"),
            "candidate_manifest_hash": lease.get("change_manifest_hash"),
            "details": details or {},
            "recorded_at": time.time(),
        })
        digest = _sha256(artifact_store.read_bytes(relpath))
        return WorktreeRollbackResult(status, reason_code, relpath, digest)

    def rollback_checkpoint(
        self,
        *,
        db,
        runner,
        artifact_store,
        workspace_id: str,
        recovery_id: str,
        on_prepared: Callable[[WorktreeRollbackResult], None],
    ) -> WorktreeRollbackResult:
        """验证冻结候选后恢复 base_commit；任何预检失败都不修改文件。"""
        lease = db.get_worktree_lease(workspace_id)
        if not lease:
            raise KeyError(f"unknown worktree lease: {workspace_id}")
        if lease["lease_status"] not in {"PRESERVED", "FAILED"}:
            raise WorktreeRollbackError("worktree_not_rollbackable")
        run = db.get_agent_run(lease["run_id"])
        if not run or run["status"] not in {
            "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
        }:
            raise WorktreeRollbackError(
                "source_run_not_terminal", status="ROLLBACK_SKIPPED"
            )
        self._assert_runner_stopped(runner, workspace_id)

        context = self._context_from_lease(lease)
        audit, manifest = self._verify_frozen_candidate(
            context=context,
            lease=lease,
            artifact_store=artifact_store,
        )
        rollback_paths = self._validate_rollback_paths(context, audit.changes)
        base_paths, new_paths = self._partition_rollback_paths(
            context.workspace_root, context.base_commit, rollback_paths
        )
        main_before = self._main_workspace_fingerprint(
            Path(lease["git_root"]).resolve(strict=True)
        )
        prepared_details = {
            "phase": "PREPARED",
            "current_changes": list(audit.changes),
            "current_violations": list(audit.violations),
            "tracked_diff_sha256": _sha256(audit.tracked_diff),
            "manifest_tracked_diff_sha256": manifest["tracked_diff_sha256"],
            "restore_paths": list(base_paths),
            "remove_paths": list(new_paths),
            "main_workspace_fingerprint_before": main_before,
        }
        try:
            prepared = self.persist_rollback_result(
                artifact_store=artifact_store,
                lease=lease,
                recovery_id=recovery_id,
                status="PREPARED",
                reason_code=None,
                details=prepared_details,
            )
        except Exception as exc:
            raise WorktreeRollbackError(
                "rollback_artifact_unavailable",
                f"{type(exc).__name__}: {exc}",
                status="ROLLBACK_SKIPPED",
            ) from exc
        try:
            on_prepared(prepared)
        except Exception as exc:
            raise WorktreeRollbackError(
                "rollback_state_transition_failed",
                f"{type(exc).__name__}: {exc}",
                status="ROLLBACK_SKIPPED",
                artifact_relpath=prepared.artifact_relpath,
                artifact_hash=prepared.artifact_hash,
            ) from exc

        try:
            self._restore_candidate_paths(
                context.workspace_root,
                context.base_commit,
                base_paths,
                new_paths,
            )
            final_audit = self.audit_candidate(context)
            if final_audit.changes or final_audit.violations or final_audit.tracked_diff:
                raise WorkspaceOperationError(
                    "rollback_verification_failed",
                    "candidate did not return to a clean base_commit state",
                )
            main_after = self._main_workspace_fingerprint(
                Path(lease["git_root"]).resolve(strict=True)
            )
            if main_after != main_before:
                raise WorkspaceOperationError(
                    "main_workspace_changed_during_rollback"
                )
            return self.persist_rollback_result(
                artifact_store=artifact_store,
                lease=lease,
                recovery_id=recovery_id,
                status="ROLLED_BACK",
                reason_code=None,
                details={
                    **prepared_details,
                    "phase": "VERIFIED",
                    "main_workspace_fingerprint_after": main_after,
                    "remaining_changes": [],
                },
            )
        except Exception as exc:
            reason = getattr(exc, "reason_code", "rollback_apply_failed")
            artifact_relpath = prepared.artifact_relpath
            artifact_hash = prepared.artifact_hash
            try:
                failed = self.persist_rollback_result(
                    artifact_store=artifact_store,
                    lease=lease,
                    recovery_id=recovery_id,
                    status="ROLLBACK_CONFLICT",
                    reason_code=reason,
                    details={
                        **prepared_details,
                        "phase": "APPLY_FAILED",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    },
                )
                artifact_relpath = failed.artifact_relpath
                artifact_hash = failed.artifact_hash
            except Exception:
                pass
            raise WorktreeRollbackError(
                reason,
                str(exc),
                status="ROLLBACK_CONFLICT",
                artifact_relpath=artifact_relpath,
                artifact_hash=artifact_hash,
            ) from exc

    def cleanup_rolled_back_candidate(
        self,
        *,
        db,
        runner,
        artifact_store,
        recovery_id: str,
    ) -> dict:
        """仅在 ROLLED_BACK 证据仍有效时删除干净候选和其私有资源。"""
        recovery = db.get_failure_recovery(recovery_id)
        if not recovery or recovery.get("status") != "ROLLED_BACK":
            raise WorkspaceOperationError("rollback_not_verified")
        workspace_id = recovery.get("workspace_id")
        lease = db.get_worktree_lease(workspace_id)
        if not lease:
            raise WorkspaceOperationError("worktree_lease_missing")
        if lease["cleanup_status"] == "SUCCEEDED":
            return lease
        if lease["lease_status"] not in {"PRESERVED", "FAILED", "REJECTED"}:
            raise WorkspaceOperationError("worktree_not_cleanup_ready")
        try:
            payload = artifact_store.read_bytes(recovery["result_artifact_relpath"])
            if _sha256(payload) != recovery["result_artifact_hash"]:
                raise WorkspaceOperationError("rollback_artifact_hash_mismatch")
            document = json.loads(payload.decode("utf-8"))
            if (
                document.get("recovery_id") != recovery_id
                or document.get("workspace_id") != workspace_id
                or document.get("status") != "ROLLED_BACK"
            ):
                raise WorkspaceOperationError("rollback_artifact_identity_mismatch")
            self._assert_runner_stopped(runner, workspace_id)

            root = Path(lease["git_root"]).resolve(strict=True)
            expected = self._repository_managed_root(root) / workspace_id
            actual = Path(lease["worktree_path"]).resolve(strict=False)
            if actual != expected or lease["branch_name"] != WORKTREE_BRANCH_PREFIX + workspace_id:
                raise WorkspaceOperationError("worktree_cleanup_identity_mismatch")
            if actual.exists():
                if _is_link_or_reparse_point(actual) or not actual.is_dir():
                    raise WorkspaceOperationError("unmanaged_worktree_path")
                audit = self.inspect_candidate(lease)
                if audit.changes or audit.violations or audit.tracked_diff:
                    raise WorkspaceOperationError("rolled_back_candidate_not_clean")
            elif lease["lease_status"] != "REJECTED":
                raise WorkspaceOperationError("worktree_missing_before_cleanup")

            if lease["lease_status"] != "REJECTED":
                lease = db.transition_worktree_lease(workspace_id, status="REJECTED")
            if actual.exists():
                self._git_bytes(root, "worktree", "remove", str(actual))
            if self._git_ref_exists(root, lease["branch_name"]):
                branch_head = self._git_text(
                    root, "rev-parse", "--verify", lease["branch_name"]
                ).strip().lower()
                if branch_head != lease["base_commit"]:
                    raise WorkspaceOperationError("worktree_branch_head_changed")
                self._git_bytes(root, "branch", "-d", lease["branch_name"])
            task_temp = expected.parent / ".runtime" / workspace_id
            if task_temp.exists():
                if (
                    task_temp.parent != expected.parent / ".runtime"
                    or _is_link_or_reparse_point(task_temp)
                    or not task_temp.is_dir()
                ):
                    raise WorkspaceOperationError("unmanaged_task_temp_path")
                shutil.rmtree(task_temp)
            return db.set_worktree_cleanup_status(
                workspace_id, cleanup_status="SUCCEEDED"
            )
        except Exception as exc:
            try:
                db.set_worktree_cleanup_status(
                    workspace_id,
                    cleanup_status="FAILED",
                    failure_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            raise

    def discard(self, db, workspace_id: str) -> dict:
        raise WorkspaceOperationError(
            "unsafe_discard_api_disabled",
            "use RecoveryController.discard_worktree for verified rollback and cleanup",
        )

    def _assert_runner_stopped(self, runner, workspace_id: str) -> None:
        checker = getattr(runner, "has_active_processes", None)
        if not callable(checker):
            raise WorktreeRollbackError(
                "runner_state_unavailable", status="ROLLBACK_SKIPPED"
            )
        try:
            active = checker(workspace_id)
        except Exception as exc:
            raise WorktreeRollbackError(
                "runner_state_unavailable",
                f"{type(exc).__name__}: {exc}",
                status="ROLLBACK_SKIPPED",
            ) from exc
        if active:
            raise WorktreeRollbackError(
                "runner_still_active", status="ROLLBACK_SKIPPED"
            )

    def _verify_frozen_candidate(
        self,
        *,
        context: WorkspaceExecutionContext,
        lease: dict,
        artifact_store,
    ) -> tuple[CandidateAudit, dict]:
        prefix = f"{lease['run_id']}/worktrees/{lease['workspace_id']}"
        if (
            lease.get("diff_relpath") != f"{prefix}/candidate.diff"
            or lease.get("change_manifest_relpath") != f"{prefix}/change-manifest.json"
            or not lease.get("diff_hash")
            or not lease.get("change_manifest_hash")
        ):
            raise WorktreeRollbackError("candidate_artifact_reference_invalid")
        try:
            diff_payload = artifact_store.read_bytes(lease["diff_relpath"])
            manifest_payload = artifact_store.read_bytes(
                lease["change_manifest_relpath"]
            )
        except Exception as exc:
            raise WorktreeRollbackError(
                "candidate_artifact_unavailable", str(exc)
            ) from exc
        if (
            _sha256(diff_payload) != lease["diff_hash"]
            or _sha256(manifest_payload) != lease["change_manifest_hash"]
        ):
            raise WorktreeRollbackError("candidate_artifact_hash_mismatch")
        try:
            manifest = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorktreeRollbackError("candidate_manifest_invalid") from exc
        if (
            manifest.get("workspace_id") != lease["workspace_id"]
            or manifest.get("run_id") != lease["run_id"]
            or manifest.get("base_commit") != lease["base_commit"]
            or manifest.get("branch_name") != lease["branch_name"]
            or manifest.get("write_scope") != list(lease["write_scope"])
            or manifest.get("tracked_diff_sha256") != _sha256(diff_payload)
            or not isinstance(manifest.get("changes"), list)
            or manifest.get("violations") != []
        ):
            raise WorktreeRollbackError("candidate_manifest_identity_mismatch")
        audit = self.audit_candidate(context)
        if audit.violations:
            raise WorktreeRollbackError(
                "candidate_scope_or_type_violation",
                details={"violations": list(audit.violations)},
            )
        if (
            list(audit.changes) != manifest["changes"]
            or audit.tracked_diff != diff_payload
        ):
            raise WorktreeRollbackError(
                "candidate_changed_after_finalization",
                details={"current_changes": list(audit.changes)},
            )
        return audit, manifest

    def _validate_rollback_paths(
        self,
        context: WorkspaceExecutionContext,
        changes: Sequence[dict],
    ) -> tuple[str, ...]:
        paths: set[str] = set()
        for change in changes:
            for key in ("path", "old_path"):
                value = change.get(key)
                if value is None:
                    continue
                normalized, _ = _normalize_relative_path(value, scope=False)
                if not _path_matches_scope(normalized, context.write_scope):
                    raise WorktreeRollbackError("rollback_path_outside_scope")
                _assert_safe_candidate_path(context.workspace_root, normalized)
                paths.add(normalized)
        return tuple(sorted(paths))

    def _partition_rollback_paths(
        self,
        root: Path,
        base_commit: str,
        paths: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        base_paths: list[str] = []
        new_paths: list[str] = []
        for path in paths:
            entry = self._git_bytes(
                root, "ls-tree", "-z", "--full-tree", base_commit, "--", path
            )
            if not entry:
                new_paths.append(path)
                continue
            records = [record for record in entry.split(b"\0") if record]
            if len(records) != 1 or b"\t" not in records[0]:
                raise WorktreeRollbackError("base_path_identity_invalid")
            metadata, raw_path = records[0].split(b"\t", 1)
            mode_type = metadata.split()
            decoded_path = raw_path.decode("utf-8", errors="surrogateescape")
            if (
                decoded_path != path
                or len(mode_type) < 2
                or mode_type[1] != b"blob"
                or mode_type[0] == b"120000"
            ):
                raise WorktreeRollbackError("base_path_type_unsupported")
            base_paths.append(path)
        return tuple(base_paths), tuple(new_paths)

    def _restore_candidate_paths(
        self,
        root: Path,
        base_commit: str,
        base_paths: Sequence[str],
        new_paths: Sequence[str],
    ) -> None:
        for batch in _path_batches(base_paths):
            self._git_bytes(
                root,
                "restore", f"--source={base_commit}", "--staged", "--worktree",
                "--", *batch,
            )
        for batch in _path_batches(new_paths):
            self._git_bytes(
                root, "rm", "--cached", "-f", "--ignore-unmatch", "--", *batch
            )
        for path in new_paths:
            _remove_regular_candidate_file(root, path)

    def _main_workspace_fingerprint(self, root: Path) -> str:
        digest = hashlib.sha256()
        for payload in (
            self._git_bytes(root, "rev-parse", "--verify", "HEAD"),
            self._git_bytes(
                root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            ),
            self._git_bytes(root, "diff", "--binary", "--full-index", "HEAD", "--"),
        ):
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        untracked = self._git_bytes(
            root, "ls-files", "--others", "--exclude-standard", "-z"
        )
        for raw_path in sorted(item for item in untracked.split(b"\0") if item):
            digest.update(raw_path)
            path = raw_path.decode("utf-8", errors="surrogateescape")
            target = root.joinpath(*path.split("/"))
            try:
                payload = target.read_bytes() if target.is_file() else b"<unsafe>"
            except OSError:
                payload = b"<unreadable>"
            digest.update(_sha256(payload).encode("ascii"))
        return digest.hexdigest()

    def _git_ref_exists(self, root: Path, branch_name: str) -> bool:
        try:
            completed = subprocess.run(
                [
                    self.git_executable, "-C", str(root), "show-ref", "--verify",
                    "--quiet", f"refs/heads/{branch_name}",
                ],
                capture_output=True,
                check=False,
                timeout=self.probe_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceOperationError("git_ref_query_failed", str(exc)) from exc
        if completed.returncode not in {0, 1}:
            raise WorkspaceOperationError("git_ref_query_failed")
        return completed.returncode == 0

    def reconcile(self, db, *, artifact_store=None) -> dict[str, int]:
        """启动时关闭遗留 active lease，绝不自动删除或重跑。"""
        counts = {"failed": 0, "preserved": 0}
        for lease in db.list_active_worktree_leases():
            status = lease["lease_status"]
            if status not in {"PROVISIONING", "READY", "RUNNING"}:
                continue
            path_exists = Path(lease["worktree_path"]).is_dir()
            if status == "RUNNING" and path_exists:
                try:
                    if artifact_store is None:
                        db.transition_worktree_lease(
                            lease["workspace_id"], status="PRESERVED"
                        )
                        counts["preserved"] += 1
                        continue
                    context = self._context_from_lease(lease)
                    context.db = db
                    context.artifact_store = artifact_store
                    audit = self.audit_candidate(context)
                    refs = self.persist_candidate_audit(context, audit)
                    if audit.violations:
                        db.transition_worktree_lease(
                            lease["workspace_id"],
                            status="FAILED",
                            failure_code="scope_violation",
                            failure_message="; ".join(audit.violations)[:500],
                            **refs,
                        )
                        counts["failed"] += 1
                    else:
                        db.transition_worktree_lease(
                            lease["workspace_id"], status="PRESERVED", **refs
                        )
                        counts["preserved"] += 1
                except Exception as exc:
                    current = db.get_worktree_lease(lease["workspace_id"])
                    if current and current["lease_status"] == "RUNNING":
                        db.transition_worktree_lease(
                            lease["workspace_id"],
                            status="FAILED",
                            failure_code="candidate_reconcile_failed",
                            failure_message=f"{type(exc).__name__}: {exc}"[:500],
                        )
                    counts["failed"] += 1
            else:
                db.transition_worktree_lease(
                    lease["workspace_id"],
                    status="FAILED",
                    failure_code="runtime_restart",
                    failure_message="Runtime restarted before Worktree setup or execution completed",
                )
                counts["failed"] += 1
        return counts

    def inspect_git_workspace(
        self,
        working_directory: str | Path,
        *,
        repository_busy: bool = False,
    ) -> GitWorkspaceInspection:
        """只读检查仓库；返回全部已确认的拒绝原因，不修改现场。"""
        requested = Path(working_directory).expanduser().resolve(strict=False)
        failures: list[GitGateFailure] = []
        git_root: Path | None = None
        git_dir: Path | None = None
        head_commit: str | None = None
        version: tuple[int, int, int] | None = None

        if not requested.is_dir():
            return GitWorkspaceInspection(
                False,
                requested,
                None,
                None,
                None,
                None,
                (GitGateFailure(
                    "working_directory_unavailable",
                    "working directory does not exist or is not a directory",
                ),),
            )

        try:
            version_output = self._git_text(None, "--version")
            version = parse_git_version(version_output)
            if version < self.minimum_git_version:
                failures.append(GitGateFailure(
                    "git_version_unsupported",
                    "Git version is older than the configured minimum",
                ))
        except _GitProbeError as exc:
            return GitWorkspaceInspection(
                False,
                requested,
                None,
                None,
                None,
                None,
                (GitGateFailure(exc.reason_code, str(exc)),),
            )
        except WorkspaceValidationError as exc:
            return GitWorkspaceInspection(
                False,
                requested,
                None,
                None,
                None,
                None,
                (GitGateFailure(exc.reason_code, str(exc)),),
            )

        try:
            bare = self._git_text(requested, "rev-parse", "--is-bare-repository").strip()
        except _GitProbeError as exc:
            return GitWorkspaceInspection(
                False,
                requested,
                None,
                None,
                None,
                version,
                (GitGateFailure("not_git_repository", str(exc)),),
            )
        if bare == "true":
            failures.append(GitGateFailure(
                "bare_repository_unsupported",
                "bare Git repositories cannot host a write Worktree task",
            ))
            return GitWorkspaceInspection(
                False, requested, None, None, None, version, tuple(failures)
            )

        try:
            git_root = Path(
                self._git_text(requested, "rev-parse", "--show-toplevel").strip()
            ).resolve(strict=False)
            raw_git_dir = Path(
                self._git_text(git_root, "rev-parse", "--absolute-git-dir").strip()
            )
            git_dir = raw_git_dir.resolve(strict=False)
        except _GitProbeError as exc:
            failures.append(GitGateFailure("not_git_repository", str(exc)))
            return GitWorkspaceInspection(
                False, requested, None, None, None, version, tuple(failures)
            )

        if not git_root.is_dir() or not _is_within(requested, git_root):
            failures.append(GitGateFailure(
                "not_git_repository",
                "working directory is not inside the resolved Git root",
            ))
        if not (git_root / ".git").is_dir():
            failures.append(GitGateFailure(
                "linked_source_worktree_unsupported",
                "the source must be the primary repository Worktree",
            ))
        if _is_link_or_reparse_point(git_root):
            failures.append(GitGateFailure(
                "linked_workspace_root",
                "Git root must not itself be a symbolic link or junction",
            ))

        try:
            head_commit = self._git_text(git_root, "rev-parse", "--verify", "HEAD").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_commit):
                raise _GitProbeError("head_unavailable", "Git HEAD is not a full object id")
            head_commit = head_commit.lower()
        except _GitProbeError as exc:
            failures.append(GitGateFailure("head_unavailable", str(exc)))

        status = b""
        try:
            status = self._git_bytes(
                git_root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
            if status:
                failures.append(GitGateFailure(
                    "workspace_dirty",
                    "tracked, staged, or untracked changes are present",
                ))
        except _GitProbeError as exc:
            failures.append(GitGateFailure("git_status_failed", str(exc)))

        operation_markers = (
            ("MERGE_HEAD", "merge"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
        )
        for marker, operation in operation_markers:
            marker_path = self._git_path(git_root, marker)
            if marker_path is not None and marker_path.exists():
                failures.append(GitGateFailure(
                    "git_operation_in_progress",
                    f"Git {operation} operation is in progress",
                ))
                break
        lock_path = self._git_path(git_root, "index.lock")
        if lock_path is not None and lock_path.exists():
            failures.append(GitGateFailure(
                "git_index_locked",
                "Git index.lock exists",
            ))

        index = b""
        try:
            index = self._git_bytes(git_root, "ls-files", "--stage", "-z")
            if any(entry.startswith(b"160000 ") for entry in index.split(b"\0")):
                failures.append(GitGateFailure(
                    "submodule_unsupported",
                    "Git submodules are not supported by the first Worktree release",
                ))
        except _GitProbeError as exc:
            failures.append(GitGateFailure("git_index_unavailable", str(exc)))

        try:
            visibility = self._git_bytes(git_root, "ls-files", "-v", "-z")
            if _index_has_hidden_paths(visibility):
                failures.append(GitGateFailure(
                    "index_visibility_flags_unsupported",
                    "assume-unchanged or skip-worktree paths would make auditing incomplete",
                ))
        except _GitProbeError as exc:
            failures.append(GitGateFailure(
                "git_index_visibility_unavailable", str(exc)
            ))

        if index and self._tracked_attributes_use_lfs(git_root, index):
            failures.append(GitGateFailure(
                "git_lfs_unsupported",
                "Git LFS attributes require an unsupported materialization strategy",
            ))

        if _status_contains_nested_repository(git_root, status):
            failures.append(GitGateFailure(
                "nested_repository_unsupported",
                "an untracked nested Git repository is present",
            ))

        try:
            self._git_bytes(git_root, "worktree", "list", "--porcelain")
        except _GitProbeError as exc:
            failures.append(GitGateFailure("git_worktree_unavailable", str(exc)))

        if repository_busy:
            failures.append(GitGateFailure(
                "repository_busy",
                "repository is already in a managed integration or cleanup operation",
            ))

        failures = _deduplicate_failures(failures)
        return GitWorkspaceInspection(
            not failures,
            requested,
            git_root,
            git_dir,
            head_commit,
            version,
            tuple(failures),
        )

    def _repository_managed_root(self, git_root: Path) -> Path:
        normalized = os.path.normcase(str(git_root.resolve(strict=False))).encode(
            "utf-8", errors="surrogatepass"
        )
        fingerprint = hashlib.sha256(normalized).hexdigest()[:20]
        return self.managed_root / fingerprint

    def _validate_managed_paths(
        self, git_root: Path, workspace_path: Path, task_temp: Path
    ) -> None:
        root = git_root.resolve(strict=True)
        managed = self.managed_root.resolve(strict=False)
        for candidate in (managed, workspace_path.resolve(strict=False), task_temp.resolve(strict=False)):
            if _is_within(candidate, root):
                raise WorkspaceOperationError(
                    "managed_path_inside_repository",
                    "managed Worktree paths must remain outside the source repository",
                )
        if workspace_path.exists() or task_temp.exists():
            raise WorkspaceOperationError("managed_path_already_exists")

    @staticmethod
    def _ensure_managed_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        current = path
        while True:
            if _is_link_or_reparse_point(current) or not current.is_dir():
                raise WorkspaceOperationError("managed_directory_is_linked")
            if current == current.parent:
                break
            current = current.parent

    def _assert_context_identity(
        self, context: WorkspaceExecutionContext, root: Path
    ) -> None:
        lease = context.db.get_worktree_lease(context.workspace_id) if context.db else None
        if lease:
            if (
                Path(lease["worktree_path"]).resolve(strict=False) != root
                or lease["branch_name"] != context.branch_name
                or lease["base_commit"] != context.base_commit
                or tuple(lease["write_scope"]) != context.write_scope
            ):
                raise WorkspaceOperationError("worktree_lease_identity_mismatch")
        actual_head = self._git_text(root, "rev-parse", "--verify", "HEAD").strip().lower()
        actual_branch = self._git_text(root, "branch", "--show-current").strip()
        if actual_head != context.base_commit or actual_branch != context.branch_name:
            raise WorkspaceOperationError("worktree_identity_changed")

    def _context_from_lease(self, lease: dict) -> WorkspaceExecutionContext:
        root = Path(lease["worktree_path"]).resolve(strict=True)
        expected = self._repository_managed_root(
            Path(lease["git_root"]).resolve(strict=True)
        ) / lease["workspace_id"]
        if root != expected:
            raise WorkspaceOperationError("unmanaged_worktree_path")
        return WorkspaceExecutionContext(
            manager=self,
            db=None,
            runner=None,
            artifact_store=None,
            workspace_id=lease["workspace_id"],
            run_id=lease["run_id"],
            workspace_root=root,
            task_temp_root=expected.parent / ".runtime" / lease["workspace_id"],
            write_scope=tuple(lease["write_scope"]),
            base_commit=lease["base_commit"],
            branch_name=lease["branch_name"],
        )

    @staticmethod
    def validate_write_scope(
        values: Sequence[str],
        *,
        workspace_root: str | Path | None = None,
    ) -> tuple[str, ...]:
        return normalize_write_scope(values, workspace_root=workspace_root)

    @staticmethod
    def resolve_workspace_path(
        workspace_root: str | Path,
        requested_path: str,
        *,
        write_scope: Sequence[str] | None = None,
        require_write: bool = False,
        allow_root: bool = False,
    ) -> Path:
        """将模型路径限制到一个真实工作区，并同时执行写入范围检查。"""
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not root.is_dir() or _is_link_or_reparse_point(root):
            raise WorkspaceValidationError(
                "workspace_root_unavailable",
                "workspace root must be a real, non-linked directory",
            )

        if requested_path == "." and allow_root and not require_write:
            return root
        normalized, _ = _normalize_relative_path(requested_path, scope=False)
        lexical_target = root.joinpath(*normalized.split("/"))
        resolved_target = lexical_target.resolve(strict=False)
        if not _is_within(resolved_target, root):
            raise WorkspaceValidationError(
                "path_outside_workspace",
                "resolved path escapes the Worktree root",
            )

        resolved_relative = resolved_target.relative_to(root).as_posix()
        if _contains_forbidden_part(resolved_relative):
            raise WorkspaceValidationError(
                "protected_path",
                "Git metadata and Runtime-private paths are not accessible",
            )

        if require_write:
            if not write_scope:
                raise WorkspaceValidationError(
                    "write_scope_required",
                    "write operations require a frozen write_scope",
                )
            frozen = normalize_write_scope(write_scope, workspace_root=root)
            if not _path_matches_scope(normalized, frozen):
                raise WorkspaceValidationError(
                    "path_outside_write_scope",
                    "requested path is outside the frozen write_scope",
                )
            if not _path_matches_scope(resolved_relative, frozen):
                raise WorkspaceValidationError(
                    "resolved_path_outside_write_scope",
                    "resolved target escapes the frozen write_scope through a link",
                )
        return resolved_target

    def _tracked_attributes_use_lfs(self, git_root: Path, index: bytes) -> bool:
        for entry in index.split(b"\0"):
            if not entry or b"\t" not in entry:
                continue
            raw_path = entry.split(b"\t", 1)[1]
            path_text = raw_path.decode("utf-8", errors="surrogateescape")
            if Path(path_text).name != ".gitattributes":
                continue
            candidate = git_root.joinpath(*path_text.split("/"))
            resolved = candidate.resolve(strict=False)
            if not _is_within(resolved, git_root) or not resolved.is_file():
                continue
            try:
                if _LFS_ATTRIBUTE.search(resolved.read_bytes()):
                    return True
            except OSError:
                return True
        return False

    def _git_path(self, git_root: Path, name: str) -> Path | None:
        try:
            value = self._git_text(git_root, "rev-parse", "--git-path", name).strip()
        except _GitProbeError:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = git_root / path
        return path.resolve(strict=False)

    def _git_text(self, cwd: Path | None, *args: str) -> str:
        return self._git_bytes(cwd, *args).decode("utf-8", errors="replace")

    def _git_bytes(self, cwd: Path | None, *args: str) -> bytes:
        completed = self._git_completed(cwd, *args)
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise _GitProbeError(
                "git_command_failed", message[:300] or "Git command failed"
            )
        return completed.stdout

    def _git_bytes_input(
        self, cwd: Path | None, input_bytes: bytes, *args: str
    ) -> bytes:
        completed = self._git_completed(cwd, *args, input_bytes=input_bytes)
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise _GitProbeError(
                "git_command_failed", message[:300] or "Git command failed"
            )
        return completed.stdout

    def _git_completed(
        self,
        cwd: Path | None,
        *args: str,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        command = [self.git_executable]
        if cwd is not None:
            command.extend(("-C", str(cwd)))
        command.extend(args)
        try:
            completed = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=self.probe_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise _GitProbeError("git_unavailable", "Git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise _GitProbeError("git_probe_timed_out", "Git probe timed out") from exc
        except OSError as exc:
            raise _GitProbeError("git_unavailable", str(exc)[:200]) from exc
        return completed

    def _integration_conflicts(self, root: Path) -> tuple[str, ...]:
        completed = self._git_completed(
            root, "diff", "--name-only", "--diff-filter=U", "-z"
        )
        if completed.returncode != 0:
            return ()
        return tuple(sorted(
            item.decode("utf-8", errors="replace")
            for item in completed.stdout.split(b"\0") if item
        ))

    def _abort_merge(self, root: Path) -> None:
        marker = self._git_path(root, "MERGE_HEAD")
        if marker is None or not marker.exists():
            return
        completed = self._git_completed(root, "merge", "--abort")
        if completed.returncode != 0:
            raise WorkspaceIntegrationError(
                "merge_abort_failed",
                completed.stderr.decode("utf-8", errors="replace")[:300],
            )


def _path_batches(paths: Sequence[str], size: int = 100) -> Iterable[tuple[str, ...]]:
    for offset in range(0, len(paths), size):
        yield tuple(paths[offset:offset + size])


def _assert_safe_candidate_path(root: Path, relative_path: str) -> None:
    """拒绝候选路径中任一符号链接、junction、目录叶子或越界解析。"""
    current = root
    parts = relative_path.split("/")
    for index, part in enumerate(parts):
        current = current / part
        exists = current.exists() or current.is_symlink()
        if not exists:
            continue
        if _is_link_or_reparse_point(current):
            raise WorktreeRollbackError("rollback_path_is_linked")
        if index < len(parts) - 1 and not current.is_dir():
            raise WorktreeRollbackError("rollback_path_parent_not_directory")
        if index == len(parts) - 1 and not current.is_file():
            raise WorktreeRollbackError("rollback_path_type_unsupported")
    if not _is_within(current.resolve(strict=False), root.resolve(strict=True)):
        raise WorktreeRollbackError("rollback_path_escape")


def _remove_regular_candidate_file(root: Path, relative_path: str) -> None:
    _assert_safe_candidate_path(root, relative_path)
    target = root.joinpath(*relative_path.split("/"))
    if target.exists() or target.is_symlink():
        if _is_link_or_reparse_point(target) or not target.is_file():
            raise WorkspaceOperationError("rollback_remove_type_unsupported")
        target.unlink()
    parent = target.parent
    while parent != root:
        if _is_link_or_reparse_point(parent) or not parent.is_dir():
            raise WorkspaceOperationError("rollback_parent_type_changed")
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def parse_git_version(value: str) -> tuple[int, int, int]:
    match = _GIT_VERSION.search(value)
    if not match:
        raise WorkspaceValidationError("git_version_unrecognized")
    return tuple(int(item or 0) for item in match.groups())


def normalize_write_scope(
    values: Sequence[str],
    *,
    workspace_root: str | Path | None = None,
) -> tuple[str, ...]:
    """校验、排序并折叠重复的文件或目录范围。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise WorkspaceValidationError(
            "write_scope_required",
            "write_scope must be a non-empty list of relative paths",
        )
    root = None
    if workspace_root is not None:
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not root.is_dir() or _is_link_or_reparse_point(root):
            raise WorkspaceValidationError("workspace_root_unavailable")

    normalized: list[str] = []
    for value in values:
        path, is_directory = _normalize_relative_path(value, scope=True)
        if root is not None:
            target = root.joinpath(*path.split("/")).resolve(strict=False)
            if not _is_within(target, root):
                raise WorkspaceValidationError(
                    "scope_link_escape",
                    f"write_scope path resolves outside the workspace: {value!r}",
                )
            lexical = root.joinpath(*path.split("/"))
            if lexical.exists():
                if is_directory and not lexical.is_dir():
                    raise WorkspaceValidationError(
                        "directory_scope_is_not_directory",
                        f"directory write_scope points to a file: {value!r}",
                    )
                if not is_directory and lexical.is_dir():
                    raise WorkspaceValidationError(
                        "directory_scope_requires_trailing_slash",
                        f"directory write_scope must end with '/': {value!r}",
                    )
        normalized.append(path + ("/" if is_directory else ""))

    collapsed: list[str] = []
    candidates = sorted(
        set(normalized),
        key=lambda item: (
            item[:-1] if item.endswith("/") else item,
            0 if item.endswith("/") else 1,
        ),
    )
    for candidate in candidates:
        plain = candidate[:-1] if candidate.endswith("/") else candidate
        if any(_scope_contains(existing, plain) for existing in collapsed):
            continue
        collapsed.append(candidate)
    return tuple(collapsed)


def _normalize_relative_path(value: str, *, scope: bool) -> tuple[str, bool]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkspaceValidationError("invalid_relative_path")
    if (
        value.startswith(("/", "~", "\\\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise WorkspaceValidationError("absolute_path_forbidden")
    if "\x00" in value or "\\" in value or ":" in value:
        raise WorkspaceValidationError("invalid_relative_path")
    if any(character in value for character in _GLOB_CHARACTERS):
        raise WorkspaceValidationError("wildcard_path_forbidden")

    is_directory = scope and value.endswith("/")
    body = value[:-1] if is_directory else value
    parts = body.split("/")
    if not body or any(part in ("", ".", "..") for part in parts):
        raise WorkspaceValidationError("path_traversal_forbidden")
    if any(part.lower() in _FORBIDDEN_PATH_PARTS for part in parts):
        raise WorkspaceValidationError("protected_path")
    return "/".join(parts), is_directory


def _scope_contains(scope: str, relative_path: str) -> bool:
    if scope.endswith("/"):
        prefix = scope[:-1]
        return relative_path == prefix or relative_path.startswith(prefix + "/")
    return relative_path == scope


def _path_matches_scope(path: str, scopes: Iterable[str]) -> bool:
    return any(_scope_contains(scope, path) for scope in scopes)


def _contains_forbidden_part(path: str) -> bool:
    return any(part.lower() in _FORBIDDEN_PATH_PARTS for part in path.split("/"))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        )
    except OSError:
        return True


def _status_contains_nested_repository(git_root: Path, status: bytes) -> bool:
    for entry in status.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        relative = entry[3:].decode("utf-8", errors="surrogateescape").rstrip("/")
        candidate = git_root.joinpath(*relative.split("/"))
        if candidate.is_dir() and (candidate / ".git").exists():
            return True
    return False


def _parse_candidate_status(status: bytes, root: Path) -> tuple[list[dict], list[str]]:
    entries = status.split(b"\0")
    changes: list[dict] = []
    violations: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            violations.append("unrecognized Git status entry")
            continue
        code = entry[:2].decode("ascii", errors="replace")
        path = entry[3:].decode("utf-8", errors="surrogateescape")
        old_path = None
        if "R" in code or "C" in code:
            if index >= len(entries) or not entries[index]:
                violations.append("incomplete Git rename status")
                continue
            old_path = entries[index].decode("utf-8", errors="surrogateescape")
            index += 1
        try:
            normalized, _ = _normalize_relative_path(path, scope=False)
            normalized_old = None
            if old_path is not None:
                normalized_old, _ = _normalize_relative_path(old_path, scope=False)
        except (UnicodeError, WorkspaceValidationError):
            violations.append("unsafe path in Git status")
            continue
        change, unsafe = _candidate_file_record(
            root,
            normalized,
            _candidate_status_name(code),
            code,
            old_path=normalized_old,
        )
        if unsafe:
            violations.append(unsafe)
        changes.append(change)
    return changes, violations


def _candidate_file_record(
    root: Path,
    path: str,
    status: str,
    git_status: str,
    *,
    old_path: str | None = None,
) -> tuple[dict, str | None]:
    """Build one auditable change record without following unsafe file types."""
    change = {
        "status": status,
        "git_status": git_status,
        "path": path,
    }
    if old_path is not None:
        change["old_path"] = old_path

    target = root.joinpath(*path.split("/"))
    if not (target.exists() or target.is_symlink()):
        return change, None
    try:
        if target.is_symlink() or not target.is_file():
            return change, f"unsafe changed file type: {path}"
        payload = target.read_bytes()
    except OSError:
        return change, f"changed file is unreadable: {path}"
    change["sha256"] = _sha256(payload)
    change["size"] = len(payload)
    return change, None


def _candidate_status_name(code: str) -> str:
    if code == "??":
        return "untracked"
    if "R" in code:
        return "renamed"
    if "C" in code:
        return "copied"
    if "D" in code:
        return "deleted"
    if "A" in code:
        return "added"
    return "modified"


def _index_has_hidden_paths(visibility: bytes) -> bool:
    return any(
        entry
        and (chr(entry[0]).islower() or entry[:1] == b"S")
        for entry in visibility.split(b"\0")
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _deduplicate_failures(failures: Iterable[GitGateFailure]) -> list[GitGateFailure]:
    result: list[GitGateFailure] = []
    seen: set[str] = set()
    for failure in failures:
        if failure.reason_code in seen:
            continue
        seen.add(failure.reason_code)
        result.append(failure)
    return result
