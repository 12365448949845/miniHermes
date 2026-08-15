# 15 - 失败恢复、修复重跑与受控回滚开发文档

> 状态：Phase E0-E3 已完成；下一阶段为 W2 显式集成
>
> 前置文档：[11-multi-agent-runtime-roadmap.md](11-multi-agent-runtime-roadmap.md)、[12-reproducible-execution-foundation.md](12-reproducible-execution-foundation.md)、[13-worktree-write-parallelism.md](13-worktree-write-parallelism.md) 的 W0/W1
>
> 协作文档：[14-graph-engineering-workflow-runtime.md](14-graph-engineering-workflow-runtime.md)

## 1. 目的与结论

MiniHermes 已经能记录 Tool Execution、Run、NodeRun、命令日志、代码快照和重放结果；网络类只读工具也已有有限自动重试。但目前系统只能告诉 Agent “工具失败了”，还没有统一回答四个问题：

1. 这次失败是否值得按原样再试一次？
2. 如果原样重试没有意义，应该交给 Agent 诊断并修复什么？
3. 某次失败是否留下了半写入的文件，能否安全撤销？
4. 取消、超时、审批拒绝和进程崩溃后，哪些东西必须保留，哪些可以清理？

本阶段建立**受控失败恢复规则**。它不是另一个会替主 Agent 写代码的“恢复 Agent”，也不是“失败就无限重试、失败就删代码”的开关。它将恢复分成四条明确路径：

```text
工具失败
  -> 由工具适配器给出可信错误码
  -> RecoveryController 按固定规则判定
     -> 原样重试：仅无副作用、幂等、临时性故障
     -> 修复后重跑：把诊断材料交给现有主 Agent
     -> 自动撤销：仅系统独占且可证明归属的工作区
     -> 停止并说明：审批、参数、权限、环境或未知失败
  -> 所有决定与结果写入现有 Run / NodeRun / Tool Execution 审计链
```

核心结论：

- “重试”只表示**相同输入再执行一次**，不能解决代码写错、路径写错、缺依赖等问题。
- “修复后重跑”由主 Agent 看日志、读代码、改动后验证；这是一个新的工具执行和新的证据记录，不能伪装成透明重试。
- 测试失败通常是修复信号，不是回滚信号。自动回滚测试失败的代码会让 Agent 永远无法继续修复。
- 自动回滚只在系统能证明“这是 Agent 自己独占的改动、当前内容仍等于该 Agent 最后写入的状态”时进行。W1 Worktree 已将每个写入 Agent 隔离到独立代码副本；共享主工作区不做自动代码回滚。

## 2. 与当前真实系统的关系

| 现有能力 | 当前位置 | 本文如何复用 |
| --- | --- | --- |
| `ToolExecutionResult`、`error_code`、`retryable`、`attempts` | `tools/registry.py`、`session/db.py` | 保持为单次工具执行的事实记录，增加统一错误契约而非重新发明结果类型。 |
| `execute_with_retry_detailed()` | `tools/retry.py` | 保留其取消感知、次数限制和退避；只扩展可信分类与策略，不从模型输出猜错误。 |
| Tool 元数据 `side_effect`、`retry` | `tools/registry.py` | 增加幂等性和恢复限制，所有自动动作必须由元数据和工具适配器共同许可。 |
| Run / NodeRun / Event 状态机 | `agent/runtime.py`、`agent/graph_runner.py`、`session/db.py` | Runtime 仍是唯一调度器和终态所有者；恢复记录不能私自修改 Run 终态。 |
| 审批和 Hardline | `approval/engine.py` | 审批拒绝、Hardline、用户取消永远优先，恢复机制不能绕过审批。 |
| 执行证据、快照、重放 | `agent/reproducibility.py` | 每次重试、修复验证、回滚前后都关联新的证据，不覆盖历史记录。 |
| Worktree lease 设计 | `docs/13-worktree-write-parallelism.md` | 作为安全自动撤销的首个工作区边界；主工作区始终不作为自动回滚目标。 |

