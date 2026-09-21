# D4 写入入口收敛与 v3 评测契约

日期：2026-09-20。状态：**D4 验收记录保留；v3 契约已完成本轮一致性审查，版本 `followup-intent-contract-v3.0`；案例、v3 评分器及盲测尚未实施**。本文最初编写于 D4 开发前，本轮在数据冻结前修订。第 5 节为已有实施记录，不代表本轮重新执行了这些测试。

## 1. 计划文档写入入口收敛

### 1.1 D4 修改前的问题（历史背景）

| 路径 | 触发 | 授权 | 缺口 |
| --- | --- | --- | --- |
| `create_plan_draft` → Tool Registry | 模型调用工具 | 写入审批（approvals + binding） | 只允许创建，已有文档返回 `PLAN_ALREADY_EXISTS` |
| 聊天 artifact（`v=2 plan_document upsert`）→ `PlanDocumentService.save_model_revision` | 模型返回控制头 artifact | 用户消息内的显式保存请求 | 直接提交新版本，覆盖已有文档，没有工具审批与调用身份 |

同一个“修改并保存”请求可能走两条确认强度不同的路径，违反“复用同一写入契约”。

### 1.2 收敛决策（D4 实施）

1. **单一提交层**：所有模型来源的文档写入最终都调用 `PlanDocumentService.save_model_revision`。工具写入使用 `operation_key`、版本检查与恢复契约；artifact 首次保存沿用真实来源轮次的幂等路径。不能把工具 operation key 的保证描述成所有旧入口都已迁移。首次保存必须保持 create-only，竞争创建失败不能转为无审批覆盖。
2. **授权分层**：
   - **首次保存（该会话尚无文档）**：保留 artifact 快路径。用户消息里的显式保存请求即授权，沿用现有策略；不新增审批。
   - **草稿创建**：`create_plan_draft` 工具，写入审批（保持不变）。
   - **修改已有文档**：新增 `modify_plan_document` 工具，写入审批。模型不得再通过 artifact 覆盖已有文档。
   - **用户在界面直接编辑**：`PUT /api/plans/...`（actor=user + 界面明确确认）继续直接写入，不属于模型来源。
3. **artifact 兼容规则（已实现）**：artifact 提交时若该会话已有文档，写入不再直接落盘，而是自动转换为一次 `modify_plan_document` 待审批调用（同一绑定内容、同一操作身份），turn 进入 `AWAITING_TOOL_APPROVAL`，用户看到与工具调用完全一致的审批卡片；批准后由工具契约提交新版本，拒绝则保持原版本。已删除文档不转换、不恢复。若目标工具不可用（`GOAL_TOOLS_ENABLED=0` 等），回退为 `plan.document_conflict` 记录且绝不覆盖。
   `v=2` 协议字段不变，只收紧写入语义。
4. **不隐式重编译（已实现）**：修改文档只产生新版本；已激活目标继续绑定原来源版本，工具返回 `execution_updated: false` 与说明。重新编译只能通过 `activate_goal_plan` 的预览流程。
5. **身份与幂等（已实现）**：工具身份、thread/project、来源与 operation key 全部由服务端注入；artifact 转换使用 `{turn_id, 内容摘要}` 派生稳定调用 ID 与 operation key，重放返回原版本。

### 1.3 迁移与兼容

- 无文档的老会话：行为不变，artifact 首次保存仍可用。
- 有有效文档的老会话且用户明确要求保存：artifact 自动转换为待审批的修改调用；工具不可用或无法安全转换时记录冲突，不覆盖。已删除文档不转换、不恢复。
- 已存在的 `save-plan`（UI“将此回答设为计划”）与计划页保存按钮不受影响，仍为显式用户操作。

## 2. `modify_plan_document` 工具契约（D4）

参数（严格类型，禁止额外字段）：

