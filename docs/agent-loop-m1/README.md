# Loop-M1 逐步任务

总览与架构：[LLM-Harness-统一AgentLoop开发任务与验收-2026-10-09.md](../LLM-Harness-统一AgentLoop开发任务与验收-2026-10-09.md)

计划文档已锁定**方案 B**：先流式输出正文，再调用 `publish_plan_document` 绑定保存。

## 怎么做

1. 一次只做一篇任务文档。
2. 任务文档里的「禁止」优先于「要做」。
3. 本步验收用例通过、且未扩大范围，才开下一步。
4. 需要付费的真实模型运行（T01、T13）必须按既有评估启动方式显式授权，不得自动发起。

## 顺序

```text
T00 评测案例与 mock 框架 ─────────────┐
T02 AgentLoop 类型与接口 ──► T03 循环与守卫 ──► T04 对话循环替换
T05 抽出移交收尾 ─────────────────────────────────────┤
                                                       ▼
                              T06 start_research   ─┐
                              T07 delegate_experts ─┼─► T09 对话 Profile ──► T10 publish_plan_document
                              T08 remember         ─┘
T03 ──► T11 终止型工具 ──► T12 目标步骤接入 ─────────────┤
                                                       ▼
T01 旧路径基线 ────────────────────────────────► T13 对比 ──► T14 切换并删除
```

可并行：T00 ∥ T02；T05 ∥ T02–T04；T06 ∥ T07 ∥ T08（均依赖 T04/T05）；T11 ∥ T06–T10。

## 任务列表

| 编号 | 文档 | 完成标准（一句话） |
|---|---|---|
| T00 | [评测案例与 mock 框架](T00-评测案例与mock框架.md) | 案例文件与 mock 评测命令可跑通，HOLDOUT 已冻结 |
| T01 | [受控旧路径基线](T01-受控旧路径基线.md) | 有授权的真实模型旧路径报告 |
| T02 | [AgentLoop 类型与接口](T02-AgentLoop类型与接口.md) | 类型与协议可导入，无循环实现 |
| T03 | [循环与守卫](T03-AgentLoop循环与守卫.md) | AL-T01–T07 用 fake 通过 |
| T04 | [对话循环替换](T04-对话循环替换-行为不变.md) | 既有对话测试不改断言全部通过 |
| T05 | [抽出移交收尾](T05-抽出移交收尾共用函数.md) | `run_once` 研究/专家分支改为调用共用函数 |
| T06 | [start_research](T06-注册start_research.md) | AL-T09、T10、T13、T14（研究） |
| T07 | [delegate_experts](T07-注册delegate_experts.md) | AL-T11、T12、T13、T14（专家） |
| T08 | [remember](T08-注册remember.md) | AL-T15 |
| T09 | [对话 Profile 与开关](T09-对话Profile与loop开关.md) | `loop` 模式下研究/专家/记住不再走正则与分类 |
| T10 | [publish_plan_document](T10-publish_plan_document.md) | AL-T23–T26；控制头 v2 写入路径可关 |
| T11 | [目标终止型工具](T11-目标步骤终止型工具.md) | 三个终止型工具注册并通过单测 |
| T12 | [目标步骤接入](T12-目标步骤接入AgentLoop.md) | AL-T19–T21 |
| T13 | [新旧路径对比](T13-新旧路径对比.md) | AL-T22；第 7 节门禁有结论 |
| T14 | [切换并删除旧路径](T14-默认切换并删除旧路径.md) | 默认 `loop`，满足条件的旧逻辑已删 |

证据目录：`docs/acceptance/agent-loop-m1/`。
