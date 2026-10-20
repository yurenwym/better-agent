# 后续开发计划：W05 收尾、PostgreSQL 测试与 Legacy 清理

> 执行者：Grok 或其他开发 Agent。按本文逐任务实现；有 executing-plans 技能时使用其执行流程。完成实现、验证、复审分别记录，不自行把“测试通过”改写成“复审通过”。

**目标：** 修复通知恢复竞争，用真实领域入口验证四类 Worker 的页面轨迹，将数据库测试迁到隔离 PostgreSQL，再按使用证据关闭和删除旧路径。

**架构：** 延续现有 NotificationService、TaskRuntime、各领域 Worker、Event Envelope 与 TraceQuery。通知保留 dispatch_key 领取和未知结果语义；测试设施复用现有 PostgreSQL compose、目标校验、Alembic 与 Playwright，不另造 Worker 或观测框架。

**技术栈：** Python、pytest、PostgreSQL、Alembic、TypeScript、Vitest、Playwright。

**依据：** `docs/harness-mainline/P05起后续开发任务与最终验收-2026-10-15.md`、`docs/harness-mainline/implementation-notes.md` 及本轮复审结论。本文是后续执行增补，不覆盖旧阶段的历史验证记录。

## 1. 基线与当前状态

- 日期：2026-10-19。
- 已提交并推送基线：`2360fb1949f6b40d87b3e1f455e83eb937b391e4`，分支 `front0920`，远程 `origin/front0920`。
- P/T 已交付范围、W01–W04 保持此前复审结论。
- W05 已实现，复审未通过：通知恢复会覆盖仍在发送的请求；四类 Worker 的页面验证不足。
- L01–L05 尚未开始。
- 基线提交钩子实际结果：后端 2696 passed、7 skipped，1835.25 秒；前端 51 个文件、370 passed，24.93 秒；Vite 构建 979 毫秒。
- 该次门禁仍包含旧 SQLite 测试，不含 Playwright，不能称为 PostgreSQL-only 门禁。
- `.commandcode/` 与 `scripts/_e2e_net.txt` 是未提交本地项，不纳入后续功能变更。

## 2. 全局约束

1. 主链路优先；不增加深度研究功能，不开展新的付费评测支线。
2. 新增及迁移后的数据库测试只使用隔离 PostgreSQL；数据库不可用必须失败，禁止回退 SQLite。
3. 保留业务行为覆盖。不得通过删除断言、扩大 skip、缩小默认收集范围、放宽扫描规则来使测试通过。
4. 测试清除开发机模型与通知凭证，使用本地确定性模型和假 provider；不真实发送 webhook、不调用付费模型。
5. 不持有数据库事务或行锁跨越网络请求。若使用数据库会话锁，必须说明连接生命周期、连接丢失行为和并发资源上限。
6. 通知 UNKNOWN 不自动重发；显式重发继续要求幂等键、重复风险确认和原投递关联。
7. 状态与对应领域事件必须同事务提交；无法关联真实来源时不编造事件或可信身份。
8. 复用现有服务；不为四个 Worker 增加统一空接口，不引入新的通用任务框架。
9. 已通过的授权、来源撤销、租约、脱敏、预算与 canary 安全回滚边界不得削弱。
10. 未提供真实观察环境和有效流量时，L03 保持待观察，相应 L04/L05 不删除。无需部署也可以完成此前的代码任务。
11. 每阶段只在稳定版本跑一次完整门禁；开发期间跑受影响的定向测试。提交钩子自动执行的完整门禁可计入该版本，不重复运行相同检查。不得绕过强制钩子。
12. 本文仅授权规划；后续提交、推送、部署按对应用户指令处理。

## 3. 执行顺序与交付批次

