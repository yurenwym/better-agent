# DeepSeek 上下文预算修复补测：16 轮真实对话

日期：2026-09-20。真实对话完成于北京时间 22:03，22:05 只读复核后台调用。

结论：补齐预算契约保存校验、逐次请求计量证据和真实澄清续接验收后，相关测试 **350 passed**；重启项目后的 **16/16 轮真实模型对话完成**，无上下文超限，验收脚本返回 `passed`、`acceptance_errors=[]`。

## 1. 本次补齐内容

- `backend/app/model_admin.py`：保存 profile 时校验 A 级 admitted limit、计数证据和协议预算；拒绝冲突的 soft/context 上限；协议预算的 `declared` 由服务端推导，校验最终输入预算。未声明 validation tier 的旧配置保留兼容行为。
- `backend/app/model_control.py`、`backend/app/model_gateway.py`：路由和单模型调用在发送前记录 `model.request.estimated`，绑定 invocation、attempt、profile 版本，记录最终适配器 payload 的规范化 JSON 字节数、摘要及估算输入。事件不保存原文或凭据。
- `backend/scripts/deepseek_budget_acceptance.py`：升级为 16 轮，强制验证真实 ask 表单及续接；按同一个 attempt 配对估算与供应商 usage，缓存输入计入上下文。分类、安全评审、主回答及重试各自保留，不能合并计算计数误差。缺少真实续接或配对用量即验收失败。
- 新增计量配对、验收失败条件和发送 payload 一致性回归测试，并修正旧报告的“低估 43%”结论。未增加自动校准或修改现有估算公式。

## 2. 真实调用与轮数

- 服务：`http://127.0.0.1:8000`，重启后 PID **21276**，readiness `READY`。
- 模型：**deepseek-flash**；本批全部真实调用端点为 `https://api.deepseek.com`。
- 活动 profile：`model_profile_version_9ad09ba106bc4b80914811bcc38dbdee`。
- 窗口 **1,000,000**，来源 `official-default`；输出预留 **8,192**；counter `deepseek-text-estimate-v1`，模式 `estimate`，公式仍为 `ceil(UTF-8 bytes / 4)`。
- 对话：`thread_b1103c0b8955422f83835dfd5910c089`，标题“预算修复验收”。
- **16 个用户对话轮次**；第 2 轮的表单回答及续接不额外算一轮。
- 后台收尾复核：17 次主对话调用、16 次分类、1 次澄清问题生成、16 次安全评审，共 **50 次真实模型调用全部 SUCCEEDED**。这些调用数不作为用户对话轮数。
- `results.json` 生成瞬间最后一次异步安全评审仍为 STARTED；后续 `postcheck.json` 确认其成功，保留两个时间点的原始状态。

## 3. 每轮主回答计量

下表仅取每轮最后一个主回答 invocation 的 selected attempt。第 2 轮取回答表单后的续接请求；前置请求、ask、分类与评审的逐次用量保留在 JSON。误差 = `(估算 - 实际) / 实际 × 100%`；负值表示低估。字节数是最终适配器 payload 的规范化 JSON 字节数，不是 HTTP 报文总大小。

| 轮 | 场景 | payload bytes | 估算输入 tokens | 实际输入 tokens（含缓存） | 误差 | 结果 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | 你好 | 20,118 | 5,030 | 5,210 | -3.45% | COMPLETED |
| 2 | 真实澄清表单及续接 | 28,450 | 7,113 | 7,193 | -1.11% | COMPLETED |
| 3 | 同行人、节奏调整 | 35,083 | 8,771 | 9,149 | -4.13% | COMPLETED |
| 4 | 时间与目的地 | 41,442 | 10,361 | 11,025 | -6.02% | COMPLETED |
| 5 | 细化第一天 | 48,242 | 12,061 | 13,018 | -7.35% | COMPLETED |
| 6 | 雨天备选 | 56,226 | 14,057 | 15,337 | -8.35% | COMPLETED |
| 7 | 控制预算 | 66,088 | 16,522 | 18,159 | -9.01% | COMPLETED |
| 8 | 整理攻略 | 67,132 | 16,783 | 18,204 | -7.81% | COMPLETED |
| 9 | 43,230 字符长输入 | 219,746 | 54,937 | 57,056 | -3.71% | COMPLETED |
| 10 | 补充交通 | 222,126 | 55,532 | 57,669 | -3.71% | COMPLETED |
| 11 | 放缓行程 | 239,402 | 59,851 | 62,837 | -4.75% | COMPLETED |
| 12 | 一句话概括 | 259,041 | 64,761 | 68,741 | -5.79% | COMPLETED |
| 13 | 复述已确认约束 | 259,477 | 64,870 | 68,845 | -5.77% | COMPLETED |
| 14 | 待核实信息 | 260,111 | 65,028 | 69,011 | -5.77% | COMPLETED |
| 15 | 连续两天下雨 | 264,508 | 66,127 | 70,234 | -5.85% | COMPLETED |
| 16 | 交付七天行程表 | 267,940 | 66,985 | 71,218 | -5.94% | COMPLETED |

