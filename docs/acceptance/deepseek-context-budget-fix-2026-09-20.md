# DeepSeek 上下文预算最小修复验收

日期：2026-09-20。范围：按 `docs/superpowers/plans/2026-09-20-deepseek-context-budget-minimal-fix.md` 实施 T1–T3，并执行配置、计量链路与真实模型 12 轮对话验收。

复核更正：本批证据确认普通多轮与长输入成功，但没有发生 ask 表单续接。下表“实际输入”原先错误地合计了同轮分类、主回答和安全评审，不能用于计算主回答估算误差。原始 results.json 保留作为历史记录；最终验收以补测报告为准。复核另发现 `test_context_budget_contract.py` 中 A 级写入校验失败，不能仅凭原 316 项报告预算契约全部通过。

补测已完成：见 [16 轮真实对话补测报告](deepseek-context-budget-retest-2026-09-20.md)，包含修复后的 350 项测试、原请求模拟重放、真实 ask 续接和逐次请求用量配对。

## 1. 问题与修复前证据

1. `_hot_window` 在 `gateway.control_store` 上调用 `resolved_profile`，异常被吞后回退到归档器静态保留值 12,000 units。
2. 旧 profile 使用 32,768 窗口，扣除 8,192 输出与 1,228 余量后可打包预算 23,149；截图中的澄清续接请求实测 25,408 UTF-8 字节单位，正式回答尚未调用便被本地拒绝。
3. bytes 与 tokens 混用：所有容量判断直接拿 UTF-8 字节数与 token 窗口比较。

## 2. 实现清单

**T1 配置与持久化**（`model_capacity.py`、`config.py`、`model_admin.py`、`model_control.py`）

- 官方 `api.deepseek.com` 的已识别模型（`deepseek-flash` 及别名）默认工作窗口 1,000,000：官方页面只写“1M”，按 `official-default` 保守整数默认记录，来源 `catalog-official-default`，不宣称验证精确边界；第三方代理与未知模型不继承（catalog 端点精确匹配）。
- 用户显式 `DEEPSEEK_CONTEXT_WINDOW` / `AGENT_MODEL_CONTEXT_WINDOW` 更小上限生效（manual）；本次临时添加的 131072 覆盖已删除。
- profile 版本注册/存储/重载补齐容量与计数契约字段（validation_tier、admitted/soft、context_window_verified、counter_id/version/evidence、capacity_evidence、protocol_budget、history_min_turns、compact_ratio、recent_window_*、archive_*）；这些字段参与版本摘要，counter 变化生成新版本。
- 公开 API 写入改为服务端解析：manual 只提供更小上限，auto 只能由服务端 catalog 解析（verified 才接受），伪造客户端证据返回 422；`ensure_profile` 作为可信内部路径可注册官方默认与 legacy 回退。
- `version()` 返回全部容量字段与 `capacity` 记录，重载后不再丢失来源与计数策略。

**T2 估算器**（`token_budget.py`）

- 新增 `deepseek-text-estimate-v1`：`estimated_tokens = ceil(utf-8 bytes / 4)`，显式 `estimate` 模式，注册为启发式适配器，不冒充 verified。
- 不全局替换 `DEFAULT_TOKEN_COUNTER`；仅按 profile 的 counter_id 选择。

**T3 统一容量判断**（`conversation.py`、`token_budget.py`、`model_gateway.py`、`model_control.py`、`memory_archive.py`）

- `hot_window`/`packing_limit`/`envelope_overhead`/`per_message`/`per_tool` 全部使用目标 profile 的 counter；`HotWindow` 携带 counter、counter_mode、reserved_output。
- `_hot_window` 从实际路由 profile 取窗口与计数策略；有路由但解析失败抛出配置错误，不再静默回退 12k。
- 归档测量循环 `measure_full_request` 与 legacy `builder.measure` 使用目标 counter；`context.counted`、`context.archiving`、`context.continuation_overflow`、新增 `context.request_estimated` 事件记录 profile 版本、窗口、counter ID/版本/模式、估算输入、输出预留、有效输入预算与 `canonical_payload_bytes`（显式字节字段）。
- 归档 `enqueue`/`static_trigger_state`/`uncovered_cost` 接受并透传 counter（前台来自 window，后台来自 profile 版本行）；纯字节限制（recent_window_bytes、tool_result_bytes）保持 bytes 语义。
- 路由候选逐个使用自身窗口与计数（原逻辑保留），fallback 不被套用主模型预算；最终 `assert_provider_payload_fits` 用 profile counter 计数，不再以原始字节比较 token 窗口。

