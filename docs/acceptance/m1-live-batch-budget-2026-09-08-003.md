# M1 真实模型验收批次预算单（003）

状态：已批准并执行，第二轮失败后停止；本批次已关闭，不得重跑。

日期：2026-09-08，Asia/Shanghai。
批次：`M1-LIVE-20260908-003`。

## 执行结果

- 第1轮完整通过；第2轮首次条目 embedding 请求 timeout 后立即停止；第3轮未执行。
- 发起6个 DeepSeek attempts、3个 embedding 请求，embedding 输入 UTF-8 上界423 token。
- 本批次保守费用上界 US$0.151392 与 ¥0.00005922；加上 `-002` 后累计为 US$0.353248 与 ¥0.00016758。
- 失败报告：`m1-live-results-2026-09-08-003.json`；隔离容器已停止，未启动应用服务或其他工作流。
- 根因是60秒 timeout 只应用于预检对象，实际 round 客户端仍重新加载0.8秒默认值。`-004` 已修复实际 profile 构造并增加离线断言，须重新批准。

## 前序批次与累计费用

- `M1-LIVE-20260908-001` 未执行，外部调用和费用均为0。
- `M1-LIVE-20260908-002` 第1轮通过、第2轮失败后停止；已发起8个 DeepSeek attempts、4个 embedding 请求，保守费用上界 US$0.201856 与 ¥0.00010836。
- 本批次为修复后的独立重跑，连续3轮从0计数。新增上限为 US$0.53 与 ¥0.001721；批准并用满后，全部 M1 批次累计最坏上界为 US$0.731856 与 ¥0.00182936。

## 修复与冻结版本

- 修复：条目向量化后查询 `embedding_jobs.status`，只有 `COMPLETED` 才继续；真实验收专用 embedding timeout 固定为60秒。生产记忆召回逻辑和阈值未改。
- 代码基线：HEAD `c7221cf34ae065ddb7d2de86b14e9dbb90889af4`；已跟踪工作树 diff 对象 `ecbf1268bc6bb2458fd87984dec18ea2b38b6d30`。执行前若相关文件变化，批次作废并重新核算。
- 关键文件 SHA256：
  - `costs.py`: `024ce73af79d286e764fddfe819d115d0b16b443a912d98ddadaec975b3ebecd`
  - `live_model.py`: `c10640c148ef517ac0af8dfb76522fc1fb20ebf633d01b7d3abdd614e26fba4c`
  - `memory_archive.py`: `6b13889007957df671ffdb05ed1dbe261ba20bda7a404732ebdbfee7b0d71b34`
  - `memory_v2.py`: `857b74803a1f06b9fddd6a67aca50b85c3f9aa749a5825cef65fd26000e6cddb`
  - `conversation.py`: `9e51691df04da066af9a00813140316ef5de5e2eddc1aaacbc66c51724c9953d`
- 验收驱动 SHA256：
  - `scripts/m1_live_acceptance.py`: `2e2fe899fc01a7b826492275490052a6125d05391adf270c33542c7541ba0957`
  - `tests/test_m1_live_acceptance_budget.py`: `5428488afa7997c7743b688cd2b9fdc54749ffdae8786d53c353efe452f0998a`
  - `tests/integration/test_m1_live_harness.py`: `7afa6919905bc58adaf91785d0d65f9705c65eabcd2e41ae55af40b67190a3b7`
- 对话模型：`deepseek-v4-flash`（`DeepSeek-V4-Flash-0731`），OpenAI-compatible API；上下文配置32,768，单次最大输出8,192，无备用模型。
- 归档提示词：`episode-v4`；记忆 renderer：`memory-v5`；tokenizer：`utf8-upper-bound-v1`。
- Embedding：SiliconFlow `Qwen/Qwen3-Embedding-4B`，1,024维，timeout 60秒；不使用其他 embedding 模型。

## 固定案例与连续次数

每轮使用新的隔离 PostgreSQL 数据库、owner 和 thread 命名空间，不读取或修改应用业务数据。完整核心流程连续执行3轮；任一硬失败立即停止，修复后连续计数从0重新开始。

1. 基线无记忆回答与 `memory-v5` 候选回答使用同一问题；当前消息要求 SQLite 时回答遵从，但长期 PostgreSQL 偏好不得被改写。
2. “骑行负荷每周增幅不超过10%”须被同义问题召回；未向量化 `recovery_friday` 须由关键词召回；`solarized`、跨项目 Rust、过期30%和已删除周二恢复不得出现。
3. Episode 必须保留用户确认的“每天60分钟”及消息来源；助手未获确认的“每天90分钟”不得进入 decisions/outcomes；原消息和归档游标保持一致，重试不得产生重复 Episode。
4. 缺失历史时，“什么是哈希表？”允许受限回答并标记上下文不完整；“继续刚才的计划并保存”必须失败，且 `turn_asks`、计划、研究、Agent run、长期记忆计数不变。
5. 每轮核对模型 invocation/attempt、usage、费用、来源 hash、pin/renderer 版本和数据库终态。

每轮固定6个模型 invocation：基线回答、候选回答、Episode归档、两次独立性分类、一次独立问题受限回答。不是模型盲评，不把文风差异计为 M1 质量收益。

## 新增调用与费用上限

### DeepSeek

官方价格页：<https://api-docs.deepseek.com/quick_start/pricing>，2026-09-08核验。按 `deepseek-v4-flash` 峰值、全部缓存未命中估算：输入 US$0.44/1M token，输出 US$1.32/1M token。

- 三轮预期18 attempts，允许全批次最多3个额外 retry；总硬上限21，无 fallback。
- 单 attempt 预留：`32768 × 0.44 / 1M + 8192 × 1.32 / 1M = US$0.025232`。
- 21 attempts 最坏费用 US$0.529872；DAILY 硬预算 `530000 microusd`，INVOCATION 预算 `25232 microusd`。
- 第22个 attempt 必须在网络请求前拒绝；usage 缺失按完整预留上限计入，不记为0。

### SiliconFlow Embedding

官方价格页：<https://siliconflow.cn/pricing>，2026-09-08核验。`Qwen/Qwen3-Embedding-4B` 输入 ¥0.14/1M token，输出为0。

- 每轮最多2次请求，三轮硬上限6次；单条 UTF-8 输入上界2,048，全批次12,288。
- 最坏费用 `12288 × ¥0.14 / 1M = ¥0.00172032`，向上列为 ¥0.001721。
- 请求数、单条和总输入上限均在网络前检查；任一超限不得发请求。

## 时间、停止条件与数据

- 最长25分钟；超时取消当前调用并停止批次。
- 任一 provider timeout、认证或余额失败、安全越权、跨作用域召回、原文丢失、重复 Episode、错误归因、依赖请求放行、阻断后产生业务副作用、usage 或费用不可解释，立即停止。
- 不充值、不切换账号、不启用备用模型；不启动研究、多 Agent、计划执行、经验候选、Canary、发布或应用库后台 worker。
- 仅使用专用隔离 PostgreSQL；不得把 `TEST_DATABASE_URL` 指向应用库，不清理应用库。
- 测试数据标记 `M1-LIVE-20260908-003`；失败数据和日志保留，密钥、Authorization 和完整私密正文不得写入报告。
- 只有三轮连续通过且最终离线回归通过，才把 M1 标记完成并允许进入 M2。

## 已关闭授权

本批次已在硬失败后停止，不得依据原批准继续执行或补跑。
