# R1 实盘验收：真实 DeepSeek Flash 上的预算与压缩

**批次** `R1-LIVE-20260914-001` · **结论** PASSED · 4 次真实网络调用 / 11.1s
**脚本** `backend/scripts/r1_live_acceptance.py`（默认无网络预检；`--execute` + `R1_LIVE_APPROVED=1` 才发起真实调用）
**报告** `outputs/r1-live-acceptance.json`

## 0. 为什么必须做这一轮

`app/context_replay.py` 的离线回放用的是**确定性归档器**，不是真实 LLM 摘要器。它证明了打包层的优先级正确，
但证明不了两件事：

1. 真实摘要器会不会漏掉关键事实；
2. 真实 provider 会不会接受我们判定"装得下"的请求。

第 2 条在本轮**第一次运行就失败了**，抓出一个真实缺陷（见 §2）。

## 1. 冻结配置

| 项 | 值 |
|---|---|
| profile | `openai_compatible` / `deepseek-flash` / `https://api.deepseek.com` |
| 窗口 / 输出预留 | 32768 / 8192 |
| `H`（输入预算，legacy tier） | **23348** |
| `min_turns` | 5 |
| 摘要优先级 | post-R1 = **60**，pre-R1 = **20**（两臂同预算，只差优先级） |
| 适配器开销预留 | 常数 199 + 每消息 0 + 每工具 0 |

## 2. 本轮抓出的真实缺陷（P0）

**打包器与 wire gate 用的不是同一把尺子。**

- `pack_messages_newest` 只数 `{"messages","tools"}`；
- 适配器随后把这份 payload 包进信封（`model` / `stream` / `stream_options` / `temperature` /
  `max_tokens` / `thinking`），并交给 `assert_provider_payload_fits` 用**成品请求体**再数一次。

第一次实盘运行的结果：

```
provider payload requires 23411 conservative budget units but the input budget is 23348
[keys=['max_tokens','messages','model','stream','stream_options','temperature','thinking','tools']]
```

即：按 `H` 打满的请求，在 gate 上超了 63 单位被拒。生产路径有 **4 处**这样打包
（`conversation.py::_history` 的 `pack_recent`，`live_model.py` 的 3 处 `pack_messages_newest`），
所以这是一个"极少触发但一旦触发就是整轮失败"的真实缺陷。

### 修复

1. `model_gateway.py` 把三个适配器的 payload 构造抽成**纯函数**（`_openai_payload` /
   `_anthropic_payload` / `_gemini_payload`）并导出 `provider_payload(profile, request)`——
   测量用真实适配器，而不是在别处重写一遍它的字典。
2. `token_budget.py` 新增 `envelope_overhead()` / `packing_limit()`：**从适配器实测**开销，
   分解为「常数 + 每消息斜率 + 每工具斜率」，选择器按 `packing_limit` 打包。
3. `HotWindow` 携带 `envelope_units` / `per_message_units` / `per_tool_units`，暴露
   `packing_limit(message_count, tool_count)`。
4. 4 处打包点全部改走 packing limit。

### 实测的三个适配器（同一 32k profile）

| 适配器 | 常数 | 每消息 | 100 消息时的实际差值 | 预留 | 余量 |
|---|---|---|---|---|---|
| `openai_compatible` | 143 | 0 | 143 | 199 | **+56** |
| `anthropic` | 24 | 0 | 24 | 77 | **+53** |
| `gemini` | 64 | **9** | 968 | 973 | **+5** |

关键点：**Gemini 的开销随消息数线性增长**（每条消息多包一层 `parts`）。
在把它测出来之前，固定预留的缺口在 400 条消息时达到 **3604 单位**——一个常数预留永远补不上。
这也是为什么预留做成"常数 + 实测斜率"而不是一个固定数字。

回归测试 `backend/tests/test_context_packing_limit.py`（28 个）钉住这条不变量，包括
"按 `H` 打包必被拒 / 按 packing limit 打包必通过"，以及"预留不随会话增长而漂移"。

## 3. 实盘结果

### 用例 1 — 边界不被误拒（`boundary`）

把请求填充到 wire gate **恰好等于 H**（`gate_units = 23348 = H`），真实发出：