本文不替换 ReAct Loop、Provider、审批系统、GraphRunner 或 Worktree 设计。它建立在 W1 已实现的独占 Worktree 边界上，给这些已有层增加一套共同遵守的失败恢复约束。

首版的自动动作范围以**工具执行失败**为主。Provider 在工具调用产生前的连接、限流和临时服务失败仍由 Provider 的既有重试处理；最终失败只关闭对应 Run、写入精简事件，不会凭空创建或重放工具调用。Agent Runtime 的取消、超时、进程重启等无工具来源事件也可创建 `STOP` 类型恢复记录，但不触发自动修复或自动回滚。

## 3. 固定术语和不可违反的规则

| 术语 | 含义 |
| --- | --- |
| 原样重试（retry） | 相同工具、相同参数、相同权限快照再次执行。 |
| 修复后重跑（repair-and-rerun） | Agent 根据失败证据修改代码、参数或配置后，执行新的验证命令。 |
| 回滚（rollback） | 将系统拥有的工作区从一个明确检查点恢复到检查点内容。 |
| 检查点（checkpoint） | 写入前保存的文件哈希、内容材料和范围清单；不是笼统的 Git `reset`。 |
| 归属（ownership） | 系统能证明某个工作区、文件集合和本次 Run 的唯一关系。 |
| 恢复决定（recovery decision） | 对一次失败采取何种动作、为何采取、执行结果为何的持久化审计记录。 |

固定规则：

1. **没有结构化错误码，就不自动恢复。** 不扫描网页、终端或模型生成的自由文本来决定重试或回滚，防止错误信息诱导系统执行操作。
2. **原样重试只允许无副作用且幂等的调用。** 读取本地文件、查询网页等可适用；写文件、执行任意 shell、调用外部付费 API、提交数据、发消息均默认不适用。
3. **修复不是重试。** 修复后执行必须生成新的 `ToolExecution`；如果验证使用 `bash`，还必须生成新的 `ExecutionRecord`，并通过 `recovery_parent_id` 关联原因。
4. **用户拒绝、取消和 Hardline 永不重试。** 也不得换一种命令、换一个 Agent 或换一个工具偷偷绕过。
5. **回滚不能覆盖用户的更新。** 回滚前必须重新校验当前文件哈希仍等于该 Run 写入后的预期哈希；任何不匹配都停止并转人工处理。
6. **回滚只撤销代码，不抹掉证据。** 日志、差异、快照、恢复决定和失败原因按文档 12 的保留策略保存。
7. **成功集成是边界。** Worktree 候选尚未集成时可丢弃或恢复；一旦用户确认并成功合并到主分支，系统不自动 `git reset`、不自动改写提交历史。
8. **失败测试默认保留修复现场。** 它让 Agent 能依据失败继续工作，而不是被提前判为“需要撤销”。

## 4. 失败分类和默认动作

错误码由具体工具适配器产生，格式采用小写稳定标识，例如 `network_transient`。未知工具与未知错误一律落到 `unknown_failure`，默认不自动重试。

