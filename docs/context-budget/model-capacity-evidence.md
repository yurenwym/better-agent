# DeepSeek 模型容量规格核验（T01）

核验时间（UTC）：2026-09-19T11:07:32Z  
核验方式：直接访问 DeepSeek 官方 API 文档页面（非搜索缓存），保存渲染快照并计算 SHA-256。  
结论状态：**部分核实**。输出上限整数已核实；上下文长度精确整数仍为 unresolved。

## 1. 已核实事实

| 项目 | 值 | 官方来源 | 状态 |
| --- | --- | --- | --- |
| 模型 ID | `deepseek-flash` | pricing 页 Model Details | 已核实 |
| 模型版本 | `DeepSeek-V4.1-Flash` | pricing 页 MODEL VERSION | 已核实 |
| 旧别名 | `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` | pricing 页脚注 (1)：已下线，请求由 DeepSeek-V4.1-Flash 服务 | 已核实（别名映射） |
| OpenAI 格式端点 | `https://api.deepseek.com` | pricing 页 BASE URL | 已核实 |
| Anthropic 格式端点 | `https://api.deepseek.com/anthropic` | pricing 页 BASE URL | 已核实 |
| 上下文长度 | `1M`（无精确整数） | pricing 页 CONTEXT LENGTH | **unresolved** |
| 最大输出 | `384K (393216)` | create-chat-completion 页 `max_tokens` 说明 | 已核实 |
| 输入输出关系 | “The total length of input tokens and generated tokens is limited by the model's context length.” | create-chat-completion 页 | 已核实（上下文为输入+输出合计） |
| 默认输出预算（供应商默认，不是本应用预算） | 非思考 8K；思考 64K；`reasoning_effort=max` 128K | create-chat-completion 页 | 已核实 |
| 官方 tokenizer | `deepseek_v4_tokenizer.zip`（离线 demo） | token_usage 页 | 存在，但未完成与本应用计数契约的验证 |
| `/models` 列表 | `deepseek-flash`、`deepseek-v4-pro` | list-models 页 | 已核实（仅存在性，不作为容量证据） |

### 1.1 为什么上下文精确整数是 unresolved

pricing（英文/中文）与 API 参考页均只写 `1M`，没有给出 `1000000` 或 `1048576`。虽然 `384K` 被明确为 `393216`（即 K=1024），但按任务书要求，不得据此推断 `1M` 的精确整数，也不得因为页面写了 `1M` 就自行选择任一整数写入生产配置。因此：

- `model_max_output_limit = 393216` 可进入已验证目录；
- `model_context_limit = null`，`context_verified = false`，DeepSeek 的“已验证 auto 工作窗口”在本轮不得发布；
- 需要大窗口的用户只能使用显式手动窗口，或在官方给出精确整数后更新目录条目。

### 1.2 计数方式

官方 token_usage 页只给出近似换算（1 英文字符 ≈ 0.3 token，1 中文字符 ≈ 0.6 token），并明确“以 API 返回的 usage 为准”。官方 tokenizer zip 未在本次核验中完成与本应用预算链的一致性验证，因此：

- 运行时继续使用 `utf8-upper-bound-v1` 保守估算；
- 不注册“已验证精确”计数器；
- `counter_evidence_version` 对 DeepSeek 不提供精确计数声明。

## 2. 证据快照

快照目录：`docs/context-budget/evidence/`

| 文件 | URL | SHA-256 |
| --- | --- | --- |
| `pricing.en.html` | https://api-docs.deepseek.com/quick_start/pricing | `2FECEE48BF6AD791BCE38D1D5504D8AD5C8B0FD4DA93E6DC198AE88FF1A4506A` |
| `token-usage.en.html` | https://api-docs.deepseek.com/quick_start/token_usage | `4922DA4DD1A67DEEBB3FBD4A0A5F0D4E109B04874D4167CCC61766EDE2FCC4A7` |
| `create-chat-completion.en.html` | https://api-docs.deepseek.com/api/create-chat-completion | `4C2384D7AD4E5F1929EDB922900B2A96546A7E699E8B067D6E82DCB9A330989D` |
| `list-models.en.html` | https://api-docs.deepseek.com/api/list-models | `0E3FB976EF13048F0C41048536BEDEF5D3715EBABE539DD18DDDBE967DAA6837` |
| `error-codes.en.html` | https://api-docs.deepseek.com/quick_start/error_codes | 见文件；无精确上下文整数 |

历史快照（与本次核验分开）：

- `backend/artifacts/learning-v2-official-pricing.html`，SHA-256 `1B0717A8E5A91E1E1802ADFEBF84A557569BEB67EC99E1C787EE4BDB65AE514B`（任务书引用值一致）。

## 3. 对开发的影响

1. 容量目录可以登记 DeepSeek 的输出上限和别名映射，但 `context_limit` 必须留空并标 `unresolved`。
2. auto 工作窗口在目录未核实上下文时不得启用“已验证”路径；解析结果必须返回 `verified=false` 并给出原因。
3. 未知端点/模型同样返回未验证状态，不得静默继承官方端点的 1M。
4. 计数适配器本轮不注册，保留 UTF-8 保守估算并在预算对象中如实标注 `estimate`。

## 4. 待联网复核项

- 上下文长度的精确整数及是否包含输出/思考 token（官方未给出）。
- `deepseek_v4_tokenizer.zip` 与本应用请求计数的等价性（需要真实 usage 对照，属于 T12 受控实测范围）。
- 端点容量是否随账户/并发等级变化（官方未说明）。
