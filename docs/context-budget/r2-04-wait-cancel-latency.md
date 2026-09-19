# R2-04 等待状态、取消与时延验收

状态：**已实现，离线验收通过**（`passed=true`，0 problems）。在线 P50/P95 未测（见 §9）。

## 1. 这一项解决什么问题

R1 的前台压缩循环是：

```python
for _ in range(128):          # ≈ 128 × 0.25s ≈ 32 秒
    ...
    await asyncio.sleep(0.25)
```

它**没有挂钟上限**，也**没有任何状态提示**。用户在回答路径上被扣住，不知道发生了什么，
也不能停。R2-04 给这段等待加三件可验收的事：

| 性质 | 手段 |
|---|---|
| **有界** | `ArchiveWaitPolicy.deadline_ms`（默认 2000ms），到点上报超时 |
| **可取消** | 每个工作单元之前检查一次取消请求 |
| **诚实** | 等待状态是 **context 阶段**事件，永不触碰 `first_token_ns` |

## 2. 等待策略

```python
ArchiveWaitPolicy(deadline_ms=2000, poll_ms=50)     # DEFAULT_ARCHIVE_WAIT_MS / _POLL_MS
```

环境变量 `AGENT_ARCHIVE_WAIT_MS` / `AGENT_ARCHIVE_WAIT_POLL_MS`，两者都校验 `> 0`。

**它不是 Profile 策略**，这是有意的：它是一个 UX 预算（"用户愿意等多久"），
不是"模型能吃多少"。所以它**不随 `H` 缩放**，也不进 `config_digest`。
把它挂到 Profile 上会让"换一个模型"顺带改变用户等待时长，那是两件不同的事。

`WaitDeadline.sleep_seconds()`：

```python
max(min(poll_ms, remaining_ms), 0) / 1000
```

**不越过 deadline**：只剩 10ms 时返回 `0.01` 而不是整个 50ms 的 poll。
已用测试钉住（`test_the_sleep_slice_never_passes_the_deadline`）。

## 3. 两种用完预算的方式

`reason` 区分两种，因为它们要修的东西完全不同：

| `reason` | 含义 | 该做什么 |
|---|---|---|
| `deadline` | 一直在空等（Job 被别人持租约） | 等待预算太短，或后台太慢 |
| `attempt_exceeded_deadline` | **一次归档模型调用本身超时** | 摘要器太慢，或 deadline 太短 |

第二种靠 `asyncio.wait_for` 包住**前台自己的**那次 attempt：

```python
outcome = await asyncio.wait_for(
    self._archive_attempt_once(...),
    timeout=max(deadline.remaining_ms, 1) / 1000.0,
)
```

**没有这层包裹，"有界"是假的**：一次挂住的摘要器调用可以远远跑过 deadline。
被切断的 Job 留在 `QUEUED`/`RUNNING` 的租约之下，可被别的 worker 回收 ——
可恢复，而且无论哪种结局都如实告诉用户。

## 4. 超时 ≠ 归档坏了

```python
class ArchiveWaitTimeout(ArchiveUnavailable):
    code = "archive_wait_timeout"
```

与 `archive_unavailable` **是两个不同的 code**："我们等了并放弃了"和"归档本身坏了"
是不同的事实，运维需要能分开。`public_message`：

> 历史整理超时；你的输入已保存，稍后可以直接继续，不需要重新输入。

它在 `_process` 的异常链里**排在 `ArchiveUnavailable` 之前**被单独接住并原样抛出，
**不会**落进 `except ArchiveUnavailable` 那条把回答降级成 `context_incomplete` 的路径。
理由：降级回答是在"归档坏了、只能带着不完整的上下文答"时给的；而这里是
"归档还在跑，只是我们没等"——把它伪装成上下文不完整会误导用户以为历史丢了。

## 5. 取消

取消在**每个工作单元之前**检查，所以 30 秒的 deadline 不会把用户扣满 30 秒。

两条硬性质：

1. **取消不回滚已提交的 Episode**。覆盖游标只在 `_commit` 事务内前进，永不后退。
   取消只是"停止等待"，不是"撤销归档"。
2. **被取消的 Turn 不会重新执行**。它结束为 CANCELLED，线程保持可接受新消息。

取消发生在等待真正开始之后才有意义 —— 一个在等待开始前到达的取消**不产生状态噪音**
（没有 `waiting` 也没有 `cancelled`）。两种情形都有测试钉住。

## 6. 本地计数成本

`_history` 里新增 `context.counted` 事件，`counting_ms` 从
`project_context → render_history → pack_recent → render_history` 计时。
**单独计时**是必须的：把它混进整个 `context_ms` 会被检索和归档时间淹掉，
而它才是 R2 加到回答路径上的那部分工作。

