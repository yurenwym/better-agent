# R1 基线报告：调用清单、预算路径与 A 级证据状态

对应任务：M1-01（现状审计与A级证据）
日期：2026-09-18
状态：代码审计与调用清单已完成；**A/C/O 均未取得生产证据**，见 §5。
方法：以函数定位为准，不依赖行号；行号为当前工作区快照（`codex/personal-agent-v1`，工作区存在未提交改动）。

---

## 1. 真实 LLM 调用清单

所有调用最终都经过 `ModelGateway.complete`（`app/model_gateway.py`）或路由版 `RoutedModelGateway.complete`（`app/model_control.py`）。差异在于**谁做预算压缩**。

| 入口 | 位置 | Profile 来源 | 消息构建 | 工具 Schema 来源 | 最终校验 |
|---|---|---|---|---|---|
| 对话首调 | `live_model.py` `complete_once` | 路由 Bundle | `pack_messages_newest`（`live_model.py:294-301`） | `self.tool_schemas` | `assert_request_fits`（`model_gateway.py:176-179`） |
| 对话工具续调 | `live_model.py:779-791` | 同上 | `pack_messages_newest` | 同上 | 同上 |
| 控制头修复调用 | `live_model.py:264`、`:445`、`:448` | 同上 | 直接构造 | 无 | 同上 |
| Ask 回填 | `live_model.py:791` | 同上 | `pack_messages_newest` | ask schemas | 同上 |
| Planner / Executor ReAct | `runtime.py` `_execute_step` → live_model 决策 | 角色路由 | live_model 内 | step 工具 | 同上 |
| Research / 专家 | `agents.py:804`、`:829`、`:847` | 角色路由 | 直接构造 | — | 同上 |
| Episode 摘要 | `memory_archive.py:687` `LiveEpisodeSummarizer` | `role="reflector"` 路由 | 直接构造 | 无 | 同上 |
| 学习抽取 | `learning_extraction.py:19`、`learning_prompt.py:155` | 角色路由 | 直接构造 | — | 同上 |
| 进化 / 评审 | `evolution.py:58`、`:68`、`:1711`、`:1735` | 角色路由 | 直接构造 | — | 同上 |
| Goal 程序编译 | `goal_program_compiler.py:174` | 角色路由 | 直接构造 | — | 同上 |
| 真实评估 | `real_evaluation.py:1247`、`:1277` | 显式 profile | 直接构造 | — | 同上 |
| 模型连通性自检 | `model_admin.py:293` | 显式 profile | 直接构造 | 无 | 同上 |
| 离线评估 | `eval.py:75` | 显式 profile | 直接构造 | — | 同上 |

### 1.1 审计结论（三条，均影响 R1 设计）

1. **最终校验已经存在且覆盖全部入口。** `assert_request_fits`（`token_budget.py:234`）在 `ModelGateway.complete` 与 `RoutedModelGateway.complete` 内被调用，所以「所有实际请求都经过统一预算校验」这条**现状已部分成立**——R1 要补的不是"有没有校验"，而是**校验口径**（§3）和**压缩触发时机**（§4）。
2. **只有 `live_model` 的三处会做优先级丢弃。** 其余 10 类入口直接构造完整请求，超限即抛 `ContextOverflow`。这与方案里"无可压缩来源的超限请求明确失败，不伪造摘要"一致，属于可接受现状，R1 不改。
3. **`_context_*` 提示字段只存在于 `live_model` 的打包路径。** 因此 M1-03 要求的"统计前剥离 `_context_*`"只在打包路径上是必要的；其余入口本就不带这些键。

## 2. Profile 来源与不可变性

- 构造点共 3 个 loader（`config.py`）：`load_llm_ap`、`load_model_profile_from_env`、`load_model_profile_from_environment`。
- 运行期 Profile 从 `model_profile_versions` 表读取（`model_control.py`、`model_admin.py`），该表有 **frozen 触发器**（`db.py:885-888`：禁止 UPDATE 与 DELETE），不可原地修改，符合方案要求。
- `ModelProfile`（`model_gateway.py:35-64`）字段：`context_window`、`max_output_tokens`，无 A、无 soft、无计数器版本、无证据字段。

### 2.1 已发现的口径不一致（M1-02 需处理）

| 项 | 值 | 位置 |
|---|---|---|
| `ProviderPreset.max_output_tokens` | 8192 | `config.py:106` |
| `ModelProfile.max_output_tokens` | **4096** | `model_gateway.py:46` |

两个默认值不同。同一份配置在不同构造路径下会得到不同的 O。R1 必须让 O 只有一个来源。

## 3. 当前预算路径（完整）

```text
C = profile.context_window                       # 默认 32768，来源=仓库默认值，非证据
O = profile.max_output_tokens                    # 默认 8192（preset）/ 4096（ModelProfile）
margin = min(2048, max(1, (C-O)//20)) = min(2048, 1228) = 1228
input_limit = C - O - margin = 32768 - 8192 - 1228 = 23348   # 字节
```

- 计数器：`Utf8UpperBoundTokenCounter`（`token_budget.py:16`），`count_text` = `len(utf-8 bytes)`，`version = "utf8-upper-bound-v1"`。**单位是字节，不是 token。**
- 消费点（全部）：
  - `model_gateway.py:159-160` → `input_limit`
  - `model_gateway.py:176-179` → `assert_request_fits`
  - `model_control.py:399-405` → `input_limit`
  - `model_control.py:431-446` → `assert_request_fits`（主 Profile + fallback 各一次）
  - `live_model.py:294-301`、`:674-681`、`:779-781` → `pack_messages_newest`

### 3.1 三处独立且互不知情的预算

