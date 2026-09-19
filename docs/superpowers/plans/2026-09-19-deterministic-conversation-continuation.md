# Better Agent：归档摘要确定性注入开发方案

日期：2026-09-19  
状态：待实施。本文为开发方案，不代表代码已修改或测试已通过。  
范围：普通会话 `ConversationTurnWorker → LiveConversationModel → Gateway` 的上下文续接。

## 1. 目标与验收底线

用户要求：生成摘要后，下一轮回复必须基于“归档摘要＋最近几轮原文＋当前用户消息”。这里的“包含摘要”指发送给 LLM 的请求上下文，不要求把内部摘要展示在用户可见的回答里。

需要修复的核心行为：

```text
当前：近期原文 + 按查询相关度和小额预算选中的 Episode（可能为空）
目标：按归档覆盖区间确定的必带摘要 + 未归档近期原文 + 当前消息
      + 有剩余预算时加入的长期记忆等辅助上下文
```

必须同时满足：

1. 下一轮构建请求前已经提交的归档，必须由本轮的承接摘要覆盖；选择与当前问题是否命中关键词无关。
2. 被归档游标排除的原文有对应摘要承接，不能出现“原文退出了，摘要也没进入”的静默空洞。
3. 摘要与近期对话在最终发送给模型的消息中保留，不能只在某个中间变量里出现。
4. 默认保护最近 5 个完整历史 Turn；有模型配置时使用 `history_min_turns`。当前用户消息另计。
5. 必需内容确实无法容纳时，返回明确的上下文容量错误，保留用户输入。不得悄悄去掉摘要或最近几轮后继续生成。

这份文档细化此前《模型上下文预算与压缩：分期开发实施方案》中“摘要确定性注入”的要求。已有文档的历史代码描述不能代替当前源码事实。

## 2. 当前实现与缺口

| 位置 | 当前行为 | 本次要解决的问题 |
|---|---|---|
| `memory_archive.py::ConversationArchiver._commit` | 在同一事务中保存 Episode、推进 `archived_through_seq` | 复用此提交边界作为覆盖事实源 |
| `conversation.py::_history` | 根据归档游标排除旧原文，再构建近期历史 | 目前没有同步装入同一覆盖区间的必带摘要 |
| `memory_v2.py::MemoryContextProvider.select` | 当前会话 Episode 按关键词相关性、新近程度排序，并受 `episode_token_budget` 限制 | 不读取归档游标，不提供覆盖保证 |
| `MemoryContextRequest` | 默认 Episode 预算为 1,000；实际计数器是 UTF-8 字节上界 | 摘要可能因大小被整条跳过 |
| `conversation.py::_process` | 记忆块 priority=60，但 required=False | 优先级提高仍不等于必带 |
| `token_budget.py::pack_messages_newest` | 支持 required/group，可裁剪可选消息组 | 需要把承接摘要和保护轮次纳入 required 集合 |
| `memory_v2.py::_binding/_pin/_save_pin` | 固定检索内容，支持 Episode 编辑/删除后的失效 | 需要固定覆盖边界及必带摘要，不能重放归档前的旧快照 |
| `live_model.py::route_and_respond` | 还会再次打包；保存已有计划等分支可能绕过普通 history | 所有实际发送路径都要保留承接契约 |

注意两个相关现状：

- 摘要生成上限 1,600 实际是序列化 JSON 的 UTF-8 字节上限，不是精确模型 tokens；召回渲染后还会增加标记和来源信息。
- 前台超限归档按模型预算判断，但 `_archive_attempt_once` 没有向归档选择器传入同一套目标与轮次保护，仍可能走 `keep_tokens=12_000` 的旧分支。本次必须修正该接线，才能保证近期轮次不会在注入之前就被归档掉。

## 3. 实施范围

本期复用现有 Episode、归档游标、归档任务、租约、Profile 预算和上下文 pin。

本期完成：确定性选择、分离必带/可选上下文、完整请求预算、近期轮次保护、缓存一致性、错误处理和真实发送边界测试。

本期不新增：事实抽取系统、向量召回方案、第二套归档状态机、自动恢复补读 Agent、递归摘要模型链。原始对话继续保留在数据库。

首版承接摘要可以由多个已提交 Episode 按来源顺序组成，不要求先实现一条不断重写的滚动摘要。所有承接摘要共同构成一个必带块。其持续增长后的明确容量边界见第 7 节，不能宣称首版支持无限长度对话。

## 4. 确定性覆盖契约

### 4.1 一次请求使用一个边界

为当前请求构建 `ContinuationSnapshot`（建议名称，新增内存数据结构即可）：