| 字段 | 规则 |
| --- | --- |
| document_id | 必填 string |
| expected_version_id | 必填 string，来自最近一次读取 |
| title | 必填 string，1–200 字符 |
| markdown_content | 必填 string，1–30000 字符，完整修订内容 |

行为：

1. 归属与项目范围校验（文档所属 thread 的 owner/project）；已删除文档返回 `RESOURCE_NOT_FOUND`，不恢复。
2. 事务内比较 `expected_version_id` 与当前版本；不一致返回 `VERSION_CONFLICT` 并给出当前版本提示。
3. 经现有写入审批写入新版本；返回 `version_id`、`version`、`content_hash`、标题、投影状态与 `execution_updated: false` 的明确说明。
4. 返回可交付引用，不创建目标、不激活、不开启提醒或复盘。
5. 恢复窗口：业务提交成功、工具结果未落盘时，用 operation key 找回同一版本，不重复写入。

错误码：`INVALID_ARGUMENT`、`RESOURCE_NOT_FOUND`、`VERSION_CONFLICT`、`INTERNAL_ERROR`（沿用既有定义）。

## 3. v3 评测契约：`followup-intent-contract-v3.0`

### 3.1 测量对象与优先规则

标签描述**当前用户请求的操作意图**，不描述已生效状态，也不是写入授权凭据。正确识别但当前没有调度能力，仍应识别该意图，并在实际回答中如实说明能力边界。

1. 当前用户的明确更正优先于旧偏好；未提及的其他服务不被连带修改。
2. 历史只用于恢复指代、原请求及既有状态；工具状态以案例提供的权威快照为准。助手单方面“已开启”的旧文本不作为成功证据。
3. 领域、每天投入时间、日程颗粒度不推导主动服务。用户自行复盘、模板、引用、假设和条件尚未成立的将来指令，不表示立即配置。
4. 不要求用户说固定关键词。按句义判定；“带着我”“照顾一下”若不能确定服务形式，应澄清，不能擅自开启所有维度。
5. tracking、review、reminder、document 独立；有一种请求不自动打开另一种。
6. 意图清楚但缺时间、时区、目标 ID 或其他执行参数时，保留已知意图。业务是否可执行另行判定，不把缺参数等同于分类错误。
7. 当前消息内部真正矛盾且没有明确更正关系时，仅将冲突维度标 clarify。明确的“不是……改成……”按最终意图标注。

### 3.2 每个目标的四维标签

| 维度 | 枚举 | 精确定义 |
| --- | --- | --- |
| tracking | none / enable / disable / clarify | none：没有本次跟踪配置变更，含保留已有服务、一次进展记录、查看今日任务及明确“不需要跟踪”；enable：要求开始持续记录进度、管理执行或恢复已停用的跟踪；disable：明确取消/暂停持续跟踪；clarify：无法确定是否要求开始或停止跟踪 |
| review | none / once / daily / weekly / event_recurring / other_recurring / stop / clarify | none：不要求助手开展或改变复盘服务；once：一次回顾分析；daily/weekly：按日/周反复复盘；event_recurring：以后每次指定事件后复盘；other_recurring：已明确的其他重复周期，如每月或每两天；stop：明确停止/暂停既有或用户声明既有的复盘服务；clarify：有相关服务意图但形式/频率不明确或矛盾 |
| reminder | none / once / recurring / stop / clarify | none：没有本次提醒变更，含保留已有提醒；once：一次提醒；recurring：重复提醒，包括日历或事件触发；stop：停止/暂停既有或用户声明既有的提醒；clarify：是否提醒或重复方式不明确 |
| document | none / create / modify / clarify | none：不要求持久化写入，含直接生成答案、模板和仅修改聊天中的内容；create：明确创建/保存新文档；modify：明确修改/保存到已有持久化文档；clarify：是否保存、写入哪份文档或创建/修改关系确实不明确 |

补充裁决：