| 失败类别 | 典型错误码 | 默认动作 | 说明 |
| --- | --- | --- | --- |
| 用户或系统控制 | `cancelled`、`timed_out`、`process_restarted` | 停止；终止子进程；Worktree 保留为候选 | 不自动重跑，防止用户取消后仍继续执行。 |
| 审批与安全 | `approval_rejected`、`hardline_blocked`、`policy_denied` | 停止并展示原因 | 永不换命令绕过，也不再请求同一审批。 |
| 参数和路径 | `invalid_arguments`、`path_invalid`、`path_outside_scope` | 交主 Agent 修复输入；范围违规终止 lease | 重试相同参数没有价值。 |
| 身份、权限、配置 | `authentication_failed`、`permission_denied`、`missing_configuration` | 停止并说明需要用户处理的项目 | API Key、文件权限和审批不能靠重试解决。 |
| 本地前置条件 | `file_not_found`、`dependency_missing`、`command_not_found` | 交主 Agent 诊断；必要时请求用户 | 可能是路径错误，也可能需要安装依赖，不能猜。 |
| 代码与测试 | `nonzero_exit`、`test_failed`、`build_failed`、`syntax_error` | 进入修复后重跑路径 | 记录失败输出、定位文件、修改、执行验证。 |
| 瞬时网络/服务 | `network_transient`、`rate_limited`、受控 `timeout` | 有资格时有限原样重试 | 仅无副作用、幂等工具；尊重服务端 `Retry-After`。 |
| 竞争与暂时资源 | `resource_busy`、`lock_conflict` | 仅有专门适配器且无副作用时有限重试 | 首版不把普通 shell 文本识别为锁冲突。 |
| 部分写入或运行器异常 | `partial_write_possible`、`runner_crashed`、`scope_violation` | 停止；独占 Worktree 回到检查点或保留候选；共享目录人工处理 | 不知道实际写了什么时，不能原样重试。 |
| 工具内部或未知 | `internal_error`、`unknown_failure` | 停止并保留诊断 | 先修工具或人工判断，避免隐藏真实 Bug。 |

### 4.1 原样重试门槛

一次调用只有同时满足以下条件才可自动重试：

1. 工具适配器明确标记该错误 `retryable=true`。
2. `ToolMetadata.side_effect == "none"`。
3. `ToolMetadata.idempotency == "idempotent"`。
4. 当前 Run 未取消、未超时、父 Run 未取消，且仍在合法运行状态。
5. 当前权限快照仍允许该工具和参数；不得使用过期授权。
6. 该工具定义了重试上限、退避和总耗时预算。

首版默认：最多额外 2 次；指数退避加少量抖动，基础等待 2 秒；如服务端提供合理的 `Retry-After`，优先使用它；任何一次成功立即停止。等待期间始终响应取消。

现有 `web_search`、`web_extract` 可迁移到此模型。`read_file`、`list_dir` 虽然无副作用，但除非其适配器定义了可信的临时错误码，否则仍不重试。`bash`、`write_file`、`skill_manage`、`memory`、`generate_image`、`execute_code` 和所有未知第三方工具首版均为 `never`。

### 4.2 修复后重跑的流程

代码测试失败时，系统不新增一个“自动修复模型”。现有主 Agent 已具备读取日志、读取代码、编辑、运行验证的能力；Runtime 只给它一个可信、结构化的恢复上下文：

```text
测试命令失败
  -> 保存 stdout / stderr、退出码、命令前快照和失败类别
  -> 写入 recovery decision = REPAIR_REQUIRED
  -> 主 Agent 在下一轮看见简短失败摘要和证据引用
  -> 读取相关文件与完整日志，提出并执行修改
  -> 运行新的验证命令
  -> 验证成功：将该 recovery decision 标为 RESOLVED
  -> 达到预算、用户取消或无法解决：标为 ABANDONED / MANUAL_REQUIRED
```

模型只能看到脱敏后的摘要、制品 ID 和允许读取的日志；原始外部输出不能成为系统指令。是否“已经修好”的唯一技术依据是新的验证执行结果，不是模型的自然语言判断。

## 5. 回滚模型

### 5.1 不同场景，不能用同一种回滚

| 场景 | 是否自动撤销代码 | 原因 |
| --- | --- | --- |
| 主工作区，Agent 通过任意 `bash` 修改过文件 | 否 | 无法证明所有改动归 Agent 所有，也无法阻止用户同时编辑。 |
| 主工作区，未来受事务保护的 `write_file` 修改 | 首版仍否 | 虽可记录前后内容，但多工具连续修改与用户同时编辑仍需要充分验证；先只提供“可审计、可人工确认的撤销”。 |
| 独占 Worktree 中单个失败写操作 | 可以，满足检查点和哈希条件时 | Runtime 保证一个 lease 同一时刻只绑定一个写入 Agent；目录、分支和 lease 由系统创建，且不影响主工作区。 |
| 独占 Worktree 中测试失败、Agent 仍在修复 | 不撤销 | 失败是诊断材料，撤销会破坏后续修复。 |
| Worktree Run 取消、超时或崩溃 | 不合并；默认保留候选 | 主项目已经没有变化，保留候选和日志更便于继续修复与审计。 |
| Worktree 候选主动丢弃或达到保留期 | 可以清理系统拥有的 Worktree | 必须先完成 Runner 退出、制品归档和 lease 状态转换。 |
| 已成功集成到主分支 | 否 | 这是用户确认后的历史边界，不能自动改写 Git 历史。 |