```text
owner_id / thread_id
archived_through_seq: A
history_through_seq: Q       # 本轮用户消息之前的历史上界
episode_versions: [(id, version, start_seq, end_seq), ...]
rendered_summary
coverage_hash               # 范围、版本、正文及渲染版本的摘要
```

规则：

- 在前台必要归档结束后构建快照。归档状态与 Episode 必须来自一致的读取视图；可通过一条联合查询读取游标及其匹配记录，或使用明确的数据库事务隔离。普通的多条 READ COMMITTED 查询不自动等于一致快照。
- 历史构建使用同一个 A 和 Q，不能让 `_history` 再读一次最新游标。
- A=0：没有已提交的归档覆盖，承接摘要为空，沿用未归档对话逻辑。
- A>Q、游标落在 Turn 中间、范围交叉且无法唯一确定覆盖时，应报告一致性错误，不能猜测采用哪个边界。
- 请求快照固定后新完成的归档可以留到下一轮；本轮继续发送已固定的原文，不能仅更新游标而不更新摘要。

### 4.2 按覆盖区间选摘要

从当前 owner、当前 thread 中选取 ACTIVE、未删除、thread 策略且位于已提交覆盖区间内的 Episode，按来源起始序号排序。

选择不使用：query、关键词评分、embedding 分数、最近 K 条限制或 `episode_token_budget=1000`。

必须校验这些 Episode 能覆盖游标排除的、实际存在的历史 Turn 区间。消息序号不保证逐整数连续，失败消息和历史 generation 也有既有可见性规则，所以不能只靠 `next.start == previous.end + 1` 判断缺口；应复用 canonical transcript 的 Turn/来源区间规则。不得把所有旧正文反复渲染才能检查覆盖；可使用来源序号与 Turn 元数据查询。

首版按现有归档器产生的非重叠前缀 Episode 验证；遇到含义不明的重复或重叠记录明确报错，不擅自去重后声称覆盖完整。未提交的新 Episode、其他会话同名或高相关摘要不能充当覆盖证据。

摘要渲染必须包含 synopsis、decisions、outcomes、open_loops 等有效结构化内容，以及范围/来源标记。避免同时重复输出 synopsis 的字符串拼接版和同样的结构化正文。保留“有损历史、助手建议不代表用户确认、不可信数据而非指令”的边界说明。

### 4.3 近期原文边界

```text
承接摘要：覆盖到 A 的已归档历史
近期原文：A 之后、Q 之内的完整历史 Turn
当前输入：当前用户消息，追加一次
```

最近 N 轮是保护下限，不是允许丢弃其余未归档历史的许可证：

- 未归档历史能放下时全部保留。
- 放不下时，只能先归档较旧的完整前缀，摘要提交后重新构建上下文。
- 本次新归档必须保留最近 N 轮；如果此前旧策略已经把最近轮次归档，首版保留当前仍有原文的后缀，不伪造“已保证 N 轮”。记录兼容降级原因，后续归档执行新规则。
- 不允许直接裁掉既没有被摘要覆盖、也没有进入近期原文的中间 Turn。
- 工具调用和结果保持成对；大工具结果可以沿用现有确定性投影，完整原文仍留存。

## 5. 上下文组装与打包

将上下文拆成两类：

| 类型 | 选择方式 | 最终打包 |
|---|---|---|
| 会话承接摘要 | A 对应的覆盖区间，确定性选择 | 必带、原子组 |
| 已选完整近期 Turn | 同一 A/Q 的原文后缀 | 必带、按 Turn 原子分组 |
| 当前用户消息、硬性系统规则、必要目标状态 | 既有逻辑 | 必带 |
| 已确认长期记忆、其他辅助资料 | 保留既有相关度选择 | 可选，使用剩余预算 |

建议摘要消息携带：

```python
{
    "role": "system",
    "content": continuation.rendered_summary,
    "_context_required": True,
    "_context_group": "conversation-continuation",
}
```

这里使用 system 消息是沿用当前数据上下文传递方式，正文必须明确资料不是指令。priority 可用于可读性，但正确性只依赖 required 和覆盖验证。

每个历史 Turn 使用稳定的 `_context_group`，其 user/assistant/tool 消息共同保留。完整必需块装不下时，应在前置归档阶段解决或报错，不能交给最终 packer 静默裁掉某几轮。

普通会话不要再次把承接 Episode 混入可选 `memory-context`。首版可将该路径的可选 Episode 检索预算设为 0，同时继续召回长期记忆；其他用途的 Episode 检索保持现有行为。

## 6. 预算与前台归档

预算必须依据实际路由 Profile，并使用项目已有 `effective_input_budget`、适配器开销和最终 wire gate。不要建立另一套字符数预算。

