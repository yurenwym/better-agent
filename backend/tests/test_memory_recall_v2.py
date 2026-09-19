import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from contextlib import nullcontext

import pytest


def test_v2_labels_and_entity_holdout():
    data = Path(__file__).resolve().parents[2] / "docs/evaluation/memory-recall-v2"
    memories = {m["id"]: m for m in json.loads((data / "memories.json").read_text(encoding="utf-8"))}
    cases = json.loads((data / "cases.json").read_text(encoding="utf-8"))
    assert len(memories) == 168 and len(cases) == 100
    entities = {split: {c["entity"] for c in cases if c["split"] == split and c["entity"] != "unknown"}
        for split in ("dev", "holdout")}
    assert not entities["dev"] & entities["holdout"]
    for case in cases:
        assert case["budget_bytes"] in (450, 750, 1500)
        assert bool(case["gold_memory_ids"]) != case["expected_abstention"]
        for mid in case["gold_memory_ids"]:
            m = memories[mid]
            assert m["status"] == "ACTIVE" and m["owner"] == case["owner"] and m["project"] == case["project"]
            assert mid not in case["forbidden_memory_ids"]


def test_default_rrf_has_separate_pin_binding_from_legacy(tmp_path):
    from app.db import Database
    from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore, MemoryConflict
    db = Database(tmp_path / "agent.db")
    with db.transaction() as c:
        c.execute("INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES ('t','t','u','now','now')")
    store = MemoryStore(db, tmp_path / "memory")
    entry = store.remember("u", "fact", "user", "", "PostgreSQL indexing", "m")
    request = MemoryContextRequest("u", "t", None, "PostgreSQL", model_invocation_id="same-call")
    legacy = MemoryContextProvider(db, ranking_strategy="legacy")
    result = legacy.select(request)
    assert result.revision_ids == (entry.revision_id,)
    scope = legacy.scopes.resolve_scope("t", "u")
    assert "ranking_strategy" not in legacy._binding(request, scope)
    default = MemoryContextProvider(db)
    assert default.ranking_strategy == "rrf"
    assert default.semantic_candidate_limit == default.lexical_candidate_limit == 50
    assert default._binding(request, scope)["ranking_strategy"] == "rrf"
    with pytest.raises(MemoryConflict, match="different context"):
        default.select(request)
    new_request = MemoryContextRequest("u", "t", None, "PostgreSQL", model_invocation_id="new-call")
    assert default.select(new_request).trace["ranking_strategy"] == "rrf"
    assert default.select(new_request).trace["retrieval_mode"] == "pin_hit"
    assert legacy.select(request).trace["retrieval_mode"] == "pin_hit"
    with pytest.raises(ValueError):
        MemoryContextProvider(db, ranking_strategy="invalid")


@pytest.mark.skipif(os.name != "nt", reason="Windows registry startup integration")
def test_startup_imports_explicit_embedding_reference_without_overriding_process(monkeypatch):
    from app.config import load_user_model_environment
    values = {"EMBEDDING_API_KEY_ENV": "CUSTOM_VECTOR_KEY", "EMBEDDING_MODEL": "registry-model",
        "EMBEDDING_BASE_URL": "https://vector.example/v1", "CUSTOM_VECTOR_KEY": "fake-only"}
    def query(_, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=1,
        OpenKey=lambda *args: nullcontext(1), QueryValueEx=query))
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "process-model")
    loaded = load_user_model_environment()
    assert "CUSTOM_VECTOR_KEY" in loaded
    assert os.environ["CUSTOM_VECTOR_KEY"] == "fake-only"
    assert os.environ["EMBEDDING_MODEL"] == "process-model"