因此，“自动回滚”在第一版的用户可见效果是：**每个写入 Agent 都在自己的 Worktree 中工作，失败修改绝不会进入你的主项目；需要放弃时，系统会自动回到该候选的检查点或安全清理候选。** 哈希和 lease 校验用于防御程序异常或手工误操作，不是让正常流程依赖“多个 Agent 不要碰同一目录”的人为约定。

### 5.2 自动撤销的硬前置条件

`WorkspaceManager.rollback_checkpoint()` 只能在以下全部条件成立时运行：

1. 该 workspace 是系统创建的有效 Worktree lease，且 Runtime 已确认它只归属本次写入 Run；`lease_status` 不是 `RUNNING` 或 `INTEGRATING`。
2. 所有 Runner 和子进程均已确认退出；取消时先完成进程树终止。
3. 回滚范围来自冻结的 `write_scope` 和检查点清单，拒绝绝对路径、路径穿越、`.git`、符号链接或工作区外路径。
4. 检查点保存了每个受影响文件的“写前状态”和“该 Run 写后预期哈希”。
5. 回滚前重新读取文件；当前哈希必须仍与“写后预期哈希”一致。否则标为 `ROLLBACK_CONFLICT`，不覆盖任何内容。
6. 先将 diff、未跟踪清单、哈希和失败原因写入制品；制品失败时不执行删除性清理。
7. 所有状态更新通过 `SessionDB` 的受控方法与 lease 状态机提交，不能由 Agent 或 shell 直接改表。

回滚的实现必须使用受控文件 API 或受控 Git 命令，禁止把 `git reset --hard`、`git clean -fd` 交给模型生成或对用户主工作区执行。

### 5.3 回滚状态

恢复记录使用独立状态，不污染 Run、NodeRun 与 ToolExecution 的既有终态：

```text
PENDING
  -> RETRYING -> RETRY_SUCCEEDED | RETRY_EXHAUSTED
  -> REPAIR_REQUIRED -> RESOLVED | ABANDONED | MANUAL_REQUIRED
  -> ROLLBACK_RUNNING -> ROLLED_BACK | ROLLBACK_SKIPPED | ROLLBACK_CONFLICT
  -> NOT_APPLICABLE
```

`ROLLBACK_SKIPPED` 表示策略上不应撤销，例如测试失败仍在修复；`ROLLBACK_CONFLICT` 表示安全校验没有通过，必须保留现场并告知用户。两者都不是“回滚成功”。

## 6. 数据、事件和接口设计

### 6.1 数据库

在 `SessionDB` v9 增量迁移中新增 `failure_recovery_records` 表，不修改或重建历史表。字段如下：

| 字段 | 说明 |
| --- | --- |
| `recovery_id` | UUID 主键。 |
| `source_kind` / `run_id` / `node_run_id` / `tool_execution_id` | 来源类别为 `TOOL_EXECUTION` 或 `RUN`；`run_id` 必填，工具来源必须有 `tool_execution_id`，纯 Runtime/Provider 来源可为空。 |
| `parent_recovery_id` | 修复后重跑、后续重试或回滚尝试的链路。 |
| `failure_class` / `error_code` | 来自受控适配器的分类。 |
| `selected_action` | `RETRY`、`REPAIR_REQUIRED`、`ROLLBACK`、`STOP`、`NOT_APPLICABLE`。 |
| `status` | 使用 5.3 的状态机。 |
| `attempt_number` / `max_attempts` | 原样重试的次数与限制。 |
| `workspace_id` / `checkpoint_id` | 可空；仅 Worktree 回滚时关联。 |
| `reason_json` | 大小受限、脱敏、可解析的规则依据和摘要。 |
| `result_record_id` | 新的验证或回滚证据记录。 |
| `created_at` / `updated_at` / `finished_at` | 审计时间线。 |

