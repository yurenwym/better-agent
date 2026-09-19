# 模型容量工作窗口验收报告（T12）

日期：2026-09-19。本文区分三个结论：**规格已核实**、**配置已生效**、**实测到多大**。未实测的容量不得写成已验收。

## 本轮审查修复验收（F01—F06）

状态：2026-09-19 已完成实现与离线验收。下文原 T12 的历史测试数量保留作追溯，本轮结果以本节为准。

| 任务 | 实现与验证结果 |
| --- | --- |
| F01 手动窗口 | manual 统一写入 soft 上限；Tier A 允许 soft 小于 admitted；复现配置 32768/131072/8192 的实际总窗口为 32768、H=24576，注册和重载后不变 |
| F02 服务端容量 | 新建和新增版本均从服务端目录解析；HTTP 路径统一显式模式；伪造 verified、伪装 legacy 来源及未知代理 auto 被拒绝；合成已验证目录覆盖客户端错误容量 |
| F03 旧文件配置 | LLM_AP 文件显式窗口在解析前参与优先级选择；profile、证据、数据库和恢复结果的 mode/source/window/soft 一致；已知上限和冲突 soft 不能绕过 |
| F04 控制台 | 只提交配置意图；端点/协议/模型变化清空容量展示，通过查询序号与模型键丢弃旧响应；并发查询回归通过 |
| F05 费用测试 | 修正测试对默认模式的假设，分别验证 enforce 和 observe；保留冻结价格、每日结算金额、余额清零及预留记录唯一性断言 |
| F06 集成验收 | 后端主回归、配置专项、控制台测试和前端构建通过；未修改现有业务数据库中的冻结版本 |

### 本轮测试结果

在 `backend/` 运行：

```powershell
python -m pytest tests/test_capacity_review_fixes.py tests/test_model_capacity.py tests/test_context_budget_contract.py tests/test_context_wire_gate.py tests/test_archive_continuation.py tests/test_static_archive_policy.py tests/test_model_admin_api.py tests/test_model_control.py tests/test_context_hot_window.py tests/test_context_packing_limit.py tests/test_memory_archive.py tests/test_live_model.py tests/test_model_gateway.py -q --basetemp=../outputs/capacity-fix-acceptance-03 -p no:cacheprovider --junitxml=../outputs/capacity-fix-acceptance-03.xml
# 322 passed in 74.74s

python -m pytest tests/test_llm_config.py tests/test_model_control.py::test_evaluation_attempt_pins_price_snapshot_while_regular_call_uses_current_price -q --basetemp=../outputs/capacity-fix-config-01 -p no:cacheprovider
# 12 passed in 1.57s（费用用例按 enforce/observe 参数化；与主回归有重叠）

python -m pytest tests/test_capacity_review_fixes.py::test_llm_file_cannot_override_known_capacity_or_conflicting_soft_limit -q --basetemp=../outputs/capacity-fix-file-01 -p no:cacheprovider
# 1 passed in 0.38s（主回归启动后补充的文件容量边界用例）
```

主回归机器报告：`outputs/capacity-fix-acceptance-03.xml`。复跑请使用新的专用 basetemp，避免 pytest 清理已有验收目录。

在 `frontend/` 运行：

```powershell
npm test -- --run src/__tests__/ControlPages.test.tsx
# 13 passed
npm run build
# TypeScript 与 Vite 构建通过；Vite 提示主 JS chunk 超过 500 kB，非阻断项
```

### 费用失败根因与测试修正

当前 `BETTER_AGENT_COST_MODE` 默认 `observe`：每次调用在 INVOCATION、DAILY、MONTHLY 三层记录零金额 RESERVE，再按真实 usage 记账。旧测试假设只有 DAILY 层且执行金额预留，因此把正常的三层零金额记录判断为失败。

测试现在显式覆盖两种模式，按 DAILY 层比较冻结/当前价格预留，并检查所有预留的 `(attempt_id, period_kind, period_key)` 唯一。两次调用的 DAILY 实际费用分别为 20、200，最终 reserved=0、charged=220；enforce 共 2 条预留，observe 共 6 条。未改变费用业务逻辑。

另有原 wire-gate 测试使用“窗口 512、输出 1024”的非法配置作为小窗口 fallback。创建时新增校验会直接拒绝该配置，故将测试输出量调整为窗口的四分之一，仍以过大的真实请求验证 fallback 跳过行为，不削弱发送门检查。

### 验收范围与限制

- 本轮是代码与离线验收，不包含生产部署、已有配置迁移或真实长上下文调用。
- DeepSeek 目录仍为上下文精确整数 unresolved；环境 auto 仍采用标明来源的保守默认窗口；计数仍为 UTF-8 估算。此次修复不代表已自动使用 1M。
- Git 对象无法读取，未使用 Git 差异确认历史引入时间，也不能将费用失败断言为某个历史提交引入。
- 旧冻结版本不原地改写；需要修正既有窗口配置时创建新版本，再按明确范围切换路由。

