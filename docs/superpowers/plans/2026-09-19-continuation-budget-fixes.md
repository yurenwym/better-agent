# Better Agent：近期历史丢失与完整请求预算修复方案

日期：2026-09-19  
状态：待实施；本次仅提供开发文档。  
前置方案：[归档摘要确定性注入](2026-09-19-deterministic-conversation-continuation.md)。

## 1. 修复范围与完成标准

本轮只修复代码复查确认的两个 P1 问题：

1. `_history()` 在标记 required 之前裁剪原文，导致近期轮次减少、未归档内容失去承接。
2. 前台归档未按完整请求判断容量，停止条件也没有使用同一口径，导致超限请求未压缩就返回。

已有的按覆盖区间读取 Episode、摘要独立必带、长期记忆可选召回等实现继续复用。不要重做摘要系统、扩展向量检索或引入滚动摘要模型链。简单保存已有计划的确定性快捷分支不调用 LLM，不属于本次缺陷。

**完成标准：历史足够时，最近 N 个完整 Turn 不被静默裁掉；任何退出常规原文的较旧 Turn 都有已提交摘要承接；需要压缩与压缩后是否可发送，均由同一个完整请求预算判断。无法满足时明确失败，不能发出残缺请求。**

## 2. 已验证问题及复现依据

### P1-A：先裁剪、后 required，保护已经太晚

当前调用顺序：

```text
_history()
  → build(A, Q)
  → project_context()
  → pack_recent(..., min_turns=N, recent_budget=R)
  → _render_required_history(已经被裁剪的 transcript)
```

`transcript.py::pack_recent` 会因 R 不足而释放较旧轮次的软保护；单轮超过硬预算时还有进一步降级逻辑。之后添加 `_context_required=True` 只能保护剩余消息，不能找回已裁掉的原文。

复查中的实际构造：

| 输入/结果 | 值 |
|---|---:|
| 完整历史 Turn 数 | 5 |
| `history_min_turns` | 5 |
| 模型 `context_window` / 输出预留 | 8,192 / 1,024 |
| 该 legacy Profile 的有效输入预算 | 6,810 |
| 归档游标 A | 0 |
| 保护 N 后可归档的前缀轮数 | 0 |
| `_history()` 最终返回轮数 | **1** |

该构造每轮 user/assistant 正文使用约 800 个 ASCII 填充字符，并通过真实 transcript 构建器测量。四轮消失，既无原文也无摘要。正确结果应是保护五轮，并在完整请求确实超限时报告容量不足。

涉及位置：`conversation.py::_history/_render_required_history`、`transcript.py::pack_recent`。

### P1-B：入口和出口没有使用完整请求预算

当前存在三个相连的缺口：

- `_continuation_required_units()` 仅计算已有摘要正文；它没有计入当前用户消息、系统提示、工具 schema、必需目标状态和完整协议开销。
- 入口检查 `pending_units + extra_required_units <= H`，循环却仅以 `pending_units <= compact_target` 返回。
- 归档提交后只重算原文，不重算新增摘要和完整请求；`nothing_selectable` 还会直接被标记为 ready。

控制流复现（隔离测量值验证，不代表一次真实模型 tokenizer 计数）：

```text
历史计数 pending = 6,000
额外必需内容 extra = 5,000
有效输入预算 H = 10,000
compact_target = 7,000

入口：6,000 + 5,000 > 10,000，需要处理
循环：6,000 <= 7,000，直接返回
实际归档调用次数：0
```

正确行为：若仍有 N 轮之外的旧前缀，则归档后重算；若已无可归档前缀，则明确报超限。不能返回“上下文已准备好”。

涉及位置：`conversation.py::_continuation_required_units/_archive_history_before_generation/_archive_attempt_once`、`live_model.py::route_and_respond` 的请求准备及最终打包。

## 3. 必须保持的上下文契约

### 3.1 覆盖边界

沿用已有 A/Q 快照：A 是已提交归档上界，Q 是当前用户消息之前的历史上界。

```text
已归档部分：由确定性摘要覆盖，来源范围不超过 A
未归档部分：完整保留 (A, Q] 内可见的历史 Turn
当前输入：追加一次
```

较旧的未归档 Turn 超过预算时，必须先完成摘要提交，再推进 A 并从原文集合移除。单纯调用 `pack_recent`、记录 degraded 事件，不构成完成归档。