构建顺序：

1. 加载硬性系统提示、工具定义、当前输入、必需目标状态。
2. 读取承接快照，构建近期完整 Turn；计算必需请求的完整成本。
3. 若可容纳，加入可选长期记忆、计划、Skill 等上下文，由既有优先级处理剩余预算。
4. 若不可容纳且存在最近 N 轮之前的未归档前缀，使用当前 Profile 的目标启动/等待归档。提交后重新读取快照并重算；不是只在未归档原文超过 H 时才启动。
5. 若已无可归档前缀，或必带摘要、最近 N 轮、当前输入本身超限，明确失败。
6. `LiveConversationModel` 最终打包仍保留所有 required 组；Gateway 发送前校验最终协议 payload。

不能把 canonical transcript JSON 的测量结果当成完整请求成本。系统指令、工具 schema、消息包装、摘要渲染和输出预留都必须计入现有预算链。

前台 `_archive_attempt_once` 应显式传递模型派生的保留目标和 `min_turns`，不要借用后台是否跨过静态触发线的判断，也不能回退到默认 12,000 的保留选择。循环继续/停止条件以“重建后的完整请求可容纳”为准，并保留现有等待时限和取消行为。

Profile fallback 时也不能重新裁掉必带摘要。较小模型无法容纳同一个必需上下文，应跳过该候选或返回容量错误。打包提示移除后，fallback 仍需保留必需上下文的身份/校验信息，不能把已经清洗过的 messages 当成可任意裁剪的普通历史。

## 7. 边界行为

| 场景 | 要求 |
|---|---|
| 未生成摘要/归档失败未提交 | A 不前移，继续使用未归档原文；放不下则沿用明确失败路径 |
| 游标已前移但摘要缺失、删除、失效或覆盖不完整 | 报告覆盖不可用；不默默回退为“只看当前问题” |
| 用户删除摘要 | 尊重删除，不从 pin 或旧副本重新注入；给出明确可恢复状态 |
| 摘要被编辑 | 新请求用新版本，旧 pin 失效；发送前/生成中撤销检查仍有效 |
| 所有承接 Episode 累计过大 | 明确容量错误；首版不按相关度删摘要，也不机械截断摘要正文 |
| 单个近期 Turn 或当前输入过大 | 在现有允许的工具结果投影之后仍超限则报错，不降低 N 后静默生成 |
| 等待归档超时或取消 | 保留原输入与已提交摘要，沿用时限和取消语义 |
| 保存已有计划的快捷分支 | 可以减少重复计划正文，但不能绕过必带承接摘要和近期上下文契约 |

建议为覆盖缺失和必需上下文过大提供可区分的 reason code，例如 `archive_coverage_missing`、`continuation_context_overflow`。这些是拟新增状态，不是当前已存在的错误码。

后续若需要支持更长会话，可单独实现“把旧 Episode 合并成覆盖相同区间的新摘要”的滚动压缩。届时也必须保留原摘要来源和事务替换语义。本期验收不以这项扩展为前置条件。

## 8. Pin、重试与撤销一致性

确定性注入不能绕开现有记忆撤销机制。

建议复用 `MemoryContextBundle` 和既有 pin 表，增加承接上下文的结构化部分；保持可选检索正文与必带摘要分开，不能把整个长期记忆包设为 required。

接口契约建议：

```text
MemoryContextBundle
  rendered                       # 可选检索块，保留既有语义
  continuation_rendered          # 必带摘要块
  continuation_through_seq
  continuation_episode_versions
  continuation_hash
```

实施要求：

- pin 固定 A、Q、Episode id/version/范围、承接正文、渲染版本及预算/Profile 标识；覆盖与近期原文边界必须成套复用。
- 把承接 Episode 也注册进 `memory_context_pin_items`，使编辑/删除能触发现有失效链。
- 区分“复用同一次已固定的模型调用”与“同一用户输入重新构建上下文”。前者复用快照；后者须使用新的构建/调用标识，不能覆盖旧 pin，亦不能在旧 pin 下偷偷推进 A。
- pin 已存在时先加载其覆盖边界，再构建对应的原文后缀；不能先按新 A 截历史，再拿旧摘要 pin。
- 采用新 renderer/payload 版本存储分开的正文。可在既有 payload 文本列中保存带版本的 JSON envelope，并在 `_pin/_save_pin` 成对处理；旧版本仍走旧读取规则，不能把 envelope JSON 直接发给模型。绑定哈希验证必须覆盖新增字段。
- pin 的 token_count 按实际渲染消息计量，存储 envelope 的 JSON 大小不能混作模型上下文计数。
- 发送前与生成中的撤销检查必须覆盖必带摘要。pin 尚未注册期间发生编辑的竞态，也要通过版本复核或事务校验发现。

