# M1 真实模型验收批次预算单（002）

状态：已批准并执行，第二轮失败后停止；本批次已关闭，不得重跑。

日期：2026-09-08，Asia/Shanghai。
批次：`M1-LIVE-20260908-002`。

## 执行结果

- 第1轮完整通过；第2轮因条目 embedding 请求 timeout，语义记忆未入选而失败；第3轮未执行。
- 实际发起8个 DeepSeek attempts、4个 SiliconFlow embedding 请求；embedding 输入 UTF-8 上界774 token。
- 模型 usage 缺少输入 token 分项，按每 attempt 完整预留保守记账，本批次费用上界 US$0.201856；embedding 费用上界 ¥0.00010836。实际供应商账单可能更低，不按更低值申报。
- 失败报告：`m1-live-results-2026-09-08-002.json`。隔离容器已停止，未启动应用服务或其他工作流。
- 验收驱动已修复为检查 embedding job 必须达到 `COMPLETED`，并将验收专用请求超时冻结为60秒；修改后的新批次为 `M1-LIVE-20260908-003`，必须重新批准。

## 重新冻结原因

- `M1-LIVE-20260908-001` 在网络请求前的离线 PostgreSQL 预检中发现缺陷；修复导致冻结输入变化，旧批准已作废。
- 旧批次 DeepSeek model attempts 为0，SiliconFlow embedding 请求为0，费用为0。
- 本批次调用数量与费用上限不变，只更新代码和验收驱动指纹；仍须再次明确批准。

## 冻结版本

- 代码基线：HEAD `c7221cf34ae065ddb7d2de86b14e9dbb90889af4`；已跟踪工作树 diff 对象 `ecbf1268bc6bb2458fd87984dec18ea2b38b6d30`。执行前若相关文件变化，批次作废并重新核算。
- 关键文件 SHA256：
  - `costs.py`: `024ce73af79d286e764fddfe819d115d0b16b443a912d98ddadaec975b3ebecd`
  - `live_model.py`: `c10640c148ef517ac0af8dfb76522fc1fb20ebf633d01b7d3abdd614e26fba4c`
  - `memory_archive.py`: `6b13889007957df671ffdb05ed1dbe261ba20bda7a404732ebdbfee7b0d71b34`
  - `memory_v2.py`: `857b74803a1f06b9fddd6a67aca50b85c3f9aa749a5825cef65fd26000e6cddb`
  - `conversation.py`: `9e51691df04da066af9a00813140316ef5de5e2eddc1aaacbc66c51724c9953d`
- 验收驱动 SHA256：
  - `scripts/m1_live_acceptance.py`: `dd964a937bf207d650c303ea26e74dcd8d9264f81e3b99a3754f0c1870d4caf3`
  - `tests/test_m1_live_acceptance_budget.py`: `5428488afa7997c7743b688cd2b9fdc54749ffdae8786d53c353efe452f0998a`
  - `tests/integration/test_m1_live_harness.py`: `7afa6919905bc58adaf91785d0d65f9705c65eabcd2e41ae55af40b67190a3b7`
- 对话模型：`deepseek-v4-flash`，官方当前版本 `DeepSeek-V4-Flash-0731`，OpenAI-compatible API；无备用模型。
- 模型上下文配置：32,768；单次最大输出配置：8,192；网关最多3次 attempt，本批次总 attempt 硬上限优先。
- 归档提示词版本：`episode-v4`；记忆 renderer：`memory-v5`；tokenizer：`utf8-upper-bound-v1`。
- Embedding：SiliconFlow `Qwen/Qwen3-Embedding-4B`，1,024维；不使用其他 embedding 模型。

## 固定案例与重复次数

每轮使用新的隔离 PostgreSQL 数据库、owner 和 thread 命名空间，不读取或修改应用业务数据。候选核心流程连续执行3轮；任一硬失败后停止，修复后连续计数从0重新开始。

