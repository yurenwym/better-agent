import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

PATH = Path(__file__).resolve().parents[1] / "scripts/followup_intent_v3.py"
spec = importlib.util.spec_from_file_location("followup_intent_v3", PATH)
v3 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v3
spec.loader.exec_module(v3)


def intent(target="a", review="daily", initiation="proactive"):
    return dict(target=target, tracking="none", review=review, review_initiation=initiation,
                reminder="none", document="none", reason="test")


def case():
    return dict(id="test", targets=[{"id":"a"},{"id":"b"}], history=[], input="test",
                expected={"intents":[intent(),intent("b","none","not_applicable")]})


def row(intents):
    return dict(id="test", status="ok", prediction={"intents":intents})


def test_correct_and_order_independent():
    c = case()
    s = v3.score([c],[row(list(reversed(c["expected"]["intents"])))])
    assert s["probe_threshold_passed"] and s["exact_accuracy"] == 1
    assert s["behavior_acceptance"] == "not_evaluated"


def test_daily_wrong_target_is_fp_and_fn():
    s = v3.score([case()],[row([intent("a","none","not_applicable"),intent("b")])])
    assert s["daily"]["fp"] == s["daily"]["fn"] == 1
    assert s["cross_target_witnesses"] > 0 and s["proactive_escalations"] == 1


def test_extra_target_cannot_hide_in_field_accuracy():
    c = case()
    s = v3.score([c],[row(c["expected"]["intents"]+[intent("invented")])])
    assert s["fields"]["review"] == 1 and s["exact_accuracy"] == 0
    assert s["hallucinated_targets"] == s["daily"]["extra_target_fp"] == 1


@pytest.mark.parametrize("rows", [[],[{"id":"test","status":"error"}], [row([intent(),intent()])]])
def test_errors_and_missing_rows_remain_in_denominator(rows):
    s = v3.score([case()],rows)
    assert s["n"] == 1 and s["errors"] == 1 and s["daily"]["fn"] == 1


def test_proactive_escalation_when_frequency_is_correct():
    c = case()
    c["expected"]["intents"][0]["review_initiation"] = "user_initiated"
    s = v3.score([c],[row([intent(),intent("b","none","not_applicable")])])
    assert s["fields"]["review"] == 1 and s["proactive_escalations"] == 1
    assert not s["probe_threshold_passed"]


def test_prompt_allowlist_and_separate_protocol():
    c = case()
    c["expected"]["secret"] = "GOLD_SECRET"
    c["rationale"] = "SECRET_REASON"
    payload = json.dumps(v3.messages(c,v3.system_prompt()),ensure_ascii=False)
    assert "GOLD_SECRET" not in payload and "SECRET_REASON" not in payload
    assert "先在单独一行返回一个 JSON 控制头" not in payload


def test_strict_output_and_cross_field_rules():
    for invalid in [dict(intent(),review="event_once"),dict(intent(),review="none"),dict(intent(),extra=True)]:
        with pytest.raises(ValidationError):
            v3.Prediction.model_validate({"intents":[invalid]})


def test_dataset_coverage_and_split_isolation():
    cases = json.loads((v3.DATA/"cases.json").read_text(encoding="utf-8"))
    v3.validate_cases(cases)
    bad = copy.deepcopy(cases)
    bad[-1]["family"] = bad[0]["family"]
    with pytest.raises(ValueError,match="leaks"):
        v3.validate_cases(bad)


def test_duplicate_results_rejected():
    c = case()
    r = row(c["expected"]["intents"])
    with pytest.raises(ValueError):
        v3.score([c],[r,r])