## 9. 开发任务拆分

以下任务全部待实施。按 T01 → T14 顺序执行，每项完成时记录改动位置、验证命令及结果。每项都要随实现补充对应测试，T14 负责汇总回归，不把测试全部推迟到最后。中间步骤可单独提交供审查，但完成主链前不按成品发布。

文中 `app/...`、`tests/...` 均相对仓库的 `backend/` 目录；实际执行先进入 `backend/`。新增接口名称可以调整，契约与验收条件不能省略。

| 状态 | 任务 | 前置依赖 | 交付结果 |
|---|---|---|---|
| [x] | T01 固定现状与复现用例 | 无 | 可重复的归档后漏摘要测试 |
| [x] | T02 定义承接快照与错误契约 | T01 | A/Q、版本、正文、哈希的数据结构 |
| [x] | T03 实现按覆盖区间加载和验证 | T02 | 不依赖 query 的确定性 Episode 集合 |
| [x] | T04 实现承接摘要渲染 | T03 | 完整、去重、可计量的摘要块 |
| [x] | T05 扩展 pin 存储与重试绑定 | T04 | 覆盖边界与摘要成套固定 |
| [x] | T06 按固定边界构建近期原文 | T05 | 同一 A/Q 下的完整 Turn 后缀 |
| [x] | T07 接入普通会话组装 | T06 | 必带摘要与可选检索分离 |
| [x] | T08 统一完整请求预算与必带打包 | T07 | 不会被二次打包裁掉的承接内容 |
| [x] | T09 修正前台归档与重建循环 | T08 | 模型预算、归档目标、N 一致 |
| [x] | T10 打通错误、超时和取消处理 | T09 | 明确失败且保留用户输入 |
| [x] | T11 补齐重试、工具及快捷分支 | T10 | 所有会话发送路径保持承接契约 |
| [x] | T12 验证 fallback 与撤销竞态 | T11 | 换模型、编辑、删除时不静默漏摘要 |
| [x] | T13 增加最终发送审计 | T12 | 可证明摘要实际被发送的记录 |
| [x] | T14 完整回归与文档交付 | T13 | 验收矩阵及变更说明 |

### T01：固定现状并建立漏摘要复现用例

**文件**：新增 `tests/test_archive_continuation.py`；参考 `tests/test_memory_conversation_flow.py` 和 `tests/test_memory_archive.py`。

**动作**：

1. 检查适用仓库指令和工作区改动，记录实际起点；版本控制读取失败时记录失败，不能假定工作区干净。
2. 用现有 runtime/数据库测试工具构造已完成历史，调用真实 `ConversationArchiver` 提交摘要，保留最近原文。
3. 下一轮使用与摘要无关键词重叠的问题，在 `LiveConversationModel` 最终打包之后捕获模型请求。
4. 加入一个渲染后超过旧 1,000 字节召回预算、但仍能装进完整请求的摘要用例。单纯“问题不相关”不一定复现，因为当前逻辑也可能选择未命中的摘要。
5. 记录现有相关测试基线；测试临时目录必须隔离且可写。

**验收**：新用例因“最终请求缺少应有摘要”失败，不能因模型未配置、工具 mock 不完整或目录权限失败而冒充有效复现。保留失败输出，后续步骤将其修绿。

### T02：定义承接快照与错误契约

**文件**：`app/memory_v2.py`、`app/memory_archive.py`；`tests/test_archive_continuation.py`。

**动作**：

1. 定义 `ContinuationSnapshot`，包含 owner/thread、A、Q、Episode id/version/范围、渲染正文及哈希。
2. 扩展 bundle，分别表达可选检索正文和必带承接正文。新字段提供兼容默认值，避免破坏其他现有调用。
3. 定义覆盖缺失与必需上下文超限的错误类型/原因码，明确调用方不能把它们当作“可以无历史回答”。
4. 明确 A=0 的空快照、A>Q 的非法快照，以及同一次请求冻结后边界不变的约束。

**验收**：结构测试覆盖空快照、非法边界、稳定哈希和版本变化；既有非会话记忆调用仍能构造 bundle。

### T03：按覆盖区间读取并验证 Episode

**文件**：`app/memory_v2.py`；必要时在 `app/transcript.py` 复用/提取来源区间查询；`tests/test_archive_continuation.py`。

**动作**：

