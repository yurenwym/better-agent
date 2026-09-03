# 更新后的记忆系统说明

本文用通俗的方式说明当前项目的记忆系统。这里的“记忆”不是一张表，而是几类数据和一条处理流程。

## 一句话结论

当前系统可以从产品角度理解为三层：

```text
短期上下文：本次回答时，模型能看到的内容
对话摘要：较早对话的压缩档案
长期记忆：以后还需要持续记住的用户信息
```

严格来说，第二层不是长期记忆，而是“对话存档”。它可以被放入上下文，但不能直接当成用户的永久事实。

## 三类内容的区别

| 类型 | 代码中的名称 | 作用 | 是否属于长期记忆 |
|---|---|---|---|
| 原始对话 | `Turn`、`thread_messages` | 保存最完整的聊天事实 | 否 |
| 对话摘要 | `Episode`、`memory_episodes` | 概括一段较早的对话 | 否 |
| 长期记忆 | `MemoryEntry`、`memory_entries` | 保存以后仍要使用的信息 | 是 |

还有一个容易混淆的词：上下文。上下文不是一种永久存储，而是“这一次模型请求实际能看到的内容”。原始对话、对话摘要和长期记忆，都可能被选择放入上下文。

## 第一层：短期上下文

### 它是什么

短期上下文是当前回答需要使用的信息集合，通常包括：

- 当前聊天窗口的 Thread 编号；
- 当前用户编号；
- 当前用户刚发送的问题；
- 最近若干个完整的用户提问和助手回答；
- 已回答的提问工具调用和工具结果；
- 当前目标、计划和执行步骤；
- 工具执行结果；
- 当前对话绑定的技能说明；
- 当前用户已经确认的长期记忆；
- 当前聊天窗口内可用的对话摘要。

Thread 是一个独立的聊天窗口。用户编号用于确定数据归属和权限。它们主要用于数据库查询和安全校验，不一定会原样显示给模型。

### 它不是什么

短期上下文不是“只保存最近 50 条消息”的表，也不代表旧消息会被删除。

原始消息仍然保存在数据库中。短期只表示：

```text
这次模型回答暂时能看到哪些内容
```

### 如何选择最近内容

系统先把数据库里的消息整理成一份标准版聊天记录，再从最新的完整对话轮次开始，按 Token 预算选择。

Token 可以简单理解为模型处理文本时使用的计量单位。当前代码使用保守的 UTF-8 字节上界估算，宁可多算，也不冒险低估请求大小。

选择时遵守这些规则：

- 一轮对话必须整体保留或整体丢弃；
- 不截断半条消息；
- 不保留用户问题而丢掉对应的助手回答；
- 不保留失败、取消或中断的助手半成品；
- 不读取其他聊天窗口的内容；
- 不越过尚未完成的一轮对话。

相关代码：

- [transcript.py](/D:/RAG/better/backend/app/transcript.py)
- [conversation.py](/D:/RAG/better/backend/app/conversation.py:2118)
- [context.py](/D:/RAG/better/backend/app/context.py)

## 什么是标准版聊天记录

代码中的 `Canonical Transcript` 可以翻译成：

> 给模型使用前，系统整理出来的可信聊天记录。

它不是一张数据库表，也不是 LLM 摘要，而是一个中间对象。

原始数据库内容可能是：

```text
用户消息
助手第一次失败的回答
助手第二次失败的回答
助手最终回答
助手调用提问工具
工具返回用户答案
```

标准版聊天记录会把它整理成：

```text
用户消息
助手最终回答
助手的完整工具调用
工具返回结果
```

它还会记录：

- 用户编号；
- 聊天窗口编号；
- 项目编号；
- 对话轮次；
- 有效消息编号；
- 原始消息指纹。

这样，线上回答和后台摘要使用的是同一套对话含义，不会出现两个模块各自理解历史的问题。

## 第二层：对话摘要

### 它是什么

代码中的 `Episode` 可以理解为：

> 一段过去对话的总结档案。

例如：

```text
本次对话讨论了训练计划。用户提到膝盖有旧伤，最后确定先采用低强度训练。
```

它是对过去对话的压缩，可能丢失细节，因此系统把它当作：

```text
不完整的历史资料
```

而不是：

```text
用户已经确认的永久事实
```

### 什么时候生成摘要

当前默认保留最近约 `12000` 个预算单位的完整对话。

当较早的完整对话已经无法继续放进这个近期窗口时：

```text
最近约 12000 Token：继续保留为近期原文
更早的完整 Turn：交给 LLM 生成 Episode 摘要
```

这不是“模型总上下文超过 12000 Token 就立刻压缩全部内容”。`12000` 是历史保留预算，不是模型完整请求的最大上下文。

模型最终请求还要包括系统提示、当前问题、工具说明、计划、记忆和模型输出预留空间，因此网关会再次检查最终请求是否超限。

### 摘要是怎么触发的

一轮 Turn 变成 `COMPLETED` 后，数据库触发器会写入一条摘要通知。后台 Worker 发现通知后，才检查是否真的有足够早的内容需要归档。

如果全部历史仍然在近期窗口内：

```text
不创建摘要任务
不调用 LLM
```

如果确实有较早内容：