默认 N=5，有 Profile 配置时用其 `history_min_turns`。可见原文不足 N 时保留全部现有轮次；旧策略已归档了最近轮次的兼容情况单独标记，不声称已经恢复 N 轮。新归档不得继续吞掉受保护的最近 N 轮。

### 3.2 全请求预算

设 `H` 为已有 Profile 预算函数给出的有效输入上限，`U(request, profile)` 为项目已有的完整请求预算计数。单位仍为当前保守计数单位，不能称为精确模型 tokens。

请求中的必需项包括：

- 实际硬性系统指令和本分支附加指令；
- 必带承接摘要与完整未归档 Turn；
- 当前用户输入、必需目标状态；
- 本次发送的工具定义、必要工具调用及结果；
- 对应适配器计入的消息与协议包装。

输出预留已从 H 中扣除，不要再扣一次；协议开销按现有预算实现计入，不要重复相加。

先验证必需请求，再将可选长期记忆、可选计划/Skill 等放入剩余预算。不要为了保住可选资料而额外归档历史。

**唯一成功条件：最终准备发送的请求满足现有 Profile 预算及 wire gate，且承接摘要和完整近期后缀仍在。**

`compact_target`、`archive_target` 可继续用来建议单批归档范围，但不能成为“请求已经能发送”的替代判据。前台一旦可发送即可结束；后台提前归档策略保持现有行为。

## 4. 修复设计

### 4.1 历史构建只投影，不丢 Turn

修改普通会话 `_history()`：

1. 接收冻结的 A/Q，构建完整未归档后缀。
2. 沿用已有工具大结果投影，数据库原结果不变。
3. 对后缀每个完整 Turn 渲染 required 原子组，保留 tool call/result 配对。
4. 不再对这个后缀执行会删除 Turn 的 `pack_recent`。
5. 把是否可容纳交给完整请求预算与归档循环。

不要全局删除或更改 `pack_recent`，它仍可能用于归档前缀选择和其他调用。新会话主路径不调用其软保护降级行为即可。

归档选择器同样要检查 N：在选择待归档来源之前明确划分“可归档旧前缀”和“受保护后缀”。不能依赖 `pack_recent` 在极大单轮情况下仍然保护 N 的假设。无论目标大小如何，都只能归档旧前缀内的完整 Turn。

### 4.2 提取实际请求准备逻辑

从 `LiveConversationModel` 现有构造链中提取可复用的、无发送副作用的请求准备/测量逻辑；由前台容量判断与最终发送共同使用。

推荐接口契约（名称不强制，不要求引入框架）：

```text
prepare_request(已解析的分支状态, 摘要, 完整历史, 当前消息, profile)
    → 实际 messages/tools
    → 必带组集合、冻结覆盖标识、Profile 标识

evaluate_request(候选请求, profile)
    → 可选项打包后的可发送请求，或明确的必需内容超限结果
```

注意：

- 不在 worker 里复制一份系统提示字符串或用固定常数预留其成本。
- 需要异步分类才能确定的分支状态，在准备流程中只解析一次；每轮归档重算不能重复执行分类、保存记忆、创建计划等副作用。
- 重试附加的提示或工具结果也需要重新测量；不能拿第一轮成本代表后续实际请求。
- 保留现有协议适配器与 wire gate；不为修复另建一种 tokenizer 或预算公式。

### 4.3 将前台归档改为“准备—测量—归档—重建”

```python
# 示意流程，不是可直接复制的生产代码。
deadline = existing_wait_deadline()
branch_state = resolve_branch_once()

while True:
    check_cancelled()
    snapshot = load_valid_coverage_snapshot()
    history = render_all_unarchived_turns(snapshot.A, snapshot.Q)
    candidate = prepare_request(branch_state, snapshot, history, current_input, profile)
    result = evaluate_request(candidate, profile)

    if result.fits:
        freeze_final_context(result, snapshot)
        return result.request

    prefix = older_complete_turns_outside_protected_N(history)
    if not prefix:
        raise ContinuationContextOverflow(...)

    check_deadline()
    outcome = await archive_one_bounded_batch(prefix, profile, deadline)
    # 已提交：下一轮加载新的摘要、A 和完整请求成本。
    # 被其他 worker 占用：保留既有有界等待和取消逻辑。
    # 失败/无进展：明确退出，不返回 ready。
```