候选目标 **P95 ≤ 200ms**。

## 7. 前端

- 轨迹（`trajectory.ts`）把 `context.archiving` / `context.archive_wait_timeout` /
  `context.incomplete` 归入 **context 阶段**，并注释说明它们**不可能**被读成首字进度。
- `latestArchiveStatus(events)`：最新的相关事件获胜，`ready` 清除提示。
- `ArchiveStatus` 组件新增 `events` 可选 prop，**事件流提示优先于 5 秒轮询的 Job 状态**。

为什么必须走事件流：候选目标是"状态提示 1 秒内出现"，而轮询周期是 5 秒 ——
**轮询在结构上就达不到这个目标**，不是调参问题。

## 8. 验收矩阵（离线）

`backend/scripts/r2_04_wait_acceptance.py`，`BATCH_ID = R2-04-OFFLINE-001`。
报告：`outputs/r2-04-wait-acceptance.json`。

**这个脚本驱动的是真实入口** `ManagedTurnWorker._archive_history_before_generation`，
`agent_runtime` 按 `startup.py` 的方式接到 archiver 上。

> 上一版脚本为了"直接驱动选择器"把 `agent_runtime = None`。结果该方法在
> `archiver is None` 处早返回，**整个矩阵什么都没测**（`wait_rate=0.0`、`episodes=0`）。
> 可达性是夹具的一部分，不是偶然 —— 这是本轮修掉的第一个缺陷。

| 用例 | `H` 来源 | counting p50/p95 | 状态提示 max | 等待率 | 超时率 | Episode | 游标 |
|---|---|---|---|---|---|---|---|
| `background-on` | unrouted 回退 | 26.4 / **36.6ms** | 43.9ms | 0.50 | 0 | 7 | 55 |
| `background-off` | unrouted 回退 | 25.5 / **27.2ms** | 33.5ms | 0.50 | 0 | 7 | 55 |
| `background-on-slow-summariser` | unrouted 回退 | 26.7 / **44.5ms** | 34.6ms | 0.50 | 0 | 7 | 55 |
| `background-off-cancel` | unrouted 回退 | 26.1 / **29.5ms** | 40.7ms | 0.667 | 0 | 8 | 57 |
| `routed-profile-h23348` | **冻结 Profile** | 26.2 / **27.7ms** | 33.0ms | 0.333 | 0 | 5 | 51 |

`routed-profile-h23348` 走真实 `ModelProfile`（32768/8192 → `H = 23348`），
让 200ms 目标在**它原本被写定的预算上**被测量，而不是只在回退路径的 `H=4000` 上。
矩阵输出逐行打印每个用例的 `H` 与来源，读者能看出这一行是对着哪个预算量的。

`background-on` 与 `background-off` 的数值**逐项相同**，这正是设计要的结果：
后台 worker 的存在不改变前台需要做的工作量，只改变它是"等别人"还是"自己做"。

### 取消的证据

```
live-0 waiting  pending=53308 target=2800 H=4000 waited_ms=0
live-0 cancelled pending=42231 target=2800 H=4000 waited_ms=151
```

取消在 **206ms** 就把用户放走（deadline 是 2000ms，约 10 倍余量），
此时前缀还有 **42231 / 53308 ≈ 79%** 未压缩。这是"等待可取消"的直接证据：
用户没有被扣到 deadline，代价是这一轮少压了一些历史 —— 而下一轮会补上
（用例里 `live-1` 随即把它压完，`states=['waiting','cancelled','waiting','ready',...]`）。

矩阵还断言取消**必须落在等待之中**（`cancel_landed_in_wait`）：记录到一次
"从未开始等待"的取消不算证据。上一版正是踩了这个 —— 取消打在 index 2 上，
那一轮恰好没跨过触发线，于是"取消"测的是一个根本没等待的 Turn。

### 重启的证据

```
restart  timed_out=True (3051ms)  reclaimed=True  recovered=True  cursor=49  episodes=6
```

- 第一轮：摘要器挂住 30 秒，**只有 deadline 能结束它** → 3051ms 超时（deadline 3000ms）。
- 被放弃的 Job 留在 `RUNNING` 租约下；用例把租约缩短到 1 秒，**等租约自然过期**
  （走 `claim()` 里真实的回收路径），而不是伸手去重置那一行。
- 第二轮：换上可用摘要器 → 正常归档，游标前进到 49。

即：**一次超时不会让线程永久不可用**。

### 确定性

矩阵跑两遍，`wait_rate` / `timeout_rate` / `episodes` / `archived_through_seq` /
`counted_events` / `summarizer_calls` / `archiving_states` / `input_limit`
**逐项完全一致**。第二遍留档在 `outputs/r2-04-run-b.json`。

## 9. 诚实说明与未做

