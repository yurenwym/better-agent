"""Compare genuine human blind labels against a frozen DEV Judge result; no model calls."""
import argparse
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--results", type=Path, required=True)
parser.add_argument("--labels", type=Path, required=True)
args = parser.parse_args()
report = json.loads(args.results.read_text(encoding="utf-8"))
labels = json.loads(args.labels.read_text(encoding="utf-8"))
judgments = {row["case_id"]: row for row in report["records"]}
mapping = {row["case_id"]: row["left_is_baseline"] for row in report["blind_records"]}
seen, pairs, disagreements, correctness_pairs, correctness_disagreements = set(), [], [], [], []
for row in labels:
    identity = row["case_id"]
    if identity in seen or identity not in judgments:
        raise ValueError("duplicate or unknown human case")
    seen.add(identity)
    for side in ("left", "right"):
        human_correctness = row.get(side + "_correctness")
        if human_correctness is None:
            continue
        if human_correctness not in {"pass", "fail", "uncertain"}:
            raise ValueError("human correctness must be pass/fail/uncertain or null")
        arm = "baseline" if (side == "left") == mapping[identity] else "candidate"
        model_correctness = judgments[identity].get(arm + "_correctness", {}).get("verdict", "unassessed")
        correctness_pairs.append((human_correctness, model_correctness))
        if human_correctness != model_correctness:
            correctness_disagreements.append({"case_id": identity, "side": side,
                "human": human_correctness, "judge": model_correctness, "reason": row.get("reason", "")})
    if row["winner"] is None:
        continue
    if row["winner"] not in {"left", "right", "tie"}:
        raise ValueError("human winner must be left/right/tie or null")
    human = "tie" if row["winner"] == "tie" else (
        "baseline" if (row["winner"] == "left") == mapping[identity] else "candidate")
    judge = judgments[identity]["winner"]
    pairs.append((human, judge))
    if human != judge:
        disagreements.append({"case_id": identity, "human": human, "judge": judge, "reason": row.get("reason", "")})
count = len(pairs)
agreement = sum(human == judge for human, judge in pairs) / count if count else None
human_counts, judge_counts = Counter(x[0] for x in pairs), Counter(x[1] for x in pairs)
chance = sum(human_counts[key] * judge_counts[key] for key in {"baseline", "candidate", "tie"}) / count**2 if count else None
print(json.dumps({"labeled": count, "total": len(judgments), "agreement": agreement,
    "cohen_kappa": (agreement - chance) / (1 - chance) if count and chance < 1 else None,
    "human_calibration": "COMPLETE" if count == len(judgments) and len(correctness_pairs) == 2 * len(judgments) else "PENDING",
    "correctness_labeled": len(correctness_pairs),
    "correctness_agreement": sum(a == b for a, b in correctness_pairs) / len(correctness_pairs) if correctness_pairs else None,
    "correctness_disagreements": correctness_disagreements,
    "disagreements": disagreements}, ensure_ascii=False, indent=2))