实现细节：

- 必须删除仅依赖 `pending_units <= compact_target` 的成功出口。
- 归档提交后，原文、摘要、消息数、包装及可选项空间全部重算；不复用旧 `extra_required_units`。
- 原文已经低于静态归档目标，但加上系统提示/摘要仍超限时，只要存在 N 轮之外的旧前缀，就应允许前台显式选择一批旧 Turn。后台“是否达到静态触发线”的规则不能阻止这个操作。
- 单批依然受摘要输入预算限制；不拆开完整 Turn 的归档覆盖语义，超大输入继续复用既有分块摘要能力。
- `nothing_selectable` 后再核验实际请求，仍超限即失败，不记录 ready。
- 本 worker 完成处理却没有推进覆盖、且没有可等待的其他活动任务时，应报告归档失败/无进展。其他 worker 正持有任务租约时可以有界等待，不能误判为永久无进展。
- 不要求每次提交后 U 必然下降：短前缀的摘要包装可能较大。以覆盖是否推进、剩余可归档前缀和总等待期限判断是否继续，最终仍以请求能否容纳判断成功。

### 4.4 Pin 与错误接线

预算探测期间不要把每个候选上下文反复写到同一 invocation 的 pin。优先在完整候选可发送后固定最终 pin；如果调用链已有 pin，则保持原快照，或用新的构建标识重建，不能原地换 A。

正常每轮回复的稳定 turn 标识与上下文构建标识应区分；若需新的 pin 标识，发送前/生成中的撤销检查必须使用实际标识，不能仍硬编码读取旧 `conversation:{turn_id}`。

覆盖读取异常不能当成“摘要成本为零”。传播既有 `ContinuationError`，记录对应状态。

容量不足使用已有/已定义的承接超限错误接入 worker，不能被 `ArchiveUnavailable → answer_without_history` 吞掉。最终 packer 或 wire gate 拒绝仍是失败，不代表前台压缩成功。

## 5. 顺序任务清单

路径中的 `app/`、`tests/` 相对 `backend/`。每项实现时同步补测试；全部步骤待完成。

| 状态 | 任务 | 前置 | 交付 |
|---|---|---|---|
| [x] | F01 补两个缺陷回归 | 无 | 修复前可复现失败的测试 |
| [x] | F02 移除会话原文预裁剪 | F01 | 未归档 Turn 不静默丢失 |
| [x] | F03 提取完整请求准备与预算入口 | F02 | 测量和实际发送使用同一请求 |
| [x] | F04 归档选择严格保护 N | F03 | 前台只归档可用旧前缀 |
| [x] | F05 重写归档成功条件及重算循环 | F04 | 每次提交后完整重建 |
| [x] | F06 接通 pin、异常、取消和超时 | F05 | 不重放旧边界、不伪报成功 |
| [x] | F07 验证真实发送与回归 | F06 | 发送层证据及测试结果 |

### F01：补两个缺陷回归

**文件**：`tests/test_archive_continuation.py`、`tests/test_archive_wait.py`。

动作：保留第 2 节两个复现构造；增加一个真实有预算 gateway 的调用链测试。不要只使用没有 `packing_limit/input_limit` 的 RecordingGateway，否则会绕开预算主链。

验收：旧实现分别失败在“返回轮数不足且无覆盖”和“完整超限却没有压缩/失败状态”，不是环境权限或 mock 不完整造成失败。

### F02：移除 `_history` 的破坏性预裁剪

**文件**：`app/conversation.py::_history`；必要时复用 `app/transcript.py` 的投影/渲染函数。

动作：构建整个 `(A,Q]` 后缀，投影工具结果，按完整 Turn 标记 required；移除主路径中 `pack_recent` 的删轮行为。相应计数事件报告实际完整后缀。

验收：5 轮案例返回完整 5 轮待预算处理；大单轮不被丢弃；最终请求不超限由后续步骤保证。不要单独将此中间状态当完成功能发布。

### F03：共用请求准备与预算入口

**文件**：`app/live_model.py`、`app/conversation.py`、必要的 `app/token_budget.py` 复用接线。

动作：提取实际消息和工具准备；系统提示、当前输入、摘要、目标状态、协议开销全部计入。保留必带/可选分组。停止使用“旧摘要正文长度”代表额外必需成本。