- **没有在线 P50/P95**。摘要是 stub，没有网络。这些是**本地成本**数字，
  不是用户感知时延。
- **`status_hint_ms` 只测了后端一半**：从 context 阶段开始到 `context.archiving`
  事件**落库**的那一刻。剩下的是传输与前端渲染，离线测不到。
  测量点在**事件提交时**而不是等待返回时 —— 后者会把一次 2 秒的等待报成
  "2 秒的状态提示"，那是粉饰不是描述。
- **`H` 的来源按用例标注**。四个用例走 unrouted 回退（`H` = archiver 的
  `keep_tokens`），一个走冻结 Profile。
- **`max_summary_tokens` 保持生产默认（12000）**。它决定一次批的大小，
  也就决定一次压缩需要几个切片 —— 而"取消在切片之间被注意到"**只有在多于一个切片时
  才可观察**。上一版把它抬到 1e7，一趟就压完，取消检查根本没被走到。
- **`restart` 用例把租约缩短到 1 秒**（生产默认 30 秒），以便回收发生在用例内部。
  走的回收路径是真实的。
- **被取消的那一轮把压缩留给了下一轮**。这是观察到的行为，不是缺陷，但值得记下：
  取消省下的是用户的等待，不是压缩工作本身。
- **没有灰度 / 回滚**。没有在线观测，谈不上灰度。

## 10. 回归

| 范围 | 结果 |
|---|---|
| `test_archive_wait.py` + `test_static_archive_policy.py` + 9 个上下文/归档套件 | **185 passed**（39.7s） |
| 前端全量（vitest） | **333 passed / 48 files**（21.9s） |
| 前端类型检查（`tsc -b`） | 干净 |
| 后端全量（`tests --ignore=tests/integration -p no:randomly`） | **1223 collected，22 failed，1201 passed**（10m51s） |

失败集合与 R2-02 / R2-03 基线**逐项一致**，无新增：

| 文件 | 数量 | 归因 |
|---|---|---|
| `test_cost_control.py` | 10 | 用户未提交的 `costs.py` 价格/预算链路 |
| `test_plan_security.py` | 3 | Windows 符号链接用例 |
| `test_m5_release_gates.py` | 2 | 同上（价格链路） |
| `test_real_evaluation.py` | 2 | 同上 |
| `test_evaluation_api.py` | 1 | 同上 |
| `test_golden_journey.py` | 1 | 同上 |
| `test_model_control.py` | 1 | 同上 |
| `test_routed_model_gateway.py` | 1 | 同上 |
| `test_startup_contract.py` | 1 | 同上 |

通过数比 R2-03 的记录（1181）多 **20**，两个后端测试文件的改动正好解释了这个差额：
`test_archive_wait.py` 是本轮新增（18 条），`test_static_archive_policy.py` 在 R2-03 回归
**之后**又补了 2 条（现在收集 46 条）。

证据：`outputs/r2-04-regression.txt`（stdout）与 `outputs/r2-04-regression.xml`（JUnit XML，
计数与失败清单取自 XML —— 本机有一个测试目录清理钩子在收尾时吞掉了 pytest 的汇总行，
所以这次不依赖 stdout）。

`tests/integration/*` 仍整体报错（Docker 不可用，`exit status 125`），环境问题。

## 11. 计划验收项对照

| 计划验收项 | 状态 |
|---|---|
| 时限有边界 | **离线钉住**：deadline 是挂钟；一次超时的 attempt 被 `asyncio.wait_for` 切断并报 `attempt_exceeded_deadline`；被切断的 Job 仍可回收。 |
| 等待可取消 | **离线钉住**：206ms 结束一次 2000ms 的等待，剩余 79% 未压缩；取消不回滚已提交 Episode；取消前的取消无噪音；取消的 Turn 不重跑。 |
| 任务不因一次超时永久不可用 | **离线钉住**：`restart` 用例 —— 超时 3051ms → 租约回收 → 下一轮正常归档，游标前进。 |
| 本地新增耗时 P95 ≤ 200ms | **离线测量**：最坏 44.5ms（unrouted 路径），routed 路径 27.7ms。 |
| 状态提示 ≤ 1 秒 | **仅后端一半**：最坏 43.9ms。传输与渲染未测。 |
| 测后台开/关的 P50/P95 | **已测**，两者数值一致。 |

计划同时写明的一条限制照旧成立，这里重复一遍以免被当成承诺：

> **相关原任务可能仍需等待摘要，不能承诺所有后续消息立即回答。**

源码快照：`outputs/snapshots/r2-04-src-20260919-150622.zip`（866 文件，3.9MB）。
本仓库的 git 对象库已被清空过两次（`git status` 报 `fatal: bad object HEAD`），
源码只靠工作区磁盘 + 快照保全，所以每个任务收尾都打一份。