必须提供 `SessionDB` 方法控制合法状态迁移、乐观版本或条件更新、来源归属校验。不要让 `RecoveryController` 拼接任意 SQL；不要把完整 stdout、用户输入、模型 reasoning 或秘密写进该表。

E1 在 `SessionDB` v10 增量迁移中新增 `tool_retry_attempts`，不修改 `tool_executions` 和 `failure_recovery_records` 的既有语义。每次真实调用先写入 `RUNNING` 尝试，完成后记录稳定错误码、可重试标记、脱敏输出预览和耗时；需要重试时，同一行再记录等待时长与 `WAITING -> COMPLETED | CANCELLED`。`(tool_execution_id, attempt_number)` 唯一，成功重试也不会覆盖前一次失败。`ToolExecution` 仍是一整个工具调用的最终汇总，`failure_recovery_records` 仍只负责最终失败后的恢复决定。

E2 在 `SessionDB` v11 增量迁移中为 `execution_records` 新增 `verification_key`。该键只基于脱敏后的命令和工作目录，不包含 `snapshot_id`，因此代码修改后的新快照仍能识别为同一验证命令；实际匹配还必须同时满足同一 Run 和同一 Worktree。每个工具执行只能拥有一条恢复来源记录，修复后再次失败通过 `parent_recovery_id` 接续，成功复验通过 `result_record_id` 指向新的执行证据。

### 6.2 事件

向现有 `agent_events` 追加精简事件，至少包括：

- `tool_failure_classified`
- `recovery_decided`
- `tool_retry_scheduled`
- `repair_required`
- `rollback_started`
- `rollback_succeeded`
- `rollback_skipped`
- `rollback_conflict`

事件仅包含 ID、错误码、次数、耗时、状态和脱敏摘要。完整日志仍在文档 12 的制品目录。

### 6.3 代码职责

| 位置 | 责任 |
| --- | --- |
| `tools/retry.py` | 维护工具适配器到稳定错误码的映射、取消感知等待和原样重试执行；不作 LLM 判断。 |
| `tools/registry.py` | 扩展工具元数据，冻结单次调用的恢复资格，返回结构化 `ToolExecutionResult`。 |
| `agent/recovery.py` | 新增纯规则 `RecoveryPolicy` 与编排器 `RecoveryController`；只选择动作、创建记录、发事件，不直接修改用户代码。 |
| `agent/runtime.py` | 创建和传递 Run 上下文，检查取消/超时，作为唯一的 Run 生命周期所有者。 |
| `agent/worktree.py`（W1 已实现） | 管理检查点、Worktree 回滚和清理；执行前后都进行 hash 与范围校验。 |
| `session/db.py` | 迁移、状态机、查询与原子写入。 |
| `cli/commands.py` | 提供只读查询和安全的显式操作入口。 |

`Provider` 的 API 请求重试保留在 Provider 层：它只处理尚未形成工具调用的模型连接、限流和临时服务错误。Provider 不得据此重放已经提交给工具的调用。

### 6.4 用户交互

首版不弹出“是否重试”窗口打断普通网络恢复；系统只展示简短状态，例如“网络暂时失败，正在第 2/3 次重试”。

需要用户介入的情况应明确告诉用户缺少什么，而不是让模型反复猜：

- 审批被拒绝：说明本次操作已停止。
- 缺少 API Key、登录状态、文件权限或依赖：说明需要用户配置的项。
- 回滚冲突：说明系统没有覆盖当前文件，保留了候选和证据。
- 修复预算耗尽：提供失败命令、证据 ID、候选 Worktree ID 与当前差异摘要。

