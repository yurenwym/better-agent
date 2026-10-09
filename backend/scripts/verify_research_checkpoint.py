"""Verify a saved first response is reused; prohibit any new provider send."""
import argparse
import json
from pathlib import Path
import os
from app.db import Database
from app.costs import CostService
from app.model_control import ModelControlStore, RoutedModelGateway
from app.research_replay import ResearchEvaluationRunner
from app.research_judge_checkpoint import JudgeCheckpoint, JudgeCheckpointError

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
args = parser.parse_args()
report = json.loads((args.directory / "results.json").read_text(encoding="utf-8"))
checkpoint = JudgeCheckpoint(args.directory / "judge-checkpoint.sqlite3")
with checkpoint.connect() as connection:
    rows = connection.execute("SELECT request,state FROM calls ORDER BY rowid").fetchall()
assert [row[1] for row in rows] == ["RECEIVED", "STARTED"]
requests = [json.loads(row[0]) for row in rows]
case_id = requests[0]["case_id"]
case = next(c for c in json.loads((args.directory / "frozen-input.json").read_text(encoding="utf-8"))["cases"] if c["id"] == case_id)
answers = {r["slot"]: json.loads(r["request"]["messages"][1]["content"])["answer"] for r in requests}
db = Database(os.environ["RESEARCH_DEV_DATABASE_URL"], workspace=args.directory / "verify-workspace")
control = ModelControlStore(db, costs=CostService(db))
sent = []
async def forbidden(*args, **kwargs):
    sent.append(True)
    raise AssertionError("unexpected provider send")
runner = ResearchEvaluationRunner(None, control, gateway_factory=lambda _: RoutedModelGateway(db, control, execute_attempt=forbidden), judge_checkpoint=checkpoint)
try:
    with db.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
    try:
        runner.judge(report["config"])({"case": case, "baseline": answers["left"], "candidate": answers["right"]})
        raise AssertionError("unresolved request should stop")
    except JudgeCheckpointError as exc:
        assert "UNRESOLVED_CALL" in str(exc)
    with db.connection() as connection:
        after = connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
    assert not sent and before == after and len(runner.bindings) == 1
    print(json.dumps({"saved_response_reused": True, "new_provider_sends": 0, "new_attempts": after-before}))
finally:
    db.close()
