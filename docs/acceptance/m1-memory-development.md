# M1 记忆系统实施记录

状态：M1、M2、M3、M4 已完成；M5 本轮不开发、不评测、不灰度。

## 基线与恢复

- 开发基线HEAD：c7221cf34ae065ddb7d2de86b14e9dbb90889af4；在用户现有未提交修改上增量开发，不回退历史修改。
- 2026-09-08一致性pg_dump备份：data/backups/m1-20260908/better-agent.dump。
- SHA256：16399FF87F34481DE10B704B96ED614931C2B2CE56F94C1B0C6F88C04A706CF7。
- pg_restore --exit-on-error成功恢复到独立库better_agent_m1_restore_20260908；未覆盖应用库。
- 恢复检查：迁移20260905_0001、102张表、7个thread、83条消息、0条长期记忆、4个Episode、1份计划。恢复验证库暂保留，不与应用连接。

## 冻结的离线行为门槛

1. 非置顶长期记忆只有达到语义门槛、有效词项匹配或全文召回时才可进入上下文；空查询不填充非置顶条目。
2. 置顶仍受用户/项目作用域、有效期、删除和预算限制；不得通过置顶跨作用域召回。
3. 语义召回与关键词召回并行逻辑合并、按revision去重，部分嵌入覆盖不关闭已有语义结果；过期/失效embedding不占有效候选位。
4. 结构化Episode包含归因摘要、决策、未决事项及版本/来源标识，整体按预算选择；不得把助手建议重标为用户确认事实。
5. 新renderer版本使旧pin不可误重放；既有pin固定快照，编辑/删除使对应pin失效；确认、幂等、隔离回归保持。
6. 提前归档使用现有队列/fencing；归档失败保留原文、游标和死信；恢复必须有界并可观察。
7. 缺失历史只允许独立普通回答；依赖历史或写入必须阻断；不得静默截断后声称完整。

当前相关性基准沿用semantic_min_score=0.35，关键词新增有效词项过滤；这些为离线基线参数，不宣称已完成真实语义质量校准。性能仅记录测量，不杜撰P95。

## 阶段进度

- M0备份与恢复验证：完成。
- M1相关性/混合召回/结构化Episode与pin：实现并完成离线边界审查；renderer 已升级为 `memory-v5`，全文候选再次经过英文词边界校验，预算包含分段标题/分隔符。真实质量验收未执行。
- M1提前归档/恢复入口/完整性与受限回答：实现并完成离线边界审查；移动端提示与空对话布局重叠已修复，归档旧 prompt/tokenizer 版本、并发重试和受限路径副作用均有回归覆盖。真实模型验收未执行。
- M1真实模型费用审批、连续三次验收：`M1-LIVE-20260908-001` 曾获批准，但因离线预检修复导致冻结输入变化而作废，付费调用为0；新预算单 `m1-live-batch-budget-2026-09-08-002.md` 待用户明确批准。

## M2-M5 离线准备（2026-09-08）

> 2026-09-09 更新：M2 已正式验收完成；M3 由 `M3-LIVE-20260909-022` 连续三轮通过；M4 由 `M4-LIVE-20260909-003` 连续三轮通过，正式验收见 `m3-acceptance-final-2026-09-09.md` 与 `m4-acceptance-final-2026-09-09.md`。下列“待真实验收”文字保留为 2026-09-08 当时的历史记录。

- M2：目标调整、计划版本、ask 续接、上下文预算、重启恢复和运行 bundle 绑定回归通过；修复启动时能力子集路由及无未来行动时调整候选被清空的问题。
- M3：Agent 任务、角色合同、部分成功/全失败、取消 fencing、共享预算和 API 回归 `61 passed`。
- M3 真实验收驱动已补齐：`backend/scripts/m3_live_acceptance.py` 默认无网络预检；固定案例覆盖完整协作、部分/全部失败、取消 fencing、幂等、租约和预算门禁。离线门禁与 Agent 回归 `19 passed`；真实三轮仍待独立授权，未宣称 M3 通过。
- M4：研究来源、证据链、部分交付、取消/超时、预算和 API 回归 `76 passed`。
- M5：观察器、经验谱系、评测、审批绑定、Canary 回滚契约回归 `68 passed`；未启动候选生成、真实评测或 Canary。
- M4/M5 阶段预检入口已补齐：`backend/scripts/m4_live_acceptance.py` 与 `backend/scripts/m5_live_acceptance.py` 默认无网络，分别冻结 M4 研究和 M5 评测/Canary 的调用、样本、价格快照与顺序门禁；离线预检测试通过，真实阶段仍必须按 M3→M4→M5 单独授权。
- 全量离线证据：后端 `839 passed, 3 skipped`；M2核心 `105 passed`；前端 `203 passed`；`npm run build` 成功；`git diff --check` 通过。
- 真实 M2-M5 批次仍须分别建立预算单、调用上限、最坏费用和停止条件，并取得明确批准；不得复用 M1 真实批次证据，也不得将离线回归标记为真实验收完成。
- M3 真实批次预算草案：`m3-live-batch-budget-2026-09-08.md`；已通过 DeepSeek 官方定价页核验峰值费率，补充保守上界（18 attempts = US$0.454176）及第19次网络前拒绝门禁，测试 `test_m3_live_acceptance_budget.py` 通过。执行时仍须在实际 profile version 上创建价格快照并取得用户批准，未执行任何真实调用。
- M4/M5 真实预算草案：`m4-live-batch-budget-2026-09-08.md`、`m5-live-batch-budget-2026-09-08.md`；已冻结调用、样本、来源和停止条件，但价格快照与用户批准尚未具备，未执行任何真实流程。

