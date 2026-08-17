# Evaluation baselines

V1 deterministic cases are defined in [`../cases/v1.json`](../cases/v1.json) and executed by `python -m app.eval run --suite v1 --mode deterministic` from `backend/`.

Baseline JSON files are intentionally kept out of the initial product commit. Generate a local report, review it, and compare future reports with `python -m app.eval compare baseline.json latest.json`. Runtime results belong in the ignored `evals/results/` directory.
