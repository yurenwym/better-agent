# 自进化 v2 继续开发与验收

日期：2026-09-10。本记录接续 `agent-evolution-v2-development-2026-09-10.md`，不删除此前失败及缺口记录。

后续用户授权了策略及费用选择，已执行真实模型分阶段验收，见 [真实模型验收记录](learning-v2-real-model-2026-09-10.md)。下文“尚未执行”描述保留为该阶段历史状态，不代表最新结果。

## 本轮补齐

### Prompt 自动流程

新增 `learning_prompt.py`：持久政策选定冻结评测集，收集具有诊断标签的独立 researcher 失败证据，创建 `prompt_cycle` job；同一独立 learning root 下执行一次生成和双臂配对评测，然后由既有持久采用 job 做 policy 决策。

- proposer 不接触 HOLDOUT/SAFETY；评测集内容以 owner/hash 隔离文件保存，数据库保留冻结元数据。
- 已消费的 holdout 不允许用于新一轮调参；生成前检查，避免白花生成费用。
- 当前 M5 合同为 60 个案例：生成 1 次，每例两个写作臂和质量/安全检查共 240 次，总计至少 241 次调用。
- root 同时约束最大尝试次数、deadline、费用；学习日/月预算采用保守预留，另受 owner 总预算约束。
- 发送前检查 policy/租约；学习网络请求关闭自动重试/备用模型重放。发出后无法确定结果时 job 为 UNKNOWN，后续轮询不重放。
- 正常启动接入后台 observer worker，支持已配置政策的多个 owner。
- 前端新增模型调用上限和冻结评测集编号设置。初始暂停、费用为零仍保持。

### 记忆作用范围与自然语言

新增单次模型提取器 `learning_extraction.py`，在政策有预算且用户文本具有明确持久约束信号时启用。结果必须属于编译器白名单、数值有精确来源片段支持，未知返回不改变。当前模型提取仍限于练习时长这一首版结构化 setting，不将任意自由文本升级为可执行指令。

Memory entry 增加 `applicability_json`，支持 `program_id`、`run_id` 或 `turn_id`。`POST /api/learning/constraints` 可把明确用户消息绑定至同 owner 的具体任务。普通 MemoryContextProvider 和计划编译器都会过滤不匹配的限定。未绑定的临时描述不会升级成全局偏好。显式纠正使旧 pin 失效，并撤销依赖旧 revision 的资产。

### 依赖、请求快照和撤销

新增小型 `learning_dependencies` 与 `learning_snapshots`，仅保存引用与使用记录，不复制 Memory/Skill 正文。

- 自动采用记录来源消息、反馈、经验引用。
- 模型调用记录 bundle、Skill 版本/包 digest/冻结 grant、Memory revision 及 included/dropped 信息。
- 发送前与返回后检查已包含资产是否仍有效；明确遗忘和来源删除撤销依赖 job、归档/清理相应记忆及旧请求快照。
- 删除来源导致的记忆清理尊重当前 revision，避免清除用户后来的独立纠正。
- Skill 会话绑定改为 owner 对应平台，测试覆盖 Alice 的技能不能由 Bob 绑定。

这些记录和撤销机制扩展了现有各消费者的 pin，不应宣称已提供任意外部系统的事务快照或撤回已发送的外部请求。

### 研究策略消费者

增加 `research_stop_condition=coverage_satisfied`，谓词只允许跳过可选探索：至少两个来源且各章节都有匹配证据。必需章节、引用检查、最终交付审查与补救仍执行。

后台从不同已完成研究任务的初始覆盖记录生成局部 TRIAL；后续 PARTIAL/FAILED 会暂停；不把 HTTP 成功或初始覆盖当最终交付成功。记录实际决策和有效 limits。研究额外检索也计入 `max_queries`，修复了反思阶段可能突破查询上限的问题。与 Prompt 同时存在时按现有 bundle 精确匹配，不静默合并不同实验。

### 发布回归合同

旧 canary/进化测试改用已冻结、经过现行 researcher 配对评测的候选。保留版本冲突、样本不足、原子回滚、owner/role 隔离和已完成 exposure 不可重写的验证。旧 HTTP 冒烟发布路径改为明确断言 409 拒绝；生产发布门禁未放宽。

## 验收证据