| 顺序 | 任务 | 独立交付结果 | 前置 |
|---|---|---|---|
| 1 | R01 | 通知恢复竞争回归与最小修复 | 当前基线 |
| 2 | R02 | 四类 Worker 真实事件到页面的断言 | R01 |
| 3 | R03 | 长会话等待不稳定的定位与必要修复 | R02 |
| 4 | PG01 | 数据库测试清单、覆盖映射和隔离 fixture | 可在 R 阶段完成后实施 |
| 5 | PG02 | 通知、Trace、租约和 Worker 测试迁移 | PG01 |
| 6 | PG03 | 对话、计划、审批、复盘测试迁移 | PG02 |
| 7 | PG04 | 其余持久化测试与迁移测试迁移 | PG03 |
| 8 | PG05 | PostgreSQL-only 默认门禁与禁止回退检查 | PG04 |
| 9 | L01–L02 | 旧入口使用可见、执行开关明确 | W05 收尾通过、PG05 |
| 10 | L03 | 有效运行观察及逐入口退出判断 | L01–L02、实际观察条件 |
| 11 | L04–L05 | 仅删除有证据支持的旧实现 | 对应入口通过 L03 |
| 12 | G01–G02 | 固定版本验收与交付说明 | 实际交付范围收束 |

R01–R03 是原 W05 的补充任务，不重复开一个 Worker 项目。PG 编号只表示测试迁移，不更改原 L 编号。

## 4. R：关闭 W05 的两项审查问题

### R01 通知恢复不能覆盖活跃发送

**修改入口：**

- `backend/app/notifications.py`：`recover_interrupted_sends`、`send`、`_finish`。
- `backend/app/startup.py`：启动恢复调用。
- `backend/app/research/worker.py`：Worker 启动恢复调用。
- `backend/tests/integration/test_notification_postgres.py`：独立连接并发回归。

**现有问题：** 恢复入口把所有 SENDING 改成 UNKNOWN；另一个仍活跃的发送随后收到成功响应时，`_finish` 只更新 SENDING，成功结果无法落盘。启动第二个服务或 Worker 就能触发，不需要真实崩溃。

- [ ] **R01.1 先写失败回归。** 使用两个 NotificationService、独立数据库连接和带 `started/release` 闸门的假 provider。A 领取并阻塞在 HTTP；B 执行恢复；释放 A 并返回成功。断言 provider 调用一次、最终 SENT、成功事件一次。该测试在当前基线上必须暴露问题。
- [ ] **R01.2 明确恢复资格。** 在实际多进程启动方式下，恢复必须能够区分活跃执行和已中断执行。不得以“另一个进程刚启动”“超过固定时长”“当前进程没有对象”证明原发送已死亡。
- [ ] **R01.3 实现最小边界。** 优先复用已有可证明的执行者生命周期或互斥机制；若没有，先移除无条件全库恢复的调用，并为显式恢复建立可验证的执行者终止条件。仅移除恢复而不提供中断恢复路径，不算完成 R01。确需持久化执行标识或数据库会话锁时，只服务于通知恢复，不引入租约到期自动重发。
- [ ] **R01.4 核对晚到结果。** 明确同一投递的原发送成功、失败、取消和恢复交错如何收敛。禁止简单允许任意 UNKNOWN 被任意调用改成 SENT。结果写回必须属于原投递的执行权；旧执行者不得覆盖另一轮发送。
- [ ] **R01.5 补中断与竞争断言。** 真正终止执行者后只把原行恢复为 UNKNOWN，不增加 provider 次数、不新建投递；两个恢复者仅产生一次状态变化和未知事件；同 key 回放不发送；新 key 的 UNKNOWN 确认规则保持。
- [ ] **R01.6 运行 PostgreSQL 定向并记录。** 若引入 schema 变化，补空库和旧数据升级测试，并同步 Alembic head；若无需迁移，明确记录“无新迁移”。

**验收：** 活跃发送不被启动恢复覆盖；真实中断仍可保守收敛为 UNKNOWN；不自动重复发送；无持锁跨网络的长事务。通知外部副作用仍不宣称 exactly-once。

```powershell
# 在 backend 目录
python -m pytest tests/integration/test_notification_postgres.py -q --tb=short
```

