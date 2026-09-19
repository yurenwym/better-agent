# Better Agent：基于模型真实容量的上下文窗口改造任务书

日期：2026-09-19。交付范围：开发设计与任务拆分；本次不修改业务代码。

## 1. 目标与证据状态

将固定 32K 默认工作窗口改为：**按实际接入端点和模型版本解析容量，默认跟随已核实的容量，允许用户设置更小的工作窗口；压缩策略按解析后的预算计算。**

本次在线核验未完成：读取 DeepSeek 公开文档的提权请求被自动审批服务拒绝，返回审核模型配置错误（404）。没有绕过该拒绝。以下规格来自仓库已有官方页面快照，不代表本次联网确认。

| 项目 | 本地快照内容 | 使用限制 |
| --- | --- | --- |
| 模型 ID | `deepseek-flash` | 别名可能随供应商更新 |
| 对应版本 | `DeepSeek-V4.1-Flash` | 必须保存核验日期和版本映射 |
| OpenAI 格式端点 | `https://api.deepseek.com` | 不自动推广到代理端点 |
| Anthropic 格式端点 | `https://api.deepseek.com/anthropic` | 单独核实协议及计数方式 |
| 上下文 | `1M` | 尚未核实精确整数及输入、输出合计规则 |
| 最大输出 | `384K` | 是支持上限，不是本应用必须预留的输出量 |
| tokenizer／计数 API | 未确认 | 不得标记为精确计数已验证 |

证据文件：`backend/artifacts/learning-v2-official-pricing.html`。

SHA-256：`1B0717A8E5A91E1E1802ADFEBF84A557569BEB67EC99E1C787EE4BDB65AE514B`。

待联网核验的官方入口：

- https://api-docs.deepseek.com/quick_start/pricing
- https://api-docs.deepseek.com/quick_start/token_usage
- https://api-docs.deepseek.com/api/create-chat-completion
- https://api-docs.deepseek.com/api/list-models

不得因为页面写了 `1M` 就自行选择 `1000000` 或 `1048576` 写入生产配置；不得把 `/models` 的模型存在性或一次连通性检查当作容量证据。

## 2. 当前实现与改造边界

- `backend/app/config.py` 的 ProviderPreset、通用环境配置和旧配置加载路径均包含 32768 默认值；只改一个入口不够。
- `backend/app/model_gateway.py` 的 ModelProfile 也有默认窗口；控制台表单同样默认提交 32768。
- `backend/app/token_budget.py` 当前默认计数器按 UTF-8 字节估算。配置 `counter_id` 本身不会替换实际计数算法。
- 模型配置存在不可变版本和运行时冻结快照。修改默认值不会更新已有会话引用的版本。
- 当前会话续接已经按归档覆盖边界注入摘要，再拼接未覆盖的近期消息；该行为必须保留，不回退为相关度检索摘要。
- 默认保护最近 5 个完整 Turn；工具调用与结果不得拆开。
- 当前后台基于未归档历史预算触发，前台基于完整请求兜底。保留双层机制，本次不强制改成新的触发比例。

不在本次范围：LLM + Harness 整体重构、重做记忆检索、修改业务回复方式。摘要是下一次模型请求的上下文，不是直接作为面向用户的答复返回。

## 3. 目标数据契约

下面为逻辑字段，开发时优先映射已有字段，避免重复保存同一含义。

| 概念 | 建议表达 | 规则 |
| --- | --- | --- |
| 官方模型容量 | `model_context_limit` | 经证据确认的 token 整数 |
| 端点接入容量 | 复用 `admitted_context_limit` | 代理限制、已验证接入限制；证据不能伪造 |
| 工作窗口设置 | `working_window_mode=auto/manual`，复用 `soft_context_limit` 保存手动值 | auto 不写死 32K |
| 实际工作窗口 | `effective_context_limit`，解析结果 | auto 取适用且已核实容量的最小值；manual 再加入用户上限取最小值 |
| 本次输出预算 | 复用 `max_output_tokens` | 独立设置，例如继续使用 8192 |
| 官方输出上限 | `model_max_output_limit` | 用于校验，不自动充当本次输出预算 |
| 计数方式 | 复用 counter 与 evidence/version 字段 | 必须与运行时实际计数器一致 |
| 规格证据 | 复用／扩展 `capacity_evidence` | URL、核验时间、端点、模型版本、原文、快照哈希 |

概念公式：`W = min(已核实模型容量, 适用的端点限制, 可选手动上限)`；输入预算由现有预算契约在 W 上扣除本次输出等必要项得到 H。内部完整计算保持一致，不能只修改 UI 数字。