## 3. 验收清单

### A. 配置和持久化

- [x] 官方 DeepSeek 无手动覆盖时工作窗口 1,000,000（`test_deepseek_auto_uses_the_official_default_window_without_claiming_verification`、`test_env_loader_labels_official_deepseek_auto_as_official_default`）。
- [x] 手动更小上限生效；第三方同名模型不误用官方默认（`test_explicit_generic_window_is_manual_and_kept`、`test_unknown_proxy_auto_is_unverified_and_does_not_inherit_official_capacity`）。
- [x] 注册→存储→重载后窗口、counter ID/版本/模式、来源不丢失（`test_official_default_window_and_counter_survive_registration_and_reload`；运行库证据见第 4 节）。
- [x] counter 变化生成新版本（`test_changing_the_counter_creates_a_new_version`；公开 API 的 auto/manual 边界见 `test_capacity_review_fixes.py`）。
- [x] 有路由但 profile 解析失败不再静默 12k（`test_routed_profile_resolution_failure_is_a_configuration_error`）。

### B. 计量链路

- [x] 受控 32k profile：原始字节数超预算、估算 token 未超的请求通过最终模拟传输校验（`test_32k_profile_uses_tokens_at_the_final_gate_not_raw_bytes`，字节 80k+ vs 估算 20k）。
- [x] 估算 token 确实超预算仍被拒绝（`test_estimated_tokens_over_budget_are_still_rejected`）。
- [x] 预检、打包、路由、单模型网关与最终 payload 使用目标 profile 策略（`test_packing_and_hot_window_share_the_profile_counter` 及既有 `test_model_control`/`test_routed_model_gateway` 全绿）。
- [x] 中文、英文、混合文本与工具参数覆盖（32k 边界用例为中文；既有 token/capacity 套件含英文与工具 schema）。
- [x] 11 个工具定义与澄清历史参与计数（`test_tools_and_history_participate_in_the_estimate`；真实运行第 4 节）。
- [x] fallback 与工具循环新增结果重新计量（候选逐个 `assert_request_fits`；chat 工具循环每次 `complete_once` 重新打包并经网关校验，`test_chat_goal_tools.py` 13 项）。
- [x] 字节限制未被缩小为四分之一（`test_byte_limits_are_not_scaled_by_the_token_estimate`）。
- [x] 严格计数模式未被新估算器冒充（`counter_for_profile` 的 verified 无证据回退测试保持通过）。

### C. 故障回归和真实模型

- [ ] 原约 25,408 字节的澄清请求回归：本批第 1 轮只是 `canonical_payload_bytes=19,992` 的问候，不能替代澄清续接证据。
- [ ] 本批没有 `ask.requested` / `ask.answered` 事件；表单澄清续接由后续补测完成。
- [x] 受控长输入（43,230 字符）：估算 48,564 units / 194,255 bytes，COMPLETED。
- [x] 记录估算输入、供应商实际输入、工作窗口与结果（第 4 节；缓存命中计入实际输入）。
- [x] 真正的供应商超限仍停止、不无限重试、不重复业务写入（网关既有 `context_overflow` 路径与测试保留；本方案未改）。
- [x] readiness 仅作配置检查：另有真实请求验收。

## 4. 真实模型 12 轮验收

- 脚本：`backend/scripts/deepseek_budget_acceptance.py`；产物：`docs/evaluation/deepseek-budget-acceptance-2026-09-20/results.json`。
- 模型与配置：`deepseek-flash`（供应商别名），官方端点，温度 0、thinking=false；活动 profile：窗口 1,000,000、counter `deepseek-text-estimate-v1`（estimate）、输出预留 8,192、capacity status `official-default`。
- 结果：**12/12 COMPLETED**，无 `context.continuation_overflow`，无工具/审批交互。

