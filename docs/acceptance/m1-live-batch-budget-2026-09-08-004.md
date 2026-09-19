# M1 真实模型验收批次预算单（004）

状态：待用户明确批准；本文不授权执行。

日期：2026-09-08，Asia/Shanghai。
批次：`M1-LIVE-20260908-004`。

## 前序批次与累计费用

- `-001` 未执行，费用为0；`-002` 与 `-003` 均在第1轮通过、第2轮 embedding timeout 后停止。
- 前序批次合计14个 DeepSeek attempts、7个 embedding 请求、1,197 token embedding 输入上界；保守费用上界 US$0.353248 与 ¥0.00016758。
- 本批次新增上限为 US$0.53 与 ¥0.001721；批准并用满后，全部 M1 批次累计最坏上界为 US$0.883248 与 ¥0.00188858。

## 修复与冻结版本

- 实际 round 客户端现在强制使用60秒 embedding timeout；向量化任务只有达到 `COMPLETED` 才继续。离线 fake client 明确断言收到60秒配置。
- 生产记忆召回逻辑、语义阈值和应用默认 embedding timeout 均未改。
- 代码基线：HEAD `c7221cf34ae065ddb7d2de86b14e9dbb90889af4`；已跟踪工作树 diff 对象 `ecbf1268bc6bb2458fd87984dec18ea2b38b6d30`。执行前若相关文件变化，批次作废。
- 关键生产文件 SHA256：
  - `costs.py`: `024ce73af79d286e764fddfe819d115d0b16b443a912d98ddadaec975b3ebecd`
  - `live_model.py`: `c10640c148ef517ac0af8dfb76522fc1fb20ebf633d01b7d3abdd614e26fba4c`
  - `memory_archive.py`: `6b13889007957df671ffdb05ed1dbe261ba20bda7a404732ebdbfee7b0d71b34`
  - `memory_v2.py`: `857b74803a1f06b9fddd6a67aca50b85c3f9aa749a5825cef65fd26000e6cddb`
  - `conversation.py`: `9e51691df04da066af9a00813140316ef5de5e2eddc1aaacbc66c51724c9953d`
- 验收驱动 SHA256：
  - `scripts/m1_live_acceptance.py`: `f3a8c40d65366d15c7b06b730728dc80697160f44a2a098298e372b3dddb0533`
  - `tests/test_m1_live_acceptance_budget.py`: `5428488afa7997c7743b688cd2b9fdc54749ffdae8786d53c353efe452f0998a`
  - `tests/integration/test_m1_live_harness.py`: `a2c88c3eb22763e3639925441c6f460ca863aaec742f4e38ee754d0d2b057fe4`
- 模型：DeepSeek `deepseek-v4-flash`，上下文32,768、最大输出8,192；无备用模型。
- Episode `episode-v4`；memory renderer `memory-v5`；tokenizer `utf8-upper-bound-v1`。
- Embedding：SiliconFlow `Qwen/Qwen3-Embedding-4B`，1,024维，验收 timeout 60秒。

## 固定案例与连续次数

每轮使用新的隔离 PostgreSQL 数据库、owner 和 thread，不接触应用业务数据。完整核心流程连续执行3轮，任一硬失败立即停止，修复后从0重新计数。

1. 同一问题比较无记忆基线与 `memory-v5` 候选；当前消息要求 SQLite 时遵从，但不得改写长期 PostgreSQL 偏好。
2. 同义召回“骑行负荷每周增幅不超过10%”，关键词召回未向量化 `recovery_friday`；排除 `solarized`、跨项目 Rust、过期30%和已删除周二恢复。
3. Episode 保留用户确认“每天60分钟”及来源；助手未获确认的“每天90分钟”不得进入 decisions/outcomes；不得丢原文、错游标或生成重复 Episode。
4. 缺失历史时允许“什么是哈希表？”受限回答并显示上下文不完整；“继续刚才的计划并保存”必须失败，且 ask、计划、研究、Agent run、长期记忆均无新增。
5. 每轮核对模型 invocation/attempt、usage、费用、来源 hash、pin/renderer 版本和数据库终态。

每轮固定6个模型 invocation：基线、候选、Episode归档、两次独立性分类、一次受限回答。三轮必须连续完整通过，不复用前序批次已通过的轮次。

## 新增调用与费用上限

### DeepSeek

官方价格页：<https://api-docs.deepseek.com/quick_start/pricing>，2026-09-08核验。按峰值且全部缓存未命中：输入 US$0.44/1M token，输出 US$1.32/1M token。

- 三轮预期18 attempts，允许最多3个额外 retry；硬上限21，无 fallback。
- 单 attempt 预留 US$0.025232；21 attempts 最坏 US$0.529872，向上设硬预算 US$0.53。
- 第22个 attempt 在网络前拒绝；usage 缺失按完整预留计费，不记为0。

### SiliconFlow Embedding

官方价格页：<https://siliconflow.cn/pricing>，2026-09-08核验。输入 ¥0.14/1M token，输出为0。

- 每轮最多2次、三轮最多6次；单条输入上界2,048，全批次12,288。
- 最坏 ¥0.00172032，向上设硬上限 ¥0.001721；请求数和输入均在网络前拦截。

## 时间、停止条件与数据

- 最长25分钟；任一 provider timeout、认证/余额失败、安全越权、跨作用域召回、原文丢失、重复 Episode、错误归因、依赖请求放行、副作用、usage 或费用不可解释，立即停止。
- 不充值、不换账号、不启用备用模型；不启动应用服务、研究、多 Agent、计划执行、经验候选、Canary 或发布。
- 仅使用专用隔离 PostgreSQL；测试数据标记 `M1-LIVE-20260908-004`，失败报告和数据库证据保留，不记录密钥或 Authorization。
- 只有三轮连续通过且最终离线回归通过，才标记 M1 完成并允许进入 M2。

## 待批准事项

请用户明确批准或拒绝 `M1-LIVE-20260908-004` 的完整新增边界：最多21个 DeepSeek model attempts、US$0.53；最多6个 SiliconFlow embedding 请求、12,288输入 token、¥0.001721；最长25分钟；仅隔离验收数据库；不启用备用模型和其他工作流。此前保守费用上界为 US$0.353248 与 ¥0.00016758；批准后累计最坏上界为 US$0.883248 与 ¥0.00188858。
