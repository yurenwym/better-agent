# Agent 自进化 v2：开发与验收记录

日期：2026-09-10。依据：`docs/architecture/agent-evolution-system-plan-v2-2026-09-10.md`。

后续进展与最新测试结果见 [继续开发与验收记录](agent-evolution-v2-continuation-2026-09-10.md)。以下保留第一轮历史记录，不能作为最新缺口清单。

**结论：已实现并验证一批核心链路，但 v2 全部 P0–P5 尚未通过正式验收。不能将本记录解读为完整自主学习效果已经得到证明。**

本次在原有大量未提交变更上继续开发，未回退或提交这些变更。未迁移生产数据库，未更改生产学习政策，未启动付费模型实验。PostgreSQL 验证使用测试夹具创建的独立容器和数据库，浏览器验收使用临时数据目录。

## 实现内容与边界

| 阶段 | 已实现 | 仍缺少的正式退出证据或实现 |
| --- | --- | --- |
| P0 | `learning_policies`、`learning_jobs`；事务内消息入队；终态事件独立 job；observer 水位不越过实际读取范围；同 root 后续事件保存在 job；租约 token、CAS、发送标记、UNKNOWN 不重发；独立 learning root、单轮和日/月保守预留；暂停阻止采用 | 通用付费学习调度尚未全部接入；所有来源流的故障注入覆盖仍需扩展 |
| P1 | 确定性解析明确持久练习时长；复用 Memory proposal/revision，决策 actor 为 policy；项目覆盖用户范围；compiler 真实消费、时长校验、revision inclusion 事件；纠正 CAS、删除/归档阻止复活 | 解析器仅支持代码中声明的窄句式；不限句式的自然语言提取、program/run/turn 级临时约束和完整冻结资产集合未完成 |
| P2 | `instruction_only`；内容按 RUN/turn 冻结版本读取；当前授权与冻结 grant 取交集；候选存储不启用；从不同已完成计划的明确方法成功反馈采用固定“诊断—练习—复查”流程；相似任务自动绑定、无关任务不绑定；最终请求 inclusion；禁用和来源撤销检查 | 不是开放式方法蒸馏；适用性检查为确定性组件测试，不能替代真实新任务质量/步骤执行验收；多 owner 的自动会话 Skill 绑定尚未完整接入 |
| P3 | bundle 中严格 `task_policy`；未知字段拒绝；compiler 行动上限/估时校准；ResearchLimits 有实际消费者；不同计划实际用时形成估时试用；后续误差变差暂停；不删必需交付、不提高硬上限 | 研究策略自动提炼、停止条件和工具偏好学习尚未实现；跨任务类型归因与真正不可变的评测合同需完善 |
| P4 | researcher 生产/评测共用交付后处理；raw/normalized/delivered 及 transform 版本，持久报告只保存正文 digest；仅冻结合格评测可 policy 自动采用；按 owner/role/purpose 解析；不改全局 stable；实际 writer 命中与后续结果检查；发送前撤销校验 | **候选自动生成→自动配对评测的后台调度尚未打通**，目前从已有 EVALUATED 候选自动采用；沿用 M5 60-case 合同，并未完成 v2 的所有分类型合同；没有真实生产 Prompt 成功/失败闭环验收 |
| P5 | 成长页学习开关、允许资产、美元费用限额、试用期、状态/效果区分、暂停变更；独立 PostgreSQL/浏览器验证；Memory/Skill/Task Policy/Prompt 的若干撤销路径 | 通用跨资产传递依赖撤销、完整最终请求资产快照、真实模型和足量后续任务效果证据未完成 |

## 可复核的行为

1. `以后每次练习最多30分钟` 自动形成有用户消息证据的 Memory revision；新线程计划生成不超过 30 分钟的行动。
2. `这个项目每次练习最多20分钟` 只覆盖对应项目；其他项目沿用用户级 30 分钟。
3. 暂停 Memory 学习变更时，归档对应当前 revision、移除检索索引、使相关 pin 失效；显式 purge 后旧 job 不恢复内容。
4. 方法学习的当前输入契约是 action feedback：`kind=method_success`，`note=先诊断薄弱点，再练习，最后复查`。需要多个独立已完成 program 和不同来源计划 hash。新技能不申请任何工具权限。
5. 估时学习读取 `goal_action_feedback.actual_minutes`；至少三个不同 program/来源计划，前面的计划拟合倍数，最后一个计划做隔离误差比较；下一计划执行服务记录 nominal/calibrated minutes。后续实际误差增大，变更暂停并回到基线。
6. Prompt 采用要求已有冻结通过评测及当前交付 transform 版本；更换标签或仅选择 bundle 不计实际使用。个人采用不改 stable。低样本状态不宣称统计非劣。