| 轮 | 输入字符 | 主回答预检估算 units | 有效输入上限 | payload bytes | 同轮所有调用累计输入 tokens（非单次上下文） | 累计输出 tokens |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 4,998 | 989,710 | 19,992 | 5,442 | 207 |
| 2 | 23 | 5,181 | 989,710 | 20,724 | 6,864 | 1,260 |
| 3 | 20 | 6,199 | 989,710 | 24,795 | 8,126 | 1,323 |
| 4 | 19 | 7,288 | 989,710 | 29,152 | 9,548 | 1,453 |
| 5 | 21 | 8,520 | 989,710 | 34,077 | 11,280 | 1,771 |
| 6 | 13 | 10,019 | 989,710 | 40,074 | 13,412 | 2,158 |
| 7 | 19 | 11,910 | 989,710 | 47,638 | 15,791 | 2,423 |
| 8 | 12 | 11,338 | 989,710 | 45,352 | 15,028 | 2,612 |
| 9（长输入） | 43,230 | 48,564 | 989,710 | 194,255 | 85,349 | 568 |
| 10 | 17 | 49,059 | 989,710 | 196,234 | 56,943 | 3,530 |
| 11 | 17 | 52,032 | 989,710 | 208,126 | 60,556 | 3,642 |
| 12 | 13 | 55,114 | 989,710 | 220,455 | 60,278 | 57 |

观察：

- 撤回“第 9 轮低估约 43%”的结论：85,349 是分类 32,166 + 主回答 52,579 + 安全评审 604 的总和。主回答预检 48,564 与 52,579 相差约 7.6%，但预检与最终请求组成可能不同，该差值也不能作为准确的计数误差。补测须用同一 attempt 的最终请求估算与 usage 配对。
- 旧字节规则会在第 1 轮（19,992 bytes > 旧 12,000）和第 9 轮（194,255 bytes）直接拒绝；修复后按估算 token 判断并通过。
- 供应商实际输入包含缓存读取，已计入上下文用量。

## 5. 测试结果

```powershell
cd D:\RAG\better\backend
python -m pytest tests/test_deepseek_context_budget_fix.py tests/test_model_capacity.py `
  tests/test_capacity_review_fixes.py tests/test_startup_contract.py tests/test_context_hot_window.py `
  tests/test_context_packing_limit.py tests/test_static_archive_policy.py tests/test_memory_archive.py `
  tests/test_archive_continuation.py tests/test_conversation_worker.py tests/test_routed_model_gateway.py `
  tests/test_model_control.py tests/test_model_gateway.py tests/test_chat_goal_tools.py -q
# 316 passed
```

此前容量链路的 9 项失败（`test_model_capacity.py`、`test_static_archive_policy.py` 等）已随容量字段持久化与版本读取修复而通过。`test_routed_model_gateway.py::test_routed_budget_block_...` 与 `test_startup_contract.py` 的定价用例改为显式 `BETTER_AGENT_COST_MODE=enforce`，此前依赖环境默认，属测试卫生修复。

## 6. 残留限制

- `deepseek-text-estimate-v1` 可能低估，不是数学上界；本批聚合数据不能证明低估比例。若按请求配对后的真实数据表明误差频繁影响使用，再单独引入 usage 校准或经验证 tokenizer。
- 手动设置较小窗口（如 32k）时，低估可能让请求越过供应商真实上限；供应商返回的超限仍按既有 `context_overflow` 停止，不盲目重试，但本地无法提前拦截。本次不实现自动压缩重试。
- `context.request_estimated` 只记录估算值；供应商真实 usage 需从 `model_attempts` 读取（本报告第 4 节即如此）。
- 本次未改业务工具权限、审批、幂等与提示词；v3 评测集仍待按既有契约冻结。

## 7. 复现

```powershell
# 启动（无需 DEEPSEEK_CONTEXT_WINDOW 覆盖；官方默认 1M）
python scripts\start.py

# 当前脚本已升级为 16 轮；旧 12 轮结果保留，复跑请使用新的输出目录
cd backend
python scripts\deepseek_budget_acceptance.py --out ..\docs\evaluation\deepseek-budget-next-run `
  --database-url $env:DATABASE_URL
```
