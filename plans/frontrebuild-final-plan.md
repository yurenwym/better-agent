# 前端优化最终方案：「蓝晒工作台」(frontrebuild)

> 产生过程：3 个研究智能体分别从视觉（A 蓝晒视觉 / B 布局与信息架构 / C 交互与一致性）角度产出方案，
> 3 个评审智能体（终端用户代表 / 工程负责人 / 设计总监）按 美观、简洁、清晰、合理 四维打分投票。
> **投票结果：A 3:0 当选主方案**（A=18/18/17，B=14/15/16，C=14/15/14）。
> 本文件是三方合并共识，是所有编码智能体的唯一规范来源。

基线：`npx vitest run` 37 文件 213 用例全绿；`npm run build` 通过。
铁律：**只改 `frontend/`，不碰 `backend/`；不改 `frontend/src/api.ts` 与 `frontend/src/types.ts`；存量测试文件一行不改**（允许新增测试文件）。文案即契约：用户可见中文文案（按钮名、aria-label、报错文本、"确认删除"/"正在处理…"等）非必要不动，凡动必须先 grep `frontend/src/__tests__` 确认无断言依赖。

## 一、设计方向（A 案核心，全量实施，暗色主题除外）

母题：**墨蓝深色侧栏做锚 + 暖白纸面承载内容 + 藏青单主色贯穿；等宽字体只做"工程标注"。**

### 1. tokens.css 令牌重校准（全部改值，命名体系不变）

| 令牌 | 新值 | 说明 |
|---|---|---|
| `--color-paper` | `#f4f5f7` | 降饱和纸面 |
| `--color-paper-soft` | `#ebecf0` | |
| `--color-sidebar` | `#141a29` | 墨蓝深色侧栏（识别度核心） |
| `--color-panel` / `-raised` / `-soft` | `#ffffff` / `#fbfcfd` / `#f1f3f7` | |
| `--color-line` / `-strong` | `#e3e6ec` / `#c9d0dc` | |
| `--color-ink` | `#1a2333` | 蓝黑 |
| `--color-muted` | `#55617a` | |
| `--color-faint` | `#6b7686` | 对比度修复，须 ≥4.5:1 |
| `--color-accent` / `-strong` / `-soft` | `#2b4ac9` / `#1f3aad` / `#edf1fd` | 深藏青 |
| `--color-success/-soft` | `#0e8a5f` / `#e6f5ef` | |
| `--color-warning/-soft` | `#b25e09` / `#fcf1e3` | |
| `--color-danger/-soft` | `#cd3548` / `#fcedf0` | |
| `--color-info/-soft` | `#2c6cb8` / `#ebf3fb` | |
| `--color-user` | `#2b4ac9` | 跟随主色 |

新增令牌：`--color-panel-muted:#f1f3f7`（修复幽灵变量）、`--color-text:#1a2333`（别名）、
`--control-h:40px`、`--shadow-lift: 0 2px 4px rgba(16,24,44,.06), 0 6px 16px rgba(16,24,44,.10)`、
`--shadow-focus: 0 0 0 3px rgba(43,74,201,.22)`、
侧栏系列 `--color-sidebar-ink:#eef1f9 / -muted:#9aa5bd / -faint:#68738f / -line:rgba(255,255,255,.09) / -item-hover:rgba(255,255,255,.06) / -active:rgba(101,127,235,.22)`。
阴影改贴地双层：`--shadow-panel: 0 1px 2px rgba(16,24,44,.04), 0 3px 10px rgba(16,24,44,.04)`；`--shadow-raised` 加深。
圆角刻度收敛 4 档：`sm 6 / md 10 / lg 14 / xl 18`，气泡尾巴角用 `14px 4px 14px 14px` 语义。
字体栈补中文回退：`"IBM Plex Sans","Segoe UI","PingFang SC","Microsoft YaHei UI","Noto Sans SC",system-ui,sans-serif`；数字用 `tabular-nums`。
布局令牌（B 案吸收）：`--shell-sidebar-w:264px`、`--shell-topbar-h:56px`、`--content-default:1200px`、`--content-wide:1440px`、`--content-pad:clamp(16px,2.5vw,40px)`、`--measure-reading:76ch`、`--measure-chat:860px`。

**不做暗色主题**（三方评审一致砍掉/降级）。

### 2. styles.css / index.css 治理（主战场）