E0 已新增只读命令：

```text
/recovery <recovery_id>       查看一次失败的分类、决策、次数和证据
/recoveries [run_id]          列出当前或指定 Run 的恢复链
```

E3 已升级显式丢弃命令：

```text
/discard-worktree <workspace_id>  丢弃已停止的系统候选
```

`/resume-worktree` 不属于 E3。继续旧候选需要创建新 Run、重新签发工具权限并建立新旧 Run 的归属关系，后续应作为独立生命周期设计，不能复用“回滚并删除”的接口。

不提供“回滚任意路径”或“自动重试任意命令”的命令。

## 7. 与 Worktree 的先后关系

两者没有设计冲突，但 Worktree 应先完成：

- **W0/W1 先建立隔离边界。** Runtime 为每个写入 Agent 创建独立 Worktree 和唯一 lease，正常流程不存在两个 Agent 修改同一代码副本的问题。
- **失败恢复在 W1 之后接入。** 这样错误分类、修复后重跑和自动撤销都能绑定到明确的 workspace、范围和检查点，而不是尝试猜主工作区里哪些改动属于 Agent。
- Worktree W1 的“失败保留候选”仍然成立：测试失败先保留现场给 Agent 修复；只有明确需要中止某个受控写事务或丢弃候选时，才执行检查点恢复/清理。
- W2 的集成失败发生在临时集成 Worktree，主工作区本来就不应被写入，因此不需要也不允许对主分支自动回滚。
- W3 并行前必须验证：一个 Worktree 的重试、取消、回滚或清理不能阻塞、删除或修改另一个 lease。

建议实施顺序：

```text
已完成：R0-R3 可复现执行
  -> W0：Worktree 纯校验与数据模型
  -> W1：串行 Worktree 写入、检查点和失败保留
  -> E0：失败分类契约和恢复记录（无行为扩大）
  -> E1：无副作用工具的受控原样重试
  -> E2：修复后重跑提示与恢复链
  -> E3：Worktree 的受控撤销 / 丢弃闭环
  -> W2：显式集成和临时集成验证
  -> W3：最多两个独立 Worktree 的受控并行
```

E0/E1 在 W1 的独立工作区与 lease 可用后实施；每一步都必须先独立验收，不应为了并行开发而跳过错误分类和取消语义。

## 8. 实施阶段和验收门禁

### Phase E0：契约与审计，不改变自动行为

目标：定义 `ToolFailure` / `RecoveryPolicy`、错误码注册表、数据库迁移和查询，不扩大任何工具重试或写入权限。

验收：

1. 现有 `web_search`、`web_extract`、`bash` 的历史结果兼容。
2. 未注册错误、自由文本、异常对象都不会被自动标为可重试。
3. 每个失败工具执行至多产生一个初始恢复记录，重复回调不会重复创建。
4. 取消、审批拒绝、Hardline、Run 终态的恢复状态转换均被拒绝或正确收束。
5. 数据库迁移、状态机和脱敏内容均有离线测试。

**验收记录（2026-08-14）**：新增纯规则 `ToolFailure`、稳定错误码注册表、`RecoveryPolicy` 与只写审计的 `RecoveryController`。SessionDB 从 v8 增量迁移到 v9，恢复记录按 ToolExecution 来源唯一，校验 Run、NodeRun、Worktree 归属、错误分类一致性、乐观版本和合法状态迁移；原因 JSON 在数据库边界限长并脱敏。工具完成后只对 `FAILED`、`DENIED`、`CANCELLED` 终态创建记录与精简事件，重复回调不会重复创建；审计异常不会改变工具结果。`/recoveries` 与 `/recovery` 仅提供查询。E0 没有修改 `tools/retry.py` 的重试次数、退避或资格，也没有调用模型、工具、回滚或 Worktree 集成。E0 专项测试 `6 passed`，核心回归 `109 passed, 3 skipped`，全量离线回归 `157 passed, 3 skipped`。下一阶段为 E1。