1. 增加确定性加载入口，参数包含 owner/thread/Q，不接受 query 作为选择依据。
2. 一致地读取已提交游标及对应 Episode，按来源序号排序；排除未提交范围、删除记录和其他作用域。
3. 通过 Turn/来源序号元数据验证被 A 排除的历史确实有摘要覆盖，不反复加载全部旧正文。
4. 检测缺口、重叠、重复覆盖、边界位于 Turn 内等问题；按第 4 节规则报错。

**验收**：改变 query 不改变选择；多段摘要全部入选；自然序号跳号不误报；缺少一段会报错；不同 owner/thread 的高相关 Episode 不能补洞。生产 PostgreSQL 的一致性读取行为应有验证，不能仅靠 SQLite 测试推断。

### T04：渲染独立的必带摘要块

**文件**：`app/memory_v2.py`；`tests/test_archive_continuation.py`。

**动作**：

1. 按来源顺序渲染 synopsis、decisions、outcomes、open_loops 等结构化内容和范围标识。
2. 避免 synopsis 文本重复输出；对旧版或经用户编辑、结构化字段为空的 Episode，使用其当前有效 summary 文本。
3. 加入有损历史、来源归属、不可信资料及助手建议不是用户确认的说明。
4. 使用现有计数器计算实际渲染成本；不套用可选 Episode 的 1,000 字节预算，不截断正文。

**验收**：outcomes 不丢失；旧版/编辑后摘要可读；多段顺序稳定；相同概要不重复；超过旧召回预算但可容纳的正文保持完整。

### T05：扩展 pin，并区分复用与重新构建

**文件**：`app/memory_v2.py::_binding/_pin/_save_pin`；`tests/test_memory_v2.py`、`tests/test_archive_continuation.py`。

**动作**：

1. 将 A/Q、Episode 版本/范围、承接正文及哈希、renderer 版本与预算/Profile 标识加入快照绑定。
2. 新 payload 版本分开保存可选正文与必带正文；读取时解码回结构，不能把存储用 JSON envelope 发给模型。
3. 兼容旧 payload 读取；旧 pin 没有承接契约时不得假定其已经满足新请求。
4. 把承接 Episode 注册到现有 pin items，复用编辑/删除失效链。
5. 固定模型调用重试复用原快照；重新归档并构建上下文时创建新的构建/调用标识，不覆盖旧 pin。

**验收**：pin 读写往返不改变 A/Q、正文或哈希；编辑/删除使 pin 失效；同调用不会串入新边界；新构建不会误用归档前 pin；旧 payload 不作为乱码正文注入。

### T06：使用固定 A/Q 构建近期原文

**文件**：`app/conversation.py::_history`、`app/transcript.py`；`tests/test_context_hot_window.py`、`tests/test_archive_continuation.py`。

**动作**：

1. `_history` 接收已固定的覆盖边界，不再自行查询最新 `archived_through_seq`。
2. 读取 `(A, Q]` 内的完整历史 Turn；当前用户消息由主请求追加一次。
3. 保留 Turn 身份，为后续打包生成稳定 group，工具调用和结果属于同一原子上下文单元。
4. 延续现有工具结果投影；不足预算时返回需要归档/容量处理的状态，不直接裁出未被摘要覆盖的空洞。
5. 保留 A=0 的短会话兼容行为，以及旧归档已经侵入最近 N 轮时的明确兼容记录。

**验收**：摘要对应旧原文不重复出现；近期后缀连续；当前用户消息不重复；工具成对；固定 A 后归档游标前进不会改变本轮历史边界。

### T07：接入普通会话的摘要＋近期原文组装

**文件**：`app/conversation.py::_process`、`app/memory_v2.py`；`tests/test_memory_conversation_flow.py`、`tests/test_archive_continuation.py`。

**动作**：

1. 加载既有 pin 或创建承接快照，再用其 A/Q 构建历史。
2. 注入独立 `conversation-continuation` 必带组，近期 Turn 也标记为 required 原子组。
3. 长期记忆仍按相关度选择，保持可选；普通会话关闭重复的可选 Episode 注入。
4. 无归档时不创建空摘要消息；其他用途的 Episode 检索保持原行为。

**验收**：T01 的不相关问题、大摘要用例在预算充足时通过；多段摘要都进入下一轮请求；可选记忆没有被连带变成必带项。

### T08：在完整请求上预算并保留必带组

**文件**：`app/live_model.py`、`app/token_budget.py`、`app/conversation.py`；`tests/test_context_packing_limit.py`、`tests/test_context_wire_gate.py`。

**动作**：

