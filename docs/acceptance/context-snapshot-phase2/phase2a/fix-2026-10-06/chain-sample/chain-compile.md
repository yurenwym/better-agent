# 脱敏链路样本（修复轮）：编译 / 修复两个 span，retry 共用同一快照

由 `dump_compile_chain.py` 驱动**真实 `GoalProgramCompiler` + 真实 `ModelGateway`** 产生；
传输层被脚本化（`httpx.MockTransport`）：attempt 1 返回 504，attempt 2 返回非法 JSON，attempt 3 返回合法 program。
调用带真实的根执行上下文（`create_root_context`），模拟工具内部的编译调用。
只打印标识符、摘要、长度与状态，**不打印冻结的提示词正文**。

- 网络 attempt 数：**3**
- 逻辑调用数（invocation）：**2**
- attempt 记录数：**3**
- 编译结果：objective_title = `五周训练计划`

## invocation `model_invocation_e06690c1a6be4653a733cfe…`

| 字段 | 值 |
|---|---|
| role / purpose | `planner` / `compile_goal_program` |
| status | `SUCCEEDED` |
| idempotency_key | `model_invocation_e06690c1a6be465…` |
| request_digest | `24150eb4a68a45a259c7bad9…` |
| **span_id** | `de3af24bab0c46dba91f4410…` |
| **parent_span_id** | `eea7078b19ba429fba0fe629…` |
| trace_id | `a98159eb3bfe415eae7ef0f4…` |
| execution_context_digest | `5b64403202f96559007fc1a6…` |
| **context_snapshot_id** | `model_input_snapshot_305740081577460a95072b9086efa6ef` |
| **context_snapshot_digest** | `4a57b39b3c35d04fe0420062…` |
| 规范化 JSON 字节数 | 2067 |
| provenance.status | `partial` |
| 重新计算 SHA-256 == content_digest | ✅ |
| invocation.context_snapshot_digest == snapshot.content_digest | ✅ |

### attempts

| ordinal | reason | status | attempt id | request_digest |
|---|---|---|---|---|
| 1 | `primary` | `FAILED` | `model_invocation_e06690c…` | `24150eb4a68a45a259c7bad9…` |
| 2 | `retry` | `SUCCEEDED` | `model_invocation_e06690c…` | `24150eb4a68a45a259c7bad9…` |

## invocation `model_invocation_fbcdece2a24645d9bc6e8d0…`

| 字段 | 值 |
|---|---|
| role / purpose | `planner` / `compile_goal_program` |
| status | `SUCCEEDED` |
| idempotency_key | `model_invocation_fbcdece2a24645d…` |
| request_digest | `d6733f3a0c0af84900cf3254…` |
| **span_id** | `63ebe7f053414a80a2a3a4ae…` |
| **parent_span_id** | `eea7078b19ba429fba0fe629…` |
| trace_id | `a98159eb3bfe415eae7ef0f4…` |
| execution_context_digest | `1088dc9b758b24872ff45153…` |
| **context_snapshot_id** | `model_input_snapshot_a14fe9336bde4350861d0b799efea38e` |
| **context_snapshot_digest** | `ca4fb1250345101c84e3fade…` |
| 规范化 JSON 字节数 | 2353 |
| provenance.status | `partial` |
| 重新计算 SHA-256 == content_digest | ✅ |
| invocation.context_snapshot_digest == snapshot.content_digest | ✅ |

### attempts

| ordinal | reason | status | attempt id | request_digest |
|---|---|---|---|---|
| 1 | `primary` | `SUCCEEDED` | `model_invocation_fbcdece…` | `d6733f3a0c0af84900cf3254…` |

## 结论

- 两个逻辑调用（编译 / 修复）span 互不相同：✅
- 两个 span 的 parent_span_id 都是根上下文 span（`eea7078b19ba429fba0fe629…`）：✅
- 两个 span 共用同一 trace_id：✅
- 两个逻辑调用各自绑定不同快照：✅
- 首个逻辑调用的 2 个 attempt 共用同一 invocation 与同一快照：✅
- 快照摘要自校验与绑定一致：✅
- 本文件不含提示词正文，仅含摘要与标识符。
