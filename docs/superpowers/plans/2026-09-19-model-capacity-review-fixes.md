# 模型容量改造：审查问题修复任务书

日期：2026-09-19。范围：修复当前容量改造的配置一致性问题；本文件不代表修复已实施。

前置设计：[模型容量驱动窗口改造](./2026-09-19-model-capacity-driven-context-window.md)。本次不重做归档算法，不执行付费长上下文调用，不自行填写 DeepSeek 的 1M 对应整数。

## 一、已确认问题与完成目标

| 编号 | 优先级 | 已复现现象 | 修复目标 |
| --- | --- | --- | --- |
| F01 | P1 | 手动窗口 32768、Tier A、admitted=131072，实际预算总窗口仍为 131072 | 手动上限进入运行时预算，且可落库、重载 |
| F02 | P1 | DeepSeek 目录未核实，客户端提交 verified + 1000000 仍可保存 | 服务端生成容量结论与证据，不信任客户端声明 |
| F03 | P2 | LLM_AP 配置 65536，内存模式 manual，证据记录却为 auto | 所有入口通过同一解析过程生成一致快照 |
| F04 | 配套 | 前端缓存容量结果，变更模型／端点／协议后未失效 | 表单展示与提交对应当前模型，服务端仍最终校验 |
| F05 | 待排查 | 费用预留测试的金额和记录条数断言失败 | 确认根因并恢复有效测试，不能预先归因于容量改造 |

F01—F03 是独立复现的问题。F04 是源码检查发现的配套风险，不宣称已完成浏览器复现。F05 是测试失败，尚未确认是业务回归、测试假设失效还是运行环境影响。

此前本轮检查结果：相关后端 152 通过、1 失败；前端 ControlPages 12 通过。Git 报 `fatal: bad object HEAD`，无法用版本差异确认问题引入时间。

## 二、任务 F01：让手动工作窗口真正约束预算

涉及文件：

- `backend/app/config.py`：`_resolve_window_contract` 和各配置加载入口。
- `backend/app/model_capacity.py`：`resolve_working_window`。
- `backend/app/token_budget.py`：`effective_input_budget`。
- `backend/app/model_admin.py`：`_budget_contract`。
- `backend/app/model_control.py`：持久化版本恢复。

根因：手动窗口只写入 `context_window`，未必写入 `soft_context_limit`。Tier A 仅在 `context_window_verified` 为真时用 context_window 限制预算；手动上限并不是官方容量证据，不能通过强行标 verified 修复。另外，管理服务目前拒绝 `soft < admitted`，与“用户可选择更小工作窗口”的目标冲突。

开发步骤：

1. 统一手动模式的规范化结果：将明确的用户上限写入 `soft_context_limit`，在运行配置和容量证据中保持一致。
2. 保留接入容量 admitted 与手动上限 soft 的不同含义；允许合法的 `soft < admitted`，检查输出预算小于最终有效窗口。
3. Tier A 的实际总窗口按适用上限取最小值。不要把用户手动值标成“官方已核实容量”。
4. 确保环境加载、管理 API、新版本落库、运行时重载均使用相同规范化结果。
5. 保留无新增模式字段的历史冻结配置语义；不要让原来无来源的默认 32K 意外限制已验证的大容量配置。

验收用例：

| 输入 | 预期 |
| --- | --- |
| manual=32768、Tier A、admitted=131072、output=8192，附合法原有证据 | 有效总窗口 32768，输入预算 H=24576；这是扣输出后的预算，尚未扣具体请求打包开销 |
| 上述配置经创建、落库、重载 | 窗口和 H 不变，soft=32768 |
| manual=32768、output≥32768 | 创建／加载时拒绝，不等到发送时才失败 |
| 无模式字段的历史 Tier A 配置 | 既有冻结语义保持不变 |
| 修改窗口后生成 hot_window／归档策略 | 使用修正后的 H，前后台一致 |

交付物：修复代码、配置加载与持久化链路测试；不能只测试解析器。

## 三、任务 F02：保存模型配置时由服务端核验容量

涉及文件：`backend/app/model_admin.py`、`backend/app/model_capacity.py`、`backend/app/api.py`、`backend/app/model_capacity_migration.py`。

根因：`_budget_contract` 使用请求体中的 `capacity_evidence.status`、`model_context_limit` 等字段进行校验，实际只检查客户端数据是否自洽，未核对当前端点和目录。客户端可以把尚未核实的 DeepSeek 保存为 verified。顶层 working_window_mode 也不能仅靠证据对象间接表达。

