# 可读 Markdown 消息展示设计

## 背景

当前对话线程把模型的普通文本回复直接放进 `<p>`，因此模型返回的 Markdown 语法会原样暴露给用户：标题显示为 `##`，加粗显示为 `**`，表格显示为管道符文本。模型回复本身已经包含有用的结构，问题在于前端没有把这种结构转换为可阅读的消息内容。

本次改动只处理前端消息展示，不改变模型协议、轨迹事件、运行状态机、流式传输或后端数据。

## 已确认的产品结果

采用右侧对比稿的“阅读优先”消息卡片：

- 回复标题使用真实的 heading 层级，正文使用段落和合理行距。
- 无序/有序列表、引用、粗体/斜体、行内代码和代码块按语义展示。
- GFM 管道表格显示为有表头、行间分隔线的语义化表格；窄屏时表格只在自己的容器内横向滚动。
- 继续保留现有 Better Agent / 用户头像、时间、流式状态、结构化摘要、决策按钮和轨迹侧栏。
- 不把 `needs_clarification`、`action` 等模型内部 JSON 字段直接显示给用户。

## 方案与边界

### 方案

新增一个纯展示组件 `MarkdownMessage`，由它将受支持的 Markdown 子集解析为 React 节点。`ConversationThread` 负责决定展示哪类内容，`MarkdownMessage` 只负责内容排版。

不引入新的 Markdown 运行时依赖。当前前端依赖很小，消息内容来自模型且不应被当作可信 HTML；使用 React 元素渲染可以天然避免 `dangerouslySetInnerHTML` 和未经许可的 HTML 执行。

### 支持的语法

- ATX 标题：`#` 至 `###`（并兼容更深层级但映射到 `h3`）。
- 段落与软/硬换行。
- `**粗体**`、`__粗体__`、`*斜体*`、`_斜体_`、行内代码。
- 无序列表、有序列表及连续列表项。
- `>` 引用。
- 三反引号 fenced code block，保留代码文本和可选语言标记。
- GFM 管道表格，支持表头、分隔线和数据行。
- `---` 等水平分隔线。

不支持原始 HTML、脚本、图片、自动链接和复杂嵌套 Markdown；这些内容以安全文本显示或按普通文本降级。这样可以保持 V1 的实现体积可控，也避免模型输出被当作可执行标记。

## 数据流

```text
MessageRecord
    -> presentMessage()
    -> 结构化 JSON：继续转换为用户友好的 summary/detail/bullets
       普通回复：保留 Markdown 文本
       未完成 JSON 流：继续显示“模型正在生成回答…”状态，不暴露 JSON
    -> ConversationThread
    -> MarkdownMessage(summary/detail/user content)
    -> React semantic elements + message-content styles
```

结构化 JSON 的 `bullets` 仍由现有列表逻辑显示；`summary` 和 `detail` 可通过同一渲染组件显示其中的轻量 Markdown。正在流式接收且看起来仍是 JSON 的内容不交给 Markdown 解析器，避免半截 JSON 或内部字段进入用户界面；已有的流式占位提示和打字指示器保持不变。

## 组件与样式

`MarkdownMessage` 使用可访问的语义元素：`h2/h3`、`p`、`ul/ol`、`blockquote`、`table`、`pre/code`。表格外包裹 `.message-table-scroll`，代码块使用 `.message-code-block`，正文容器使用 `.message-markdown`，避免污染全局 `pre` 或其他页面。

样式遵循现有 Better Agent 令牌：深色高对比文字、低干扰分隔线、清晰的 heading 间距、至少可读的正文尺寸、代码块独立背景、表头弱强调色。移动端不缩小正文，而是让宽表在局部容器滚动；按钮、决策区和轨迹面板不受影响。

## 错误与降级

- 空字符串继续显示现有空状态或不渲染消息正文。
- 未识别的 Markdown 行按普通文本段落显示，不抛出异常。
- 未闭合的代码围栏在消息末尾自动结束，保证流式内容仍可见。
- 表格无法形成有效表头/分隔线时按普通段落和列表文本降级，不生成错误表格。
- 不使用 HTML 注入；模型输出中的标签只作为文本处理。

## 测试策略

遵循 TDD：先写会失败的测试，再实现最小解析器和组件。

1. `MarkdownMessage` 渲染标题、段落、强调、列表、引用、代码块和表格；断言真实语义元素存在，且 `##`、`**`、管道符原文不出现在可见文本中。
2. HTML/脚本输入不会创建 DOM 元素或执行标记，作为文本安全降级。
3. `ConversationThread` 对普通助手回复使用 Markdown 展示；JSON 结构化回复仍展示用户友好摘要，不展示 `needs_clarification` 等原始字段。
4. 流式 JSON 仍展示现有生成中提示，不把半截 JSON 当成回复正文。
5. 运行前端完整 Vitest、TypeScript 构建，并检查窄屏样式相关组件测试不回归。

## 非目标

- 不改后端 Markdown 内容、不改 SSE/流式协议。
- 不引入 RAG、富文本编辑器、依赖重量级 Markdown/HTML 渲染器。
- 不重做已经存在的轨迹系统、计划页、记忆页或布局导航。
- 不把原始事件 JSON 或模型内部控制字段放进普通对话内容。
