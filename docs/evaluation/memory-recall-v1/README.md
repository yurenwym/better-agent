# 长期记忆召回评测集 v1

日期：2026-09-19。22 条记忆，24 道问题（16 dev、8 holdout）。这是可执行、带人工标准答案的源码事实与构造场景集，不是生产用户日志。

数据文件： [记忆语料](./memories.json)、[问题与标准答案](./cases.json)、[冻结配置](./manifest.json)。

## 记忆语料

| ID | 来源 | 作用域／状态 | 正文 |
| --- | --- | --- | --- |
| r01 | 源码：`backend/app/db.py` | eval-user / better-eval / ACTIVE | Better 当前 PostgreSQL 检索需要 vector 和 pg_trgm 扩展。 |
| r02 | 源码：`backend/app/embedding.py` | eval-user / better-eval / ACTIVE | Better 默认 embedding 模型是 Qwen/Qwen3-Embedding-4B，维度为 1024。 |
| r03 | 源码：`backend/app/embedding.py` | eval-user / better-eval / ACTIVE | Better embedding 请求默认超时时间为 0.8 秒。 |
| r04 | 源码：`backend/app/memory_v2.py` | eval-user / better-eval / ACTIVE | Better 长期记忆默认语义召回预算是 1500，实际按 UTF-8 字节保守计数。 |
| r05 | 源码：`backend/app/memory_v2.py` | eval-user / better-eval / ACTIVE | Better 记忆向量候选最多 50 条，相似度最低阈值为 0.35。 |
| r06 | 源码：`backend/app/token_budget.py` | eval-user / better-eval / ACTIVE | Better 默认保留最近至少 5 个完整对话轮次。 |
| r07 | 源码：`backend/app/memory_v2.py` | eval-user / better-eval / ACTIVE | Better 同一次模型调用的记忆 pin 默认有效期为 900 秒。 |
| r08 | 源码：`backend/app/config.py` | eval-user / better-eval / ACTIVE | Better 默认费用模式是 observe，只记账，不启用金额限制。 |
| r09 | 源码：`backend/app/conversation.py` | eval-user / better-eval / ACTIVE | Better 普通对话的 episode_token_budget 设置为 0，会话归档摘要通过确定性续接注入。 |
| r10 | 源码：`backend/app/model_capacity.py` | eval-user / better-eval / ACTIVE | Better 精确标注的 DeepSeek 最大输出容量为 393216，上下文精确整数尚未核实。 |
| s01 | 明确构造 | eval-user / better-eval / ACTIVE | 苍鹭项目唯一部署区域是 cn-north-7。 |
| s02 | 明确构造 | eval-user / better-eval / ACTIVE | 苍鹭项目回滚保留期为 17 天。 |
| s03 | 明确构造 | eval-user / better-eval / ACTIVE | 苍鹭项目只允许在每周二 03:20 执行数据库维护。 |
| s04 | 明确构造 | eval-user / better-eval / ACTIVE | 苍鹭项目的错误代号 HERON_E417 表示索引租约过期。 |
| s05 | 明确构造 | eval-user / better-eval / ACTIVE | 苍鹭项目当前检索延迟目标是 230 毫秒。 |
| s06 | 明确构造 | eval-user / user / ACTIVE | 演示用户的技术文档语言偏好为简体中文。 |
| d01 | 明确构造 | eval-user / other-project / ACTIVE | 黑鹭项目唯一部署区域是 eu-west-9。 |
| d02 | 明确构造 | eval-user / better-eval / EXPIRED | 苍鹭项目旧版检索延迟目标是 900 毫秒。 |
| d03 | 明确构造 | eval-user / better-eval / ARCHIVED | 苍鹭项目归档的回滚保留期为 3 天。 |
| d04 | 明确构造 | other-user / better-eval / ACTIVE | 另一个用户的苍鹭项目唯一部署区域是 us-east-8。 |
| d05 | 明确构造 | eval-user / better-eval / ACTIVE | 向量绘图软件可以生成 50 种插画，不涉及检索。 |
| d06 | 明确构造 | eval-user / better-eval / ACTIVE | 租约车辆的归还时间为周二下午。 |

## 完整题目与标签