开发步骤：

1. 在创建模型和新增版本的公共保存路径，根据实际 base_url、协议、model_name、用户模式、手动上限和输出预算调用容量解析器。
2. 客户端只提供配置意图。由服务端生成 effective window、status、source、model limits、counter 来源及容量证据。
3. 对客户端提交的“已核实”结果、与顶层字段冲突的记录，选择明确拒绝或丢弃后重新解析；统一策略并补测试。不得原样落库。
4. 当前目录中的 DeepSeek auto 未核实，公共保存接口应拒绝并提示手动模式。manual 可按规则保存，但不得标为官方容量已核实。
5. 现有接入证据若需要独立于官方目录使用，必须通过已有受信任服务端来源验证；不能以浏览器传入一段 evidence 文本作为核验完成的依据。
6. 环境注册、旧配置导入和迁移需要兼容保守默认值时，通过明确的内部调用路径传入规范化记录。公共 API 不能靠 `source=legacy-conservative-default` 字符串获得例外。
7. 历史不可变版本按原快照读取，不在每次调用时重新查最新目录；新版本创建时才生成新的证据快照。

验收用例：

- 官方 DeepSeek、auto、伪造 verified + 1000000：返回可解释的 422，不创建版本。
- 顶层 auto 但不传 capacity_evidence：同样由服务端解析，不能退回旧字段校验绕过检查。
- 未知代理复用官方模型证据：不能登记为 verified。
- manual 配置未先查询容量：后端仍检查已知模型输出上限等约束。
- 使用服务端注入的合成已验证目录：auto 保存成功，返回值、落库值与目录一致。
- 客户端 effective limit 与 context_window 冲突：拒绝或以统一规则重新解析，不保存双重结论。
- 环境保守配置可注册，旧版本可读取；公共 API 不能冒充内部兼容来源。

交付物：服务端统一保存校验、创建与新增版本 API 测试、内部注册／迁移兼容测试。

## 四、任务 F03：统一 LLM_AP 的窗口解析与证据

依赖：F01 的规范化规则；持久化验收与 F02 联调。

涉及文件：`backend/app/config.py`、`backend/app/model_capacity.py`、`backend/app/model_admin.py`、`backend/app/model_control.py`。

根因：`load_llm_ap` 先调用环境解析器生成证据，再覆盖 context_window、mode、status、source，却没有重新生成 capacity_evidence。文件中的显式值也可能绕过统一解析器的上限检查。

开发步骤：

1. 先收集文件配置与适用环境配置，明确冲突优先级，再进行一次统一解析。
2. 建议保留此入口当前文件显式窗口优先的行为；环境 soft/admitted 与其冲突时按共同契约处理，不能静默留下两套值。
3. 将文件的 LLM_CONTEXT_WINDOW 作为显式手动输入传给解析器，不在解析完成后覆盖结果。
4. 从最终解析结果同时构造 ModelProfile 和 capacity_evidence。若保留 `llm-file` 来源，内存与记录必须一致。
5. 检查 evidence 的透传逻辑，避免已有旧字符串覆盖本次解析结果；确需保留历史原始证据时，将其与本次窗口解析快照区分。

验收用例：

- 文件 `LLM_CONTEXT_WINDOW=65536`：profile 与 record 都为 manual，工作窗口均为 65536，soft=65536，来源一致。
- 注册不可变版本后重载：模式、来源、窗口、输出预算和 H 与注册前相同。
- 文件与环境显式窗口冲突：按文档化优先级执行；与约束冲突时明确报错。
- 文件窗口超过合成已核实模型容量：统一拒绝，不能后置覆盖绕过。
- 文件不含窗口：按统一 auto／保守兼容规则处理，不产生伪造容量证据。

交付物：入口修复、往返持久化测试、配置优先级说明。

## 五、任务 F04：修正控制台容量缓存及请求契约

依赖：F02。

涉及文件：`frontend/src/pages/ModelsPage.tsx`、`frontend/src/api.ts`、`frontend/src/types.ts`、`frontend/src/__tests__/ControlPages.test.tsx`。

开发步骤：