## 测试结果

- 全量后端（不含 integration）：**931 passed、3 skipped、11 failed**，耗时约 418 秒。运行期间后续仍有修正，因此另跑受影响范围测试；不能宣称最后状态全量全绿。
- 核心专项及相关回归：**60 passed**（learning、M5 controlled/release gates、Skill）。
- 扩展受影响范围：**271 passed**，包含上述核心路径以及 live_model、conversation、goal_programs、memory_v2、runtime_prompt_policy、research_engine、research_service。
- 独立 PostgreSQL：**4 passed**，验证新库迁移、Memory→新计划→纠正、独立预算/UNKNOWN、并发 claim/暂停、流程技能采用/撤销。
- 前端构建：`tsc -b && vite build` 通过。
- 前端组件：GrowthPage + LearningPanel，**7 passed**。
- Playwright：**1 passed**，实际保存政策、刷新持久化、390px 无横向溢出，桌面/手机截图已人工检查。

全量失败归类：

- `test_canary_assignment.py` 6 项。
- `test_evolution.py` 4 项。
- `test_evolution_api.py` 1 项。

以上失败都在旧式 Prompt 冒烟评测的审批处触发当前 `legacy prompt evaluations are smoke tests, not release evidence` 门禁，尚未进入本次新增学习逻辑。旧测试合同与现有 M5 门禁需要统一；本次没有通过放宽门禁或绕过审批校验使其变绿。

浏览器初次失败：本机未安装 Playwright 对应 bundled Chromium；使用已安装浏览器后还遇到外部字体加载等待。最终使用 Edge、拦截测试中的外部字体资源，通过真实应用 E2E。手机截图发现全局 input 样式拉伸复选框，已增加学习面板局部样式并复测。

## 迁移与复跑

新增 PostgreSQL 迁移 `20260910_0007_learning_jobs.py`，head 为 `20260910_0007`。SQLite 合同夹具增加 migration 29。生产实例使用前需要常规 `python -m alembic upgrade head`；本次未对生产执行。

新库测试发现既有 `20260909_0005` 与基线 schema 重复定义 M5 列/表，迁移已改为 `IF NOT EXISTS`。测试夹具的迁移错误现在显示脱敏 stderr，避免异常回溯输出模型凭据环境值。

```powershell
# backend 目录；设置测试模型环境为空，避免继承本机付费配置。
$env:AGENT_MODEL_BASE_URL=''
$env:AGENT_FALLBACK_MODEL_BASE_URL=''
$env:LLM_AP_PATH=''
$env:DATABASE_URL=''
python -m pytest tests/test_learning_v2.py tests/test_m5_controlled_evolution.py tests/test_m5_release_gates.py tests/test_skills.py tests/test_skill_platform.py -q
python -m pytest tests/integration/test_learning_v2_postgres.py -q

# frontend 目录
npm.cmd run build
npm.cmd test -- --run src/__tests__/GrowthPage.test.tsx src/__tests__/LearningPanel.test.tsx
$env:BETTER_AGENT_E2E_BROWSER='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
npx.cmd playwright test e2e/learning.spec.ts
```

API：`GET/PUT /api/learning/policy`、`GET /api/learning/history`、`POST /api/learning/jobs/{id}/suspend`。变更沿用 CSRF 校验和既有 owner 标识机制。初始政策暂停、费用为零；配置数值由用户在成长页保存，不按每条资产重复审批。

## 下一步正式验收要求

1. 补全上表尚未实现的功能，尤其 Prompt 自动生成/评测调度、临时 scope、统一快照及跨资产依赖撤销。
2. 更新旧发布流程测试为现行合同，保持发布门禁，完成全量回归。
3. 在用户设定的每轮/每日/月预算及试用策略内，使用隔离的真实 PostgreSQL、真实模型和预先冻结的原始来源案例进行验收。当前没有这部分预算设置，也没有运行或声称通过真实模型实验。
4. 分别证明采用、证据不足拒绝、真实后续使用、退化暂停与再次纠正；保留失败记录和未知样本，不把合成或组件测试结果算作生产效果。