未知端点没有可信容量时，不启用“已验证 auto”。保留显式手动／旧版保守模式并显示来源；不得静默继承官方端点的 1M。

现有 Tier A 仍须满足容量、计数和协议证据要求；本次不通过虚构 evidence 绕过校验，也不启用尚未实现的 Tier B。

## 4. 分步开发任务

### T01 — 完成官方规格核验并保存证据

依赖：可用的正常联网访问。其他设计与离线测试可先做，但生产 auto 容量录入依赖此项。

操作：

1. 打开上述官方页面，确认 `deepseek-flash` 当前实际版本、两个协议端点各自支持情况。
2. 确认 `1M`、`384K` 的精确整数语义，以及窗口是否包含输出、思考 token 等。
3. 查找该版本官方 tokenizer 或计数能力，记录不存在／无法确认的情况，不借用旧版 tokenizer 冒充兼容。
4. 保存原始页面、URL、UTC 获取时间和 SHA-256；将历史快照与本次证据分开。

产物：`docs/context-budget/model-capacity-evidence.md` 和证据快照。

验收：每个启用的容量整数可追溯到官方说明；无法证实的字段标为 unresolved，不能自动上线。

### T02 — 统一容量与工作窗口契约

依赖：无；真实数值依赖 T01。

涉及：`model_gateway.py`、`token_budget.py`、配置 DTO。

操作：区分官方容量、端点限制、工作窗口、输出上限和本次输出预算；确定 auto/manual 解析规则、未知容量行为及错误提示。明确旧 `context_window` 的兼容含义，并仅在解析边界转换一次。

产物：类型定义、字段映射与优先级说明。

验收：同一配置只产生一个有效 W；用户手动上限不能超过已知接入限制；输出预算必须小于 W 且不超过已核实输出上限。

### T03 — 建立按端点与版本匹配的容量目录

依赖：T02；真实目录条目依赖 T01。

涉及：新增容量目录模块／数据文件，`config.py`。

操作：以 provider、规范化端点、协议、模型 ID 和已知版本为匹配条件；保存别名映射核验日期和证据版本。端点规范化不得将不同代理、租户路径或协议混为同一端点。

产物：可测试的容量解析器；DeepSeek 经核实条目。

验收：官方端点命中正确条目；同名代理不自动命中；未知模型明确返回未验证状态；别名更新不会改写旧快照。

### T04 — 打通所有配置入口与优先级

依赖：T02、T03。

涉及：`config.py` 的 ProviderPreset、`load_model_profile_from_env`、供应商环境加载、`load_llm_ap`，以及 ModelProfile 默认值。

操作：将缺省窗口解析为 auto 意图；保留显式 `AGENT_MODEL_CONTEXT_WINDOW`、`DEEPSEEK_CONTEXT_WINDOW`、旧 `LLM_CONTEXT_WINDOW` 的手动语义。为实际支持的每种入口记录优先级和来源，避免默认值覆盖用户值。现有 admitted/soft 限制继续参与最小值计算。

产物：统一解析入口与环境变量文档。

验收：不配置窗口时按已核实目录解析；显式 32768 仍保持 32K；通用、供应商及旧入口相同输入得到相同预算。

### T05 — 扩展持久化模型版本与 API

依赖：T02、T04。

涉及：`model_admin.py`、`model_control.py`、model_profile_versions 存储及 API schema。

操作：将容量来源、auto/manual 意图、解析值和计数证据纳入不可变版本及版本摘要；API 返回这些信息。连通性状态与容量／计数验证状态分开保存。

产物：兼容迁移、版本创建／读取接口。

验收：保存后重新加载不丢字段；规格或计数器改变会生成新版本；旧版本仍可读取；API 拒绝冲突和越界配置。

### T06 — 实现按模型选择的实际计数器

依赖：T02；官方计数适配依赖 T01。

涉及：`token_budget.py`，全仓引用 `DEFAULT_TOKEN_COUNTER` 的调用点。

操作：

1. 引入按解析后的模型版本选择计数器的工厂，替换预算主链路对全局默认计数器的直接依赖。
2. 官方 tokenizer／计数 API 经验证后才注册对应适配器；明确中文、英文、代码和特殊内容的支持范围。
3. 完整请求还包含 messages、tools 和协议转换成本；不能把“对 JSON 文本 tokenize”标为精确的供应商请求计数。
4. 无适配器时保留 UTF-8 保守估算，明确 `estimate`、计数单位、适用性证据和误差限制。
5. 计数器失败时必须显式降级并重算预算，或报可解释错误；不能沿用之前偏小的计数。