## 2026-09-08 21:32 真实批次 005 结果

- `M1-LIVE-20260908-005` 获批准后完成三轮全新隔离 PostgreSQL 验收，连续3轮通过；18个模型 attempts、6个 embedding 请求、1,161 token 输入上界，耗时103.65秒。
- 模型账本保守费用454,176 microusd（US$0.454176），低于本批次US$0.53硬上限；此前批次累计后M1最坏上界仍为US$0.958944与¥0.00194276，实际本批次供应商费用以usage为准。
- 三轮均通过 hybrid 召回、Episode来源与用户归因、重复归档保护、独立问题受限回答和依赖保存请求阻断；报告 `m1-live-results-2026-09-08-005.json`，摘要 `m1-live-batch-005-result.md`。
- 最终离线回归：后端191 passed；前端203 passed；构建成功；`git diff --check` 通过。M1 标记完成；未经新授权不得启动 M2 或任何后续工作流。

## 2026-09-08 20:23 离线预检与重新冻结

- 新增隔离验收驱动及网络前预算保护测试；每轮独立 PostgreSQL 数据库、owner 和 thread，固定6个模型 invocation、2个 embedding 请求，首个硬失败停止。
- 离线预检修复 PostgreSQL 成本预留结算、过期记忆清理、OR 全文检索和 Episode 归档 owner/thread 路由；旧批次未发起任何外部请求。
- 隔离完整 round 测试2 passed；相关回归104 passed；M1后端集合180 passed；前端203 passed；构建及 `git diff --check` 通过。
- `M1-LIVE-20260908-002` 的调用数、费用和时限上限不变，但使用新的代码与验收驱动指纹；取得再次明确批准前不得执行。

## 2026-09-08 20:31 真实批次 002 结果

- 用户批准后执行；第1轮完整通过，第2轮因条目 embedding timeout 后语义记忆未入选而停止，第3轮未执行。
- 共发起8个模型 attempts、4个 embedding 请求，输入 UTF-8 上界774 token；保守费用上界为 US$0.201856 与 ¥0.00010836。
- 根因是验收驱动把 worker 的“已处理”返回值误当作 embedding 成功，并沿用不适合真实验收的0.8秒默认超时；生产召回阈值未改。
- 修复后隔离 harness 4 passed；新批次 `M1-LIVE-20260908-003` 待批准，连续3轮从0重新计数。

## 2026-09-08 20:53 真实批次 003 结果

- 用户批准后执行；第1轮通过，第2轮首次条目 embedding timeout 后终态检查立即停止，第3轮未执行。
- 共发起6个模型 attempts、3个 embedding 请求，输入 UTF-8 上界423 token；本批次保守费用上界 US$0.151392 与 ¥0.00005922，M1累计为 US$0.353248 与 ¥0.00016758。
- 60秒 timeout 当时只应用于预检对象，round 实际客户端仍使用0.8秒；现已在真实客户端构造处固定60秒，并由 fake client 断言配置生效。
- 修复后 embedding fault 与隔离 harness 共9 passed；新批次 `M1-LIVE-20260908-004` 待批准，连续3轮从0重新计数。

## 2026-09-08 18:15 交接状态

- 最新后端扩展回归已通过（本轮新增受限路径/归档版本/词项边界/预算覆盖及 PostgreSQL 并发重试）；全量前端串行回归203 passed；npm run build通过；git diff --check通过。
- UI 验证全部 API 均 mock，未调用模型。375px 下 archive-status 底边438px，conversation-empty-mark 顶边519px；1440×1000、375×812、812×375 均无重叠、无横向溢出，键盘 Enter 重试与 busy 禁用已通过。截图与脚本见 `docs/acceptance/m1-ui-recheck/`、`frontend/scripts/m1-archive-check.mjs`。
- 最新截图：m1-archive-desktop.png、m1-archive-mobile.png、m1-archive-landscape.png。
- 尚未重启8000加载M1后端，避免后台归档/观察器触发未批准付费调用；frontend/dist已因build更新，当前8000可能呈现新UI但仍是旧后端，不能当部署完成。
- 8123仅为静态UI检查服务（python http.server，PID9416），不连接模型或应用数据库。
- 下一步详见docs/acceptance/m1-astra-handoff-2026-09-08.md。
