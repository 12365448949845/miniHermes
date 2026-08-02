"""
向用户提问工具：agent 需要澄清或决策时主动提问。
实际交互由 CLI 层 callback 接管，工具层只负责校验和分发。
"""

import json
from tools import register

_MAX_CHOICES = 4


@register({
    "type": "function",
    "function": {
        "name": "clarify",
        "description": (
            "Ask the user a question when you need clarification, feedback, or a decision. "
            "Use PROACTIVELY when: the request is ambiguous with multiple valid interpretations, "
            "a decision has meaningful trade-offs the user should weigh in on, "
            "or required context cannot be retrieved by other tools. "
            "Do NOT use for information retrievable via search/file tools or trivial yes/no decisions. "
            "Prefer retrieving information with tools over asking — but when genuinely uncertain about intent, "
            "asking is ALWAYS better than guessing wrong. "
            "Two modes: (1) Multiple choice — provide up to 4 choices. "
            "(2) Open-ended — omit choices for free-form response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user.",
                },
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                    "description": "Up to 4 answer choices. Omit for open-ended question.",
                },
            },
            "required": ["question"],
        },
    },
})
def clarify(question: str, choices: list = None, callback=None) -> str:
    """
    向用户提问并返回回答 JSON。

    Args:
        question: 展示给用户的问题，例如 "Which approach should I use?"
        choices: 可选答案列表，例如 ["Fast", "Careful"]，最多保留 4 项。
        callback: CLI 层注入的交互函数，签名为 callback(question, choices) -> str。

    Returns:
        JSON 字符串，包含 question、choices_offered 和 user_response。
    """
    if not question or not str(question).strip():
        return json.dumps({"error": "Question text is required."}, ensure_ascii=False)

    question = str(question).strip()
    normalized_choices = _normalize_choices(choices)
    if isinstance(normalized_choices, str):
        return json.dumps({"error": normalized_choices}, ensure_ascii=False)

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        response = callback(question, normalized_choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    response_text = str(response).strip() if response is not None else ""
    if not response_text:
        response_text = "(empty)"

    return json.dumps({
        "question": question,
        "choices_offered": normalized_choices,
        "user_response": response_text,
    }, ensure_ascii=False)


def _normalize_choices(choices):
    """
    校验并规整 choices 参数。

    Args:
        choices: 模型传入的 choices 参数，例如 ["A", "B"] 或 None。

    Returns:
        规整后的 list[str]、None，或错误字符串。
    """
    if choices is None:
        return None

    if not isinstance(choices, list):
        return "choices must be a list of strings."

    normalized = [str(choice).strip() for choice in choices if str(choice).strip()]
    if not normalized:
        return None

    return normalized[:_MAX_CHOICES]