验收：仅增加当前用户输入、系统指令或工具 schema，前置预算结果会相应变化；可选项过大先被裁掉，不必归档；无重复扣减输出预留或包装成本。

### F04：前台归档只选择 N 轮之前的旧前缀

**文件**：`app/memory_archive.py::enqueue/_select_archive_prefix`、`app/conversation.py::_archive_attempt_once`。

动作：先确定不可归档的最近 N 轮，再在余下前缀选批次；允许完整请求超限时显式选批，即使原文尚未超过后台静态目标。复用现有提交、分块、幂等和租约规则。

验收：总共只有 N 轮时没有可归档来源；N+若干轮时仅旧前缀入选；某个受保护 Turn 特别大也不能把它划入归档前缀；后台触发规则不被意外改变。

### F05：依据完整请求循环重建

**文件**：`app/conversation.py::_archive_history_before_generation` 及其调用处。

动作：完整请求超限才执行前台补救；每次提交后刷新摘要/历史并重算。仅 fits 能返回 ready；无前缀、无进展、超限不得成功返回。

验收：6,000+5,000 对 10,000 的案例不再直接成功；归档后新增摘要变大时继续正确判断；第一次归档仍超限可继续下一批；已经可发送就结束。

### F06：接通固定快照、错误、超时与取消

**文件**：`app/conversation.py`、`app/live_model.py`、必要的 `app/memory_v2.py` pin 接线。

动作：预算探测不覆盖已固定 pin；最终摘要与原文使用同一 A/Q；保留撤销检查。容量错误不走无历史兜底，覆盖异常不按零成本处理。沿用统一等待期限和取消检查，期限不能每次重建重置。

验收：多次归档重建不会命中旧快照；取消/超时保留用户输入和已提交摘要；不可容纳时没有模型回复请求；等待其他 worker 的正常租约不被误报为无进展。

### F07：真实请求捕获与回归交付

**文件**：第 6 节测试及本文状态记录。

动作：从真实 worker 发起下一轮，使用真实 archiver 提交和真实 LiveConversationModel 打包，并在 mock transport 或具备 Profile 预算的 gateway 记录最终请求；不要求付费调用外部 LLM。运行相关回归并记录命令、数量、失败原因。

验收：最终消息含摘要与完整近期后缀，所有省略的旧 Turn 都有已提交覆盖；两项 P1 的独立回归通过；短会话、归档、pin、工具原子性和 wire gate 既有行为无新回归。

## 6. 必测场景

| 编号 | 场景 | 预期 |
|---|---|---|
| A1 | 五轮、N=5、没有已提交归档，内容无法容纳 | 保留五轮后明确超限；不会以一轮上下文生成 |
| A2 | 多于 N 轮，存在较旧前缀 | 先提交旧前缀摘要，再以摘要＋至少 N 轮原文发送 |
| A3 | 一个近期 Turn 超大 | 允许既有工具投影；仍超限则失败，不丢该 Turn、不归档保护后缀 |
| A4 | 较旧未归档 Turn 不在最近 N 内 | 不能直接裁掉，只有覆盖提交后才移出原文 |
| B1 | pending=6,000、extra=5,000、H=10,000、target=7,000 | 压缩或明确失败；不能零次处理就成功返回 |
| B2 | 没有已有摘要，但当前输入很长 | 当前输入计入完整预算，正确触发补救或失败 |
| B3 | 长系统提示、工具 schema 或必需目标状态 | 计入成本；不会等到最终 packer 才意外发现遗漏 |
| B4 | 可选长期记忆很大，必需内容可容纳 | 去掉可选内容即可，不为可选项强制归档 |
| B5 | 原文低于静态目标但完整请求超限，仍有旧前缀 | 前台可以选择旧前缀，不被静态目标挡住 |
| B6 | 首次归档减少原文，但新增摘要让请求仍超限 | 重算摘要/协议成本，再处理下一批或明确失败 |
| B7 | `nothing_selectable` 且仍超限 | 容量错误，不出现 ready/成功回复 |
| B8 | 归档失败、游标不前进 | 明确失败或按现有租约有界等待，不死循环 |
| C1 | 重建两次以上 | 最终 pin、摘要范围、原文范围一致 |
| C2 | 取消或等待超时 | 停止处理，保留输入及已提交归档，不重置期限 |
| C3 | 缺失摘要/覆盖校验失败 | 错误不被折算为零摘要成本，不降级无历史回答 |
| C4 | 工具调用和结果、协议修复提示 | 成对保留；实际附加内容重新计数 |