- 单独开启、停止或变更复盘频率，tracking 均为 none。开启执行管理才是 tracking=enable；跟踪已有时“继续记录”是 none。
- “记录今天完成了两组训练”是一次反馈，tracking=none、review=none；这不表示反馈工具不该执行，只表示没有持续服务配置变更。
- “以后每次跑完步复盘”用 event_recurring，不能归 daily，也不使用易误读的 event_once。
- “每天我来找你时复盘”是 daily，但不授权主动触发；“以后我需要时再找你复盘”只是按需可用，不是周期配置，记 none。
- “帮我复盘今天”是 once；“只改成每周一次”是 weekly，tracking=none；“取消每日，换成每周”也是 weekly，不是 stop。
- “不需要每日复盘”在无既有服务时是 none；有既有每日服务且表达停止时是 stop。“取消”本身可表达用户认为存在的服务，即使后台状态未知也标 stop，执行必须另查状态。
- 停止跟踪不自动取消提醒或复盘；“这个目标所有跟进都停掉”按案例已知开启的三种服务分别标停止。范围或服务状态不足时先澄清，不猜测其他目标。
- 自动发起复盘本身不另外标 reminder=recurring；只有要求独立提醒（例如提前半小时提醒准备材料）才标 reminder。
- “写个每日复盘模板”“在攻略里写我每天自行回顾”只处理内容/文档，不开启 review。
- 仅有聊天 Markdown 不算已保存文档。“把这段改短一点”是 document=none；“把计划页面的第二天改轻松”是 modify；“修改这段后另存新文档”是 create。案例需明确文档存在与否，不能靠助手口头声称推定。

### 3.3 目标范围、触发方式与输出结构

输出严格 JSON 对象，顶层仅 `intents`（非空数组）。数组元素字段全部必填，禁止额外字段：

```json
{
  "intents": [
    {
      "target": "fitness",
      "tracking": "none",
      "review": "stop",
      "review_initiation": "not_applicable",
      "reminder": "none",
      "document": "none",
      "reason": "取消健身每日复盘"
    },
    {
      "target": "english",
      "tracking": "none",
      "review": "weekly",
      "review_initiation": "proactive",
      "reminder": "none",
      "document": "none",
      "reason": "英语每周日主动复盘"
    }
  ]
}
```

`target` 只能引用输入 `targets` 中的标识，或保留值 `unresolved`。输入表给出用户已提及的目标/内容对象及已知状态，不能泄露哪一个是答案。新攻略也有独立的案例标识，如 camping；它是评测引用，不是数据库 ID。输出不得虚构业务 ID。

- 每个当前请求的操作对象输出一项；同一目标兼有四维操作合并一项。同一目标显式要求保留原服务时，对应维度为 none。
- 只在历史出现、当前未涉及的目标不输出。当前明确说“健身照旧，再做露营攻略”，输出 fitness 的全 none 项及 camping 的内容交付项，不能把两者合为 daily。
- 目标不明时输出 unresolved，保留明确维度。例如“把它改为每周主动复盘”是 unresolved + weekly + proactive；不把已知频率改成 clarify。
- 无目标业务的普通问题用输入预置 general 内容对象。所有案例都提供至少一个候选，不需要空数组。
- 同一目标同时设置两个不同周期、或既有周期继续又要求本次复盘时，本协议只记录本次变更/请求。若明确同时新增两个周期，则超出 v3 单目标单策略范围：冻结前排除该种案例，记录为后续能力，不强行压成一个频率。

`review_initiation` 枚举：

| 值 | 使用条件 |
| --- | --- |
| not_applicable | review 为 none、once 或 stop，固定使用此值 |
| proactive | 明确要求助手定时/检测事件后主动发起 |
| user_initiated | 明确由用户来询问、报到或提交事件后才开展 |
| clarify | 已明确重复复盘，但主动/用户发起方式不明；或 review=clarify 且发起方式也不明 |