产物：计数器工厂、适配器、计数结果元数据。

验收：切换模型确实切换算法；counter_id 与执行一致；未验证算法不能进入要求已验证计数的契约。

### T07 — 统一打包、发送检查与 fallback 预算

依赖：T04、T06。

涉及：请求上下文打包、模型网关、最终 wire gate、模型 fallback 路径。

操作：规划预算与发送前检查使用相同模型快照和计数契约；以实际发送 messages/tools 为检查对象；切换 fallback 后重新解析容量、计数和必要压缩。传输字节数与 token 估算分列，保留独立 HTTP 请求大小限制。

产物：统一预算结果对象与发送前检查调用链。

验收：不能前台按 token 放行、最后又误用 body 字节对 token 上限拒绝；大窗口模型 fallback 到小窗口时重新检查；错误信息包含实际模型和超限原因。

### T08 — 让压缩策略随实际窗口计算

依赖：T07。

涉及：`token_budget.py`、`memory_archive.py`、`memory_v2.py` 及前台压缩循环。

操作：后台与前台均消费同一版本解析得到的 H；保留当前比例和触发口径，消除残留的固定 32K 阈值。保留按覆盖边界确定性注入摘要、最近 N 个完整 Turn、前台完整请求超限重建和强制前缀归档行为。

当前后台默认：G=floor(0.30H)，R=floor(0.20H)，target=H-G-R；实际要求 pending≥G 且 pending>target 才有归档动作，因此不能把 30% 直接宣传为实际压缩点。pending 指未归档历史，并非完整请求。

摘要长度上限、摘要器输入批次上限中已有的字节单位必须单独保留或显式版本迁移；扩大工作窗口后，待归档前缀也必须分批送给摘要器，不能一次塞入巨量历史。累计摘要与新覆盖边界一起提交，失败时不推进覆盖位置。

产物：按实际预算计算的归档策略、单位说明、长前缀分批处理。

验收：换窗口后阈值按比例变化；摘要下一轮必定注入；归档失败不丢历史；大历史不会撑爆摘要模型；不足可压缩历史时明确失败而非死循环。

### T09 — 修改模型管理界面

依赖：T05。

涉及：`frontend/src/types.ts`、`frontend/src/pages/ModelsPage.tsx`。

操作：提供“自动跟随／手动上限”；移除无条件提交 32768 的默认行为。分别展示官方容量、实际工作窗口、本次输出预算、计数模式与规格核验状态；auto 下由后端解析，不由前端猜容量。

产物：表单与详情页，兼容旧配置的展示逻辑。

验收：新建 auto 配置不会提交隐式 32K；手动值可回显；未知容量明确提示；最大输出 384K 不会自动替换用户的 8192 输出预算。

### T10 — 迁移现有配置并保持运行中会话稳定

依赖：T05、T07、T09。

涉及：模型版本迁移工具、runtime frozen bundle 解析。

操作：默认将已有显式窗口视为 manual，不猜测用户意图；提供 dry-run 列出旧窗口、新解析窗口、证据和受影响配置。用户选择升级时创建新版本，新任务使用新版本，运行中任务保持原快照；切换现有任务须在明确边界重新预算。

产物：可重复执行的迁移工具、dry-run 报告与回滚说明。

验收：原版本不变；重复迁移不产生无意义重复版本；新会话可用新窗口；旧会话可继续；回滚可重新绑定旧版本。

### T11 — 补充自动化测试

依赖：T03—T10；各模块实现时同步编写，不必等全部完成。

涉及：`backend/tests/test_context_budget_contract.py`、`test_context_wire_gate.py`、`test_static_archive_policy.py`、`test_archive_continuation.py`、模型管理／控制相关测试，以及 `frontend/src/__tests__/ControlPages.test.tsx`。

必须覆盖：