建议运行已有测试：

```powershell
# 从 backend/ 执行；基准目录使用本次新建的短路径，避免覆盖已有目录。
$fixTestRoot = Join-Path (Resolve-Path '../outputs') ('fx-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
python -m pytest tests/test_archive_continuation.py tests/test_archive_wait.py tests/test_context_hot_window.py tests/test_context_packing_limit.py tests/test_context_wire_gate.py tests/test_memory_archive.py tests/test_static_archive_policy.py tests/test_memory_conversation_flow.py tests/test_memory_v2.py -q -p no:cacheprovider --basetemp $fixTestRoot
```

此前复查选取的 76 项测试通过，但没有覆盖上述已复现边界。这个数字是修复前的局部基线，不能代替本轮验收。Windows 过长路径/临时目录权限导致的失败必须与业务断言失败分开记录。

## 7. 审查时拒绝的表面修复

- 仅提高摘要 priority，或在裁剪后加 required：找不回已经丢失的 Turn。
- 只把 N 改大、R 改大、窗口上限调大：不解决覆盖空洞和预算口径错误。
- 只在循环停止条件加旧 `extra_required_units`：仍遗漏其他必需内容，也没有计入新摘要。
- 将 `nothing_selectable` 当成功：没有可归档来源不等于请求可以发送。
- 所有预算失败都留给最终 packer：可恢复的旧前缀没有被压缩，会造成不必要的回复失败。
- 为通过测试而减少最近 N 轮、回退到无历史回答，或取消最终 wire gate。

交付报告需分别说明 P1-A、P1-B 的修复位置、独立复现结果和实际发送请求的证据。本次文档不修改业务代码、不标记任务已完成。

## 8. 实施记录

状态：F01–F07 已实施。日期：2026-09-19。

### 8.1 P1-A：先裁剪、后 required

**修复位置**：`app/conversation.py::_history`。

- 常规会话路径不再调用 `pack_recent`；仅保留 `project_context` 工具结果投影，随后对 `(A, Q]` 内每个完整 Turn 渲染 required 原子组。
- `context.counted` 事件改为报告真实完整后缀（`kept_turns`、`packing_limit`、`pre_cropped=False`），新增 `emit_count=False` 供归档循环内部重建使用，避免每轮重复写事件。
- `pack_recent` 仍保留，仅用于归档前缀选择（旧 `keep_tokens` 分支），未全局删除。

**独立复现**：`tests/test_archive_continuation.py::test_history_keeps_all_unarchived_turns_before_required_tagging`。5 轮、N=5、无已提交归档，`_history` 返回全部 5 个 Turn 且全部 `_context_required=True`。修复前 `pack_recent` 的 `recent_budget` 会释放软保护并裁到 1 轮。

### 8.2 P1-B：入口和出口未使用完整请求预算

**修复位置**：`app/conversation.py::_archive_history_before_generation`、`_archive_attempt_once`、`_hot_window`；`app/live_model.py`；`app/memory_archive.py`。

- `live_model.py` 把真实系统提示抽为 `conversation_instruction()`，拟人模式抽为 `HUMAN_MODE_INSTRUCTION`，并新增 `prepare_mandatory_request`/`mandatory_tools`，供前置预算与最终发送共用同一套消息构造。
- 常规会话前台归档改用“完整请求预算”模式：每轮迭代重建承接摘要与全部未归档 Turn，用真实系统提示、工具 schema、目标状态、当前输入和适配器 envelope 计算 `U(request)` 并对比 `H`；只有 `U <= packing_limit` 才返回 ready。
- 删除了 `pending_units <= compact_target` 成功出口；`nothing_selectable` 且仍超限 → `ContinuationContextOverflow`，不再记录 ready。
- 每次提交后完整重算，不复用旧 `extra_required_units`；`extra_required_units` 与仅摘要长度的 `_continuation_required_units` 已删除。
- 无预算的测试替身仍走 legacy 模式（原 `pending`/`compact_target` 逻辑），保证既有归档等待行为不变。
- `_hot_window` 现在会沿 `route_model.gateway.control_store` 解析真实 Profile，而不是只查适配器本身的属性；解析失败仍回退到 archiver retention。
- 归档选择器 `_select_archive_prefix` 在 `target`/`min_turns` 路径先切出受保护后缀，再在旧前缀内按 target 选批，绝不归档最近 N 轮；新增 `force_prefix` 允许完整请求超限时显式选择最旧批次，即使原文已低于静态目标。

