"""Background archive entrypoint and conversation reference call boundaries."""
from __future__ import annotations

import json

import pytest

from app.memory_archive import ConversationArchiver, LiveEpisodeSummarizer
from app.memory_reference import resolve
from app.memory_v2 import MemoryStore
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_memory_archive import _completed_thread
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.asyncio
async def test_archive_worker_summary_uses_claim_owner_and_committed_snapshot(tmp_path, monkeypatch):
    db, thread = _completed_thread(tmp_path, owner_id="archive-owner")
    db, _, _ = _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="archive-owner")
    summary = {
        "synopsis": [{"text": "用户提出了一项要求。", "source_message_ids": ["fault-user"]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }
    observer = CommittedSnapshotTransport(db, "archive-owner", "CS-MA-01", _answer(json.dumps(summary, ensure_ascii=False)))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    archiver = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"), LiveEpisodeSummarizer(gateway), keep_messages=0)
    job_id = archiver.enqueue(thread.id, source_turn_id="fault-turn")
    result = await archiver.process(archiver.claim(job_id))

    assert result is not None
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "archive-owner"
    assert observer.observations[0]["snapshot_id"]
    assert gateway.current_call_context() is None


@pytest.mark.asyncio
async def test_reference_resolution_freezes_the_final_candidate_list(tmp_path, monkeypatch):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="memory-owner")
    reply = '{"动作":"补全后检索","目标实体":["SVC03"],"依据消息编号":["m2"]}'
    observer = CommittedSnapshotTransport(db, "memory-owner", "CS-MR-01", _answer(reply))
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
    history = [
        {"id": "m1", "role": "user", "content": "SVC02 每周维护。"},
        {"id": "m2", "role": "user", "content": "还有 SVC03。"},
    ]
    context = ModelCallContext(role="coordinator", purpose="resolve_memory_reference", owner_id="memory-owner",
                               runtime_bundle_id=bundle.id)

    result = await resolve("后者呢？", history, mode="hybrid", gateway=gateway, context=context)

    assert result.action == "resolved"
    assert result.entities == ("SVC03",)
    assert len(observer.observations) == 1
    assert observer.observations[0]["owner_id"] == "memory-owner"
