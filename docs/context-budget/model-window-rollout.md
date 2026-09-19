# 工作窗口改造发布、回滚与用户说明（T13）

日期：2026-09-19。适用于“基于模型真实容量的上下文窗口”改造。

## 1. 当前可以发布什么

| 能力 | 状态 | 发布建议 |
| --- | --- | --- |
| 手动工作窗口（显式值优先，受端点/输出上限校验） | 可用 | 可发布 |
| 已核实容量的 auto 跟随（合成/未来核实条目） | 代码路径可用 | 仅在目录条目 `context_verified=true` 时启用 |
| DeepSeek `auto` 跟随 1M | 上下文精确整数 unresolved | **不发布为已验证**；用户使用手动窗口 |
| 精确 tokenizer 计数 | 未完成等价性验证 | 保持保守估算 `utf8-upper-bound-v1` |

## 2. 启用步骤

1. 运行迁移 dry-run，确认每个旧配置的旧窗口、新解析窗口与来源：

   ```powershell
   cd backend
   python scripts/migrate_model_capacity.py --db ..\data\agent.db
   ```

2. 确认报告：旧窗口按 manual 保留；`auto_fallback_reason` 说明未核实原因。
3. 应用迁移（创建新版本，旧版本不变）：

   ```powershell
   python scripts/migrate_model_capacity.py --db ..\data\agent.db --apply
   ```

4. 新建配置在控制台选择“自动跟随已核实容量”或“手动上限”；控制台展示官方容量、实际工作窗口、输出预算、计数模式与核验状态。
5. 如需强制自动跟随目录（仅当条目已核实）：

   ```powershell
   python scripts/migrate_model_capacity.py --db ..\data\agent.db --apply --prefer-auto
   ```

## 3. 环境变量与优先级

| 入口 | 变量 | 语义 |
| --- | --- | --- |
| 通用 | `AGENT_MODEL_CONTEXT_WINDOW` | 手动工作窗口（最高优先级） |
| 通用 | `AGENT_MODEL_WORKING_WINDOW_MODE` | `auto`/`manual`；显式窗口或 soft 隐含 manual |
| 通用 | `AGENT_MODEL_SOFT_CONTEXT_LIMIT` | 手动上限（与显式窗口必须一致） |
| 通用 | `AGENT_MODEL_ADMITTED_CONTEXT_LIMIT` | 端点已验证限制 |
| 供应商 | `DEEPSEEK_CONTEXT_WINDOW` 等 | 同通用，按供应商前缀 |
| 旧入口 | `LLM_CONTEXT_WINDOW`（LLM_AP 文件） | 手动；文件值优先于环境 |
| 目录 | `AGENT_MODEL_CAPACITY_EVIDENCE` | 旧版输入保留兼容；新窗口证据由最终解析结果生成，不能覆盖容量结论 |

优先级：显式窗口 > 已核实目录 auto > 旧保守默认（`legacy-conservative-default`，不宣称已验证）。

LLM_AP 文件的显式窗口先于环境窗口参与统一解析；它与环境 soft 上限不一致、超过已知容量或接入上限时会报错。手动窗口同时写入 soft 上限，Tier A 允许它小于 admitted，且输出预算必须小于最终窗口。

控制台保存接口只接受容量配置意图，服务端重新解析并生成容量记录。前端查询结果不作为已验证证据；未核实的 auto 会返回 422。环境注册可通过内部路径保留带来源的保守默认值，此例外不能通过 HTTP 请求字段取得。

## 4. 监测信号

- `context.continuation_overflow` / `context.continuation_failed` 频率；
- `context.counted.capacity_status` 分布（应为 manual/unverified，直到容量核实）；
- 供应商返回的 `context_overflow` 错误（说明估算偏低，需要核查）；
- 首 token 延迟、成本与压缩频率（`context.archiving`）。

## 5. 回滚

1. 控制台手动设置更小窗口，或
2. 将路由策略重新指向旧 `model_profile_versions` 版本（旧版本冻结且可继续读取），或
3. 环境入口显式设置 `*_CONTEXT_WINDOW` 恢复原值。

迁移只创建新版本，不修改旧版本，因此回滚不需要数据修复。

## 6. 用户说明（面向使用者）

> 系统会按实际接入的模型和端点解析可用窗口：官方容量已核实时自动跟随，否则使用你设置的手动窗口或保守默认值。本次输出预算独立设置，不会被官方最大输出自动替换。计数在官方 tokenizer 完成验证前使用保守估算，因此实际可用 token 数不会假装等于标称容量。压缩完成后，下一轮会固定携带归档摘要和最近至少 5 个完整对话轮次。

## 7. 未完成的边界

- DeepSeek 上下文精确整数与 tokenizer 等价性待官方证据/T12 受控实测；
- 接近 1M 的付费验证未执行；
- Tier B 计数未启用。
