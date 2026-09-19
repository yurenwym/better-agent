import importlib.util
import json
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[1] / "scripts/memory_recall_eval.py"
spec = importlib.util.spec_from_file_location("memory_recall_eval", path)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_dataset_labels_are_scoped_and_split_is_frozen():
    memories = json.loads((evaluation.DATA / "memories.json").read_text(encoding="utf-8"))
    cases = json.loads((evaluation.DATA / "cases.json").read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in memories}
    assert len(by_id) == len(memories) == 22
    assert len({c["id"] for c in cases}) == len(cases) == 24
    assert sum(c["split"] == "dev" for c in cases) == 16
    for case in cases:
        assert case["expected_abstention"] == (not case["gold_memory_ids"])
        for mid in case["gold_memory_ids"]:
            memory = by_id[mid]
            assert memory["status"] == "ACTIVE"
            assert memory["owner"] == case["owner"]
            assert memory["project"] in (None, case["project"])
        assert not set(case["gold_memory_ids"]) & set(case["forbidden_memory_ids"])


def test_answer_requires_injected_evidence_not_just_a_guessed_fact():
    case = dict(expected_abstention=False, answer_contains_all=["cn-north-7"], gold_memory_ids=["s01"])
    answer = dict(value="cn-north-7", evidence_ids=["s01"])
    assert not evaluation.answer_score(case, answer, [])["correct"]
    assert evaluation.answer_score(case, answer, ["s01"])["correct"]
    assert not evaluation.answer_score(case, dict(value="cn-north-7", evidence_ids=[]), ["s01"])["correct"]
    assert evaluation.answer_score(dict(expected_abstention=True), dict(value="UNKNOWN", evidence_ids=[]), [])["correct"]


def test_fixture_and_actual_selector_are_reproducible_and_isolated(tmp_path):
    memories = json.loads((evaluation.DATA / "memories.json").read_text(encoding="utf-8"))
    cases = json.loads((evaluation.DATA / "cases.json").read_text(encoding="utf-8"))
    results = []
    for run in ("one", "two"):
        out = tmp_path / run
        out.mkdir()
        provider, mapping = evaluation.seed(out, memories)
        assert provider.ranking_strategy == "legacy"
        selected = []
        for case in cases:
            row = evaluation.retrieve(provider, mapping, case, case["query"], memories)
            assert row["injected_bytes"] <= 1500
            assert not row["forbidden_hits"]
            assert not set(row["selected_ids"]) & {"d01", "d02", "d03", "d04"}
            selected.append(row["selected_ids"])
        results.append(selected)
    assert results[0] == results[1]


def test_postgres_refuses_application_database_before_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_EVAL_DATABASE_URL", "postgresql://localhost/production")
    with pytest.raises(ValueError, match="dedicated"):
        evaluation.seed(tmp_path, [], postgres=True)
