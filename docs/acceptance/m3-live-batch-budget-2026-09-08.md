# M3 真实模型验收预算单（草案）

> 2026-09-09 修订中：以下旧冻结范围暂不可用于执行批准。驱动新增单 Agent 基线，现为每轮最多 5 次调用、三轮正常路径 15 次，总硬上限仍为 18 次；同题材料为 m3-comparison-v1。成功运行只进入 AWAITING_QUALITY_REVIEW，不代表 M3 阶段通过。尚须补齐真实对话入口、多主题盲评、代码/提示词指纹与新预算单，再提出执行授权。本文以下的“四调用”描述保留为历史草案，不作为当前执行契约。

状态：执行驱动已准备；真实批次仍待用户明确批准；本文不授权执行。

离线驱动：`backend/scripts/m3_live_acceptance.py`。默认运行只做无网络预检；只有同时传入 `--execute` 且设置 `M3_LIVE_APPROVED=1` 才会启动隔离 PostgreSQL。驱动固定三轮、独立数据库、价格快照绑定和报告输出，并在网络前拒绝未授权/超预算调用。对应门禁测试：`backend/tests/test_m3_live_acceptance.py`、`backend/tests/test_m3_live_acceptance_budget.py`。

## 目的

验证 M3 “多 Agent 协作”真实核心场景：从同一对话上下文自动构造 researcher/planner/critic 子任务，保留角色来源与 runtime bundle，允许部分成功，拒绝晚到结果覆盖终态，并在连续 3 个全新隔离轮次中通过。

## 冻结范围

- 每轮使用独立 PostgreSQL 数据库、owner、thread 和 idempotency namespace。
- 每轮 1 个协作根任务，最多 3 个专家子任务（researcher、planner、critic）和 1 个 coordinator 汇总。
- 每个角色使用根任务创建时绑定的不可变 runtime bundle；不读取新的 stable bundle。
- 固定案例覆盖：完整协作、一个专家永久失败后的部分交付、全部专家失败、取消 fencing、租约接管、重复提交幂等、共享预算不得超额。
- 仅记录脱敏输入、结构化角色产物、错误分类、调用数、延迟和费用；不保存原始凭据。

## 预算与停止条件

执行前已通过 DeepSeek 官方定价页（2026-09-08）核验价格，但仍须在实际 profile version 上创建不可变 `model_price_snapshot`，并在执行前再次检查快照绑定；不得把网页价格直接当作账本记录。

建议冻结上限：每轮最多 4 个 primary attempts、每个失败子任务最多 1 次可重试 attempt；三轮总 attempts 不超过 18；最长 15 分钟。按待复核的历史峰值费率和每次 32,768 输入 / 8,192 输出上界，单次最坏预留为 `25,232 microusd`，18 次总上限为 `454,176 microusd`（US$0.454176）。第 19 次 attempt 必须在网络请求前被拒绝。任一轮发生跨作用域上下文、角色 bundle 不一致、晚到结果覆盖、取消后外部调用未停止、预算预留超额、结构化产物越权或无法解释的费用，立即停止批次。

当前本地配置：DeepSeek `deepseek-v4-flash`（版本 `DeepSeek-V4-Flash-0731`），OpenAI-compatible，context window 32,768，max output 8,192；无备用模型。官方定价页为 <https://api-docs.deepseek.com/quick_start/pricing>：峰值缓存未命中输入 US$0.44/1M token、输出 US$1.32/1M token。按该费率计算，单次最坏预留 `25,232 microusd`，18 次总上限 `454,176 microusd`（US$0.454176）；该金额仍以执行时价格快照为准。

## 证据与禁止事项

- 保留三轮独立报告及每次 attempt 的 request digest、runtime bundle id、prompt digest、usage、cost status 和终态事件。
- M1 真实结果不得作为 M3 证据；离线测试不得替代真实出口。
- 未取得批准前不得启动应用服务、后台 worker、真实模型、embedding、研究、进化候选、Canary 或发布。

## 待批准事项

请先核验价格快照并补充：模型/profile version、提示词和数据集版本、每角色输入/输出上限、总调用上限、最坏费用与硬上限。用户明确批准完整边界后，才可执行 M3 真实批次；批次通过后再准备 M4。