### Phase E1：安全原样重试

目标：迁移现有无副作用网络工具，使用统一策略执行有限重试和事件记录。

验收：

1. 429、受控 5xx、连接重置和临时超时会在上限内重试，并可被取消。
2. 401/403、配置缺失、参数错误、文件不存在、`bash` 非零退出均不重试。
3. 带副作用、未知或幂等性未声明的工具永远不被调度器重试。
4. 每次尝试次数、等待、最终错误和输出证据可查询；成功重试不覆盖第一次失败事实。
5. 全量旧测试和新增 fake-tool 测试通过。

**验收记录（2026-08-14）**：SessionDB 从 v9 增量迁移到 v10，新增逐次调用和等待流水；`/agent` 与 `/recovery` 可查看每次尝试的状态、错误码、等待和最终结果。重试资格同时要求受控错误码、适配器 `retryable=true`、`side_effect=none`、`idempotency=idempotent` 和允许重试的元数据策略；每次调度前及实际调用前重新检查取消、截止时间、Run 状态和冻结权限。`web_search`、`web_extract` 已迁移为兼容字符串的受控结果，429、受控 5xx、连接错误和超时可有限重试，401/403、缺配置、额度耗尽和永久错误不重试；可信响应头中的 `Retry-After` 有界优先，其他情况使用指数退避和少量抖动。`bash` 与有副作用、未知幂等性的工具仍不自动重试，非零退出统一记录为 `FAILED/nonzero_exit`。E1 专项测试 `22 passed`，全量离线回归 `179 passed, 3 skipped`，`compileall` 与 `git diff --check` 通过。下一阶段为 E2。

### Phase E2：修复后重跑提示与恢复链

目标：将代码测试失败可靠地交回已有主 Agent，并在下一次验证后闭合恢复链。

验收：

1. 主 Agent 收到的是固定结构的失败摘要和制品引用，而非把外部日志当作系统提示。
2. 修复后的验证创建新的执行记录，并能从 `/recovery` 回溯到原失败。
3. Agent 未修复、预算耗尽、取消或失败时，恢复记录终态正确，现场不会丢失。
4. 不新建常驻恢复 Agent，不增加新的模型调用，也不替代主 Agent 的判断。

**验收记录（2026-08-14）**：SessionDB 从 v10 增量迁移到 v11，新增不含快照 ID 的 `verification_key` 和工具恢复来源唯一约束。`ToolExecutionResult` 返回 `execution_id`，主 Agent 对 `REPAIR_REQUIRED` 只接收固定 JSON 摘要、稳定错误码、脱敏且限长的诊断摘录以及证据 ID，不再直接接收完整外部日志；恢复审计不可用时也按固定摘要失败关闭。同一 Run/Worktree 中再次运行相同验证命令，成功会把活动记录更新为 `RESOLVED` 并关联新证据，再次失败会把旧项更新为 `ABANDONED` 并创建带父恢复 ID 的新活动项；跨 Run、跨 Worktree 的证据不能闭合恢复。Run 正常结束、预算耗尽、Provider/工具内部失败会把未验证项收为 `MANUAL_REQUIRED`，用户取消、超时和进程中断会收为 `ABANDONED`，Graph 主 Run 与普通 Run 使用同一原子收口规则。`/recovery` 可显示源证据、父恢复和结果证据。E2 专项测试 `12 passed`，E0-E2 联合测试 `40 passed`，全量离线回归 `191 passed, 3 skipped`，`compileall` 与 `git diff --check` 通过。下一阶段为 E3。

### Phase E3：Worktree 受控撤销

前置：W1 已完成并通过其门禁；E0-E2 的恢复链可查询。

目标：对系统独占 Worktree 的已停止写事务支持检查点校验、撤销和安全丢弃。

验收：