### R02 四类 Worker 的真实事件在页面可见

**文件：** `frontend/e2e/trace-mainline.spec.ts`、`scripts/seed_e2e.py`、`scripts/e2e_server.py`；必要时新增仅供测试的 `scripts/e2e_worker_scenarios.py`，集中组织领域 fixture，禁止增加生产测试路由。后端真实入口分别为 `memory_archive.py`、`embedding_worker.py`、Learning pipeline 与 NotificationService。

- [ ] **R02.1 建立真实来源。** 在隔离 PostgreSQL 中创建可授权的线程和真实回合，通过现有领域入口创建任务；不得直接插入四类成功事件。可直接准备领域数据，但生命周期事件必须由实际服务写出。
- [ ] **R02.2 验证 Archive。** 执行真实领取与提交，断言摘要/覆盖游标变化；取得其 `archive.claimed`、`archive.committed` 的 event_id，页面对应线程可找到相同事件。
- [ ] **R02.3 验证 Embedding。** 使用假 embedding provider，执行现有 Worker，断言当前 revision/profile 的向量完成；页面可见相同任务的领取和提交事件。
- [ ] **R02.4 验证 Learning。** 使用确定性生成或已派发后中断场景，实际领取并恢复为 UNKNOWN；页面显示“结果未知”含义，`effect=unknown`、不可自动重试，不展示敏感来源正文。
- [ ] **R02.5 验证 Notification。** 从真实报告对应的线程/回合调用通知服务，分别返回成功和无法确认的响应。断言页面显示通知成功/结果未知，event_id 与接口一致，provider 次数符合领取规则。
- [ ] **R02.6 保留权限与完整度验证。** 其他用户看不到上述事件；删除线程后不可读；无法关联历史时仍为 partial。不能只断言“未接入”文案消失，不能把来源已连接等同于每条历史都完整。

**验收：** 每类至少一条“领域状态变化 → 真实事件 → Trace API → 页面”的对应断言。可合并成少量紧凑场景，不为每个事件增加一个独立页面启动。既有七场景保留。

```powershell
# 在 frontend 目录
npm run e2e -- e2e/trace-mainline.spec.ts
```

### R03 定位长会话 E2E 的间歇等待失败

**文件：** `frontend/e2e/trace-mainline.spec.ts` 中 `sendUntilArchive`、`scripts/e2e_scripted_model.py`、`scripts/e2e_server.py`；仅在证据证明业务错误时改实际归档或对话入口。

- [ ] **R03.1 失败时采集事实。** 保留 Playwright trace、页面错误、回合/归档任务状态、脚本模型闸门状态及服务日志。敏感输入不进入报告。
- [ ] **R03.2 识别等待对象。** 区分页面尚未就绪、回合仍运行、归档 Worker 等待、模型闸门未释放与真实后端卡住；使用确定的状态/响应同步，避免凭固定 sleep 猜测。
- [ ] **R03.3 修复被证实的根因。** 不降低 `history_min_turns`，不丢弃历史，不仅调大 90 秒超时，不用自动重试掩盖失败。
- [ ] **R03.4 验证修复。** 运行受影响场景复现并关闭原竞争，再跑完整 spec。若多次通过用于检查偶发竞争，记录每次结果；不得只保留最后一次成功。

**验收边界：** 本任务仍只要求历史整理提示、输入保留和后续发送恢复。原等待回合自动续跑不是已有测试证明的结论；未经新增需求不扩大为另一项功能开发。

## 5. PG：旧数据库测试迁移

### PG01 盘点测试并建立隔离基础

**文件：** `backend/tests/conftest.py`、`backend/tests/integration/conftest.py`、`backend/tests/integration/db_target_guard.py`、`backend/tests/integration/compose.postgres.yaml`。新增 `docs/harness-mainline/postgres-test-migration.md` 保存迁移映射。

