import copy
import json
from pathlib import Path
import subprocess
import sys
import pytest

from app.research_judgment import validate_quality, correctness_summary


def decision(verdict="fail"):
    item = {"verdict": verdict, "issues": [] if verdict == "pass" else [
        {"quote": "增加40元", "source_ids": ["s1"], "reason": "平常外食未知，比较口径不同"}]}
    return {"left": copy.deepcopy(item), "right": copy.deepcopy(item), "winner": "tie", "reason": "同样错误"}


def test_both_wrong_is_not_a_pass_even_when_tied():
    result = validate_quality(decision(), {"left": "增加40元", "right": "增加40元"}, [["材料", "s1"]])
    summary = correctness_summary([{"baseline_correctness": result["left"], "candidate_correctness": result["right"]}])
    assert summary["both_failed"] == 1
    assert summary["candidate"]["pass_rate"] == 0


@pytest.mark.parametrize("change", ["missing", "empty_issues", "fake_quote", "fake_source", "winner", "non_object"])
def test_invalid_judgments_fail_closed(change):
    value = decision()
    if change == "missing":
        del value["left"]
    elif change == "empty_issues":
        value["left"]["issues"] = []
    elif change == "fake_quote":
        value["left"]["issues"][0]["quote"] = "模型未说过"
    elif change == "fake_source":
        value["left"]["issues"][0]["source_ids"] = ["s2"]
    elif change == "winner":
        value["winner"] = "left"
    else:
        value = []
    with pytest.raises(ValueError, match="INVALID_RESEARCH_QUALITY"):
        validate_quality(value, {"left": "增加40元", "right": "增加40元"}, [["材料", "s1"]])


def test_uncertain_and_legacy_unassessed_are_not_passes():
    summary = correctness_summary([{}, {"candidate_correctness": {"verdict": "uncertain"}}])
    assert summary["candidate"] == {"pass": 0, "fail": 0, "uncertain": 1, "unassessed": 1, "pass_rate": 0}


def test_only_correct_answer_must_win():
    value = decision()
    value["right"] = {"verdict": "pass", "issues": []}
    with pytest.raises(ValueError):
        validate_quality(value, {"left": "增加40元", "right": "无法比较"}, [["材料", "s1"]])
    value["winner"] = "right"
    assert validate_quality(value, {"left": "增加40元", "right": "无法比较"}, [["材料", "s1"]])["winner"] == "right"


def test_human_correctness_is_scored_without_preference_and_respects_blinding(tmp_path):
    report = {"records": [{"case_id": "a", "winner": "candidate",
        "candidate_correctness": {"verdict": "pass"}, "baseline_correctness": {"verdict": "fail"}}],
        "blind_records": [{"case_id": "a", "left_is_baseline": False}]}
    labels = [{"case_id": "a", "winner": None, "left_correctness": "fail", "right_correctness": "fail"}]
    for name, value in (("results", report), ("labels", labels)):
        (tmp_path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
    output = subprocess.check_output([sys.executable, str(Path(__file__).parents[1] / "scripts/score_research_human.py"),
        "--results", str(tmp_path / "results.json"), "--labels", str(tmp_path / "labels.json")], text=True)
    result = json.loads(output)
    assert result["correctness_agreement"] == 0.5
    assert result["correctness_disagreements"][0]["side"] == "left"
    assert result["human_calibration"] == "PENDING"
