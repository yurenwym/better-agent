# LLM + Harness：统一结果与事件开发任务与验收

日期：2026-10-09。状态：待开发。

## 开发入口

按 [逐步任务 README](harness-contracts/README.md) 的 T00—T09 顺序实施。

先完成 **Error / Outcome**（T00—T04），再完成 **Event Envelope / Trace**（T05—T09）。两者服务于当前对话、工具、审批、研究移交与目标执行主链路，并作为后续 PolicyEngine / TaskRuntime 的基础契约。

## 设计规范

- [Error / Outcome](harness-contracts/C01-Error-Outcome契约.md)：结果层级、错误分类、副作用、恢复与代码依赖约束。
- [Event Envelope / Trace](harness-contracts/C02-Event-Trace契约.md)：身份复用、因果关系、事务、并发、幂等与旧数据兼容。

## 完成条件

1. 主链路生产者和消费者实际接入统一契约。
2. 等待、移交、执行成功、任务完成和失败不会混淆。
3. 写入结果不确定时保留对账义务，不盲目重试。
4. 审批、重试、研究移交及服务重启后身份和因果可追踪。
5. 历史事件、SSE、前端业务行为和转录 hash 兼容。
6. PostgreSQL 事务/并发/幂等测试通过，完整测试零失败。

本轮不开发新的研究评测/评分功能，不统一所有业务状态机，不引入第二套事件系统。代码质量与边界限制属于强制验收条件，不能以“功能测试通过”豁免。