- [ ] **PG01.1 分类。** 区分无数据库纯单测、通过 Database 访问数据库的测试、直接 sqlite3/内存库测试、旧数据库迁移测试、真实网络测试。保存旧 nodeid → 新 nodeid/原地迁移位置 → 保留断言的映射。
- [ ] **PG01.2 提升既有 fixture 可用范围。** 复用已存在的 PostgreSQL fixture 与迁移流程，让普通 tests 下的数据库测试也能使用；不要复制第二套 Docker 管理逻辑。
- [ ] **PG01.3 明确隔离。** 测试目标必须通过目标校验；跨连接测试使用独立可清理的库或 schema，不用只在一个连接回滚的 fixture 假装隔离所有服务连接。
- [ ] **PG01.4 关闭回退。** Docker、目标检查、迁移或连接失败均使该验证失败。关闭服务与连接池后再清理库/容器，清理失败返回非零退出码。

```powershell
# 在仓库根目录进行静态盘点，不执行 SQLite 测试
rg -n 'sqlite3|sqlite://|:memory:|BETTER_AGENT_TEST_ALLOW_SQLITE|.db['"]' backend/tests scripts frontend/e2e
```

**验收：** 有完整迁移清单和可复用的隔离 PostgreSQL fixture；不因测试名称包含 sqlite 就直接删除其业务覆盖。

### PG02 迁移通知、轨迹与 Worker 测试

**优先文件：** `backend/tests/test_notifications.py`、`test_trace_query.py`、`test_archive_events.py`、`test_memory_archive.py`、`test_lease_interface.py`、`test_task_runtime.py`、`test_learning_promotion.py`、`test_learning_v3_fixes.py`。已有 integration 下的 PostgreSQL 文件继续保留。

- [ ] **PG02.1 通知与 Trace。** 保留幂等、结果分类、软删除、分页快照、legacy 扫描续读、跨用户和脱敏断言。
- [ ] **PG02.2 Archive 与 TaskRuntime。** 保留过期前后、双连接接管、旧执行者拒绝、失败/完成状态和事件原子性断言。
- [ ] **PG02.3 Learning 与 Embedding。** 保留来源变化、policy 暂停、checkpoint 恢复、采用事务、证据锁和 canary 后续安全回滚断言。
- [ ] **PG02.4 校验数量与行为映射。** 每迁移一个模块先执行新测试，再移除其 SQLite fixture/分支。合并重复用例时逐项证明断言仍在。

**验收：** 上述模块不依赖 SQLite；并发测试使用独立连接及确定性闸门。保留租约过期瞬间和数据库锁等待的真实 PostgreSQL 行为。

### PG03 迁移对话和计划主链路

**入口：** 根据 PG01 清单迁移对话/AgentLoop、审批工具、目标程序、计划保存/激活、行动完成、`close_day` 与复盘相关测试；复用 `test_agent_loop_goal_postgres.py`、`test_chat_goal_tools_postgres.py`、`test_goal_tool_recovery_postgres.py` 的领域 fixture。

- [ ] **PG03.1 对话。** 保留失败输入、取消、重启恢复、身份冲突、审批前后与重复工具调用断言。
- [ ] **PG03.2 计划。** 保留创建、保存版本、预览、批准激活、完成行动和显式 close_day 的顺序与幂等断言。
- [ ] **PG03.3 复盘。** 验证必须显式结束当天后再复盘；Agent 读取结果与领域记录一致。
- [ ] **PG03.4 历史与权限。** 历史记录可读、被删线程及跨用户不可读；并发恢复不产生第二次业务副作用。

**验收：** 主链路后端测试已迁移；七个已有 PostgreSQL 页面场景继续保留，不回退数据库。

### PG04 迁移其余持久化测试和升级覆盖

**入口：** PG01 剩余清单、`backend/tests/integration/test_sqlite_postgres_migration.py`、现有 Alembic 测试。