## 1. 三个结论

| 结论 | 含义 | 本次状态 |
| --- | --- | --- |
| 规格已核实 | 官方文档给出可追溯的整数或明确字段 | DeepSeek 输出上限 `393216` 已核实；上下文精确整数 **unresolved** |
| 配置已生效 | 解析出的 W/H 已进入预算、打包、发送门与压缩策略 | 已通过离线测试验证 |
| 实测到多大 | 真实调用 usage 与估算误差 | 未执行（接近 1M 的付费长请求不在本次范围） |

## 2. DeepSeek 当前解析结果

```text
模型：deepseek-flash（DeepSeek-V4.1-Flash）
端点：https://api.deepseek.com（OpenAI 格式）/ https://api.deepseek.com/anthropic
官方上下文：1M（精确整数 unresolved）
官方最大输出：384K = 393216（已核实）
计数器：utf8-upper-bound-v1（保守估算，未注册精确适配器）
```

因此：

- `auto` 在官方 DeepSeek 条目上解析为 `unverified`，不会静默使用 1M，也不会写死 32768；
- 环境入口在 auto 未核实时会保留旧保守窗口并把 `capacity_source` 标为 `legacy-conservative-default`；
- API 新建 `auto` 未核实配置会被拒绝并提示改用手动窗口；
- 手动窗口可用，并受已知端点/输出上限校验。

证据见 `docs/context-budget/model-capacity-evidence.md` 与 `docs/context-budget/evidence/` 快照。

## 3. 离线回放证据（配置已生效）

运行命令（`backend/`）：

```powershell
python -m pytest tests/test_model_capacity.py -q
# 38 passed：目录匹配、auto/manual 解析、配置入口、持久化、计数器工厂、迁移、压缩比例
```

关键断言：

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 已验证合成模型 + auto | `test_verified_catalog_auto_follows_the_model_instead_of_32k` | W=1,048,576，非 32768 |
| 手动 32768 / 更低端点限制 | `test_manual_window_can_be_smaller_and_is_not_clamped_silently`、`test_smaller_endpoint_limit_wins_over_larger_model_capacity` | 较小值生效 |
| 未知代理/模型 | `test_unknown_proxy_auto_is_unverified_and_does_not_inherit_official_capacity` | 不虚构大窗口 |
| 旧入口兼容 | `test_env_loader_labels_official_deepseek_auto_as_unverified`、`test_explicit_32768_stays_32k` | 原窗口保留 |
| 输出预算独立 | `test_output_budget_is_independent_from_the_model_output_limit` | 8192 不被 131072 替换 |
| 计数器切换 | `test_registered_counter_switches_the_algorithm_and_the_recorded_version` | 算法与记录版本一致 |
| 压缩随窗口 | `test_compression_thresholds_scale_with_the_resolved_window` | 触发线与目标随 H 变化 |
| 迁移可回滚 | `test_migration_apply_creates_one_new_version_and_is_idempotent` | 新版本、旧版本不变、可重复 |
| 容量 API | `test_capacity_endpoint_resolves_official_deepseek_without_guessing` | 官方条目未核实且输出上限正确 |
| 控制台 | `frontend` `ControlPages.test.tsx` | auto 不提交隐式 32K；未核实明确提示；手动可回显 |

扩展回归：

```powershell
python -m pytest tests/test_model_capacity.py tests/test_context_budget_contract.py `
  tests/test_context_hot_window.py tests/test_context_packing_limit.py `
  tests/test_context_wire_gate.py tests/test_archive_continuation.py `
  tests/test_static_archive_policy.py tests/test_memory_archive.py `
  tests/test_live_model.py tests/test_model_gateway.py tests/test_model_admin_api.py -q
# 302 passed
```

## 4. 运行证据字段（代码已记录）

- `context.counted` 事件：`input_limit`、`packing_limit`、`counter_id`、`counter_version`、`validation_tier`、`profile_version_id`、`model_context_limit`、`capacity_status`、`capacity_source`、`working_window_mode`。
- `context.continuation_built`：承接覆盖 A/Q、Episode 版本、coverage hash、pin 命中。
- `model_profile_versions`：不可变版本保存 `capacity_evidence` JSON（mode/status/source/model limits/counter/evidence refs）。
- 不记录 API key。

## 5. 未执行的真实请求验证

以下项目需要另行确定成本、时长与样本范围，本次未执行：

- 接近 1M 的付费长请求；
- 官方 tokenizer zip 与本应用计数的等价性对照；
- 供应商 usage 与 UTF-8 估算的系统误差统计。

在此之前，所有计数器状态保持 `estimate`，不宣称精确或已用满标称容量。

## 6. 复现配置版本

- 容量目录版本：`model-capacity-2026-09-19`（`backend/app/model_capacity.py`）。
- 证据快照时间：2026-09-19T11:07:32Z。
- 迁移工具：`backend/scripts/migrate_model_capacity.py`。