| 预算 | 值 | 位置 |
|---|---|---|
| 热历史 | 12,000 字节 | `conversation.py:_history` |
| 归档触发/保留 | 12,000 字节（proactive 时 ×0.8 = 9,600） | `conversation.py:_archive_history_before_generation`、`memory_archive.py:79`、`_select_archive_prefix` |
| 请求级硬上限 | 23,348 字节 | `token_budget.request_budget` |

**热历史预算（12,000）与请求上限（23,348）无任何联动**：请求还剩一半以上空间时就会触发归档，且归档不可逆。

## 4. 离线复现用例：「历史超 12,000 但完整请求可容纳」

这是 R1 的核心回归场景，现状**必然失败**（会归档）。

构造：

```text
历史：30 个中等 Turn，合计 ≈ 16,000 字节（> 12,000 触发线）
System 提示 + 工具 Schema：≈ 4,000 字节
当前用户输入：≈ 500 字节
完整请求 ≈ 20,500 字节  <  input_limit 23,348
```

现状行为：

1. `_history()` 把历史裁到 12,000 字节 → 模型**只看到约 3/4 的历史**，即使它装得下。
2. `_archive_history_before_generation()` 判定"有 turn 装不下" → 触发归档 → 12,000 以上历史被压成 Episode，**不可逆**。
3. 该 Episode 还要靠词法检索才可能回到上下文，且 `_context_priority=20` 低于原文历史的 50，预算紧时**先被丢弃**。

期望行为（R1 验收）：完整 U_A ≤ H 时**不归档、不裁剪**，30 个 Turn 全部发送。

复现入口：`backend/tests/test_memory_conversation_flow.py` 与 `backend/tests/test_context.py`（扩展，不新建 harness）。

## 5. A / C / O 证据状态

**结论：三项均无生产证据。** 按方案 §3.3，新策略不得据此启用。

| 项 | 当前值 | 来源 | 证据强度 |
|---|---|---|---|
| C（真实最大窗口） | 32768 | `ProviderPreset` 仓库默认值 + `{PROVIDER}_CONTEXT_WINDOW` 环境变量 | **无证据**。仓库默认值本身不算证据。 |
| A（准入工作容量） | **不存在该字段** | — | **无证据，且字段缺失** |
| O（输出预留） | 8192 / 4096 | 同上，且两处不一致 | **无证据** |
| 计数器适用域 | UTF-8 字节上界 | `token_budget.py:16` | 有实现，无协议包装证据（`protocol_bound` 尚未实现） |
| 端点 / 模型 ID | `deepseek-flash` @ `https://api.deepseek.com` | `config.py:123` | 仓库默认值；需与实际运行配置核对 |

### 5.1 取证途径（本次未执行）

- 官方规格：`https://api-docs.deepseek.com/quick_start/pricing/`
- 代理配置：部署侧网关的限制
- 现有生产成功记录：`scripts/m*_live_acceptance.py` 中记录了 32768/8192 下的成功调用，**但只覆盖小输入**，不足以推出 A。
- 有界容量探测（方案 §2.4）：**本次明确不执行付费探测**。

## 6. go/no-go 判据（M1 出口）

R1 的主要收益 = 解除固定 12,000，收益上限由 **A 相对 12,000 的增量**决定。

- 若取证得到 **A ≫ 12,000**（例如 ≥ 32,768）→ 进入 M2，收益成立。
- 若 **A 只能取到接近 12,000**（例如 ≤ 16,384）→ **先重估 R1 投入产出比，不直接进入 M2**。
- 若取证失败 → 实现可交付（A 缺失时新策略拒绝启用），但**不得声称 R1 收益成立**。

## 7. 基线缺陷清单（R1 需修复，已全部在代码中确认）

| # | 缺陷 | 位置 | 归属任务 |
|---|---|---|---|
| 1 | 热历史固定 12,000，不随 Profile 变化 | `conversation.py:_history` | M2-01 |
| 2 | 归档触发固定 12,000，且与请求上限无关 | `conversation.py:_archive_history_before_generation` | M2-01 / M2-02 |
| 3 | 归档保留量固定 12,000，与触发线同源但语义不同 | `memory_archive.py:79`、`_select_archive_prefix` | M2-02 |
| 4 | A 字段缺失，公式退化为 `min(soft, C)` | `token_budget.py:60`、`ModelProfile` | M1-02 |
| 5 | O 有两个默认值（8192 / 4096） | `config.py:106`、`model_gateway.py:46` | M1-02 |
| 6 | 无 `protocol_bound`，协议包装未计入 | `token_budget.py:29-32` | M1-03 |
| 7 | 已归档 Episode 优先级 20 < 原文历史 50 | `conversation.py:1588` | M2-02 |
| 8 | 保留轮数无下限，完全由字节决定 | `transcript.py:pack_recent` | M2-01（`min_turns`） |
| 9 | 摘要输出上限 1600 字节 + 字段顺序饿死 decisions/outcomes | `memory_archive.py:16`、`_merge_chunk_summaries` | M2-02 |
| 10 | `safety_margin` 1228 与协议预算可能重复扣 | `token_budget.py:66-72` | M1-03 |

## 8. 执行前注意事项

- 工作区存在**大量未提交改动**（26 个文件、约 1393 行新增，涉及 goal/evolution/reviews 等无关模块）。R1 改动只在预算与压缩相关文件上叠加，不触碰这些。
- 仓库内**无 `AGENTS.md`**。
- `scripts/m1..m5_live_acceptance.py` 中硬断言 `context_window == 32768` 且 `max_output_tokens == 8192`。R1 不修改这两个默认值，因此这些脚本不受影响；但若后续为 DeepSeek 配置真实容量，需同步复核这些脚本。
