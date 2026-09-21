# 普通聊天业务工具与执行管理：D2+D3 联合验收（真实模型）

日期：2026-09-20。范围：D3 的页面审批链路、提示词与工具描述统一、工作台调整，以及 D2+D3 的真实模型端到端验收。D4（计划修改工具）不在本轮。

结论：**工具链路真实模型端到端 6/6 通过**（攻略交付不建目标、保存提交、执行管理预览→确认→激活、拒绝、取消、重启恢复）；意图识别层在独立场景集上有提升（全匹配 65.6% → 81.3%），但**未达到开发文档的后续修复门槛**（全匹配 ≥90%、review ≥95%、daily 误触发为零），失败全部保留。

## 1. 真实模型配置复查

- 加载路径与用户描述一致：`load_user_model_environment()` 从 Windows 用户环境导入，再由 `load_model_profile_from_environment()` 解析。
- 实际解析结果：`provider=deepseek`、`base=https://api.deepseek.com`、`model=deepseek-flash`、密钥存在（`DEEPSEEK_API_KEY`，未输出、未写入产物）。
- 探活：一次真实调用返回 `OK`。此前“环境未配置可用密钥”的结论作废。
- 网络：本机直连不稳定，运行需经 Windows 用户代理 `127.0.0.1:7897`（设置 `HTTP_PROXY`/`HTTPS_PROXY`）；应用进程若未继承代理变量，网关会超时。本轮脚本通过环境变量使用代理，未在代码或产物中持久化密钥。
- 供应商别名如实记录为 `deepseek-flash`；网关未返回供应商内部模型修订号。

## 2. D3 交付内容

1. **前端审批卡片**：新增 `ChatToolApprovalCard` 并接入 `ChatPage`。展示具体写入内容（草稿标题与全文预览、预览编译的日期/时区/预算、激活绑定快照的逐项安排、反馈与延期参数）；支持批准、拒绝、刷新状态；同一 turn+动作复用同一幂等键，重复点击或失败重试不会产生第二个 continuation；`AWAITING_TOOL_APPROVAL` 期间锁定输入框并把取消作为显式操作；`useThreadTelemetry` 在 `chat_tool.*`/`turn.awaiting_tool_approval` 事件上刷新线程，页面重载后重新拉取待确认调用。
2. **提示词与工具描述统一**：生成（直接交付）、保存（v=2 artifact / `create_plan_draft`）、执行管理（`activate_goal_plan` 预览→确认→激活）三条路径分别约束；提醒、跟踪、单次复盘、每日复盘独立判断；周期性提醒不等于跟踪、取消提醒不改变进度记录、改频率不等于取消、“每次 X 后复盘”不升级为每日；执行管理属于持续服务；记录完成情况属于跟踪而不是复盘；操作结果只依据成功回执。工具描述同步这些边界。
3. **工作台调整**：`goal_workspace` 无执行目标时返回 `DELIVERED`，主操作改为“查看或修改计划”，执行管理为可选入口；计划页主操作是“编辑计划”，“开启执行管理”降为次级入口；提示“计划已保存，可直接查看、编辑或交付使用”。
4. **端到端验收**：见第 3、4 节。

## 3. 真实模型 E2E（`backend/scripts/chat_tool_e2e.py`）

六个场景，每个独立 SQLite 数据库，生产 Worker、Registry、审批与恢复链路全量运行；审批由测试装置决定，不交给模型。两轮运行（提示词收紧前/后）均为 **6/6 通过**。

| 场景 | 断言 | 结果 |
| --- | --- | --- |
| travel_guide_delivery | 回答非空；plan_documents/goal_programs/goals/runs 全为 0；无“已保存/已开启”等声称 | 通过 |
| save_guide | 1 个 committed 文档版本；无执行目标 | 通过 |
| execution_management | 预览编译成功 → 用户消息确认具体版本 → 激活后 program ACTIVE 且有 3 个行动 | 通过 |
| feedback_reject | 待写入反馈被拒绝：0 条反馈；回答明确“没有写入” | 通过 |
| feedback_cancel | 待写入反馈被取消：0 条反馈；审批 rejected | 通过 |
| feedback_recovery | 批准后重启进程再恢复：恰好 1 条反馈，不重复写入 | 通过 |

证据与用量：

- 收紧前：`docs/evaluation/chat-tool-e2e-2026-09-20/`（25 次调用；未缓存输入 10,321、缓存读取 93,440、输出 11,058、推理 4,592 tokens）
- 收紧后：`docs/evaluation/chat-tool-e2e-2026-09-20-postfix/`（24 次调用；未缓存输入 11,370、缓存读取 93,440、输出 9,993、推理 4,264 tokens）
- 未核实计费单价，费用 unknown，不填零。

E2E 暴露并修复的三个真实缺陷：