| 场景 | 断言 |
| --- | --- |
| 已验证官方模型 + auto | W 跟随目录，非固定 32K |
| 手动 32K／更低端点限制 | 较小值生效 |
| 未知代理／未知模型／过期别名证据 | 不虚构已验证大窗口 |
| 旧环境入口与持久化旧版本 | 保持兼容和原窗口 |
| 输出 8192、官方输出上限较大 | 不按最大输出自动预留 |
| 中文、代码、工具 schema、大工具结果 | 实际计数器被调用，规划与发送单位一致 |
| 计数器失败或 fallback 模型变小 | 重算或显式失败，无静默超限 |
| pending 在 target 两侧、完整请求超限 | 后台与前台各自按正确口径触发 |
| 摘要提交失败／并发新消息 | 覆盖边界不越过已完成摘要范围 |
| 压缩后下一轮、最近 N 轮、工具调用链 | 摘要确定性注入，完整 Turn 保留 |
| 大前缀和旧摘要累积 | 分批不越过摘要器输入限制 |
| 控制台 auto 保存与重新打开 | 无隐式 32K，字段回显一致 |

产物：离线测试报告。使用合成容量验证比例，不需要付费发送 1M 请求才能验证公式。

验收：相关后端和前端检查全部通过；不得将历史测试通过数量当成本次改造的验证结果。

### T12 — 增加运行证据与受控实测

依赖：T11；真实容量验证依赖 T01。

操作：日志记录模型／配置版本、W、H、计数器及证据版本、估算输入量、供应商实际 usage、压缩前后量、触发原因和耗时；不记录 API key。usage 用于校准，不能单凭一次成功请求证明整个标称窗口可用。

先完成离线回放，再做有限真实请求验证；接近 1M 的付费长请求必须另行确定成本、时长与样本范围，本次文档任务不执行这些调用。分别记录 tokenizer 已验证路径与保守估算路径的利用率和误差。

产物：`docs/context-budget/model-window-acceptance.md`，附配置版本和可复查证据。

验收：报告能区分“规格已核实”“配置已生效”“实测到多大”三个结论，不宣称未测试的容量已经验收。

### T13 — 发布、回滚与用户说明

依赖：T10—T12。

操作：先对指定新配置启用；监测上下文超限错误、计数误差、首 token 延迟、成本和压缩频率；异常时恢复旧配置版本或显式较小手动窗口。将官方核验状态、部署版本、迁移命令与回滚步骤写入操作说明。

产物：发布记录和使用说明。

验收：管理员可判断当前是否使用真实容量、为何有效窗口更小，以及如何回退；没有静默改变运行中会话。

## 5. 执行顺序与完成标准

建议顺序：T01 与 T02 的设计分别推进 → T03 → T04 → T05 → T06 → T07 → T08 → T09 → T10 → T11 汇总回归 → T12 → T13。T11 随各项开发持续补充。

若联网核验仍被阻塞，可完成数据契约、计数接口、兼容迁移和合成测试，但不能把 DeepSeek 大窗口 auto 条目发布为已验证。

整体完成需同时满足：

1. 官方规格与计数证据可追溯；没有把历史快照冒充最新核验。
2. 新配置默认跟随经核实的模型／端点容量，手动小窗口仍可用。
3. 计数算法与发送检查一致；保守估算模式如实显示，不承诺用满真实窗口。
4. 压缩阈值随有效预算变化，压缩后固定携带摘要和近期完整对话。
5. 老版本可继续运行，迁移可审阅、可回滚，相关测试及验收记录完整。

## 6. 完成后的具体说明示例

以下是目标行为示例，不是当前已上线状态：

“我们按 DeepSeek Flash 当前版本的官方容量配置窗口，默认自动跟随，也能手动限制为更小窗口；本次输出预算独立设置为 8192。后台在未归档历史超过约一半可用输入预算时尝试归档，前台在完整请求超预算时再兜底压缩。压缩完成后，把已覆盖历史替换为摘要，下一轮固定携带摘要和最近至少 5 个完整对话轮次。计数器未完成官方适配时使用保守估算，因此实际可用 token 数不会假装等于标称容量。”

这里不填写精确的 1M 对应整数或具体 token 压缩点：必须先完成 T01，再由真实配置和计数契约计算并写入 T12 验收报告。历史的约 11.7K／23.1K 数值属于原 32K 工作配置，不能继续当作新窗口阈值。

## 7. 实施记录

状态：T01–T13 已实施（T01 为部分核实，T12 的真实付费实测未执行）。日期：2026-09-19。

### 7.1 T01 官方核验结果

- 联网核验成功（此前被阻塞的访问本次可用）。快照保存在 `docs/context-budget/evidence/`，SHA-256 见 `docs/context-budget/model-capacity-evidence.md`。
- **已核实**：模型 `deepseek-flash` = `DeepSeek-V4.1-Flash`；两个端点；最大输出 `384K (393216)`；上下文为输入+输出合计。
- **unresolved**：上下文精确整数（官方只写 `1M`，未给 `1000000`/`1048576`）。因此 DeepSeek 的已验证 auto 未发布。
- **未验证**：官方 tokenizer zip 存在但未完成与本应用计数的等价性验证，不注册精确适配器。