1. **幽灵变量修复**：补齐/替换所有未定义变量引用（`--color-panel-muted`、`--color-text`、`--font-sans`→`--font-body` 等，实测 11 个名字约 35 处引用）。
2. **旧变量回退清理**：`var(--accent,#0f766e)`、`var(--text-muted,#64748b)`、`var(--border,#dbe3e8)`、`var(--surface,#fff)` 等约 45 个旧名引用全部替换为 `--color-*` 令牌（消除 Today 页 teal 第二配色）。
3. **硬编码色归零**：57 处裸 hex/rgba 色全部改令牌引用（阴影/遮罩的黑基色 rgba 允许保留，但必须与 tokens 中定义一致）。
4. **字号下限**：8–10px 全部提到 ≥11px（mono 工程标注 11px，meta 说明 12px，正文 ≥12px）；`--color-faint` 小字同步换 `--color-muted`。
5. **按钮系统**：高度统一 `--control-h`（sm 32 / 默认 40 / lg 48 修饰类）；**删除 `.button:hover` 的 `translateY(-1px)`**，hover 用 `--shadow-lift`；primary/secondary/ghost/danger 四变体规格见 A 案。
6. **徽章/chip 归一**：`state-pill`、`status-chip`、`gate-*`、`signal-*`、专家/进化状态徽章统一 chip 规格（pill + soft 底 + tone 色深字 + 细描边）；**`permission-added` 改 success 绿、`permission-removed` 改 danger 红**（现状语义反转）。
7. **侧栏深色化**：全部侧栏选择器换 `--color-sidebar-*` 令牌；选中态统一「accent 左条 inset + soft(active) 底」一种语言（删除会话行 inset 阴影条、计划库左边框等旧语言）；brand mark 改 accent 渐变砖。
8. **消息气泡**：assistant `panel-soft`+line 描边，user `accent-soft`+accent 28% 描边；头像 30px/11px 字。
9. **输入框**：统一 `--control-h`、focus 用 `border accent + --shadow-focus`；**删除 styles.css:136 对 composer textarea focus 的 `outline:0;box-shadow:none` 显式移除**。
10. **双层页头压缩**（纯 CSS，DOM 不动）：topbar 降为单行 ~56px（h1 降为标识级 17px），page-header 的 h2 22px 承担页面主标题；三层 padding 收紧。
11. **宽度三档收敛**（B 案吸收）：默认 1200 / wide 1440 / fluid；growth 1180、goal 1280 两个私有宽度删除；magic `calc(100dvh - Npx)` 换 `var(--shell-topbar-h)` 推导。
12. **行长治理**（B 案吸收）：聊天主列与 composer 居中 `max-width: var(--measure-chat)`（860px）；时间线正文与 Today 详情列 `max-width: min(920px,100%)`，报告/正文类 `--measure-reading`。
13. hover 缺口补齐（`.trajectory-view-switch button`、`.history-version`）。

### 3. 侧栏图标（唯一允许的 JSX 视觉改动）

`WorkspaceSidebar.tsx` 的 `NavGlyph`：为 research/schedules/evaluation/models/usage 等补不同内联 SVG path（只改 `d` 属性与映射，`aria-hidden` 结构、按钮文案不动）。

## 二、交互安全网（C 案吸收项）

1. **破坏性操作必须确认**：SchedulesPage 定时任务删除、通知渠道删除接 ConfirmDialog（复用现有组件，默认 confirmLabel 不改）；MemoryPage「删除经历」若确无确认则接入。删除按钮可访问名保持「删除」。
2. **写操作失败必须可见**：
   - `ApprovalCard.act()` 补 catch → 卡片内 inline error（role=alert），按钮可重试；
   - ChatPage 研究取消/重试补 `.catch` → 现有 `showOperationError` 通道；
   - App.tsx `onHumanMode` 补 catch → AppToast；bootstrap 失败置错误态，在 topbar 区域显示「后端未连接」提示；
   - SchedulesPage 所有操作补 catch + 错误态；SkillsPage choose() 失败提示；ResearchSourcesPanel 失败显示「来源暂时无法加载」而非隐藏面板。