1. `deepseek-flash` 会在一次响应里并行返回多个工具调用（如两个 `get_today_tasks`），原聊天循环要求恰好一个，导致 turn 失败。已支持批量：按顺序执行，首个 WRITE 暂停审批，其余结果按协议回填。
2. 计划上下文只包含 Markdown，不含 `document_id`/`version_id`，模型无法发起 `activate_goal_plan`。上下文引用行现在携带真实文档与版本标识（仍标注为不可信数据）。
3. “确认，按这个版本激活执行管理”被保存分类器误判为“保存已有计划”，强制走文档 artifact，激活被吞掉。已加确定性确认判定与分类器负例提示。

## 4. 意图回归与独立场景（真实模型）

| 运行 | 数据 | 全匹配 | tracking | review | document | daily TP/FP/FN | 错误 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| live-01（历史基线） | v1 固定 42 例 | 33/42，78.6% | 81.0% | 88.1% | 92.9% | 8/2/0 | 3 |
| live-02（收紧前回归） | v1 固定 42 例 | 33/42，78.6% | 81.0% | 88.1% | 95.2% | 8/2/0 | 2 |
| live-03（收紧后回归） | v1 固定 42 例 | 33/42，78.6% | 83.3% | 85.7% | 95.2% | 8/3/0 | 2 |
| live-01（独立集基线） | v2 新增 32 例 | 21/32，65.6% | 81.3% | 90.6% | 93.8% | 3/3/0 | 0 |
| live-02（收紧后） | v2 新增 32 例 | 26/32，81.3% | 90.6% | 93.8% | 90.6% | 3/1/0 | 1 |

- 独立集是在看到任何结果前冻结标签、随后才运行的；`docs/evaluation/followup-intent-v2/cases.json`。
- 收紧后独立集全匹配提升 15.6 个百分点、tracking +9.4、review +3.2、daily 误触发 3→1；v1 回归没有同向提升，review 下降 2.4 个百分点、daily 误触发 2→3。两个集合都远小于统计显著所需规模，只能作为方向性证据。
- 失败全部保留在各 `results.json` 与逐调用记录中，没有挑选成功案例重算。

## 5. 标签争议（不修改本轮金标）

按开发文档，发现歧义只记录、不悄悄改金标：

- **X10**“每天中午提醒我吃药”（金标 none/none）：模型一致判 tracking=enable。探针的 tracking=enable 定义为“请求持续服务”，周期提醒是否属于持续服务存在真实歧义。
- **X19/X23**“模板里的复盘示例不要照做/删掉”（金标 modify）：模型判 answer 或输出协议错误，文档修改意图是否明确存在歧义。
- **X28**“不要激活，只要文档”（金标 answer）：模型判 save，把“要文档”理解为保存请求。
- **X13**“以后每次跑完步复盘一次”（金标 once）：模型判 daily，事件触发与每日定时的边界模糊。
- **X05/X26** 开启执行管理是否计入 tracking=enable：模型倾向 none，本轮金标按产品语义记 enable。

## 6. 残余缺口

- 意图层未达门槛：全匹配 <90%、review <95%、daily 仍有误触发；重点是“每日复盘”误触、跨目标偏好传播（X20）、把频率变更当成取消（X16）。
- v2 已被用于一次调优，不再是无偏盲测；后续提示词改动必须新建冻结集或预分组。
- 真实模型 E2E 只覆盖六个主链路场景；提醒/跟踪/每日复盘调度在系统中尚无可用入口，回答必须如实说明，本轮未验证调度能力。
- E2E 数据库为一次性 SQLite；关键写入的 PostgreSQL 一致性由 `tests/integration/test_chat_goal_tools_postgres.py`（2 passed）覆盖。
- 前端工作区同时存在 `front0920` 分支的进行中重构（`main.tsx`、`navigation.ts`、`router.tsx`）；合并策略由用户决定。当前分支构建与 341 个前端测试通过。

## 7. 复现

```powershell
# 真实模型（需代理时先设置，密钥由 Windows 用户环境提供）
$env:HTTP_PROXY="http://127.0.0.1:7897"; $env:HTTPS_PROXY="http://127.0.0.1:7897"
cd D:\RAG\better\backend
python scripts\chat_tool_e2e.py --execute --out ..\docs\evaluation\chat-tool-e2e-<dir>
python scripts\followup_intent_eval.py --execute --cases <cases.json> --out <dir>

# 确定性
python -m pytest tests/test_chat_goal_tools.py tests/test_goal_workspace.py -q
python -m pytest tests/integration/test_chat_goal_tools_postgres.py -q
cd ..\frontend; npm run build; npm test -- --run
```

## 8. 结论与下一步

- D2+D3 的用户可操作链路（批准/拒绝/取消/恢复、攻略交付不建目标、执行管理预览与激活）已通过真实模型端到端验收。
- 意图识别与提示词仍未达到开发文档的后续修复门槛；建议把它们作为 D4 之前的优先修复项，并在修复前新建冻结测试集，避免继续在已看过的 v2 上过拟合。