- **被接受**，HTTP 成功，回复非空，延迟 2.53s。
- 意义：字节上界是**真上界**——它只会保守，不会把 provider 本来接受的请求拒掉。

### 用例 2 — 真实摘要器（`summariser`）

把 67 轮 / 18073 字节的历史交给真实 `deepseek-flash` 归档，得到 984 字节结构化摘要。
摘要保留了全部 7 个事实，且**没有把被覆盖的旧值写成当前值**
（synopsis 里出现"最初预算5000元，后改为8000元"，`facts.预算 = 8000元`）。

这填补了离线回放 §6 明确记录的缺口："回放用的是确定性归档器……若真实摘要器漏掉某个事实，本回放不会发现"。
本轮实测：**真实摘要器没有漏。**

### 用例 3 — 真实行为差异（`probe`）

同一预算、同一摘要，只改优先级，让**真实模型**从打包后的请求里回答 7 个探针：

| 探针 | post-R1（优先级 60） | pre-R1（优先级 20） |
|---|---|---|
| 硬约束 | ✅ 总花费不能超过预算 | ❌ 不超预算（猜测） |
| 最新纠正值 | ✅ 8000元 | ❌ 不知道 |
| 已确认参数 | ✅ 3月12日 | ❌ 不知道 |
| 工具结果 | ✅ 553元 | ❌ 不知道 |
| 失败尝试 | ✅ A方案 | ❌ 不知道 |
| 未解问题 | ✅ 住宿还没决定 | ❌ 不知道 |
| 最近几轮 | ✅ 4人 | ✅ 4 人 |
| **合计** | **7/7** | **1/7** |

打包层离线包含性检查同步为 **7/7 vs 1/7**（`summary_in_payload`：post=True，pre=False）。
机制指标（保留字节数、归档次数）在两臂之间几乎没有区别——**只有真实行为暴露了差异**。

## 4. 字节上界的真实代价（本轮首次测得）

以前只能拿字节数比字节数。真实调用返回 provider 自己的 prompt token 数，第一次有了诚实的分母：

| 载荷 | counted_units | provider input tokens | 保守倍数 | H 的真实 token 利用率 |
|---|---|---|---|---|
| 边界（ASCII 填充，最密集） | 23348 | 2902 | **8.05×** | **12.4%** |
| post-R1 探针（真实中文对话） | 23013 | 4565 | **5.04×** | **19.6%** |

结论：`Utf8UpperBoundTokenCounter` 在这条链路上只用了 `H` 的 **12%–20%**。
即真实可用上下文比模型实际能力小约 **5 倍**。

这是**诚实的成本**，不是缺陷：上界保证"绝不超窗"，代价是利用率低。
但如果要提升有效上下文，这里是唯一的大头——属于 R3/R4 的候选，不在 R1/R2 范围内。

## 5. 仍然没有证明的东西

- **在线 P50/P95 时延、归档次数、费用**：本轮只测了单次延迟（2.5–3.4s），没有统计分布。
- **灰度/canary**：未实现。
- **真实 tokenizer**：利用率是用 provider 返回的计数反推的，项目内仍没有本地 tokenizer。
- **Gemini / Anthropic 适配器**：预留已实测覆盖（§2 表），但这两个适配器没有可用凭据，
  从未跑过真实调用。wire gate 仍是它们的最终权威。
- **超长会话下的归档行为**：本轮 fixture 是 67 轮归档 + 220 轮填充，没有跑数小时的真实会话。

## 6. 复现

```bash
cd backend
# 无网络预检
R1_LIVE_APPROVED=1 python scripts/r1_live_acceptance.py --output ../outputs/r1-live-acceptance.json
# 真实批次（会花钱；上限 6 次调用 / 300 秒）
R1_LIVE_APPROVED=1 python scripts/r1_live_acceptance.py --output ../outputs/r1-live-acceptance.json --execute
```

凭据来自 `LLM_AP_PATH`，默认 `D:\Users\王一鸣\Desktop\直到尽头\LLM_AP.txt`
（与 `app/eval.py::DEFAULT_LLM_AP` 一致）。密钥不会被打印或写入报告。
