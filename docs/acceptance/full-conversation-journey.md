# Better Agent 全流程验收用例

## 用例信息

- 用例编号：ACCEPT-FULL-JOURNEY-001
- 验收环境：`http://127.0.0.1:8100`
- 业务场景：用户制定并执行一份 7 天 PostgreSQL + pgvector 学习计划
- 执行策略：严格按关卡顺序执行；首个非预期结果出现后立即停止，不修复、不跳过，等待用户确认
- 数据标记：所有验收数据的标题或请求均使用 `[ACCEPT-FULL-JOURNEY-001]`

## 前置条件

1. Better Agent、PostgreSQL 和后台 worker 均处于健康状态。
2. 对话、研究、专家协作、计划编译、复盘和自进化所需模型路由均已配置。
3. PostgreSQL 是运行时唯一权威数据库。
4. 每个写请求携带 CSRF token；要求幂等键的接口携带唯一 `Idempotency-Key`。

## 关卡与通过标准

### 01 对话

- 创建一个独立会话。
- 发送普通问题：说明 PostgreSQL 中普通索引与 pgvector 向量索引的区别。
- 通过：turn 最终为 `COMPLETED`；存在非空 assistant 消息；四项耗时指标均有非负值。

### 02 询问

- 请求 Agent 帮助制定学习安排，但故意省略当前水平、每日时间和目标。
- 通过：turn 进入 `WAITING_INPUT`；产生 1 至 4 个结构化问题；回答后 continuation turn 为 `COMPLETED`，且 assistant 给出有效答复。

### 03 深度研究

- 在同一会话发起 PostgreSQL + pgvector 选型与 HNSW 参数研究，来源范围为 Web。
- 通过：research job 为 `COMPLETED`；报告非空；至少有一个来源和一条证据；报告作为 assistant 消息回写会话。

### 04 多 Agent 协作

- 发起专家协作，让不同专家审查学习计划的数据库、向量检索和测试策略。
- 通过：agent run 为 `SUCCEEDED`；任务均终态；至少存在一个结果 artifact；汇总结果作为 assistant 消息回写会话。

### 05 创建计划

- 明确要求创建一份 7 天 PostgreSQL + pgvector 学习计划文档，提供目标、每日时长和验收产物。
- 通过：turn 为 `COMPLETED`；会话关联 plan document；当前版本为 1；Markdown 以一级标题开头且包含可执行步骤。

### 06 保存计划

- 通过计划保存接口追加验收说明并生成新版本。
- 通过：版本号递增；内容哈希变化；current version、数据库版本记录和投影文件内容一致，文件状态为 `READY`。

### 07 计划进度

- 将计划编译为从当天开始的执行项目并激活；完成当天动作并提交难度与实际耗时反馈。
- 通过：program 为 `ACTIVE`；动作状态和版本正确变化；Today API 返回一致进度。

### 08 复盘

- 关闭当天所有动作，等待后台复盘 worker。
- 通过：daily review 为 `COMPLETED`；summary 非空；风险信号与实际反馈一致；若生成调整建议，其状态必须为 `PENDING` 且可读取。

### 09 自进化

- 先调用观察器，确认上述终态事件转为 evolution experience。
- 为验证候选链路，使用三个隔离会话制造相同且预期的上下文超限失败；这些是测试夹具，不属于验收故障。
- 再次观察并等待自动候选生成。
- 通过：三个独立失败经验进入 `DISCOVERY`；生成无新增权限的 prompt candidate；候选完成真实评测和人工审批；启动 Canary 后能读取样本门禁；最后主动回滚并确认 stable bundle 恢复。
- 不通过：观察遗漏终态事件、候选未生成、评测失败、审批绑定失败、Canary 状态错误或回滚后 stable bundle 不一致。

## 失败处理

首次失败时记录：关卡、请求标识、HTTP 状态、业务状态、错误码、相关资源 ID、容器日志时间点。随后停止执行，保留现场，等待用户同意修复后从失败关卡继续。

## 验收结果

执行过程中填写实际资源 ID、耗时和证据；只有 9 个关卡全部通过时，本用例才判定为通过。

### 2026-09-07 执行记录

- 会话：`thread_1ba698dc56534580a8460b55640242eb`
- 关卡 01 对话：通过。turn `turn_71a99127c9e14ee6af81e2e1ebfbc353` 为 `COMPLETED`；排队/上下文/首 token/流式耗时分别为 `33/978/1863/13 ms`。
- 关卡 02 询问：失败并停止。turn `turn_88132e797be341ddb0c41ff0070b5ea5` 为 `FAILED`；`turn_jobs.last_error_json` 为 `ask must contain between one and four questions`。
- 失败边界：三个模型调用均成功，主对话模型调用返回的 `ask_user` 参数中没有 1–4 个有效问题，应用层结构校验失败。未执行关卡 03–09。
- 关卡 02 修复后复验：通过。乱码测试 turn `turn_c1b2113d1ece437abc80cfb66fa93a7f` 已取消；UTF-8 turn `turn_19bf9ac379654ba6925800ca918f4585` 进入 `AWAITING_INPUT`，生成 4 个相关问题；回答后 continuation turn `turn_48b12a3ec5b84c06ae0b4fef43c2c99b` 为 `COMPLETED`，assistant 返回定制的 7 天学习安排。