### 7.2 各任务改动位置

| 任务 | 改动 |
| --- | --- |
| T02 | `app/model_capacity.py`：`WorkingWindowResolution`、`CapacityContractError`、`W=min(模型容量, 端点限制, 手动上限)`、未知容量返回 unverified、手动越界拒绝；`ModelProfile` 增加 `working_window_mode/model_context_limit/model_max_output_limit/capacity_status/capacity_source/counter_mode` |
| T03 | `app/model_capacity.py`：`normalize_endpoint`（保留路径，代理/租户/协议不合并）、`CapacityEntry`、`CATALOG`（DeepSeek 官方条目 context unresolved / output 393216）、别名映射 |
| T04 | `app/config.py`：`_resolve_window_contract` 统一三个入口（`load_model_profile_from_env`、`load_model_profile_from_environment` 预设、`load_llm_ap`）；显式窗口 manual 优先，auto 未核实回退并标注 `legacy-conservative-default` |
| T05 | `app/model_admin.py`、`app/model_control.py`：容量记录以 JSON 复用 `capacity_evidence` 列持久化（无 schema 迁移）；`version()`/`_profile` 解析回字段；`_budget_contract` 接受对象并校验 auto/manual、输出上限、手动越界；`config_digest` 参与容量变化 |
| T06 | `app/token_budget.py`：`CounterSelection`、`register_token_counter`、`counter_for_profile`；`effective_input_budget/hot_window/count_request_units/assert_request_fits/assert_provider_payload_fits` 按 profile 解析计数器；默认不注册适配器，保持 UTF-8 保守估算 |
| T07 | `app/model_gateway.py`、`app/model_control.py`、`app/live_model.py`：网关暴露 `request_counter`；打包、ask 打包、JSON 打包全部传入同一计数器，与发送门共用一套测量 |
| T08 | `app/token_budget.py`、`app/conversation.py`：`HotWindow` 记录容量来源；`context.counted` 事件记录 `counter_id/counter_version/validation_tier/profile_version_id/model_context_limit/capacity_status/capacity_source/working_window_mode`；压缩阈值继续由 `H` 派生 |
| T09 | `frontend/src/types.ts`、`api.ts`、`pages/ModelsPage.tsx`：auto/manual 模式、容量查询按钮与展示、未核实明确提示、手动回显、不再隐式提交 32768；`app/api.py` 新增 `GET /api/model-capacity` |
| T10 | `app/model_capacity_migration.py`、`scripts/migrate_model_capacity.py`：dry-run/apply、默认 manual 保留旧窗口、`--prefer-auto` 仅在核实后跟随、重复执行幂等、旧版本不变 |
| T11 | `tests/test_model_capacity.py`（38 用例）；`frontend/src/__tests__/ControlPages.test.tsx` 增加 auto/manual/未核实三个场景 |
| T12 | `docs/context-budget/model-window-acceptance.md` |
| T13 | `docs/context-budget/model-window-rollout.md` |

### 7.3 验证命令与结果

```powershell
cd backend
python -m pytest tests/test_model_capacity.py -q
# 38 passed

python -m pytest tests/test_model_capacity.py tests/test_context_budget_contract.py `
  tests/test_context_hot_window.py tests/test_context_packing_limit.py `
  tests/test_context_wire_gate.py tests/test_archive_continuation.py `
  tests/test_static_archive_policy.py tests/test_memory_archive.py `
  tests/test_live_model.py tests/test_model_gateway.py tests/test_model_admin_api.py -q
# 302 passed

# BETTER_AGENT_COST_MODE=enforce 全量非集成
# 1277 passed, 2 failed（均为既存环境/成本用例：test_cost_control 外键、
# test_golden_journey 目标复盘）

cd ..\frontend
npx vitest run
# 335 passed（48 files）
npx tsc -b
# ok
npx vite build
# ok
```

### 7.4 已知边界

- DeepSeek 上下文精确整数 unresolved，auto 未发布；`context_verified=true` 的条目才能产生已验证 W。
- 官方 tokenizer 未注册；计数保持 `estimate`，不承诺用满标称窗口。
- T12 的真实长请求实测未执行，需另行确定成本与样本。
- 容量元数据复用 `capacity_evidence` 文本列（JSON envelope），未新增数据库列；旧纯文本证据仍按未知元数据读取。