1. 提取/复用请求准备逻辑，使前置预算与实际发送使用相同系统提示、工具定义、消息和 Profile；避免复制两套提示词构造。
2. 先计入硬性规则、必需目标状态、承接摘要、近期原文及当前输入，再用剩余预算加入可选资料。
3. 最终 `pack_messages_newest` 保留 required 组；清理内部提示字段不改变消息内容与分组结果。
4. 必需内容超限时返回可供 T09 处理的预算结果；最终 Gateway 仍执行完整协议 payload 校验。

**验收**：加入长系统提示和工具 schema 后仍准确识别容量；可选项先被舍弃；必带组不会因优先级或二次打包消失；超限请求不会直接发给 provider。

### T09：修正前台归档与重建循环

**文件**：`app/conversation.py::_archive_history_before_generation/_archive_attempt_once`、`app/memory_archive.py::enqueue/_select_archive_prefix`；`tests/test_archive_wait.py`、`tests/test_static_archive_policy.py`。

**动作**：

1. 改用 T08 的完整请求预算结果决定是否需要前台归档，覆盖“原文未超过 H，但完整请求超限”的情况。
2. 归档调用显式传入实际 Profile 的保留目标与 N，移除该路径对默认 12,000 保留窗口的依赖。
3. 只归档最近 N 轮之前的完整前缀，复用现有归档提交事务、租约与来源校验。
4. 提交后创建新构建快照，重新获取 A/Q、渲染摘要、计算完整请求；可容纳就停止。
5. 无可归档前缀、归档后无有效进展或等待到期时停止循环，交由 T10 的明确错误路径处理；保持后台触发比例不变。

**验收**：模型大小改变时保留策略随 Profile 变化；最近 N 轮不被新归档吞掉；完整请求可容纳后停止；无进展不死循环；不会因重新构建误用旧 pin。

### T10：打通失败、超时和取消行为

**文件**：`app/conversation.py`、`app/memory_archive.py`；`tests/test_archive_wait.py`、`tests/test_archive_continuation.py`。

**动作**：

1. 将覆盖缺失、必需块超限与既有归档失败、等待超时区分，记录机器可识别原因和用户可理解消息。
2. 覆盖缺失/必需块超限不得进入 `answer_without_history` 兜底。
3. 保持用户输入持久化、已提交归档不回滚、停止请求及时生效等既有语义。
4. 用户删除摘要后不从旧 pin 或原文自动重建被删除内容；不在提示中承诺普通重试一定能恢复。

**验收**：各失败场景不发送残缺模型请求；错误可区分；原输入仍在；取消和超时仍满足现有时限。

### T11：补齐全部会话发送分支

**文件**：`app/live_model.py::route_and_respond`、`app/conversation.py::_process`；`tests/test_archive_continuation.py` 及相关会话测试。

**动作**：

1. 逐一检查普通生成、协议修复重试、ask 工具后续调用和保存已有计划分支。
2. 重试保留同一承接快照；追加修复提示或工具结果时重新计入完整预算。
3. 快捷分支即便提取了最新计划，也必须携带必带摘要与规定的近期原文。
4. 每条分支捕获真正交给 gateway 的请求，验证承接内容，而非仅检查 history 参数。

**验收**：各分支都包含相同覆盖哈希对应的摘要，且当前输入不重复；新增提示/工具结果导致超限时明确失败，不偷偷删摘要。

### T12：验证模型 fallback 与摘要撤销竞态

**文件**：`app/model_control.py`、`app/model_gateway.py`、`app/live_model.py`、`app/memory_v2.py`；相关 Gateway/记忆集成测试。

**动作**：

1. 保留必带上下文身份，使 fallback 不会在内部打包提示移除后把摘要当普通历史裁掉。
2. 每个候选 Profile 重新验证容量；小模型放不下时跳过或明确失败，不扩大其容量配置。
3. 复核 snapshot 读取到 pin 注册之间的 Episode 版本变化；注册后依靠现有失效检查阻止发送旧摘要。
4. 覆盖发送前编辑/删除、生成期间撤销，以及后台并发提交的场景。

**验收**：fallback 实际发出的请求仍满足覆盖契约；发生撤销时没有成功提交基于旧摘要的回复；并发归档不混合新游标和旧摘要。

### T13：增加最终发送层的审计证据

**文件**：`app/conversation.py`、`app/live_model.py`、必要的 Gateway 调用位置；事件相关测试。

**动作**：

1. 构建事件记录 A/Q、Episode 版本/范围、覆盖哈希、保留 Turn 数、完整预算及 pin 命中情况，不记录正文。
2. 最终发送前验证必带内容；在实际调用路径记录 applied/dispatched 证据，与仅构建成功的事件区分。
3. 记录缺口、容量不足、取消、超时和旧轮次兼容降级原因。
4. 重试关联到各自模型调用标识，避免在未真正发送时报告已应用。