- 全量后端第一轮：947 passed、3 skipped、0 failed，约 408 秒；日志 `backend/artifacts/learning-v2-regression.log`。
- 补充研究策略/撤销代码后再次运行全量：**949 passed、3 skipped、0 failed**，约 419 秒；`backend/artifacts/learning-v2-regression-final.log`。
- 最后增加评测输入遗忘入口后，受影响专项再次通过：**54 passed**；`backend/artifacts/learning-v2-final-targeted.log`。
- PostgreSQL 专项：6 passed，约 29 秒；`backend/artifacts/learning-v2-postgres-final.log`。
- 其中完整 Prompt 流程使用真实 PostgreSQL、真实路由/调用状态机/费用账本/请求快照，网络响应由受控 adapter 提供；不是外部真实模型质量结论。
- 自然语言提取、临时 scope、跨资产撤销、owner Skill 隔离、研究消费者的专项测试在 `backend/tests/test_learning_v2.py`。
- 前端 `tsc -b && vite build` 通过；GrowthPage + LearningPanel：7 passed。
- Playwright 实际应用测试：1 passed；保存政策、刷新后持久化、390px 页面无横向溢出。截图位于 `frontend/test-results/learning-desktop.png`、`learning-mobile.png`。
- 完整 integration：**59 passed、1 skipped、0 failed**；`backend/artifacts/learning-v2-all-integration-final.log`。跳过项为需显式费用配置的真实模型验收。
- 后续 owner 边界修复后，全部 integration 再次 **59 passed、1 skipped**；`backend/artifacts/learning-v2-owner-integration.log`。相关学习/Skill/数据库专项 **52 passed**，额外的 owner 与保留既有绑定升级检查 **4 passed**。
- 完整 integration 首轮有一项旧 observer 水位断言失败（未读源事件仍期待水位 1）；已改为验证四个流保持 0 并复跑通过。原始失败保留于 `backend/artifacts/learning-v2-all-integration.log`。

## 数据库升级

新增 `20260910_0008_learning_assets.py`、`20260910_0009_owner_release_suites.py`、`20260910_0010_owner_skill_packages.py`，PostgreSQL head 为 `20260910_0010`；SQLite 合同夹具迁移到 32。迁移已在全新隔离 PostgreSQL 上验证，另验证 SQLite 升级保留既有 Skill 版本、grant、冻结 binding 和不可变触发器。未修改生产数据库。

后两项迁移修复全局 digest 唯一约束阻止不同 owner 独立冻结相同评测集、安装相同 Skill 包的问题。包内容哈希不变，授权与版本引用保持隔离。计划编译上下文现在使用目标 program 的实际 owner；首字延迟使用模型 timing，端到端耗时单独记录。

```powershell
# backend
python -m alembic upgrade head
```

新增入口：

- `POST /api/learning/prompt-suites`：登记冻结案例，返回 digest；再通过 policy 选定。
- `DELETE /api/learning/prompt-suites/{digest}`：删除输入正文、清除政策引用，保留不可复活的遗忘标记及 hash 审计。
- `POST /api/learning/constraints`：`message_id` 与单一 `applicability` 限定。
- 原有 policy/history/suspend 接口保留。

## 真实模型验收：尚未执行

提供 `backend/tests/integration/test_learning_v2_live.py`，默认跳过。启用必须同时提供真实模型配置、明确费用上限及绑定实际 provider/model 的价格文件；测试使用隔离 PostgreSQL，并保留 root/费用与 revision 请求快照断言。

```powershell
$env:RUN_LEARNING_V2_LIVE='1'
$env:LEARNING_V2_LIVE_MAX_MICROUSD='<明确的总费用上限>'
$env:LEARNING_V2_LIVE_PRICE_FILE='<价格配置绝对路径>'
python -m pytest tests/integration/test_learning_v2_live.py -q
```

价格 JSON 必须包含 `base_url`、`model_name`，以及 `rates_microusd_per_million_tokens` 对象中的 `uncached_input_rate`、`cache_read_rate`、`cache_write_rate`、`output_rate`、`reasoning_rate`。价格必须来自实际供应商，不得为了让测试通过而填零价。

这个入口验证真实模型计划消费记忆。完整 Prompt 质量验收还须登记真实原始来源的冻结案例，并在明确预算内运行上述自动 cycle；后续新任务的质量、用户纠正和退化回退需要实际证据。**本轮没有启动付费模型实验，也没有把受控 adapter 结果当作真实模型改进。**

## 尚不能据此宣称的能力

首版自动流程技能仍是有用户成功反馈支持的“诊断—练习—复查”；没有开放式生成任意脚本或 connector。Memory 模型提取只开放当前白名单 setting。其他 Task Policy 示例（任意工具偏好、任意停止表达式、自动放宽尝试次数）没有开放为学习字段。候选只有 TRIAL 或 UNKNOWN 时，不能声称其真实任务效果已得到证明。

因此代码、数据库和受控端到端验证可分别验收；v2 的真实效果验收仍需明确费用配置、真实模型运行以及后续任务证据。
