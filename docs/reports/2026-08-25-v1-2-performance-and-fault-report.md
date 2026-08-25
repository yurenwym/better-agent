# Better Agent V1.2 Performance and Fault Report

## Scope

This report covers the local single-user SQLite/WAL deployment. Model network latency and provider quality are excluded from non-model API measurements. All runs use synthetic data and the configured local data root.

## Reproducible commands

```powershell
cd D:\RAG\better
$env:PYTHONPATH='backend'
python scripts/benchmark_local.py
python -m pytest backend/tests/test_agent_tasks.py backend/tests/test_checkpoint.py backend/tests/test_events.py backend/tests/test_experience_observer.py
```

The benchmark prints JSON to stdout and does not write a result file. If a report is needed, redirect it to an ignored path under `evals/results/`.

## Latest local synthetic run

- 100 goal-creation operations
- P50: **47.552 ms**
- P95: **61.679 ms**
- Backend: **440 passed, 3 skipped**
- Frontend: **161 passed**, production build passed
- Live model smoke with the configured `LLM_API.txt`: **1 passed, 0 failed**
- `docker compose config`: passed; image build was not executable because Docker Desktop was not running (`com.docker.service` stopped)

## Fault matrix

| Fault | Expected protection | Evidence |
| --- | --- | --- |
| Worker crash | Lease expiry makes the task claimable again | `test_agent_tasks.py` |
| Late worker result | Lease epoch fencing rejects the artifact | `test_agent_tasks.py` |
| Duplicate request | Idempotency receipt returns the original result | API/evolution tests |
| Model timeout/rate limit | Gateway retry policy is bounded; auth failures stop | `test_model_gateway.py`, deterministic eval |
| SQLite write conflict | WAL + busy timeout + CAS/expected version | DB, plan and goal tests |
| Observer replay | Source event/lineage uniqueness prevents duplicate Experience | `test_experience_observer.py` |
| Canary safety failure | Challenger exposure atomically rolls back | `test_canary_assignment.py` |
| SSE reconnect | `Last-Event-ID` resumes after the committed sequence | conversation and trajectory tests |

## Interpretation

Use the benchmark output as an engineering signal, not a production SLO. Before claiming improvement, record machine, Python/Node versions, case count and whether a live model was used.