**验收**：一条成功请求能追溯到实际发送的覆盖快照；被预算或撤销拦截的请求没有虚假的发送成功记录。

### T14：完整回归并交付变更说明

**文件**：第 10 节列出的测试；`docs/memory-system-boundaries.md`、`docs/memory-system-explained.md` 和本文。

**动作**：

1. 逐项执行第 10 节验收矩阵，标明自动化测试名称和结果。
2. 执行既有记忆、上下文预算、归档、会话及 fallback 相关回归；与 T01 基线区分新失败和既有环境问题。
3. 对 PostgreSQL 一致性读取/并发行为补充集成验证，不能把 SQLite 通过当成全部数据库验证通过。
4. 更新说明：摘要按覆盖区间必带，长期记忆按相关度可选，默认保护 N 轮，多段摘要累计过大时明确失败。
5. 提交变更清单、验证命令、结果与已知边界；仅完成全部验收后勾选功能完成。

**验收**：核心链路有“真实归档提交 → 下一轮实际模型请求”的自动化证据；无未经说明的相关测试失败；业务代码和文档描述一致。

## 10. 验收测试矩阵

测试至少包含一次真实 `ConversationArchiver` 提交和下一轮 `LiveConversationModel` 打包后的请求捕获；只构造一个带 `_context_required=True` 的字典不足以验收。

| 测试 | 必须验证的结果 |
|---|---|
| 不相关问题 | 归档后用户问无关键词重叠的问题，最终 messages 仍包含该范围摘要 |
| 大于旧召回预算 | 渲染后超过 1,000 字节、但完整请求可容纳的摘要仍进入最终 messages |
| 多次归档 | E1 覆盖较早区间、E2 覆盖后一区间；两个摘要均按来源顺序出现，不只带最新一个 |
| 最近 5 轮 | 最终请求含最近 N 个未归档完整 Turn，当前输入只出现一次 |
| 旧历史无空洞 | 任一被省略的可见历史 Turn 必须有已提交摘要覆盖 |
| 不重复 | 已归档原文不再常规重复发送；承接 Episode 不在可选检索块再出现一次 |
| 完整请求压力 | 加入长系统提示、工具 schema、Skill、长期记忆后，先去掉可选项，摘要和保护轮次仍在 |
| 必需块超限 | 无可归档前缀时明确失败，Gateway 未发送缺摘要的降级请求 |
| 工具原子性 | tool call/result 成对，投影不修改数据库原结果 |
| 缺失/删除摘要 | 不跳过缺口，不从旧 pin 恢复已删除正文；返回覆盖不可用 |
| 修改摘要 | 新请求使用新版本，固定调用被撤销后不能继续使用旧正文 |
| 范围隔离 | 同 owner 不同 thread、不同 owner 的 Episode 均不能参与覆盖 |
| 并发提交 | 固定 A 后后台再提交，只能整套沿用旧边界或整套重建，不混用新游标与旧摘要 |
| 重试缓存 | 归档前后的请求不能误用旧 pin；固定快照的协议重试仍带同样的必需摘要 |
| 前台归档 | 实际选择遵循 Profile 的保留目标和 N，不使用默认 12,000 兜底裁掉最近轮次 |
| 特殊分支 | 保存计划、ask 后续请求、协议修复及 fallback 都符合承接契约 |
| 无归档 | A=0 不强行生成摘要，不改变短会话现有行为 |
| 取消/超时 | 已保存输入和已提交摘要不丢失，仍遵守既有等待时限 |

相关已有测试：`test_memory_conversation_flow.py`、`test_memory_archive.py`、`test_context_hot_window.py`、`test_context_packing_limit.py`、`test_context_wire_gate.py`、`test_archive_wait.py`、`test_static_archive_policy.py`。建议新增 `test_archive_continuation.py` 承载上述跨模块回归。

测试环境若无法写入默认临时目录，应使用可写且隔离的测试目录；PermissionError 属于环境失败，不能据此宣称业务回归通过。

## 11. 可观测性与完成标准

请求构建事件记录：固定 A/Q、Episode id/version/范围、覆盖哈希、保留的 Turn 数、完整请求预算、是否命中 pin。日志不记录摘要正文。

实际模型调用的审计必须区分“构建了摘要”与“发出了摘要”。在最终打包完成、Gateway 发送路径上核对必带块的哈希/存在性，再记录 applied 状态；现有可选 memory 回调不能作为必带摘要已发送的唯一证据。

完成标准：归档已成功提交且预算可容纳时，无论下一问相关度如何，模型实际接收的内容都包含该覆盖区间的承接摘要与完整近期后缀；所有不满足此条件的情况都有明确错误或撤销状态，不静默继续生成。