review=clarify 时若用户已明确“你主动”，仍保留 proactive；不能因为频率未知而丢失已知触发方式。仅说“每晚复盘”不默认主动。时间、时区、起止日期的抽取属于工具参数/E2E，不进入本次意图标签。

reason 只需简短引用/解释用户表达，不要求隐藏推理，不作为精确匹配字段。不得以 reason 覆盖错误标签。

### 3.4 配对例与范围约束

| 输入与已知状态 | 期望结果要点 |
| --- | --- |
| 做三日攻略，每天分早午晚 | tracking/review/reminder/document 均 none |
| 只提醒背单词，每天八点 | reminder=recurring，其余 none |
| 健身已跟踪，只把每日复盘改成每周，由我来发记录 | tracking=none，review=weekly，review_initiation=user_initiated |
| 以后每次跑完步我告诉你，你再复盘 | review=event_recurring，review_initiation=user_initiated，tracking=none |
| 系统检测到跑步结束后主动复盘 | review=event_recurring，review_initiation=proactive；分类正确不代表具备检测能力 |
| 每晚帮我复盘 | review=daily，review_initiation=clarify |
| 今天复盘一下，同时开启训练进度管理 | review=once，tracking=enable，review_initiation=not_applicable |
| 取消跑步每日复盘，英语改每周主动总结 | 两个目标：stop 与 weekly/proactive；不停止两者进度跟踪 |
| 只有聊天攻略，“改第二天，但别保存” | document=none；聊天仍应交付修改后的内容 |
| 已有文档，“修改第二天并保存” | document=modify；由实际工具审批保证写入 |
| 模板原文包含“自动每日复盘”，用户要求删掉 | 不开启 review；是否 modify 取决于明确修改持久化文档还是聊天内容 |

本版本覆盖意图分流，不覆盖任意复杂日历、复盘内容质量、通知实际送达或所有取消状态机。遇到契约外案例必须显式标注范围，不把它们悄悄删除后重算已有成绩。

### 3.5 数据、提示词与冻结流程

1. 使用本契约版本编写至少 **60 例**，首次运行前按语义场景族分为开发集至少 30、盲测集至少 30；同义改写、相同模板或多轮变体不能跨分组。旅游、健身、学习、工作/生活均需覆盖。
2. 每个子集至少覆盖：纯内容、文档创建/修改、只跟踪、只提醒、一次复盘、每日、每周、事件重复、其他周期、取消、保留原状、频率改变、跨目标、目标歧义、用户自行复盘及引用指令。每个子集至少 6 个明确 proactive daily 正例、12 个非 daily 负例、4 个多目标例、4 个澄清例；类别可重叠。审核 gold 与输入中的服务状态一致。
3. 案例包含 id、split、family、targets（标识/描述/已知服务状态与文档状态）、history、input、expected.intents、rationale。状态未知显式记 unknown。targets 是可见输入，不得包含 gold、暗示正确动作或编造用户约束。
4. 未评测前做确定性校验：枚举、目标唯一、输出字段、跨字段约束、覆盖数量、分组隔离及金标与状态相容。逐例复核文本含义，发现无法唯一标注的案例先修订再冻结，不制造争议金标。
5. 冻结输入、金标、契约、提示词、评分器及模型配置的版本/hash，并记录时间。旧 v1/v2 的数据与 PROBE 保持不变；现有 `followup_intent_eval.py` 仍是旧三维协议，不能直接声称支持 v3。
6. 新 v3 探针只发送本契约的分类指令和允许的案例输入，不拼接生产“JSON 控制头+正文”指令，避免格式冲突。另走真实聊天入口评测实际行为，双方结果分别报告；不把探针注册为生产必经分类器。
7. 先跑开发集基线，修复可用 v1/v2 及 v3 开发集。候选代码、提示词固定后，才一次运行盲测集。未执行前不查看盲测结果；不把盲测题目作为调优示例。因作者已接触案例，准确称“未用于调优的留出集”，不宣称第三方独立盲审。
8. 盲测运行后公布全部结果，失败保留。看过失败再修改时，该集降为回归集；下一次独立验收另建留出集。不能先看全量基线再把同一集合当未知盲测。
9. 若基础设施中断，保留已完成样本，恢复只执行尚未完成调用；结果未知的调用不盲目重试。重复真实调用作为独立试次报告，不能选最好的一次。标签/评分器确有错误时发新版本及勘误，不覆盖原结果。