```text
创建摘要任务
调用 LLM
保存 Episode
```

### 摘要生成流程

```text
一轮对话完成
    ↓
数据库写入“需要检查”的通知
    ↓
后台 Worker 发现通知
    ↓
检查是否超过近期 Token 保留窗口
    ↓
创建正式摘要任务
    ↓
整理标准版聊天记录
    ↓
调用 LLM
    ↓
检查返回结果
    ↓
保存 Episode 和处理进度
```

相关代码：

- [db.py](/D:/RAG/better/backend/app/db.py:1073)
- [memory_archive.py](/D:/RAG/better/backend/app/memory_archive.py:50)
- [memory_archive.py](/D:/RAG/better/backend/app/memory_archive.py:341)

### LLM 使用的摘要提示词

当前提示词位于 [memory_archive.py](/D:/RAG/better/backend/app/memory_archive.py:318)。英文原文是：

```text
Return strict JSON only with exactly these fields: synopsis, topics, decisions, outcomes, open_loops, sensitivity. Each list item must contain exactly text and source_message_ids. Every source id must come from the supplied transcript. Describe past conversation as lossy, attributed history; do not turn statements into permanent user facts. sensitivity is normal, sensitive, or restricted.
```

中文含义是：

```text
只能返回严格的 JSON，并且只能包含以下字段：
synopsis、topics、decisions、outcomes、open_loops、sensitivity。

每个列表条目只能包含 text 和 source_message_ids。
所有 source_message_ids 都必须来自系统提供的这段对话记录。

请把过去的对话描述成可能有损、带来源的历史记录，
不要把对话中的临时说法直接变成用户的永久事实。

sensitivity 只能是 normal、sensitive 或 restricted。
```

字段含义：

- `synopsis`：整体总结；
- `topics`：讨论过的主题；
- `decisions`：做出的决定；
- `outcomes`：得到的结果；
- `open_loops`：还没有完成的事情；
- `sensitivity`：敏感程度。

### 摘要为什么不会直接相信 LLM

系统会检查：

- 返回内容是不是合法 JSON；
- 字段是否完整；
- 是否出现未知字段；
- 整体摘要是否为空；
- 每个条目是否有来源消息编号；
- 来源消息编号是否真的属于当前批次；
- 是否出现密码、Token、API Key 等敏感内容；
- 敏感级别是否有效。

检查失败时，不会生成有效 Episode，而是进入重试或死信状态。

## 第三层：长期记忆

### 它是什么

代码中的 `MemoryEntry` 是以后还需要持续使用的信息，例如：

```text
用户喜欢简洁回答
用户不喜欢吃辣
用户有膝盖旧伤
```

长期记忆和摘要的区别是：

```text
对话摘要：这次聊天发生了什么？
长期记忆：以后应该记住用户什么？
```

长期记忆保存在：

- `memory_entries`；
- `memory_revisions`。

它支持用户范围、项目范围、版本修改、启用、归档和审批。

### 当前是否会自动把摘要升级为长期记忆

当前还没有完整接通这条自动链路：

```text
Episode 摘要
    ↓
自动发现用户偏好
    ↓
自动创建 MemoryProposal
    ↓
自动审批
    ↓
写入 MemoryEntry
```

`MemoryProposal` 的数据结构和审批逻辑已经存在，但生产中的自动提案调用方尚未完整接入。

当前更安全的行为是：

```text
用户明确要求“请记住……”
    ↓
保存为长期记忆
```

例如用户说：

```text
我今天不想吃辣。
```

系统不能直接推断为：

```text
用户以后永远不吃辣。
```

这就是摘要和长期记忆必须分开的原因。

## 一次对话的完整过程

```text
用户发送消息
    ↓
系统创建 Turn 和原始消息
    ↓
Conversation Worker 读取当前 Thread
    ↓
标准化聊天记录
    ↓
按 Token 预算选择近期完整 Turn
    ↓
查询当前用户可用的长期记忆
    ↓
查询当前 Thread 可用的 Episode
    ↓
组装本次模型上下文
    ↓
网关再次检查最终请求是否超限
    ↓
模型返回回答
    ↓
Turn 变为 COMPLETED
    ↓
后台检查是否有旧对话需要摘要
    ↓
必要时生成 Episode
```

## 三类内容进入上下文时的区别

系统会明确标记不同来源：

```text
[confirmed memory]
```

表示用户确认过的长期记忆。

```text
[conversation episode; lossy, non-authoritative]
```

表示可能有损、非权威的历史摘要。

这样可以避免模型把：

- 历史摘要误认为永久事实；
- 用户文本误认为系统指令；
- 一个 Thread 的内容带入另一个 Thread。

## 最终理解方式

可以用下面四句话记住当前系统：

```text
原始消息：最完整的事实来源。

短期上下文：本次回答临时能看到的内容。

Episode：较早对话的压缩档案，不是永久记忆。

MemoryEntry：经过确认、以后仍要使用的长期记忆。
```

所以，当前系统不是简单地“把所有聊天都存成记忆”，而是把：

```text
原始对话、历史摘要、长期记忆、模型上下文
```

分别处理，并通过用户范围、Thread 范围、Token 预算、来源引用、事务和任务租约来保证数据不会混乱。
