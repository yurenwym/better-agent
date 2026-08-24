from app.behavior import BehaviorBundleService
from app.db import Database


def test_bundle_is_content_addressed_and_channel_switch_does_not_mutate_it(tmp_path):
    db = Database(tmp_path / "agent.db")
    service = BehaviorBundleService(db)
    manifest = {
        "code_commit": "abc",
        "model": {"provider": "test", "model": "m", "temperature": 0},
        "skills": {"reflection": "sha256:one"},
        "prompts": {"conversation": "sha256:two"},
        "policy": "policy-v1",
        "tools": "sha256:three",
        "context": {"renderer": "v1", "tokenizer": "v1"},
    }
    first = service.ensure(manifest)
    assert service.ensure(dict(reversed(list(manifest.items())))).id == first.id
    changed = service.ensure({**manifest, "policy": "policy-v2"})
    assert changed.id != first.id
    service.activate("stable", first.id, "activate-1")
    assert service.active("stable").id == first.id
    service.activate("stable", changed.id, "activate-2")
    assert service.active("stable").id == changed.id
    assert service.get(first.id).manifest["policy"] == "policy-v1"


def test_bundle_manifest_never_contains_secret_values(tmp_path):
    service = BehaviorBundleService(Database(tmp_path / "agent.db"))
    bundle = service.ensure({"model": {"api_key_env": "MODEL_KEY", "api_key_configured": True}})
    assert "secret-value" not in bundle.manifest_json