### 3.6 计分与通过门槛

- **场景全匹配**：目标集合和每个目标四维标签及 review_initiation 全部正确，数组顺序不影响，reason 不参与。多/少目标、重复目标、非法字段、截断和 API 失败都使该场景失败；分母是预先冻结的全部场景。
- **各维准确率**：按预期目标逐项匹配。目标缺失/错误按错计；额外目标另计 hallucinated_targets，并使场景全匹配失败。单独报告 target 集合准确率、review_initiation 准确率、跨目标污染及各频率混淆，不只给一个总分。
- **daily 分类**：按目标计算 TP/FP/FN。错误目标上的 daily 同时产生该错误目标的 FP 和原目标的 FN；额外 daily 目标算 FP；未返回/协议错误不能算正确否定。另报非 daily 的误判比例，分母为预期非 daily 目标数；额外目标 FP 单列。
- **主动性越界**：无 proactive 意图却预测为 proactive，或实际回答承诺未经请求的周期服务，分别计意图越界和行为越界。每日用户发起被推成每日主动，即使 review 标签相同也不通过。
- **严格与诊断分离**：严格计分不修复模型 JSON、不从正文猜标签。可离线提供语义诊断，但不能替换主指标。API 失败、协议错误、模型语义错误分开统计。
- **留出集通过条件同时满足**：场景全匹配 ≥90%；review 准确率 ≥95%；daily FP=0、FN=0；跨目标污染=0；主动性越界=0；实际行为无无回执成功声明。按整数计数向上取整，不四舍五入制造达标。小样本通过不代表线上零错误。
- **真实行为评分**：调用生产聊天入口（目标工具评测须带真实 ToolRunner），由固定测试装置提供后续用户输入及批准/拒绝，保存全轨迹。首轮询问缺参数可合理，不能按“未立即调用”算错；也不能按“问了背景”算完成。尚未观察到频率承接标 incomplete，不计行为通过。只有工具回执或业务状态支持成功声明；调度暂不可用时如实说明可以通过能力边界检查，不能记调度成功。
- 行为审阅保存每例证据引用和原因，并注明开发助手审阅/人工独立审阅/模型裁判；未执行的层级记未执行。报告调用次数、模型别名、配置、用量和费用依据，不用探针得分替代端到端成绩。

### 3.7 实施完成条件

本轮完成的是契约修订，不是 v3 工程实施。后续一次性实现：严格 Pydantic/schema、输入 allowlist、目标数组与触发方式计分、数据校验/冻结 manifest、开发/留出 split 选择、原始响应审计与离线恢复。评分器用手工构造的正确/缺目标/多目标/跨目标 daily/主动性越界/格式错误/调用失败测试验证，再运行真实模型。

本契约内例子可直接作为评分器测试种子，不得复用为留出题。完成案例复核与确定性校验后可按上述流程冻结，不再追加一次形式化许可；如遇实质矛盾，明确勘误，不能为避免修改而保留错误。

## 4. 实施顺序

1. 补齐 D3 审批展示（preview / cleared_fields / defer）——**已完成**。
2. 本文契约——**已完成定义**。
3. D4：`modify_plan_document` 工具 + artifact 覆盖拒绝 + 提示词与工具描述更新 + 测试——**已完成**（artifact 改为自动转换为待审批修改调用）。
4. 工具端到端（新增“修改文档”场景）与 v3 冻结验收——**已有工具 E2E 记录为 7/7；v3 按第 3 节完成评分器、分组案例校验后冻结，先开发集、后留出集**。