3. **AppToast**：error 默认驻留 8000ms（success 保持 4000ms）。API 不变。
4. **busy 防双击**：PlanPage approve/revise/stopStep、SchedulesPage 全部操作按钮、SkillsPage 版本 toggle 加 `disabled={busy}`；busy 条件文案 `busy ? "正在…" : "原名"`，非 busy 文案逐字不变。
5. **加载/空态（最小修复，不新建 StateFeedback 组件）**：首屏加载期不得闪现「暂无…」空态——MemoryPage、SkillsPage、ResearchPage、SchedulesPage、UsagePage、EvaluationPage 六页补 loading 判定（复用现有 `.loading-bar`/`aria-busy`，300ms 防闪烁可简化为 `!loading` 判定）；SchedulesPage 补空态与错误态。
6. **流式体验**：消息流式期间内容尾部渲染 `.stream-cursor`（复用 typing-pulse keyframes）+ 容器 `aria-busy`；ConversationThread 增加底部哨兵，距底 <120px 才自动滚动，所有 `scrollIntoView` 包 try/catch（jsdom 无布局）。
7. **可访问性**：AskCard 挂载时聚焦其内第一个可交互元素；技能面板 Esc + 外点关闭（对照 ConfirmDialog 模式）；PlanPage 英文报错文案中文化（`"Plan failed to load"`→`"计划加载失败"` 等，grep 确认无测试断言后修改）。
8. **状态词表合并**：App.tsx / WorkspaceSidebar / StatsBar / ActivityRail 四份状态文案映射合并进 `localization.ts`；**每个调用点的当前输出字符串必须逐字保持**（措辞不同的加独立键），合并后跑 App.test/ActivityRail 相关用例验证。

## 三、布局与信息架构（B 案定向吸收，仅低风险项）

1. App.tsx 的 `widePage/fluidPage/showTopbar/showPageHeader` 四个名单合并为**单张布局表**；**输出的类名组合必须与现状逐字节一致**（App.test 断言 `workspace-page-header-trajectory`、`workspace-main-viewport-wide/fluid` 等）。
2. 顶栏精简（CSS 层完成，DOM 结构与文案不动）。
3. 导航分组/顺序/文案**一律不动**；页面 DOM 重排（Growth/Models/Memory/Trajectory/Schedules 结构调整）**全部不做**；不做 `<details>`/折叠改造。
4. 导航名与页面标题的文案统一：仅当 grep 测试确认无断言后才允许改，任何一条有断言依赖就保留原文。

## 四、明确不做（本轮砍掉，记录在案）

- 暗色主题（三方一致降级，下轮首个增量）
- 侧栏五组重排、页面 DOM 重排、`.layout-md` 全量模板、断点 9→4 收敛
- StateFeedback 新组件、成功反馈全量归一、四态 13 页铺开
- ModelsPage/GrowthPage/MemoryPage 区块重排与折叠收纳
- 状态词表以外的 localization 重构

## 五、实施分工（文件独占，禁止越界）

| 批次 | 负责文件 | 依赖 |
|---|---|---|
| ① 令牌与全局样式 | `tokens.css`、`styles.css`、`index.css`、`index.html`(theme-color) | 无 |
| ② 外壳与共享层 | `App.tsx`、`localization.ts`、`components/WorkspaceSidebar.tsx`、`StatsBar.tsx`、`ActivityRail.tsx` | 与①并行（文件不相交） |
| ③a 聊天与反馈域 | `pages/ChatPage.tsx`、`components/ConversationThread.tsx`、`ApprovalCard.tsx`、`AppToast.tsx`、`AskCard.tsx`、`ResearchSourcesPanel.tsx` | 在①②合入后开工 |
| ③b 控制台域 | `pages/SchedulesPage.tsx`、`MemoryPage.tsx`、`SkillsPage.tsx`、`ResearchPage.tsx`、`UsagePage.tsx`、`EvaluationPage.tsx`、`PlanPage.tsx`、`GrowthPage.tsx` | 在①②合入后开工 |

每批次完成标准：`npx vitest run` 全绿（存量 213 用例零修改）、`npm run build` 通过；①另加 grep 验收（无未定义 `var(--` 引用、无裸 hex 色）；新增行为必须新增测试用例（新文件）。

## 六、验收与联调

1. Review 智能体对全量 diff 做一次审查（规范符合性 + 越界检查 + 回归风险），问题修复后复核。
2. 验收用例文档：`docs/frontrebuild-acceptance.md`（视觉走查清单 + 交互路径用例）。
3. 自动化：vitest 全量 + 新增用例 + `npm run build`。
4. 前后端联调：`cd frontend && npm run build && npm run e2e`（Playwright 自起 `scripts/e2e_server.py`，SQLite 测试模式，跑 5 个 spec 真实前后端链路）；另用运行中的 dev 环境（8000/5173 已在跑）做人工级浏览器走查关键页面。