- [ ] **PG04.1 完成剩余模块。** 快照、模型网关、预算、记忆、配置与服务持久化按模块迁移；纯计算单测保持不连接数据库。
- [ ] **PG04.2 升级测试。** 从真实旧 Alembic revision 准备数据再升级到当前 head，检查状态、owner、唯一约束、历史和重启；不得只测空库建表。
- [ ] **PG04.3 处理 SQLite 专属测试。** 业务断言迁到 PostgreSQL；专测已退出验收范围的 SQLite 引擎语法可删除并在清单注明。若测试对应仍承诺支持的数据导入能力，不得以本次迁移为由顺手删除生产能力；明确该能力未在 PostgreSQL-only 套件中验证的范围。
- [ ] **PG04.4 核对漏项。** 不允许未迁移模块被默认 deselect、改文件名躲开收集或整体 skip。

**验收：** 默认数据库业务验证不再需要 SQLite；旧数据升级覆盖保留。删除测试的理由逐项可核对，不能用总通过数变少代替说明。

### PG05 固定 PostgreSQL-only 门禁

**文件：** `backend/tests/conftest.py`、`scripts/test_all.py`、`.githooks/pre-commit`、`backend/tests/integration/test_db_target_guard.py` 与必要的门禁单测。

- [ ] **PG05.1 删除全局 SQLite 许可。** 移除 autouse fixture 对 `BETTER_AGENT_TEST_ALLOW_SQLITE=1` 的设置，同时保留环境凭证清理；检查子 fixture 不再重新打开该开关。
- [ ] **PG05.2 防回退。** 增加不连接 SQLite 的拒绝测试：给运行入口 SQLite 目标时明确报错；验证缺 PostgreSQL、错误库名、迁移失败时门禁非零退出。
- [ ] **PG05.3 保留完整收集范围。** 默认全量继续覆盖全部后端业务测试、前端测试和构建；页面 E2E 单列明确命令。既有 live/付费测试可保持明确 opt-in，并逐项说明 skip 原因。
- [ ] **PG05.4 核对配置。** 全局 fixture 不再默认把所有测试强制切到 legacy。只在确实验证旧路径的用例中显式开启，主链路测试覆盖生产默认配置。
- [ ] **PG05.5 执行门禁。** 在同一代码版本跑完整脚本与页面 spec；证明不存在 SQLite 连接路径，不能仅靠环境变量名改变宣称完成。

**过渡纪律：** PG05 完成前，旧 `test_all.py` 仍是混合套件，不能作为 PostgreSQL-only 证据。迁移阶段只主动执行已经迁移的定向测试；不要为逐个小任务频繁提交而反复触发旧钩子。保持任务粒度的小改动，收束到 PG05 后再形成受完整门禁保护的提交。不得跳过钩子；若必须提前提交，先明确旧钩子与“不再运行 SQLite”要求的冲突。

## 6. L：逐步退出 Legacy

### L01 记录真实旧入口使用

**文件：** `backend/app/learning.py`、`conversation.py`、`tools.py`、`memory_archive.py` 及现有事件查询。

- [ ] **L01.1** 找到实际旧 Learning 执行、conversation 路由、计划保存与 archive fallback 分支，核对静态引用、动态注册和启动配置。
- [ ] **L01.2** 在真正进入旧业务分支时记录 `legacy.learning.execute`、`legacy.conversation.route`、`legacy.plan.save`、`legacy.archive.fallback`，包含可信来源、原因、版本和副作用阶段，不含正文或密钥。
- [ ] **L01.3** 复用现有查询汇总调用次数和失败原因，不另建监控服务。
- [ ] **L01.4** PostgreSQL 测试实际走旧入口；新路径不误记，拒绝不能被记成成功执行。

### L02 明确关闭旧执行路径

**文件：** `backend/app/config.py`、`conversation.py`、`learning.py` 及对应旧入口。