## 12. 实施记录

状态：T01–T14 已实施；核心链路有自动化证据，验证结果见下。日期：2026-09-19。

### 12.1 改动位置

| 文件 | 改动 |
|---|---|
| `app/memory_v2.py` | 新增 `ContinuationEpisode`、`ContinuationSnapshot`、`ContinuationError`/`ArchiveCoverageMissing`/`ContinuationBoundaryError`/`ContinuationContextOverflow`；`MemoryContextRequest` 增加 `history_through_seq`/`include_continuation`；`MemoryContextBundle` 增加承接字段；`MemoryContextProvider` 增加 `load_continuation`/`continuation_snapshot`（一致读取游标与 Episode、按 Turn 元数据校验覆盖、按来源顺序渲染、coverage hash）；pin 使用 v2 envelope 分开存储可选正文与必带正文，并把承接 Episode 注册进 `memory_context_pin_items`；`_save_pin` 复核 Episode 版本，关闭读取到注册之间的竞态 |
| `app/conversation.py` | `_history` 接受固定 A/Q 边界并对每个 Turn 打必带原子组；`_process` 先计算 Q、构建承接快照，再注入 `conversation-continuation` 必带组，普通会话可选 Episode 预算设为 0；前台归档接收 `extra_required_units` 并按完整请求判断触发；`_archive_attempt_once` 显式传入 `window.static_policy()`（目标与 `min_turns`），移除默认 12,000 兜底；`ContinuationError` 记录 `context.continuation_failed` 并明确失败，不进入 `answer_without_history`；记录 `context.continuation_built` 审计事件 |
| `app/live_model.py` | 所有发送分支（含保存已有计划）统一携带 history，不再用计划正文替换必带承接；最终打包后校验承接正文仍在 `bounded_messages`，缺席即报 `context_overflow`；记录 `context.continuation_dispatched` 并携带 coverage hash |
| `app/token_budget.py` | `pack_messages_newest` 与 `strip_packing_hints` 统一剥离所有 `_context_*` 本地提示键 |
| `app/memory_archive.py` | 未改逻辑；`enqueue(static_policy=...)` 既有路径由前台归档接线复用 |
| `tests/test_archive_continuation.py` | 新增 16 个跨模块回归用例 |
| `tests/test_conversation_worker.py`、`tests/test_action_help_fallback.py` | 测试替身兼容 `_history`/`_archive_history_before_generation` 的新关键字参数 |
| `docs/memory-system-boundaries.md`、`docs/memory-system-explained.md` | 补充确定性承接摘要规则与覆盖边界说明 |

### 12.2 验证命令与结果

```text
python -m pytest tests/test_archive_continuation.py -q
# 16 passed

python -m pytest tests/test_archive_continuation.py tests/test_archive_wait.py \
  tests/test_static_archive_policy.py tests/test_action_help_fallback.py -q
# 77 passed

python -m pytest tests/test_memory_conversation_flow.py tests/test_memory_v2.py \
  tests/test_context_hot_window.py tests/test_transcript.py tests/test_archive_wait.py -q
# 78 passed

python -m pytest tests/test_conversation_worker.py tests/test_live_model.py \
  tests/test_context_packing_limit.py tests/test_context_wire_gate.py -q
# 183 passed
```

关键证据（`test_archive_continuation.py`）：真实 `ConversationArchiver` 提交 Episode 后，无关问题与大摘要两用例都在最终 `route_and_respond` 的 `messages` 中捕获到 summary；`context.continuation_built` 与 `context.continuation_dispatched` 的 coverage hash 一致；删除/编辑摘要使 pin 失效；固定 A 后后台提交不混用新旧边界。

### 12.3 已知边界与环境说明

- `BETTER_AGENT_COST_MODE` 默认 `observe`，本机未设 `enforce` 时，`test_cost_control.py`、`test_m5_release_gates.py`、`test_real_evaluation.py`、`test_model_control.py`、`test_routed_model_gateway.py`、`test_startup_contract.py`、`test_evaluation_api.py` 中与预算/价格相关的用例会失败；设置 `BETTER_AGENT_COST_MODE=enforce` 后除 3 个与模型档案唯一约束/启动相关的既存失败外全部通过，均与本方案无关。
- `test_golden_journey.py`、`tests/integration/*`（PostgreSQL）依赖数据库/网络环境，本机不可判定，需在目标环境单独执行。
- 首版承接摘要由多个已提交 Episode 组成，不实现滚动重写；累计过大时按 `continuation_context_overflow` 明确失败。更长的会话需要后续单独实现“合并旧 Episode 为覆盖相同区间的新摘要”。
