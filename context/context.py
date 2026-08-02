"""对话状态容器：token 追踪、迭代预算、压缩触发、进化计数器。

将原先散落在 Agent 中的 12 个私有属性集中管理，
提供统一的读写接口。
"""

from .token_utils import estimate_message_tokens


class IterationBudget:
    """迭代预算：限制单次对话中 LLM 的最大调用次数。"""

    def __init__(self, max_iterations: int):
        """初始化预算。

        Args:
            max_iterations: 最大 LLM 调用次数上限。
        """
        self.max_total = max_iterations
        self._used = 0

    def consume(self) -> bool:
        """消耗一次预算，返回是否允许继续。"""
        if self._used >= self.max_total:
            return False
        self._used += 1
        return True

    @property
    def used(self) -> int:
        """已使用的迭代次数。"""
        return self._used

    @property
    def remaining(self) -> int:
        """剩余可用迭代次数。"""
        return self.max_total - self._used


class ConversationContext:
    """单次对话的状态容器。

    集中管理 token 估算、迭代预算、压缩触发标志、
    以及进化系统计数器。所有状态修改通过方法进行，
    避免外部直接操作内部属性。
    """

    def __init__(self, max_iterations: int, system_prompt: str,
                 tools_schema_json: str):
        """初始化对话上下文，计算固定 token 开销。

        Args:
            max_iterations: 最大 LLM 调用次数。
            system_prompt: 系统提示字符串。
            tools_schema_json: 工具 schema 的 JSON 字符串。
        """
        self._budget = IterationBudget(max_iterations)

        # Token 追踪
        self._last_prompt_tokens: int = 0
        self._last_msg_count: int = 0

        # 固定开销（启动时计算一次）
        self._system_tokens = len(system_prompt) // 4
        self._tools_schema_tokens = len(tools_schema_json) // 4
        self._fixed_overhead = self._system_tokens + self._tools_schema_tokens

        # 压缩触发
        self._force_compress: bool = False

        # 进化计数器
        self._iters_since_skill: int = 0
        self._turns_since_memory: int = 0

    # ── Token 估算 ──────────────────────────────────────────

    def estimate_tokens(self, messages: list[dict]) -> int:
        """估算完整请求 token 数（含 system + tools schema 固定开销）。

        优先使用上次 LLM 调用的真实 prompt_tokens 作为基准，
        仅对新消息做粗略估算。
        """
        if self._last_prompt_tokens > 0 and self._last_msg_count <= len(messages):
            # 有真实基准：只估算新增消息
            new_msgs = messages[self._last_msg_count:]
            new_tokens = estimate_message_tokens(new_msgs)
            return self._last_prompt_tokens + new_tokens
        else:
            # 无基准：全部估算
            msg_tokens = estimate_message_tokens(messages)
            return self._fixed_overhead + msg_tokens

    def update_from_usage(self, prompt_tokens: int, msg_count: int):
        """LLM 调用后用真实 usage 数据更新追踪状态。

        Args:
            prompt_tokens: API 返回的真实 prompt token 数。
            msg_count: 当前消息列表长度。
        """
        self._last_prompt_tokens = prompt_tokens
        self._last_msg_count = msg_count

    def reset_token_tracking(self):
        """重置 token 追踪缓存（压缩、/setup、/clear 后调用）。"""
        self._last_prompt_tokens = 0
        self._last_msg_count = 0

    @property
    def last_prompt_tokens(self) -> int:
        """上次 LLM 调用的真实 prompt token 数（用于状态栏百分比）。"""
        return self._last_prompt_tokens

    # ── 预算管理 ────────────────────────────────────────────

    def consume_budget(self) -> bool:
        """消耗一次迭代预算。返回 False 表示预算已耗尽。"""
        return self._budget.consume()

    @property
    def budget_used(self) -> int:
        """已使用的迭代次数。"""
        return self._budget.used

    # ── 压缩触发 ────────────────────────────────────────────

    @property
    def force_compress(self) -> bool:
        """是否需要强制触发上下文压缩。"""
        return self._force_compress

    @force_compress.setter
    def force_compress(self, value: bool):
        """设置强制压缩标志（/compress 命令调用）。"""
        self._force_compress = value

    # ── 进化计数器 ──────────────────────────────────────────

    @property
    def iters_since_skill(self) -> int:
        """自上次 skill_manage 调用以来的 LLM 迭代次数。"""
        return self._iters_since_skill

    @iters_since_skill.setter
    def iters_since_skill(self, value: int):
        self._iters_since_skill = value

    def increment_skill_iter(self):
        """每次 LLM 调用后递增技能迭代计数器。"""
        self._iters_since_skill += 1

    def reset_skill_iter(self):
        """skill_manage 工具调用后归零。"""
        self._iters_since_skill = 0

    @property
    def turns_since_memory(self) -> int:
        """自上次 memory 调用以来的对话轮次。"""
        return self._turns_since_memory

    @turns_since_memory.setter
    def turns_since_memory(self, value: int):
        self._turns_since_memory = value

    def increment_memory_turn(self):
        """每轮对话后递增记忆轮次计数器。"""
        self._turns_since_memory += 1

    def reset_memory_turn(self):
        """memory 工具调用后归零。"""
        self._turns_since_memory = 0