| ID | 集合／类别 | 近期上下文 | 当前问题 | 必须召回 | 答案必须包含 | 禁止条目 |
| --- | --- | --- | --- | --- | --- | --- |
| q01 | dev / exact | 无 | Better 的默认 embedding 模型名称是什么？ | r02 | Qwen/Qwen3-Embedding-4B | 无特定条目 |
| q02 | dev / exact | 无 | Better embedding 默认多少维？ | r02 | 1024 | 无特定条目 |
| q03 | dev / synonym | 无 | Better 把文本转成向量的服务，请求等多久就超时？ | r03 | 0.8 | 无特定条目 |
| q04 | dev / exact | 无 | Better 默认费用模式叫什么？ | r08 | observe | 无特定条目 |
| q05 | dev / context | 我们现在讨论 Better 的 embedding 请求配置。 / 接下来确认请求的超时设置。 | 那个超时值是多少秒？ | r03 | 0.8 | 无特定条目 |
| q06 | dev / context | 我要查 Better 同一次模型调用的记忆 pin 配置。 | 这个缓存默认保留多久？ | r07 | 900 | 无特定条目 |
| q07 | dev / exact | 无 | 苍鹭项目唯一部署区域是什么？ | s01 | cn-north-7 | d01, d04 |
| q08 | dev / context | 正在检查苍鹭项目的回滚保留期。 | 它允许回退多长时间？ | s02 | 17 | d03 |
| q09 | dev / entity | 无 | HERON_E417 是什么错误？ | s04 | 索引租约过期 | 无特定条目 |
| q10 | dev / stale | 无 | 苍鹭项目现在的检索延迟目标是多少毫秒？ | s05 | 230 | d02 |
| q11 | dev / negative | 无 | 苍鹭项目数据库备份桶叫什么名字？ | 空 | UNKNOWN | 无特定条目 |
| q12 | dev / negative | 无 | 这位演示用户的生日是哪天？ | 空 | UNKNOWN | 无特定条目 |
| q13 | dev / multi | 无 | Better 默认向量候选数和最低相似度分别是多少？ | r05 | 50, 0.35 | 无特定条目 |
| q14 | dev / multi | 无 | Better 的向量和模糊文本检索需要哪两个 PostgreSQL 扩展？ | r01 | vector, pg_trgm | 无特定条目 |
| q15 | dev / budget | 无 | Better 长期记忆召回的默认预算是多少，按什么单位计数？ | r04 | 1500, UTF-8 | 无特定条目 |
| q16 | dev / precedence | 无 | 用户原来的文档语言偏好是什么？注意我这次要求用英语回答。 | s06 | Chinese | 无特定条目 |
| q17 | holdout / context | 我们只讨论苍鹭项目的唯一部署区域。 | 那个区域代码是什么？ | s01 | cn-north-7 | d01, d04 |
| q18 | holdout / synonym | 无 | 苍鹭服务最近规定要在多少毫秒内完成检索？ | s05 | 230 | d02 |
| q19 | holdout / multi | 无 | 苍鹭项目允许哪天几点维护数据库，回滚保留多少天？ | s02, s03 | 17, 03:20, 周二 | d03 |
| q20 | holdout / context | 现在解释 Better 上下文压缩后保护近期对话的默认规则。 | 最终保存几个完整轮次？ | r06 | 5 | 无特定条目 |
| q21 | holdout / negative | 无 | Better 官方承诺的在线可用性 SLA 是多少？ | 空 | UNKNOWN | 无特定条目 |
| q22 | holdout / exact | 无 | Better 普通对话的 episode_token_budget 是多少？ | r09 | 0 | 无特定条目 |
| q23 | holdout / context | 我要核对 Better 登记的 DeepSeek 官方最大输出容量，注意不是上下文窗口。 | 这个数是多少？ | r10 | 393216 | 无特定条目 |
| q24 | holdout / scope | 无 | 黑鹭项目的唯一部署区域是什么？ | 空 | UNKNOWN | d01, d04 |

## 标注约定

- Gold 表示回答这道具体问题所必需的最小证据，不等于所有同主题记忆均相关。
- q11、q12、q21、q24 是无可用证据题；必须拒绝猜测并返回 UNKNOWN。q24 的答案只存在于其他项目，不可跨项目借用。
- d01、d04 分别用于跨项目、跨用户隔离；d02 已过期，d03 已归档。d05、d06 是有效但容易产生词面误命中的干扰项。
- q16 的语种要求用于人工复核；自动检查只校验偏好事实与引证，不代表完整语言风格评分。
- r10 是仓库当前登记事实，不意味着重新联网核验了供应商容量。
- 题目与答案先冻结再调用模型，改写阶段只读取 query/history，回答阶段只读取 query/history/实际注入记忆。模型看不到 gold、标准答案或其他项目记忆。
- 开发集与留出集共享语料库，按问题划分，不能宣称实体不相交泛化；同一实体有不同问法。
- 留出集本轮仅作首次基线，不根据结果调参。如用其指导下一轮改动，应建立新留出集。
- ID 引证为评测脚本加入的观测标记，不是生产提示词的逐字复制；脚本重新检查实际注入文本不超过 1500 UTF-8 字节。
- 当前集未覆盖大规模数据、复杂矛盾记忆合并、完整会话发送门、真实用户行为分布；这些必须在开发任务中补充。

## 真实评测结果

2026-09-19 已完成 PostgreSQL＋真实 embedding＋DeepSeek 首批 A/B。参见 [完整结果与局限](../../acceptance/memory-recall-live-2026-09-19.md)。两组答案均 24/24，改写没有提高通过率；题集和 gold 未因结果改动。
