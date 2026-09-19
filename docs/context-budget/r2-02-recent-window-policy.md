# R2-02：近期窗口的 N/R 双约束

## 1. 两个约束各管什么

之前只有 `min_turns`（N），它是**轮次数下限**：最新的 N 轮无条件带上，
所以"能留下几轮"不再取决于某一轮恰好多大。

但只有下限有一个后果：**近期对话可以吃掉整个预算**，把摘要挤出去。
而摘要正是承载早期事实的那条通道——R1 的实盘验收已经量化过这一点：
同样预算下丢掉摘要，真实模型从 7/7 掉到 1/7。

R2-02 因此加上 `R`（**字节上限**），并规定释放顺序。

| | 名字 | 类型 | 作用 |
|---|---|---|---|
| N | `history_min_turns` | 轮次数下限 | 最新 N 轮是窗口的属性，不是尽力而为 |
| R | `recent_window_bytes` | 字节上限 | 近期窗口最多占 R，剩下的留给摘要/技能/目标/计划 |

`R = min(ceiling, floor(ratio × H))`；R2-03 的静态目标 `T` 与已压缩前缀 `P` 就位后，
再叠加 `max(0, T - P)` 这一项。R2-03 未启用时该项不存在——与方案一致。

32k profile（`H = 23348`）下：`R = min(16384, 5837) = 5837`。

## 2. 释放顺序

被保护的近期后缀超过 `R` 时，**从最老的近期轮次开始释放保护**：

- 最新的轮次始终保留，永远不会因为"超过 R"被丢掉；
- 释放不会低于 1 轮；
- 释放**不会跳过更新的轮次去回填更老的小轮次**——后缀始终连续。

释放不是静默的：通过 `on_degraded` 上报，落到 `context.window_degraded` 事件里，
带上 `recent_window_bytes` / `min_turns` / `input_limit` / `packing_limit`。

## 3. 修掉的一个真实设计漏洞

第一版实现只让 `R` 约束**被保护的后缀**，然后字节预算的扩展循环又把窗口填回硬上限——
**R 实际上没有为摘要预留任何空间**，正是它要防止的那个失败。

测试 `test_the_recent_ceiling_bounds_the_expansion_too` 钉住这一点：
`R` 必须同时约束扩展，否则窗口会重新填满。

## 4. 明确不做的两件事

- **全量装得下时不裁原文**。整段 transcript 若已装进硬上限，说明摘要也装得下，
  此时因 `R` 裁掉原文是纯损失。早退分支直接返回原文。
- **单轮超过硬上限时如实降级**。判定用硬上限而不是 `R`：
  一轮很大不应仅因为它超过"窗口份额"就被丢掉。降级通过 `on_degraded` 上报。

## 5. 落点

| 位置 | 改动 |
|---|---|
| `token_budget.py` | `recent_window_ratio()` / `recent_window_ceiling()` / `recent_window_budget()`；`HotWindow.recent_window_bytes` |
| `transcript.py` | `pack_recent(..., recent_budget=)`：释放循环 + 扩展受 `min(token_budget, R)` 约束 |
| `conversation.py` | `_history` 传入 `window.recent_window_bytes` |
| `model_gateway.py` | `ModelProfile.recent_window_bytes` / `recent_window_ratio` + `public_view` |
| `db.py` | 迁移 40：两列 + 重建 `model_profile_versions_frozen`；`POSTGRES_SCHEMA_HEAD = 20260918_0018` |
| `alembic` | `20260918_0018_recent_window_policy.py` |
| `model_admin.py` / `model_control.py` / `config.py` | 与 `history_min_turns` / `compact_ratio` 同一条读写与 env 通路 |
| `tests/test_recent_window_policy.py` | 16 项 |

两个新旋钮必须能过数据库往返，理由和迁移 38 一样：
**不能过数据库的策略值不是策略，只是多绕了一步的默认值**。
已实测：写入 `4096 / 0.5` → 读回 `4096 / 0.5`；DB 装载的 profile 把 `R` 从默认 5837 改成 4096。

## 6. 未做

- R2-03 的静态目标 `T` / 前缀 `P` 尚未实现，所以 `R` 目前只有前两项。
  `recent_window_budget(..., static_target=, static_prefix=)` 的接口已经就位并有测试。
- `R` 的取值只做了公式与语义验证，没有做在线质量对比（需要 R2-03 之后一起测）。