- [ ] **L02.1** 按原开发文档实现 `BETTER_AGENT_LEGACY_MODE=disabled|enabled`，默认 disabled；列出它与 loop/Learning 模式的合法组合。若已有等价配置，先核对复用方式，避免两个开关表达相反语义。
- [ ] **L02.2** 启动时拒绝冲突配置；disabled 禁止旧业务副作用，但允许必要的历史读取。
- [ ] **L02.3** 新路径失败不静默降级；旧执行请求返回 `LEGACY_PATH_DISABLED`，Trace 能解释拒绝。
- [ ] **L02.4** 验证未配置、显式启用/关闭、缺 pipeline、异常、历史读取和重启。兼容测试在用例内显式打开开关。

**L01/L02 交付：** 代码和 PostgreSQL 回归可以完成，不需要为了测试而部署。

### L03 运行观察

- [ ] **L03.1** 记录实际版本、运行环境、观察起止时间、有效操作量和采集连续性。
- [ ] **L03.2** 覆盖相应后台定时任务完整周期和关键用户流程，统计旧调用、失败与在途任务。
- [ ] **L03.3** 逐入口给出可删/继续保留结论；零事件必须同时有有效流量和已验证的采集。
- [ ] **L03.4** 无运行环境、有效流量或足够周期时标为待观察。测试库零数据不算退出证据。

### L04 按入口删除

- [ ] **L04.1** 只选择已通过 L03 的入口，核对静态引用、注册表、开关和在途任务。
- [ ] **L04.2** 每批删除一个业务旧入口；保留仍需要的历史解码器、事件写入器和数据迁移。
- [ ] **L04.3** 回归主链路、历史读取、迁移后重启与本批对应 E2E。
- [ ] **L04.4** 提交说明写清删除范围和回退条件，不引入无关重构。

### L05 删除失效配置和兼容代码

- [ ] **L05.1** 根据 L04 的实际删除结果定位悬空 adapter、导入、开关和重复分支。
- [ ] **L05.2** 删除确定无调用的代码；保留有使用者的兼容项并说明原因。
- [ ] **L05.3** 更新 README、环境变量说明和旧配置报错/迁移方式。
- [ ] **L05.4** 验证残留引用、默认启动、历史记录和 PostgreSQL 迁移。

## 7. G：验证与最终目标

### G01 固定版本验证

- [ ] **G01.1** 记录 HEAD 和实际工作区差异；修改后不能复用旧版本的通过结论。
- [ ] **G01.2** PostgreSQL 空库和旧版本升级通过，关键并发回归通过。
- [ ] **G01.3** 后端完整业务测试、前端测试和构建通过；必需用例零失败，skip 逐项解释。
- [ ] **G01.4** 单独运行实际页面 spec；检查服务先停、连接池先关、数据库最后清理，无残留测试容器。
- [ ] **G01.5** `git diff --check` 通过；新文件暂存后再次检查 `git diff --cached --check`。

```powershell
# 仓库根目录；PG05 完成后才是 PostgreSQL-only 门禁
python scripts/test_all.py
git diff --check

# frontend 目录
npm run e2e -- e2e/trace-mainline.spec.ts
```

### G02 状态与交接

- [ ] **G02.1** 更新原任务文档与 implementation-notes，只追加本轮结果，不修改旧结果数。
- [ ] **G02.2** 区分实现完成、执行者测试通过、独立复审通过、已提交、已推送、已部署。
- [ ] **G02.3** 记录实际入口、行为变化、验证命令/退出码、数据库和 provider 模式、剩余限制。
- [ ] **G02.4** L03 条件不满足时交付其余已完成范围，明确保留项；不能写“四项优化全部完成”。

### 最终目标

1. 通知恢复不破坏活跃发送，中断不会触发自动重复外发。
2. 四类 Worker 的业务状态和页面事件可以对应，未知结果、权限和 partial 信息正确。
3. 数据库业务验证全部运行在隔离 PostgreSQL，禁止回退 SQLite，原有关键覆盖保留。
4. 旧执行入口可观测、默认关闭；只在真实无依赖证据成立后删除。
5. 开发期间用定向测试快速反馈，阶段收束做一次完整验证，减少无效重复运行。
