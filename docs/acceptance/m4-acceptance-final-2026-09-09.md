# M4 深度研究正式验收（2026-09-09）

状态：`PASSED`。`M4-LIVE-20260909-003` 在冻结源码、案例、官方来源范围和预算下连续三轮通过，满足架构计划第 7 节及第 9 节的 M4 阶段出口。本轮按约定停止，不进入 M5；该结论不覆盖或改写 `001`、`002` 的失败历史。

## 实现与关键决定

- 研究终态区分 `COMPLETED`、`PARTIAL`、`FAILED`、`CANCELLED`；部分交付会明确列出未解决要求，不能冒充完整成功。
- 持久化“要求→证据→结论→引用”矩阵，包括证据 ID、来源 ID、引用 ID、正文字符位置、摘录哈希、来源内容哈希和获取时间。
- 认证、付费、配置和根预算等永久模型错误首错停止，不被 fallback 吞掉；截断的结构化输出即使可解析也不能作为完整结果。
- 研究任务固定 runtime bundle、模型 profile 和价格快照；专用 worker 精确领取指定 job，不消费当前库的其他排队任务。
- 官方 URL 直读仍经过 DNS/SSRF、重定向和最终域名校验；本批不调用付费搜索服务。
- 本批模型无 fallback、provider 网络重试为 0、结构化输出重试为 0，并显式关闭推理模式。

## 冻结版本

- 批次：`M4-LIVE-20260909-003`。
- 源码摘要：`a21c79f4d708741579c46987f848e24332af3e8a225e346c4149c83934b2a153`。
- 案例摘要：`e7b337f2487b9bf07f05dc5b1b1a028390aec4a3c7084e772f101016e1a107cc`。
- 模型：DeepSeek `deepseek-v4-flash`，32,768 token 上下文，8,192 token 输出上限。
- 执行前相关回归：`93 passed`；Python 编译和 `git diff --check` 通过；前端此前为 `203 passed` 且生产构建成功。

## 连续三轮证据

| 轮次 | 冻结主题 | 官方来源 | 模型调用 | 证据 | 矩阵 | 语义门禁 | 终态 | 费用 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- | ---: |
| 1 | Python asyncio | 2 | 8 | 14 | 2/2 supported | 2/2 | COMPLETED | 201,856 microusd |
| 2 | PostgreSQL EXPLAIN | 2 | 8 | 14 | 2/2 supported | 2/2 | COMPLETED | 201,856 microusd |
| 3 | Git revert/reset | 2 | 8 | 23 | 2/2 supported | 2/2 | COMPLETED | 201,856 microusd |

三个主题都只使用各自冻结的官方域名和两个指定 URL。每条矩阵记录均有来源版本、原文位置和一致的 evidence/source/citation 关联；不存在 unsupported 要求。

## 费用、清理与恢复

- 成功批次共 24 次模型调用、6 次官方来源请求，全部有价格记录。
- 成功批次总记账 `605,568 microusd`（US$0.605568），低于 `756,960 microusd`（US$0.756960）硬上限。
- 成功批次耗时 72.77694 秒；未发生 fallback 或网络重试。
- 在唯一当前 PostgreSQL `better_agent` 上执行，结束后按批次 owner、manifest 和关联行精确删除 504 行。
- 二次核验本批 threads、research jobs、model invocations、cost budgets、root budgets 和 runtime bundles 均为 0。
- stable 始终保持 `bundle_8040b4cc18e6bf2282d15992`。
- 包含失败批次 `001`、`002` 后，M4 三批累计实际记账为 `1,059,744 microusd`（US$1.059744）。失败费用未删除或计为 0；费用证据保存在各批结果文件中。

## 失败历史与剩余边界

- `001`：引用别名恢复不一致，且验收 runtime 构造意外切换 stable；8 次调用，US$0.201856。修复后精确清理，失败记录保留。
- `002`：三次结构化调用以 `finish_reason=length` 截断，但可解析 JSON 被错误接受，最终形成 PARTIAL；10 次调用，US$0.252320。修复后截断输出一律拒绝并关闭研究推理模式，失败记录保留。
- 三轮是核心流程稳定性的最低证据，不代表统计显著性、长期来源可用性或可靠性 P95。
- M4 证明深度研究核心链路、部分交付和证据门禁满足冻结案例，不授权 M5 自进化、真实评测、候选发布或 Canary。

## 阶段出口

M4 已正式完成。依据用户要求，本轮到此停止；M5 仍需重新研究设计并取得单独授权。

主要证据：`m4-live-results-2026-09-09-003.json`、`m4-live-preflight-2026-09-09-003.json`、`m4-live-batch-budget-2026-09-08.md`，以及 `001`、`002` 的失败结果和清理记录。