1. 回滚后主工作区哈希不变，候选 Worktree 仅在允许范围内恢复。
2. 文件被外部改写、路径逃逸、Runner 未退出、制品写入失败时，一律不覆盖文件并产生 `ROLLBACK_CONFLICT` 或 `ROLLBACK_SKIPPED`。
3. 取消、超时、进程崩溃后不自动合并；候选、日志、快照和 lease 状态一致。
4. 已合并候选无法通过此接口回滚主分支。
5. 测试失败默认进入 `REPAIR_REQUIRED`，不会误触发撤销。

**验收记录（2026-08-14）**：SessionDB 从 v11 增量迁移到 v12，恢复记录新增回滚制品路径、SHA-256 和稳定结果原因；`ROLLBACK_RUNNING` 只允许终态 Run 的 `PRESERVED/FAILED` 独占候选进入。`/discard-worktree` 现统一经过 `RecoveryController`，依次确认 Runner 已退出、最终 diff/manifest 哈希有效、当前文件与冻结清单一致、路径仍在 `write_scope` 且没有链接或危险类型，然后先写 `PREPARED` 制品，再用受控 `git restore` 和逐文件删除恢复 `base_commit`。恢复后必须证明候选为空且主工作区指纹未变，才能写入 `ROLLED_BACK` 并删除 Worktree、私有分支和 Runtime 临时目录。外部改写、制品篡改和范围违规进入 `ROLLBACK_CONFLICT`；Runner 活跃、状态不可确认或预检制品写入失败进入 `ROLLBACK_SKIPPED`；两者都保留候选。成功回滚后的清理失败不会改写恢复结果，可由同一显式命令重试清理。启动对账会为进程中断的 Worktree 补写最终候选证据，已合并候选禁止进入回滚。E3 专项测试 `14 passed`，E0-E3 与 W1 联合测试 `70 passed`。`/resume-worktree` 明确延期，不混入撤销闭环。下一阶段为 W2。

## 9. 测试矩阵

所有测试使用 fake Provider、fake tool、临时 SQLite、临时 Git 仓库和 fake Runner，不依赖真实 API Key、网络、Docker 或用户项目。

至少覆盖：

1. 网络失败后成功、耗尽、取消等待、服务端重试时间、未知网络文本。
2. 审批拒绝、Hardline、权限不足、缺配置、参数错误、文件不存在、依赖缺失、非零退出。
3. 同一工具失败回调重复到达、Run 已终态、数据库状态版本冲突、进程重启后的遗留恢复记录。
4. 测试失败后 Agent 修复成功、修复后仍失败、预算耗尽、用户取消。
5. Worktree 的检查点成功撤销、哈希冲突拒绝、范围外文件拒绝、符号链接拒绝、Runner 未退出拒绝。
6. 两个候选并行时，一个失败/清理不会影响另一个；父 Run 取消会通知所有子项。
7. 回滚和清理失败时，完整日志、diff、快照和 `failure_recovery_records` 仍可查询。

## 10. 明确不做的事

- 不做“任意失败自动重试”。
- 不让 LLM 或错误日志决定安全策略。
- 不在共享主工作区执行自动 `git reset --hard`、`git clean`、`checkout --` 或删除目录。
- 不自动安装依赖、修改 API Key、提升权限、重新请求被拒绝的审批或绕过 Hardline。
- 不把失败测试直接视为应撤销的事务。
- 不因为回滚机制存在就自动提交、自动推送或自动合并代码。
- 不让失败恢复机制创建新的常驻 Agent、线程或第二套 Run 状态机。

## 11. 最终判断

这套设计把“出错以后怎么办”从模型临场猜测，变成可审计的固定规则：临时故障才原样重试；代码错误交给主 Agent 修复后验证；独占 Worktree 中可证明归属的异常写入才允许自动撤销；共享主工作区和所有不确定情况宁可保留现场，也不冒险覆盖用户内容。

它与 Worktree 的关系是前后衔接，不是二选一：先由 Worktree 提供独立代码副本、唯一 lease 和检查点，再在这个边界上建立失败分类、修复重跑与受控回滚，最后才考虑并行写入。