1. 相关性与当前纠正：写入相关、无关和置顶长期记忆；基线回答不注入记忆，候选回答使用同一问题及 `memory-v5` 上下文。候选必须命中相关约束、不注入无关条目；当前用户明确改用 SQLite 时本轮回答遵从，但长期 PostgreSQL 偏好不得被改写。
2. 同义语义召回与部分覆盖：已向量化的“骑行训练负荷增幅不超过10%”须被“强度递增限制”问题召回；另一个未向量化但有关键词命中的条目仍须召回；跨 owner/project、过期和删除条目不得出现。
3. 结构化 Episode 与归因：真实归档摘要必须包含用户确认的“每天60分钟”，保留消息来源；助手提出但用户未确认的“每天90分钟”不得渲染为用户决定；原消息、覆盖游标和版本一致。
4. 缺失历史降级：真实分类器对独立问题“什么是哈希表？”放行并显示上下文不完整提示；对“继续刚才的计划并保存”阻断。阻断后 `turn_asks`、计划、研究、Agent run、长期记忆均不得新增；用户可见失败提示不计为业务副作用。
5. 恢复与一致性：归档完成后重试或刷新不得产生重复 Episode；每轮核对模型 attempt、usage、费用、来源 hash、pin/renderer 版本和数据库终态。

基线与候选：基线无记忆回答3次，候选有记忆回答3次；其余 M1 核心流程候选3次。不是模型盲评，不把文风差异计为 M1 质量收益。

## 调用与费用上限

### DeepSeek 对话与归档模型

官方价格页：<https://api-docs.deepseek.com/quick_start/pricing>，2026-09-08再次核验。`deepseek-v4-flash` 峰值为：缓存命中输入 US$0.014/1M token、缓存未命中输入 US$0.44/1M token、输出 US$1.32/1M token；非峰值为峰值的一半。本批次一律按峰值且全部缓存未命中估算。

- 每轮预期：基线回答1 + 候选回答1 + Episode归档1 + 独立性分类2 + 独立回答1 = 6 attempts。
- 三轮预期：18 attempts；允许全批次最多3个额外自动 retry；无 fallback。
- 总 attempt 硬上限：21。第22个 attempt 必须在网络请求前被预算账本拒绝。
- 单 attempt 保守预留：`32768 × 0.44 / 1M + 8192 × 1.32 / 1M = US$0.025232`。
- 18 attempts 的保守预留：US$0.454176。
- 21 attempts 的最坏费用：US$0.529872；数据库 DAILY 硬预算设为 `530000 microusd`，INVOCATION 预算不得超过 `25232 microusd`。
- 实际费用按供应商 usage 结算；usage 缺失不记为0，按已预留上限保守计入。

### SiliconFlow Embedding

官方价格页：<https://siliconflow.cn/pricing>，2026-09-08再次核验。`Qwen/Qwen3-Embedding-4B` 输入价格为 ¥0.14/1M token，输出为0。

- 每轮最多2次请求（条目批量1次、查询1次），三轮硬上限6次。
- 全批次输入以 `utf8-upper-bound-v1` 计数，硬上限12,288；单条输入不得超过2,048。
- 最坏费用：`12288 × ¥0.14 / 1M = ¥0.00172032`，向上列为 ¥0.001721。
- 项目账本尚不管理 embedding 费用，因此验收驱动必须在调用前同时检查请求数和 UTF-8 上界；任一超限不得发请求。

## 时间与停止条件

- 最长持续时间：25分钟；超时取消当前调用并停止批次。
- 任一安全越权、跨作用域召回、原文丢失、重复 Episode、错误来源归因、依赖请求被放行、阻断后产生业务副作用、usage 或费用不可解释，立即停止。
- 任一固定案例失败，不继续挑选成功样本；保留失败证据，修复后另开批次且连续3次重新计数。
- 模型余额不足、认证失败、价格页或模型版本变化时停止；不充值、不切换账号、不启用备用模型。
- 不启动研究、多 Agent、计划执行、经验候选、Canary 或发布；不启动应用库上的后台候选生成。

## 数据与交付

- 执行目标为专用隔离 PostgreSQL；不得把 `TEST_DATABASE_URL` 指向应用库，不清理应用库。
- 测试数据显式标记 `M1-LIVE-20260908-002`；失败数据和日志保留，密钥、Authorization、完整私密正文不得写入报告。
- 记录每轮资源 ID、入选与排除记忆、Episode 来源、模型 invocation/attempt、输入输出 usage、实际费用、时延和终态。
- 只有三轮连续通过，且离线回归、安全和一致性检查仍通过，才把 M1 标记完成并允许进入 M2。

## 已关闭授权

本批次已在硬失败后停止，不得依据原批准继续执行或补跑。
