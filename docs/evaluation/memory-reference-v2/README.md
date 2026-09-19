# 长期记忆指代补全评测 v2

120 道构造题，60 dev / 60 holdout；40 个新服务实体按场景组分开，312 条索引记忆。数据不是生产用户日志。

已完成真实验收：新 holdout 的 50 道有答案题，off 14/50、rules 39/50、hybrid 48/50；10 道真歧义题 rules/hybrid 均正确澄清。见 [验收报告](../../acceptance/memory-reference-2026-09-19.md)、[完整题目](questions.md)。代码默认按准入结果启用 hybrid，现有应用未重启。

每类 20 题：单实体、前者/后者有序关系、话题切换、问题自带实体、无依据/多实体歧义、假设和提示词注入干扰。每个场景组包含两个服务，实体不跨 dev/holdout。问题模板复用，不能把 120 题当成 120 个完全独立业务场景。

三个方案：关闭补全 off；仅规则 rules；规则优先、按需 LLM 的 hybrid。固定 RRF k=60、向量/关键词各 50、记忆注入 450 字节。只改变检索查询，回答模型仍收到原问题和近期对话。

LLM 指代调用使用生产 ModelGateway、中文提示词 reference-v3、温度 0、关闭 thinking、JSON 输出、输出上限 256 tokens、2 秒截止时间、单次尝试；失败时澄清，不猜测。回答使用独立评测提示词和真实 DeepSeek。embedding 使用真实 Qwen/Qwen3-Embedding-4B、1024 维、实验超时 5 秒。

有答案题 100（dev/holdout 各 50），必须报告严格回答正确数和不必要澄清数；真正歧义题 20（各 10）单列正确澄清数。不能把“澄清”混入答案正确率。额外记录错误实体补全、来源权限违规、LLM 触发率、延迟和调用 usage。

## 历史实验与修订

v1 首次运行在第 51 道 dev 题后中止：提示词把两个动作枚举写在同一个示例字符串，真实模型照抄后被校验拒绝；没有运行任何 holdout。修正示例后，v1 第二次运行暴露供应商 JSON 模式要求提示词包含 JSON 字样的协议错误，16 次指代调用全部失败；不能称作有效的混合策略效果验收。

本版本显式写明“只输出合法 JSON 对象”，并更换全部新增服务实体和维护时间，使用未评测过的新 holdout。v1 数据、原始调用和失败报告保留；不覆盖旧分数，不把已经消费的 v1 留出集继续当新留出集。

数据：`memories.json`、`cases.json`、`manifest.json`。脚本：`backend/scripts/memory_reference_eval.py --execute --data ../docs/evaluation/memory-reference-v2 --out <新目录>`。需要独立的 `better_memory_eval_*` 数据库（从已完成的 v3 候选评测库克隆），不得指向生产库。

调用上限为每轮最多 360 次回答、40 次指代判断、160 次新增 embedding；实际因澄清与相同 query 缓存而减少。旧失败批次和诊断请求另外列出，不能隐藏在成功批次以外。
