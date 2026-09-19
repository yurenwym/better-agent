# M5 真实评测与灰度预算单（草案）

状态：阶段预检入口已准备；真实批次仍待用户明确批准；本文不授权执行。

预检入口：`backend/scripts/m5_live_acceptance.py`。默认只写出无网络 `NOT_AUTHORISED` 报告；执行前必须先有 M4 三轮真实通过证据、脱敏候选与固定 DEV/HOLDOUT/SAFETY 套件、四个独立 profile/价格快照绑定及人工 Canary 批准。全量发布始终不由脚本自动执行。

## 目的与范围

验证自进化的真实故障回放、paired evaluation、留出集隔离、权限/正文删除、零增益拒绝晋升、预算退避、审批失效、角色级灰度绑定、样本门槛、自动回滚和重启恢复。M5 不自动全量晋升。

## 调用与预算边界

- 先执行脱敏故障回放和 DEV/HOLDOUT/SAFETY 固定套件；候选只允许修改受影响角色的提示词部分。
- 每个候选最多 60 个评测样本（baseline/candidate 配对及 quality/safety judge），候选不超过 1 个；总 model attempts 不超过 300，最长 60 分钟。
- 评测、judge 和灰度调用必须绑定不可变 runtime bundle、prompt digest、tool/context digest 和实际 `model_price_snapshot`；按最坏输入/输出上界配置共享预算，无法可靠计价则网络前拒绝。
- Canary 最低样本门槛沿用系统默认 20 个 challenger + 20 个 champion；仅在人工批准后启动受限灰度。任何安全失败、权限变化、样本不足、零增益、预算超限或审批绑定变化立即拒绝/回滚。

## 证据与禁止事项

保留问题谱系、候选 digest、评测报告、权限 diff、目标 bundle、样本分区、实际 prompt digest、灰度命中和回滚/重启事件。M1-M4 真实结果不能充当 M5 候选效果证据；未批准前不得启动候选生成、真实评测、Canary 或发布。

## 待批准事项

执行前必须确认候选来源、套件版本、模型/profile、价格快照、总调用上限、最坏费用、样本门槛和人工灰度批准人。全量发布始终需要另行人工决定。