第 13 轮正确复述 2026-10-15 至 10-21、情侣两人、人均 4000 元、慢节奏，并保留“大交通是否包含在预算内尚待确认”。第 16 轮交付七天表格，包含活动、交通和雨天备选。完整问题及回答均在 `results.json`。

## 4. 原故障与澄清续接证据

**原请求精确重放（模拟传输）**：读取原生产历史，重建原始 25,408-byte 请求，保留全部消息与 11 个工具。在受控 32,768 窗口下估算为 6,352 tokens，input_limit=23,348、packing_limit=23,298，通过最终网关并发生 1 次模拟传输。此项用于证明修复不只依赖扩大窗口，不能算真实模型调用。证据：`original-replay.json`。

**真实澄清续接**：第 2 轮记录 1 次 `ask.requested` 和 1 次 `ask.answered`，填写日期、同行人、预算与偏好后完成续接。

- ask：`ask_b97ea78c5a9342ca81bdcf356c6e33d4`
- 父轮：`turn_af569a7f8b1544388a5d3f1ae4170fb3`
- 续接：`turn_0dc43b385856406db75d90691e1687bf`
- 续接预检：28,324 bytes → 7,081 units；最终适配器请求：28,450 bytes → 7,113 tokens；供应商实际输入：7,193 tokens。

整个对话未发生上下文超限事件或意外业务审批交互。

## 5. 自动测试

在 `backend` 下运行以下相关套件，结果 **350 passed in 136.50s**；此前定向测试 104 项通过。两者有重叠，不相加。

```powershell
python -m pytest -q -p no:cacheprovider `
  tests/test_deepseek_context_budget_fix.py tests/test_deepseek_budget_acceptance.py `
  tests/test_model_capacity.py tests/test_capacity_review_fixes.py tests/test_startup_contract.py `
  tests/test_context_hot_window.py tests/test_context_budget_contract.py tests/test_context_wire_gate.py `
  tests/test_context_packing_limit.py tests/test_static_archive_policy.py tests/test_memory_archive.py `
  tests/test_archive_continuation.py tests/test_conversation_worker.py tests/test_routed_model_gateway.py `
  tests/test_model_control.py tests/test_model_gateway.py tests/test_chat_goal_tools.py
```

## 6. 结论边界与旧结论更正

- 旧批次长输入的 85,349 tokens 是分类 32,166 + 主回答 52,579 + 安全评审 604 的合计，不能与主请求估算 48,564 比较。撤回“低估 43%”；旧预检与最终 payload 也不完全相同，不能直接用差值认定误差。
- 本批对相同 attempt 配对后，16 轮最终主回答低估 **1.11%–9.01%**。这仅描述本批样本，不能作为所有文本的误差保证；估算器仍不是严格上界。
- 最大实际主请求输入 **71,218 tokens**。本次没有逼近 1M 边界，也没有验证供应商超限返回；不能把窗口来源升级为 verified。
- 本次验证容量链路、澄清续接和多轮可用性，不是旅游事实准确性验收，回答中的价格、营业时间等仍需独立核实。
- readiness 的 `network_verified=false` 表示该检查自身不探测网络，真实连接证据来自上述调用。备用 Qwen profile 仍有价格快照缺失提示；本批全部使用 DeepSeek，不声称备用模型经过验收。

## 7. 证据与复跑

- 原始逐轮记录：`docs/evaluation/deepseek-budget-retest-2026-09-20/results.json`
- 原故障模拟重放：`docs/evaluation/deepseek-budget-retest-2026-09-20/original-replay.json`
- 调用收尾与服务只读检查：`docs/evaluation/deepseek-budget-retest-2026-09-20/postcheck.json`
- 历史报告及更正：`docs/acceptance/deepseek-context-budget-fix-2026-09-20.md`

项目已启动且当前环境配置好 `DATABASE_URL` 时，从仓库根目录运行，输出目录须尚不存在：

```powershell
python backend/scripts/deepseek_budget_acceptance.py --out docs/evaluation/deepseek-budget-next-run
```

脚本会调用真实模型并产生供应商费用，驱动应用的实际会话、worker、网关和数据库。
