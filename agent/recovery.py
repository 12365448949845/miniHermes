"""Deterministic failure classification, recovery audit, and rollback control."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping


class FailureClass(str, Enum):
    CONTROL = "CONTROL"
    SECURITY = "SECURITY"
    INPUT = "INPUT"
    CONFIGURATION = "CONFIGURATION"
    PRECONDITION = "PRECONDITION"
    CODE_EXECUTION = "CODE_EXECUTION"
    TRANSIENT = "TRANSIENT"
    RESOURCE = "RESOURCE"
    PARTIAL_WRITE = "PARTIAL_WRITE"
    INTERNAL_UNKNOWN = "INTERNAL_UNKNOWN"


class RecoveryAction(str, Enum):
    RETRY = "RETRY"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    ROLLBACK = "ROLLBACK"
    STOP = "STOP"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ErrorDefinition:
    failure_class: FailureClass
    default_action: RecoveryAction


@dataclass(frozen=True)
class ToolFailure:
    tool_name: str
    tool_status: str
    source_error_code: str
    error_code: str
    failure_class: FailureClass
    registered_error: bool
    retryable: bool
    side_effect: str
    idempotency: str
    attempts: int
    side_effects_possible: bool
    workspace_id: str | None = None

    @property
    def retry_eligible(self) -> bool:
        return (
            self.retryable
            and self.side_effect == "none"
            and self.idempotency == "idempotent"
            and self.failure_class in {
                FailureClass.TRANSIENT,
                FailureClass.RESOURCE,
            }
        )


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    status: str
    reason: dict
    attempt_number: int = 0
    max_attempts: int = 0


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _definitions(
    failure_class: FailureClass,
    action: RecoveryAction,
    *codes: str,
) -> dict[str, ErrorDefinition]:
    definition = ErrorDefinition(failure_class, action)
    return {code: definition for code in codes}


ERROR_CODE_REGISTRY: Mapping[str, ErrorDefinition] = {
    **_definitions(
        FailureClass.CONTROL,
        RecoveryAction.STOP,
        "cancelled",
        "user_interrupt",
        "parent_cancelled",
        "deadline_exceeded",
        "timed_out",
        "process_restarted",
        "runtime_shutdown",
    ),
    **_definitions(
        FailureClass.SECURITY,
        RecoveryAction.STOP,
        "approval_denied",
        "approval_rejected",
        "hardline_blocked",
        "policy_denied",
        "tool_not_allowed",
    ),
    **_definitions(
        FailureClass.INPUT,
        RecoveryAction.REPAIR_REQUIRED,
        "invalid_arguments",
        "path_invalid",
        "path_outside_scope",
        "path_outside_write_scope",
        "absolute_path_forbidden",
        "path_traversal_forbidden",
        "protected_path",
    ),
    **_definitions(
        FailureClass.CONFIGURATION,
        RecoveryAction.STOP,
        "authentication_failed",
        "permission_denied",
        "missing_configuration",
        "quota_exceeded",
        "docker_image_required",
        "docker_image_unavailable",
    ),
    **_definitions(
        FailureClass.PRECONDITION,
        RecoveryAction.REPAIR_REQUIRED,
        "file_not_found",
        "dependency_missing",
        "command_not_found",
        "tool_not_found",
    ),
    **_definitions(
        FailureClass.CODE_EXECUTION,
        RecoveryAction.REPAIR_REQUIRED,
        "nonzero_exit",
        "test_failed",
        "build_failed",
        "syntax_error",
    ),
    **_definitions(
        FailureClass.TRANSIENT,
        RecoveryAction.RETRY,
        "network_transient",
        "rate_limited",
        "timeout",
    ),
    **_definitions(
        FailureClass.RESOURCE,
        RecoveryAction.RETRY,
        "resource_busy",
        "lock_conflict",
    ),
    **_definitions(
        FailureClass.PARTIAL_WRITE,
        RecoveryAction.STOP,
        "partial_write_possible",
        "runner_crashed",
        "runner_failed",
        "scope_violation",
        "candidate_audit_failed",
        "docker_unavailable",
        "docker_spawn_failed",
        "workspace_mount_unavailable",
    ),
    **_definitions(
        FailureClass.INTERNAL_UNKNOWN,
        RecoveryAction.STOP,
        "internal_error",
        "tool_internal_error",
        "permanent_failure",
        "unknown_failure",
    ),
}


def classify_tool_failure(
    *,
    tool_name: str,
    tool_status: str,
    error_code: object | None,
    retryable: bool,
    attempts: int,
    side_effects_possible: bool,
    side_effect: str = "unknown",
    idempotency: str = "unknown",
    workspace_id: str | None = None,
) -> ToolFailure:
    if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code):
        source_code = error_code
    else:
        source_code = "unregistered_error"
    definition = ERROR_CODE_REGISTRY.get(source_code)
    registered = definition is not None
    if definition is None:
        definition = ERROR_CODE_REGISTRY["unknown_failure"]
        normalized_code = "unknown_failure"
    else:
        normalized_code = source_code
    return ToolFailure(
        tool_name=str(tool_name or "unknown")[:128],
        tool_status=str(tool_status or "FAILED")[:32],
        source_error_code=source_code,
        error_code=normalized_code,
        failure_class=definition.failure_class,
        registered_error=registered,
        retryable=bool(retryable),
        side_effect=(
            side_effect if side_effect in {"none", "local", "external", "unknown"}
            else "unknown"
        ),
        idempotency=(
            idempotency
            if idempotency in {"idempotent", "non_idempotent", "unknown"}
            else "unknown"
        ),
        attempts=max(0, int(attempts or 0)),
        side_effects_possible=bool(side_effects_possible),
        workspace_id=workspace_id,
    )


class RecoveryPolicy:
    """Select an auditable E0 decision without executing recovery behavior."""

    def decide(self, failure: ToolFailure) -> RecoveryDecision:
        reason = {
            "contract_version": 1,
            "audit_only": True,
            "registered_error": failure.registered_error,
            "source_error_code": failure.source_error_code,
            "tool_status": failure.tool_status,
            "retryable": failure.retryable,
            "retry_eligible": failure.retry_eligible,
            "side_effect": failure.side_effect,
            "idempotency": failure.idempotency,
            "side_effects_possible": failure.side_effects_possible,
        }
        if failure.tool_status in {"DENIED", "CANCELLED"}:
            return RecoveryDecision(
                RecoveryAction.STOP, "NOT_APPLICABLE", reason
            )
        definition = ERROR_CODE_REGISTRY[failure.error_code]
        if definition.default_action == RecoveryAction.RETRY:
            if failure.retry_eligible:
                return RecoveryDecision(
                    RecoveryAction.RETRY,
                    "RETRY_EXHAUSTED",
                    reason,
                    attempt_number=failure.attempts,
                    max_attempts=failure.attempts,
                )
            return RecoveryDecision(
                RecoveryAction.STOP, "NOT_APPLICABLE", reason
            )
        if definition.default_action == RecoveryAction.REPAIR_REQUIRED:
            return RecoveryDecision(
                RecoveryAction.REPAIR_REQUIRED, "REPAIR_REQUIRED", reason
            )
        return RecoveryDecision(
            definition.default_action, "NOT_APPLICABLE", reason
        )


class RecoveryController:
    """统一记录工具恢复决策，并协调显式 Worktree 回滚。"""

    def __init__(
        self,
        db,
        *,
        policy: RecoveryPolicy | None = None,
        event_callback: Callable[[str, dict], None] | None = None,
    ):
        self.db = db
        self.policy = policy or RecoveryPolicy()
        self.event_callback = event_callback

    def discard_worktree(
        self,
        *,
        workspace_manager,
        runner,
        artifact_store,
        workspace_id: str,
    ) -> dict:
        """执行一次显式、可审计的 Worktree 回滚，并在成功后安全清理。"""
        from agent.worktree import (
            WorktreeRollbackError,
            WorkspaceOperationError,
        )

        lease = self.db.get_worktree_lease(workspace_id)
        if not lease:
            raise KeyError(f"unknown worktree lease: {workspace_id}")
        recovery, created = self.db.create_worktree_rollback_recovery(
            recovery_id=uuid.uuid4().hex,
            workspace_id=workspace_id,
            reason={
                "contract_version": 1,
                "recovery_mode": "explicit_worktree_discard",
                "automatic": False,
            },
        )
        if not created:
            if recovery["status"] == "ROLLED_BACK":
                return workspace_manager.cleanup_rolled_back_candidate(
                    db=self.db,
                    runner=runner,
                    artifact_store=artifact_store,
                    recovery_id=recovery["recovery_id"],
                )
            raise WorkspaceOperationError(
                "rollback_already_running",
                f"recovery {recovery['recovery_id']} is {recovery['status']}",
            )

        def mark_running(prepared):
            nonlocal recovery
            recovery = self.db.transition_failure_recovery(
                recovery["recovery_id"],
                status="ROLLBACK_RUNNING",
                expected_version=recovery["version"],
                result_artifact_relpath=prepared.artifact_relpath,
                result_artifact_hash=prepared.artifact_hash,
            )
            self._emit("rollback_started", {
                "recovery_id": recovery["recovery_id"],
                "workspace_id": workspace_id,
                "artifact_relpath": prepared.artifact_relpath,
            })

        try:
            result = workspace_manager.rollback_checkpoint(
                db=self.db,
                runner=runner,
                artifact_store=artifact_store,
                workspace_id=workspace_id,
                recovery_id=recovery["recovery_id"],
                on_prepared=mark_running,
            )
        except WorktreeRollbackError as exc:
            status = exc.status
            reason_code = exc.reason_code
            artifact_relpath = exc.artifact_relpath
            artifact_hash = exc.artifact_hash
            if artifact_relpath is None:
                try:
                    refused = workspace_manager.persist_rollback_result(
                        artifact_store=artifact_store,
                        lease=lease,
                        recovery_id=recovery["recovery_id"],
                        status=status,
                        reason_code=reason_code,
                        details={
                            **exc.details,
                            "phase": "PREFLIGHT_REFUSED",
                            "error": str(exc)[:500],
                        },
                    )
                    artifact_relpath = refused.artifact_relpath
                    artifact_hash = refused.artifact_hash
                except Exception:
                    status = "ROLLBACK_SKIPPED"
                    reason_code = "rollback_artifact_unavailable"
            recovery = self.db.get_failure_recovery(recovery["recovery_id"])
            if recovery["status"] == "PENDING":
                recovery = self.db.transition_failure_recovery(
                    recovery["recovery_id"],
                    status="ROLLBACK_RUNNING",
                    expected_version=recovery["version"],
                    result_artifact_relpath=artifact_relpath,
                    result_artifact_hash=artifact_hash,
                )
            if recovery["status"] == "ROLLBACK_RUNNING":
                recovery = self.db.transition_failure_recovery(
                    recovery["recovery_id"],
                    status=status,
                    expected_version=recovery["version"],
                    result_artifact_relpath=artifact_relpath,
                    result_artifact_hash=artifact_hash,
                    result_reason_code=reason_code,
                )
            event_type = (
                "rollback_conflict"
                if status == "ROLLBACK_CONFLICT"
                else "rollback_skipped"
            )
            self._emit(event_type, {
                "recovery_id": recovery["recovery_id"],
                "workspace_id": workspace_id,
                "reason_code": reason_code,
                "artifact_relpath": artifact_relpath,
            })
            raise WorkspaceOperationError(reason_code, str(exc)) from exc
        except Exception as exc:
            reason_code = "rollback_internal_error"
            recovery = self.db.get_failure_recovery(recovery["recovery_id"])
            if recovery["status"] == "PENDING":
                recovery = self.db.transition_failure_recovery(
                    recovery["recovery_id"],
                    status="ROLLBACK_RUNNING",
                    expected_version=recovery["version"],
                )
            if recovery["status"] == "ROLLBACK_RUNNING":
                recovery = self.db.transition_failure_recovery(
                    recovery["recovery_id"],
                    status="ROLLBACK_CONFLICT",
                    expected_version=recovery["version"],
                    result_reason_code=reason_code,
                )
            self._emit("rollback_conflict", {
                "recovery_id": recovery["recovery_id"],
                "workspace_id": workspace_id,
                "reason_code": reason_code,
            })
            raise WorkspaceOperationError(reason_code, str(exc)) from exc

        recovery = self.db.transition_failure_recovery(
            recovery["recovery_id"],
            status="ROLLED_BACK",
            expected_version=recovery["version"],
            result_artifact_relpath=result.artifact_relpath,
            result_artifact_hash=result.artifact_hash,
        )
        self._emit("rollback_succeeded", {
            "recovery_id": recovery["recovery_id"],
            "workspace_id": workspace_id,
            "artifact_relpath": result.artifact_relpath,
        })
        try:
            return workspace_manager.cleanup_rolled_back_candidate(
                db=self.db,
                runner=runner,
                artifact_store=artifact_store,
                recovery_id=recovery["recovery_id"],
            )
        except Exception as exc:
            self._emit("rollback_cleanup_failed", {
                "recovery_id": recovery["recovery_id"],
                "workspace_id": workspace_id,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
            raise

    def record_tool_failure(
        self,
        *,
        execution_id: str,
        result,
        metadata=None,
        node_run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict | None:
        execution = self.db.get_tool_execution(execution_id)
        if not execution:
            raise KeyError(f"unknown tool execution: {execution_id}")
        status = execution["status"]
        if status == "SUCCEEDED":
            return None
        failure = classify_tool_failure(
            tool_name=execution["tool_name"],
            tool_status=status,
            error_code=execution.get("error_code"),
            retryable=execution.get("retryable", False),
            attempts=execution.get("attempts", 0),
            side_effects_possible=getattr(
                result, "side_effects_possible", False
            ),
            side_effect=getattr(metadata, "side_effect", "unknown"),
            idempotency=getattr(metadata, "idempotency", "unknown"),
            workspace_id=workspace_id,
        )
        decision = self.policy.decide(failure)
        reason = dict(decision.reason)
        if decision.action == RecoveryAction.REPAIR_REQUIRED:
            reason.update({
                "audit_only": False,
                "recovery_mode": "repair_and_rerun",
            })
        record, created = self.db.create_initial_failure_recovery(
            recovery_id=uuid.uuid4().hex,
            run_id=execution["run_id"],
            node_run_id=node_run_id,
            tool_execution_id=execution_id,
            failure_class=failure.failure_class.value,
            error_code=failure.error_code,
            selected_action=decision.action.value,
            status=decision.status,
            attempt_number=decision.attempt_number,
            max_attempts=decision.max_attempts,
            workspace_id=workspace_id,
            reason=reason,
        )
        if created:
            self._emit("tool_failure_classified", {
                "recovery_id": record["recovery_id"],
                "execution_id": execution_id,
                "failure_class": failure.failure_class.value,
                "error_code": failure.error_code,
            })
            self._emit("recovery_decided", {
                "recovery_id": record["recovery_id"],
                "selected_action": decision.action.value,
                "status": decision.status,
                "attempt_number": decision.attempt_number,
                "max_attempts": decision.max_attempts,
            })
            if decision.action == RecoveryAction.REPAIR_REQUIRED:
                linked, previous = self.db.link_repair_verification_failure(
                    record["recovery_id"]
                )
                record = linked
                evidence = self.db.get_execution_record_for_tool_execution(
                    execution_id
                )
                self._emit("repair_required", {
                    "recovery_id": record["recovery_id"],
                    "execution_id": execution_id,
                    "evidence_record_id": (
                        evidence.get("record_id") if evidence else None
                    ),
                    "parent_recovery_id": record.get("parent_recovery_id"),
                })
                if previous is not None:
                    self._emit("repair_superseded", {
                        "recovery_id": previous["recovery_id"],
                        "status": previous["status"],
                        "next_recovery_id": record["recovery_id"],
                        "result_record_id": previous.get("result_record_id"),
                    })
        return record

    def record_tool_success(self, *, execution_id: str) -> list[dict]:
        """成功工具仅用于闭合同一验证键上的活动修复项。"""
        resolved = self.db.resolve_repair_verifications(execution_id)
        for record in resolved:
            self._emit("repair_resolved", {
                "recovery_id": record["recovery_id"],
                "execution_id": execution_id,
                "result_record_id": record.get("result_record_id"),
                "status": record["status"],
            })
        return resolved

    def build_repair_summary(self, *, execution_id: str) -> str | None:
        """构造给模型的固定、限长恢复上下文，不注入完整外部日志。"""
        recovery = self.db.get_failure_recovery_for_tool_execution(execution_id)
        if not recovery or recovery.get("status") != "REPAIR_REQUIRED":
            return None
        execution = self.db.get_tool_execution(execution_id) or {}
        evidence = self.db.get_execution_record_for_tool_execution(execution_id)
        evidence_summary = None
        if evidence is not None:
            evidence_summary = {
                "record_id": evidence.get("record_id"),
                "artifact_status": evidence.get("artifact_status"),
                "log_status": evidence.get("log_status"),
                "exit_code": evidence.get("exit_code"),
            }
        payload = {
            "contract_version": 1,
            "kind": "repair_required",
            "recovery_id": recovery["recovery_id"],
            "parent_recovery_id": recovery.get("parent_recovery_id"),
            "failure_class": recovery["failure_class"],
            "error_code": recovery["error_code"],
            "tool_execution_id": execution_id,
            "workspace_id": recovery.get("workspace_id"),
            "evidence": evidence_summary,
            "diagnostic_excerpt": str(
                execution.get("output_preview") or ""
            )[:500],
            "diagnostic_trust": "untrusted_data",
            "required_action": (
                "inspect_the_failure_evidence_repair_the_cause_and_rerun_"
                "the_same_verification_command"
            ),
            "resolution_rule": (
                "only_a_new_successful_execution_record_resolves_this_recovery"
            ),
        }
        return "REPAIR_REQUIRED\n" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _emit(self, event_type: str, payload: dict) -> None:
        if self.event_callback:
            self.event_callback(event_type, payload)
