"""Phase 3 审批模式和逻辑会话授权测试。"""

import pytest

from approval import ApprovalEngine, ApprovalMode


def test_hardline_is_blocked_in_every_approval_mode():
    engine = ApprovalEngine()
    check = engine.check(
        "bash",
        {"command": "rm /"},
        conversation_id="conversation-1",
    )

    for mode in ApprovalMode:
        resolution = engine.resolve(
            check,
            tool_name="bash",
            args={"command": "rm /"},
            mode=mode,
            conversation_id="conversation-1",
        )
        assert resolution.allowed is False
        assert resolution.model_output.startswith("BLOCKED:")


def test_old_auto_approve_maps_fail_closed_and_cannot_create_trusted_mode():
    with pytest.warns(DeprecationWarning):
        assert ApprovalMode.coerce(auto_approve=True) == ApprovalMode.DENY_SENSITIVE
    with pytest.warns(DeprecationWarning):
        assert ApprovalMode.coerce(auto_approve=False) == ApprovalMode.INTERACTIVE
    with pytest.raises(ValueError, match="explicit enum"):
        ApprovalMode.coerce("trusted")


def test_session_approval_is_scoped_by_conversation_id():
    engine = ApprovalEngine()
    args = {"command": "rm old.txt"}
    first = engine.check("bash", args, conversation_id="conversation-1")
    resolution = engine.resolve(
        first,
        tool_name="bash",
        args=args,
        mode=ApprovalMode.INTERACTIVE,
        approval_callback=lambda *unused: "session",
        conversation_id="conversation-1",
    )

    assert resolution.allowed is True
    assert engine.check(
        "bash", args, conversation_id="conversation-1"
    ).action == "allow"
    assert engine.check(
        "bash", args, conversation_id="conversation-2"
    ).action == "confirm"


def test_user_denial_and_noninteractive_policy_denial_are_distinguishable():
    engine = ApprovalEngine()
    args = {"command": "rm old.txt"}
    check = engine.check("bash", args, conversation_id="conversation-1")

    user_denial = engine.resolve(
        check,
        tool_name="bash",
        args=args,
        mode=ApprovalMode.INTERACTIVE,
        approval_callback=lambda *unused: "deny",
        conversation_id="conversation-1",
    )
    policy_denial = engine.resolve(
        check,
        tool_name="bash",
        args=args,
        mode=ApprovalMode.DENY_SENSITIVE,
        conversation_id="conversation-1",
    )

    assert user_denial.allowed is False
    assert policy_denial.allowed is False
    assert "DENIED by user" in user_denial.model_output
    assert "DENIED by approval policy" in policy_denial.model_output


def test_trusted_requires_explicit_enum_and_still_respects_hardline():
    engine = ApprovalEngine()
    dangerous = engine.check(
        "bash", {"command": "rm old.txt"}, conversation_id="conversation-1"
    )
    allowed = engine.resolve(
        dangerous,
        tool_name="bash",
        args={"command": "rm old.txt"},
        mode=ApprovalMode.TRUSTED,
        conversation_id="conversation-1",
    )

    assert allowed.allowed is True
