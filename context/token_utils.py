"""统一的 token 估算工具。

供 ConversationContext 和 ContextCompressor 共用，
消除原先 agent.py 和 context_compressor.py 中的重复逻辑。
"""


def estimate_message_tokens(messages: list[dict]) -> int:
    """估算消息列表的 token 数。

    优先使用消息上的 _token_count 字段（由 LLM 返回的真实值或粗略估算），
    回退到 len(content) // 4 的粗略估算。

    Args:
        messages: 消息字典列表。

    Returns:
        估算的 token 总数。
    """
    total = 0
    for msg in messages:
        tc = msg.get("_token_count")
        if tc is not None:
            total += tc
        else:
            content = msg.get("content", "") or ""
            total += len(content) // 4
    return total