## 5. 实施状态与验收证据（2026-09-20）

实现位置：

- `backend/app/goal_tools.py`：`ModifyPlanDocumentParams`、`_modify_plan_document`、恢复路径与注册；activate 工具描述明确“只传 mode/program_id/expected_version”。
- `backend/app/conversation.py`：`_suspend_artifact_modification`（artifact→待审批修改调用）与 `_finish_success` 的 `AWAITING_TOOL_APPROVAL` 终态；删除文档不转换、不恢复。
- `backend/app/live_model.py`：提示词要求修改已有文档必须使用工具；同一失败工具调用重复出现时停止重试并给出有界最终回答。
- `frontend/src/components/ChatToolApprovalCard.tsx`：preview 展示日期/时区/每日预算/可用日与休息日；反馈展示 `cleared_fields`；延期使用独立详情；修改展示文档/基准版本与修改后内容。

验收结果：

```powershell
cd D:\RAG\better\backend
python -m pytest tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py `
  tests/test_plan_document_service.py tests/test_plan_documents.py tests/test_plan_document_api.py `
  tests/test_save_message_plan.py tests/test_messages.py tests/test_conversation_api.py `
  tests/test_v2_acceptance.py tests/test_approval.py tests/test_tool_execution_claims.py `
  tests/test_tools.py tests/test_runtime.py tests/test_materializer.py tests/test_live_model.py `
  tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py tests/test_goal_workspace.py `
  tests/test_plan_context.py -q
# 291 passed

python -m pytest tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py -q
# 10 passed

cd ..\frontend; npm run build; npm test -- --run
# 345 passed

python scripts\chat_tool_e2e.py --execute --out ..\docs\evaluation\chat-tool-e2e-2026-09-20-final
# 7/7 通过（25 次调用）：攻略交付零写入、保存 committed、执行管理预览→确认→激活、
# 修改文档新版本且已激活目标来源不变、拒绝、取消、重启恢复
```

真实模型 E2E 中又发现并修复一个缺陷：模型对同一失败调用（activate 误带预览字段）连续重试耗尽迭代预算；现在第二次相同失败即停止工具调用，并生成有界最终回答说明失败原因。

残余：v3 案例尚未冻结与运行；按第 3 节契约实施评分器、开发集与留出集。已激活目标在文档修改后需要用户主动重新生成预览，系统不会自动重编译。

## 6. 本轮代码核对发现的实施缺口

契约一致不等于全部代码已满足契约。本轮未修改生产代码或重跑第 5 节全套测试。

- **首次 artifact 保存的 create-only 保护已补齐**：`conversation.py::_finish_success` 现在显式传 `create_only=True`。进一步核对发现，正常已存在文档原本会因版本不符拒绝；原问题主要是另一入口在空检查后创建并删除文档时，旧路径可能恢复删除状态并保存新版本。新增确定性交错故障注入，分别验证并发创建和创建后删除，两种情况下已有内容、版本和删除标记都保持不变，记录冲突而不覆盖或恢复。SQLite 与 PostgreSQL 共用这组断言；本轮相关测试 37 passed（含 5 个 PostgreSQL 用例）。这不是对所有并发窗口的全局保证。
- **旧评分器不可直接运行 v3**：现有 Intent/PROBE 仍将复盘包含在 tracking 中、没有 reminder/目标数组/发起方式；必须增加版本隔离实现，不改变 v1/v2 历史协议后重算旧结果。
- 本次顺带移除了 `conversation.py` 该事件写入行的尾随空格。

因此后续实施顺序是：补首次保存竞争窗口及测试 → 实现并验证 v3 评分器 → 校验并冻结案例分组 → 开发集调试 → 候选冻结后运行留出集。这个顺序不需要再修改本契约的标签语义。
