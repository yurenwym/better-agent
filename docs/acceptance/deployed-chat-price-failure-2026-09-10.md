# 当前运行环境普通聊天失败诊断

截图请求发生于2026-09-10 17:54:55，turn `turn_2ee097b1457141de9bd2064f83c6cb2f`。

数据库事件顺序：turn accepted → memory retrieval → model route selected → cost.budget_blocked (`model price is unavailable`) → invocation budget_blocked →通用失败消息。实际模型尝试数0，总耗时约201ms。

根因：当前主模型版本 `model_profile_version_702f71bfaa214cf5aad20adaebb6fb36` 没有价格快照。之前隔离验收显式注册了价格，真实启动环境没有；健康检查只证明服务存活，不证明预算受控的模型请求可执行。此前以隔离测试和专家入口推断普通聊天可用的结论过强。

修复：

- 为当前官方 DeepSeek Flash 模型版本登记同日已核验的价格，未提高原预算或关闭费用检查。
- GatewayError 的 budget 错误提供可读原因；缺价格时明确告知重复发送无效。
- 同步修复日志发现的删除线程后SSE抛KeyError，以及 /skills、/models、/usage、/evaluations 直接访问404。
- 在实际8000服务通过普通 /turns HTTP入口提交原请求，收到澄清问题；按真实turn版本提交答案，返回七天计划。结果和事件保存于 `deployed-normal-chat-completed-2026-09-10.json`。
- 内容复核发现模型将已选的30分钟上限放宽为35分钟，因此该次仅通信/澄清链路通过，内容约束失败。普通聊天提示词补充硬约束与全篇一致性规则，重新启动后在相同线程验证明确约束请求；输出留存 `deployed-normal-chat-fixed-2026-09-10.json`。

回归：缺价格提示、删除线程SSE、gateway/会话worker共72项通过；普通模型与新增边界检查63项通过。

局限：主模型价格修复不代表所有未来模型版本均已配置价格，备用供应商价格仍需对应配置；缺价格继续安全阻止调用。状态COMPLETED不是计划语义正确或医疗适用性的证明。测试产生的新线程标有ACCEPT，不覆盖用户原失败记录。

同线程修改复测失败：`memory_archive_jobs` 进入 DEAD_LETTER，原因 `invalid_summary: decisions must cite a user message`，随后 `context.incomplete`，依赖前文的请求被阻断。该失败保留，不能把普通聊天“首次澄清→答案”通过宣称为多轮对话通过。已增加历史整理失败的明确提示；归档失败导致多轮不可用的根因仍需修复。