**独立复现**：`tests/test_archive_continuation.py::test_foreground_archive_accounts_for_the_complete_request`。选择 `H` 使 `history_cost <= compact_target` 且 `history_cost + system_cost > H`，即旧出口会零次处理直接成功；断言前台仍提交了至少一批归档（`archived_through_seq > 0`）。

### 8.3 错误、pin 与取消

- 归档循环只读取 `provider.load_continuation`（不写 pin）；最终 `provider.select` 仍然只固定一次最终 A/Q 快照，`_history` 使用该 `memory_bundle.continuation_through_seq`。
- `_process` 捕获 `ContinuationError` 记录 `context.continuation_failed` 后继续抛出；`ContinuationContextOverflow` 不是 `ArchiveUnavailable`，不会进入 `answer_without_history`。
- 循环内 `ContinuationContextOverflow` 额外记录 `context.continuation_overflow`（含 required/limit/protected 轮数）。
- 单个 `WaitDeadline` 贯穿整个重建循环，取消检查与既有租约等待语义不变。

### 8.4 验证命令与结果

```text
python -m pytest tests/test_archive_continuation.py -q
# 19 passed（含 F01 两个独立回归）

python -m pytest tests/test_conversation_worker.py tests/test_live_model.py \
  tests/test_context_hot_window.py tests/test_context_packing_limit.py \
  tests/test_context_wire_gate.py tests/test_archive_wait.py \
  tests/test_static_archive_policy.py tests/test_memory_conversation_flow.py \
  tests/test_memory_v2.py tests/test_memory_archive.py tests/test_transcript.py \
  tests/test_action_help_fallback.py tests/test_save_message_plan.py \
  tests/test_recent_window_policy.py -q
# 313 passed

python -m pytest tests/test_conversation_api.py tests/test_context_budget_contract.py \
  tests/test_expert_model.py tests/test_model_control.py \
  tests/test_routed_model_gateway.py tests/test_startup_contract.py -q
# 84 passed, 1 failed（BETTER_AGENT_COST_MODE=enforce 下仍为既存的
# model_profile_versions 配置摘要唯一约束失败，与本方案无关）
```

### 8.5 已知边界

- `prepare_mandatory_request` 覆盖真实系统提示、工具 schema、目标状态、承接摘要、全部未归档原文与当前输入；分支附加提示见第 9 节，现已前置解析。
- legacy 模式（无 `prepare_mandatory_request` 的测试替身或不带 Profile 的对照）继续以原文前缀为代理判据，不代表生产口径。
- `test_configured_startup_activates_a_changed_model_profile` 是先前复查已存在的环境/档案唯一约束失败，不属于本次两个 P1。

## 9. 复查后续修复（预检计数口径与分支指令）

日期：2026-09-19。针对复查提出的两个新问题：

### 9.1 P1：预检把本地标记算进预算

**问题**：`_archive_history_before_generation` 的完整请求测量直接统计含 `_context_required`、`_context_group` 等本地提示键的消息；最终 `pack_messages_newest`/`assert_request_fits` 会先 `strip_packing_hints`。预检因此偏大，可复现“实际 9,494 ≤ 预算 10,039，预检 10,584 报 `continuation_context_overflow`”。

**修复**：`measure_full_request` 在计数前调用 `token_budget.strip_packing_hints(messages)`，并用清理后的消息数计算 `packing_limit`，与最终打包完全同口径（`app/conversation.py` 完整请求测量处）。

**回归**：`tests/test_archive_continuation.py::test_preflight_measurement_ignores_local_packing_hints`。构造 `stripped_cost <= H < hinted_cost`，断言前台归档不触发、不报超限。

### 9.2 P2：发送阶段才加入的必需指令预检漏算

