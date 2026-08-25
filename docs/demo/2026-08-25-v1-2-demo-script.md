# Better Agent V1.2 Demo Script

This script uses only local, synthetic data. Do not paste API keys, private notes, SQLite files, or evaluation results into the recording.

## 3–5 minute flow

1. Start the app from `D:\RAG\better` with `python scripts/start.py` and open `http://127.0.0.1:8000`.
2. In a new conversation, enter a complex comparison request. The model may return a bounded `start_expert` decision; the conversation shows a readable expert progress card and the trajectory page shows committed events.
3. Open `/workspace` to show one goal workspace: current phase, next action, plan, execution and growth lineage.
4. Open `/plans`, select the generated plan, and show the rendered document. Open `/today` and complete one action; show the durable action status.
5. Return to the conversation and show the daily review/adjustment path. Explain that accepting an adjustment creates a new version and keeps the previous version auditable.
6. Open `/growth`. Show the separate “我的成长档案” and “Agent 改进” sections. On a Canary card, point out the two cohort counts, safety failures and the explicit “cannot promote yet” gate.
7. Open the trajectory page. Explain that the UI shows status, tools, checkpoints and metrics, while raw control JSON and hidden reasoning stay server-side.

## Close

The value proposition is one user-facing agent with a durable goal loop, bounded experts, append-only evidence, isolated evaluation, approval, Canary and rollback. The run can be cancelled and resumed; every claim shown in the UI has a stored source event.