1. base_url、协议或模型名变化时，清空原容量结果；查询结果绑定请求所使用的三元组。
2. 对并发查询采用取消或序号校验，防止旧查询后返回覆盖新模型的结果。
3. 保存时提交模式、手动值等意图，不再把前端构造的 capacity_evidence 当作真实容量来源；采用服务端返回的最终结果展示。
4. auto 的未核实提示可保留，但不能以此替代服务端校验；manual 不需要用户先点击“查询容量”才能正确校验。

验收用例：查询 A 后改为 B；A 请求晚于 B 返回；未查询直接保存 manual；auto 保存被后端判为未核实。各场景均不能保存或显示错误模型的容量。

交付物：表单修复和交互测试。该任务不能替代 F02 的后端修复。

## 六、任务 F05：排查费用预留测试失败

可独立于 F01—F04 执行。

失败测试：`backend/tests/test_model_control.py::test_evaluation_attempt_pins_price_snapshot_while_regular_call_uses_current_price`。

已观察到：实际调用费用对应的断言通过，但 RESERVE 记录金额和条数不符合预期；期望两条 140、1400，实际出现更多记录且含金额 0。尚未确认根因。

开发步骤：

1. 使用独立临时目录单独运行该测试，再与 model_control 测试组一起运行，区分稳定失败和测试相互污染。
2. 检查实际账本中 entry_type、invocation_id、price_snapshot_id、amount、状态和写入时机，定位额外／零金额记录产生的路径。
3. 对照当前费用预留业务契约确认：是错误多记账，还是账本设计已变但断言仍依赖旧条数。
4. 若实现错误，修复预留流程；若测试陈旧，按真实业务不变量更新断言，同时保留价格快照冻结、余额预留和结算一致性检查。不得只删断言让测试通过。
5. 在报告中写明根因及与本轮窗口改造的关系；Git 无法比较时不得声称已证明是旧问题。

验收：测试独立运行及与相关测试组合运行均通过，预留／结算不会重复扣费，评估调用使用冻结价格、普通调用使用当前价格。

## 七、任务 F06：集成回归与验收记录

依赖：F01—F05。

开发步骤：

1. 汇总新增回归用例，确保前三个复现输入分别验证修复后的结果。
2. 验证新版本规范化、保存、恢复后的完整链路；不要只验证内存字段。
3. 重跑容量、预算、发送前检查、归档续接、静态归档策略、管理 API、模型控制和控制台测试。
4. 更新 `docs/context-budget/model-window-acceptance.md`，记录本次命令、通过／失败数及尚未完成项。
5. 旧持久化版本不原地改写；若需要纠正旧错误模式或窗口，创建可审阅的新版本并明确绑定范围。

后端参考命令（在 backend 目录执行；basetemp 使用本轮专用且不存在的目录，pytest 可能清理该目录）：

```powershell
python -m pytest tests/test_model_capacity.py tests/test_context_budget_contract.py tests/test_context_wire_gate.py tests/test_archive_continuation.py tests/test_static_archive_policy.py tests/test_model_admin_api.py tests/test_model_control.py -q --basetemp=../outputs/capacity-fix-acceptance-01
```

前端参考命令（在 frontend 目录执行）：

```powershell
npm test -- --run src/__tests__/ControlPages.test.tsx
npm run build
```

完成标准：

- 手动 32K 在 Tier A 全链路中确实限制为 32K。
- 公共 API 无法凭客户端标记把未核实容量保存为 verified。
- LLM_AP 的内存配置、证据、持久化版本与重载结果一致。
- 控制台不会复用其他模型的容量结果。
- 费用预留失败已解释并解决，相关测试通过。
- 不把本轮修复描述为“已自动使用 DeepSeek 1M”。当前目录精确容量仍未核实、计数器仍为 UTF-8 估算；修复配置一致性不等于完成容量和 tokenizer 验证。

## 八、建议执行顺序

F01 → F02 → F03 → F04 → F06；F05 可单独排查，但须在 F06 前闭环。

每项提交应包含对应实现和回归测试，并在交付记录中标明完成状态。本文创建时，所有任务均为待实施。

## 九、执行结果（2026-09-19）

F01—F06 均已完成代码修复及离线验收。后端主回归 322 项通过，配置/费用专项 12 项通过，补充文件边界用例 1 项通过；前端 13 项通过，生产构建通过。专项与主回归存在重复用例，不将数量简单相加作为独立覆盖数。

具体实现、命令、费用测试根因和未部署边界见 [本轮验收记录](../../context-budget/model-window-acceptance.md)。未修改现有数据库的冻结版本，未执行付费模型调用。