**问题**：`prepare_mandatory_request()` 未包含 `inherited_plan_document_intent` 与 `save_existing_plan` 两个分支的附加系统指令，预检通过后发送阶段被 `pack_messages_newest` 以 `required model messages exceed the input budget` 拒绝。

**修复**：
- `app/live_model.py` 新增 `ConversationBranchState`、`resolve_branch_state()` 与 `_conversation_messages()`；`route_and_respond` 与 `prepare_mandatory_request` 都通过 `_conversation_messages` 构造消息，`route_and_respond` 接受外部传入的 `branch_state` 而不再重复分类。
- `app/conversation.py::_process` 在进入预算循环前解析一次分支状态（`resolve_branch_state`，同时把 gateway call context 提前到分类之前），并同时传给 `_archive_history_before_generation` 与最终 `respond`；预检与发送共用同一份分支决策。
- `ConversationService` 的 `RouteAndRespondModel` 协议与 `_FallbackConversationModel` 增加 `branch_state` 形参，兼容既有测试替身。

**回归**：`tests/test_archive_continuation.py::test_preflight_measurement_includes_branch_instructions`。构造 `plain_cost <= H < branch_cost`，断言循环必须归档或明确超限，而不是返回 ready。

### 9.3 验证结果

```text
python -m pytest tests/test_archive_continuation.py -q
# 21 passed（新增 2 个复查回归）

python -m pytest tests/test_conversation_worker.py tests/test_live_model.py \
  tests/test_context_hot_window.py tests/test_context_packing_limit.py \
  tests/test_context_wire_gate.py tests/test_archive_wait.py \
  tests/test_static_archive_policy.py tests/test_memory_conversation_flow.py \
  tests/test_memory_v2.py tests/test_memory_archive.py tests/test_transcript.py \
  tests/test_action_help_fallback.py tests/test_save_message_plan.py \
  tests/test_recent_window_policy.py tests/test_conversation_protocol_v2.py -q
# 346 passed

# BETTER_AGENT_COST_MODE=enforce 全量非集成
# 1239 passed, 2 failed（均为既存的环境/成本用例：test_cost_control 外键、
# test_golden_journey 目标复盘，与本轮无关）
```

## 10. 复查后续修复（预检工具选择与发送一致）

日期：2026-09-19。针对复查提出的工具定义口径问题：

**问题**：预检始终调用 `mandatory_tools()`（全部会话工具 schema），而实际发送在 `save_existing_plan=True` 时使用 `tools=[]`。已复现“实际请求 7,218 ≤ 预算 7,418，预检 9,733，误报 `continuation_context_overflow`”。

**修复**：
- `app/live_model.py::mandatory_tools(branch_state)` 现在按分支返回工具：`save_existing_plan=True` 返回 `[]`，否则返回 `CONVERSATION_TOOL_SCHEMAS`。
- `app/live_model.py::route_and_respond` 主发送改为显式 `send_tools = [] if save_existing_plan else None`，与预检同一分支判据（`None` 仍表示默认会话 schema，`[]` 表示无工具）。
- `app/conversation.py` 完整请求测量调用 `mandatory_tools(branch_state)`，正文成本与工具包装开销（`count_payload` 与 `packing_limit(len(messages), len(tools))`）使用同一份工具集合。

**回归**：`tests/test_archive_continuation.py::test_preflight_uses_branch_tool_selection_without_tools`。构造 `without_tools <= H < with_tools` 的保存已有计划分支，断言预检不触发归档、不报超限。

**验证**：

```text
python -m pytest tests/test_archive_continuation.py -q
# 22 passed（新增无工具回归）

python -m pytest tests/test_conversation_worker.py tests/test_live_model.py \
  tests/test_context_hot_window.py tests/test_context_packing_limit.py \
  tests/test_context_wire_gate.py tests/test_archive_wait.py \
  tests/test_static_archive_policy.py tests/test_memory_conversation_flow.py \
  tests/test_memory_v2.py tests/test_memory_archive.py tests/test_transcript.py \
  tests/test_action_help_fallback.py tests/test_save_message_plan.py \
  tests/test_recent_window_policy.py tests/test_conversation_protocol_v2.py -q
# 346 passed

# BETTER_AGENT_COST_MODE=enforce 全量非集成
# 1240 passed, 2 failed（仍为既存的 test_cost_control 外键与
# test_golden_journey 目标复盘环境失败）
```
