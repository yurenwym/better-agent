import asyncio
import json
from datetime import datetime, timezone

import pytest
from app.conversation import ConversationService
from app.db import Database
from app.memory_archive import ArchiveLeaseLost, ConversationArchiver, ManagedArchiveWorker
from app.memory_v2 import MemoryStore

async def valid_summary(payload):
 ids=[event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
 item={"text":"summary","source_message_ids":[ids[0]]}
 return {"synopsis":[item],"topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}

@pytest.mark.asyncio
async def test_archive_retry_is_scoped_and_keeps_coverage_until_commit(tmp_path):
 db, thread = _completed_thread(tmp_path,owner_id="local-user")
 async def invalid(payload): return "invalid"
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),invalid,keep_messages=0,max_attempts=1)
 await arch.archive_thread(thread.id)
 state=arch.status(thread.id,"local-user")
 job=state["jobs"][0]
 assert job["status"] == "DEAD_LETTER" and state["archived_through_seq"] == 0
 with pytest.raises((PermissionError,ValueError,KeyError)):
  arch.retry(thread.id,"other-user",job["id"],job["updated_at"])
 assert arch.retry(thread.id,"local-user",job["id"],job["updated_at"])["status"] == "QUEUED"
 assert arch.retry(thread.id,"local-user",job["id"],job["updated_at"])["status"] == "QUEUED"
 arch.summarizer=valid_summary
 assert await arch.process(arch.claim(job["id"])) is not None
 assert arch.status(thread.id,"local-user")["archived_through_seq"] > 0

@pytest.mark.asyncio
async def test_old_retry_cannot_restart_a_new_failure(tmp_path):
 from app.memory_archive import ArchiveError
 db,thread=_completed_thread(tmp_path,owner_id="local-user")
 async def invalid(payload):return "invalid"
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),invalid,keep_messages=0,max_attempts=1)
 await arch.archive_thread(thread.id)
 job=arch.status(thread.id,"local-user")["jobs"][0]
 arch.retry(thread.id,"local-user",job["id"],job["updated_at"])
 await arch.process(arch.claim(job["id"]))
 with pytest.raises(ArchiveError,match="changed"):
  arch.retry(thread.id,"local-user",job["id"],job["updated_at"])

def test_proactive_archive_uses_lower_hot_window_without_mutating_budget(tmp_path):
 from app.token_budget import DEFAULT_TOKEN_COUNTER
 db,thread=_completed_thread(tmp_path)
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary)
 transcript=arch.transcripts.build(thread.id)
 cost=DEFAULT_TOKEN_COUNTER.count_text(json.dumps(transcript.turns[0].canonical(),ensure_ascii=False,sort_keys=True))
 arch.keep_tokens=cost+1
 assert not arch._select_archive_prefix(transcript).turns
 assert arch._select_archive_prefix(transcript,proactive=True).turns
 assert arch.keep_tokens == cost+1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["prompt_version", "tokenizer_version"])
async def test_archive_old_version_is_rejected_before_model_and_preserves_source(tmp_path, field):
 db,thread=_completed_thread(tmp_path,owner_id="local-user")
 async def never_called(payload): raise AssertionError("old versions must not call the model")
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),never_called,keep_messages=0)
 job_id=arch.enqueue(thread.id)
 with db.transaction() as c:
  c.execute(f"UPDATE memory_archive_jobs SET {field}='old-version' WHERE id=?",(job_id,))
 assert await arch.process(arch.claim(job_id)) is None
 with db.connection() as c:
  job=c.execute("SELECT * FROM memory_archive_jobs WHERE id=?",(job_id,)).fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "source_changed"
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  assert c.execute("SELECT archived_through_seq FROM conversation_archive_state").fetchone()[0] == 0
  assert c.execute("SELECT content FROM thread_messages WHERE id='fault-user'").fetchone()[0]
 from app.memory_archive import ArchiveSourceChanged
 with pytest.raises(ArchiveSourceChanged):
  arch.retry(thread.id,"local-user",job_id,job["updated_at"])

@pytest.mark.asyncio
async def test_live_summary_declares_arrays_and_reserves_generation_budget():
 from types import SimpleNamespace
 from app.memory_archive import LiveEpisodeSummarizer
 class Gateway:
  async def complete(self, request):
   prompt=request.messages[0]["content"]
   assert '"synopsis":[{' in prompt
   assert "are arrays, never strings" in prompt
   assert "below 1600 tokens" in prompt
   assert "at most 6 items TOTAL" in prompt
   assert request.max_tokens == 4096
   assert "MUST cite at least one message whose role is user" in prompt
   assert request.thinking is False
   return SimpleNamespace(message='{"synopsis":[]}')
 assert await LiveEpisodeSummarizer(Gateway())({"turns":[]}) == '{"synopsis":[]}'


@pytest.mark.asyncio
async def test_archive_model_call_inherits_source_turn_bundle_and_root_budget(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute(
   "INSERT INTO runtime_bundles(id,bundle_hash,manifest_json,created_at) VALUES ('bundle-a','hash-a','{}','now')"
  )
  c.execute(
   "UPDATE turns SET runtime_bundle_id='bundle-a',root_budget_id='root-a' WHERE id='fault-turn'"
  )

 class Gateway:
  control_store = object()
  contexts = []
  def set_call_context(self, context):
   self.contexts.append(context)
   return object()
  def reset_call_context(self, _token):
   pass

 class Summarizer:
  def __init__(self):
   self.gateway = Gateway()
  async def __call__(self, payload):
   return await valid_summary(payload)

 summarizer = Summarizer()
 arch = ConversationArchiver(
  db, MemoryStore(db,tmp_path/"memory"), summarizer, keep_messages=0,
 )
 job_id = arch.enqueue(thread.id, source_turn_id="fault-turn")
 claim = arch.claim(job_id)
 assert claim.runtime_bundle_id == "bundle-a"
 assert claim.root_budget_id == "root-a"
 assert await arch.process(claim) is not None
 assert len(summarizer.gateway.contexts) == 1
 assert summarizer.gateway.contexts[0].runtime_bundle_id == "bundle-a"
 assert summarizer.gateway.contexts[0].root_budget_id == "root-a"

@pytest.mark.asyncio
async def test_archiver_keeps_recent_window_and_is_idempotent(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="2026-01-01T00:00:00+00:00"
 with db.transaction() as c:
  for i in range(4):
   turn=f"t{i}";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
   for role in ("user","assistant"):
    seq=i*2+(1 if role=="user" else 2);c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,1,?,?,?)",(f"m{seq}",thread.id,turn,role,f"内容{seq}",seq,now,now))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=4)
 episode=await arch.archive_thread(thread.id);assert episode and episode.start_message_seq==1 and episode.end_message_seq==4
 assert await arch.archive_thread(thread.id) is None

@pytest.mark.asyncio
async def test_archiver_never_crosses_a_pending_turn(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="2026-01-01T00:00:00+00:00"
 with db.transaction() as c:
  for i,status in enumerate(("COMPLETED","AWAITING_INPUT","COMPLETED")):
   turn=f"p{i}";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",(turn,thread.id,turn,status,now,now))
   c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES (?,?,?,?,?,'ready',1,1,?,?)",(f"pm{i}",thread.id,turn,"user","x",i+1,now))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0)
 episode=await arch.archive_thread(thread.id);assert episode and episode.end_message_seq==1

@pytest.mark.asyncio
async def test_archiver_keeps_whole_turn_bundles(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="n"
 with db.transaction() as c:
  for i in range(3):
   turn=f"b{i}";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
   for role in ("user","assistant"):
    seq=i*2+(1 if role=="user" else 2);c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES (?,?,?,?,?,'ready',1,1,?,?)",(f"bm{seq}",thread.id,turn,role,"x",seq,now))
 episode=await ConversationArchiver(db,MemoryStore(db,tmp_path/"m"),valid_summary,keep_messages=3).archive_thread(thread.id)
 assert episode and episode.end_message_seq==2


@pytest.mark.asyncio
async def test_archiver_accepts_ask_turn_without_duplicate_source_ids(tmp_path):
 db=Database(tmp_path/"ask.db");conv=ConversationService(db);thread=conv.create_thread();now="n"
 with db.transaction() as c:
  turn="ask-turn"
  c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES ('ask-user',?,?, 'user','question','ready',1,8,1,?)",(thread.id,turn,now))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES ('ask-assistant',?,?, 'assistant','answer','ready',1,6,2,?)",(thread.id,turn,now))
  c.execute("INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,answer_json,created_at,answered_at) VALUES (?,?,?,?, 'ANSWERED',?,?,?)",(
   "ask",turn,"call",json.dumps([{"id":"q","header":"h","question":"p","options":[],"multi_select":False,"allow_free_text":True}]),json.dumps([{"question_id":"q","selected_options":[],"free_text":"a"}]),now,now))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0)
 episode=await arch.archive_thread(thread.id)
 assert episode is not None
 with db.connection() as c:
  source_ids=json.loads(c.execute("SELECT source_message_ids_json FROM memory_episodes WHERE id=?",(episode.id,)).fetchone()[0])
  assert source_ids == ["ask-user", "ask-assistant"]

@pytest.mark.asyncio
async def test_archiver_hashes_excluded_terminal_messages_without_summarizing_them(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="n";seen=[]
 with db.transaction() as c:
  c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'CANCELLED',?,?)",("cancelled",thread.id,"cancelled",now,now))
  for seq,role,status,content in ((1,"user","ready","keep"),(2,"assistant","cancelled","partial")):
   c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES (?,?,?,?,?,?,1,1,?,?)",(f"cm{seq}",thread.id,"cancelled",role,content,status,seq,now))
 async def summarize(payload):
  seen.extend(event for turn in payload["turns"] for event in turn["events"])
  return await valid_summary(payload)
 episode=await ConversationArchiver(db,MemoryStore(db,tmp_path/"m"),summarize,keep_messages=0).archive_thread(thread.id)
 assert episode and episode.end_message_seq==2
 assert [item["content"] for item in seen]==["keep"]

@pytest.mark.asyncio
async def test_archiver_without_summarizer_fails_closed(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="n"
 with db.transaction() as c:
  turn="t";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES ('m',?,?, 'user','source','ready',1,6,1,?)",(thread.id,turn,now))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"m"),keep_messages=0)
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0]==0
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"]=="RETRY_WAIT" and job["last_error_code"]=="invalid_summary"

@pytest.mark.asyncio
async def test_terminal_turn_signal_is_consumed_by_managed_worker(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="n"
 with db.transaction() as c:
  turn="terminal";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'ROUTING',?,?)",(turn,thread.id,turn,now,now))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES ('terminal-message',?,?, 'user','source','ready',1,6,1,?)",(thread.id,turn,now))
  c.execute("UPDATE turns SET status='COMPLETED' WHERE id=?",(turn,))
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_archive_signals WHERE turn_id=?",(turn,)).fetchone()[0]==1
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"m"),valid_summary,keep_messages=0)
 worker=ManagedArchiveWorker(arch)
 assert await worker.run_once() is True
 assert await worker.run_once() is True
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_archive_signals").fetchone()[0]==0
  assert c.execute("SELECT status FROM memory_archive_jobs").fetchone()[0]=="COMPLETED"


@pytest.mark.asyncio
async def test_managed_worker_preserves_signal_when_enqueue_fails(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute("INSERT INTO memory_archive_signals(turn_id,thread_id,created_at) VALUES ('fault-turn',?,'now')",(thread.id,))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0)
 def fail_enqueue(*_args,**_kwargs):
  raise RuntimeError("temporary database failure")
 arch.enqueue=fail_enqueue
 assert await ManagedArchiveWorker(arch).run_once() is False
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_archive_signals WHERE turn_id='fault-turn'").fetchone()[0] == 1


def _completed_thread(tmp_path, *, owner_id="owner-a"):
 db=Database(tmp_path/"faults.db");conv=ConversationService(db);thread=conv.create_thread(owner_id=owner_id);now="2026-01-01T00:00:00+00:00"
 with db.transaction() as c:
  turn="fault-turn"
  c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",("fault-user",thread.id,turn,"user","source",6,1,now,now))
 return db, thread


@pytest.mark.asyncio
async def test_archiver_rejects_invalid_json_summary(tmp_path):
 db, thread = _completed_thread(tmp_path)
 async def invalid_json(_payload):
  return "{not-json"
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),invalid_json,keep_messages=0,max_attempts=1)
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "invalid_summary"
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  state=c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()
  assert state["archived_through_seq"] == 0


@pytest.mark.asyncio
async def test_archiver_rejects_summary_references_outside_source_batch(tmp_path):
 db, thread = _completed_thread(tmp_path)
 async def external_ref(_payload):
  return {"synopsis":[{"text":"summary","source_message_ids":["message-from-another-thread"]}],"topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),external_ref,keep_messages=0,max_attempts=1)
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "invalid_summary"
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  assert c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_archiver_rejects_assistant_only_decision(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute(
   "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) "
   "VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
   ("fault-assistant", thread.id, "fault-turn", "assistant", "proposal", 8, 2, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
  )
 async def assistant_decision(payload):
  ids = {
   event["role"]: event["message_id"]
   for turn in payload["turns"] for event in turn["events"] if event["message_id"]
  }
  item = {"text": "助手建议每天90分钟", "source_message_ids": [ids["assistant"]]}
  synopsis = {"text": "助手建议每天90分钟", "source_message_ids": [ids["assistant"]]}
  return {"synopsis":[synopsis],"topics":[],"decisions":[item],"outcomes":[],"open_loops":[],"sensitivity":"normal"}
 arch = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"), assistant_decision, keep_messages=0, max_attempts=1)
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  job = c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "invalid_summary"
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_archiver_rejects_structurally_bounded_but_oversized_summary(tmp_path):
 db, thread = _completed_thread(tmp_path)
 async def huge_summary(payload):
  ids=[event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
  item={"text":"x" * 800,"source_message_ids":[ids[0]]}
  return {"synopsis":[item],"topics":[item],"decisions":[item],"outcomes":[item],"open_loops":[item],"sensitivity":"normal"}
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),huge_summary,keep_messages=0,max_attempts=1)
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "invalid_summary"


@pytest.mark.asyncio
async def test_archiver_does_not_commit_when_source_changes_before_commit(tmp_path):
 db, thread = _completed_thread(tmp_path)
 async def mutate_source(payload):
  with db.transaction() as c:
   c.execute("UPDATE thread_messages SET content='changed',content_length=7 WHERE id='fault-user'")
  return await valid_summary(payload)
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),mutate_source,keep_messages=0)
 job_id=arch.enqueue(thread.id)
 claim=arch.claim(job_id)
 assert await arch.process(claim) is None
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  jobs=c.execute("SELECT id,status,last_error_code FROM memory_archive_jobs ORDER BY created_at,id").fetchall()
  assert len(jobs) == 2
  assert jobs[0]["status"] == "DEAD_LETTER" and jobs[0]["last_error_code"] == "source_changed"
  assert jobs[1]["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_archiver_requeues_current_hash_when_source_changes_before_summary(tmp_path):
 db, thread = _completed_thread(tmp_path)
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0,max_attempts=3)
 job_id=arch.enqueue(thread.id)
 claim=arch.claim(job_id)
 with db.transaction() as c:
  c.execute("UPDATE thread_messages SET content='changed',content_length=7 WHERE id='fault-user'")
 assert await arch.process(claim) is None
 with db.connection() as c:
  jobs=c.execute("SELECT id,status,last_error_code,source_hash FROM memory_archive_jobs ORDER BY created_at,id").fetchall()
  assert len(jobs) == 2
  assert jobs[0]["status"] == "DEAD_LETTER" and jobs[0]["last_error_code"] == "source_changed"
  assert jobs[1]["status"] == "QUEUED" and jobs[1]["source_hash"] != jobs[0]["source_hash"]


@pytest.mark.asyncio
async def test_archiver_old_lease_cannot_commit_after_takeover(tmp_path):
 db, thread = _completed_thread(tmp_path)
 arch1=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0,worker_id="worker-one")
 job_id=arch1.enqueue(thread.id); claim=arch1.claim(job_id)
 transcript=arch1.transcripts.build(thread.id,expected_owner_id="owner-a",after_sequence=claim.start_sequence-1,through_sequence=claim.end_sequence)
 summary=arch1._validate_summary(await valid_summary(arch1._input_payload(transcript)),transcript)
 with db.transaction() as c:
  c.execute("UPDATE memory_archive_jobs SET lease_owner='worker-two',lease_epoch=lease_epoch+1,status='RUNNING' WHERE id=?",(job_id,))
 with pytest.raises(ArchiveLeaseLost):
  arch1._commit(claim,transcript,summary)
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  job=c.execute("SELECT status,lease_owner FROM memory_archive_jobs WHERE id=?",(job_id,)).fetchone()
  assert job["status"] == "RUNNING" and job["lease_owner"] == "worker-two"


@pytest.mark.asyncio
async def test_archiver_commit_failure_rolls_back_episode_and_cursor(tmp_path):
 db, thread = _completed_thread(tmp_path)
 store=MemoryStore(db,tmp_path/"memory")
 arch=ConversationArchiver(db,store,valid_summary,keep_messages=0,max_attempts=1)
 original=store.save_episode
 def save_then_fail(*args,**kwargs):
  original(*args,**kwargs)
  raise RuntimeError("injected commit failure")
 store.save_episode=save_then_fail
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  state=c.execute("SELECT archived_through_seq,state FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()
  assert state["archived_through_seq"] == 0 and state["state"] == "FAILED"
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_archiver_chunks_a_single_oversized_turn_and_advances_coverage(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute("UPDATE thread_messages SET content=?,content_length=? WHERE id='fault-user'", ("x" * 300, 300))
 seen=[]
 async def summarize(payload):
  seen.append(payload)
  ids=payload["source"].get("source_message_ids") or [
   event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]
  ]
  item={"text":f"chunk-{len(seen)}","source_message_ids":[ids[0]]}
  return {"synopsis":[item],"topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}
 arch=ConversationArchiver(
  db, MemoryStore(db,tmp_path/"memory"), summarize, keep_messages=0,
  max_summary_tokens=700, max_attempts=3,
 )
 episode=await arch.archive_thread(thread.id)
 assert episode is not None
 assert len(seen) > 1
 assert all(arch._payload_tokens(payload) <= arch.max_summary_tokens for payload in seen)
 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code,attempts FROM memory_archive_jobs").fetchone()
  state=c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()
  assert job["status"] == "COMPLETED"
  assert job["last_error_code"] is None
  assert job["attempts"] == 1
  assert state["archived_through_seq"] == 1
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 1
 with db.transaction() as c:
  c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED','now','now')",("later-turn",thread.id,"later-turn"))
  c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES ('later-message',?,?,'user','later','ready',1,5,2,'now')",(thread.id,"later-turn"))
 assert await arch.archive_thread(thread.id) is not None
 with db.connection() as c:
  assert c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()[0] == 2
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_chunk_summary_cannot_cite_a_message_outside_its_chunk(tmp_path):
 db=Database(tmp_path/"chunk-evidence.db");conv=ConversationService(db);thread=conv.create_thread();now="n"
 with db.transaction() as c:
  c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES ('chunk-turn',?,'chunk-turn','COMPLETED',?,?)",(thread.id,now,now))
  for seq,content in ((1,"x" * 300),(2,"y" * 300)):
   c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) VALUES (?,?, 'chunk-turn',?,?, 'ready',1,?,?,?)",(f"chunk-message-{seq}",thread.id,"user" if seq == 1 else "assistant",content,len(content),seq,now))
 async def cross_chunk_reference(payload):
  supplied=payload["source"]["source_message_ids"][0]
  other="chunk-message-2" if supplied == "chunk-message-1" else "chunk-message-1"
  item={"text":"unsupported evidence","source_message_ids":[other]}
  return {"synopsis":[item],"topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"chunk-memory"),cross_chunk_reference,keep_messages=0,max_summary_tokens=700,max_attempts=1)

 assert await arch.archive_thread(thread.id) is None

 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  state=c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()
 assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "invalid_summary"
 assert state["archived_through_seq"] == 0


def test_chunk_merge_preserves_same_evidence_in_different_structured_fields():
 item={"text":"same fact","source_message_ids":["message-1"]}
 empty={"synopsis":[],"topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}
 first={**empty,"synopsis":[item],"decisions":[item]}

 merged=ConversationArchiver._merge_chunk_summaries([first])

 assert merged["synopsis"] == [item]
 assert merged["decisions"] == [item]


@pytest.mark.asyncio
async def test_archiver_renews_lease_during_slow_chunk_summary(tmp_path):
 db, thread = _completed_thread(tmp_path)
 started=asyncio.Event()
 release=asyncio.Event()
 async def slow_summary(payload):
  started.set()
  await release.wait()
  return await valid_summary(payload)
 arch=ConversationArchiver(
  db,MemoryStore(db,tmp_path/"memory"),slow_summary,keep_messages=0,
  lease_seconds=.15,max_summary_tokens=12000,
 )
 job_id=arch.enqueue(thread.id);claim=arch.claim(job_id)
 task=asyncio.create_task(arch.process(claim))
 await started.wait()
 await asyncio.sleep(.22)
 with db.connection() as c:
  row=c.execute("SELECT lease_until FROM memory_archive_jobs WHERE id=?",(job_id,)).fetchone()
  assert datetime.fromisoformat(row["lease_until"]) > datetime.now(timezone.utc)
 release.set()
 assert await task is not None


@pytest.mark.asyncio
async def test_managed_worker_keeps_signal_when_ack_delete_fails(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute("INSERT INTO memory_archive_signals(turn_id,thread_id,created_at) VALUES ('fault-turn',?,'now')",(thread.id,))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),valid_summary,keep_messages=0)
 original_enqueue = arch.enqueue
 original_transaction = arch.db.transaction
 enqueued = False
 def enqueue_then_mark(*args, **kwargs):
  nonlocal enqueued
  result = original_enqueue(*args, **kwargs)
  enqueued = True
  return result
 def fail_ack_transaction():
  if enqueued:
   raise RuntimeError("ack failure")
  return original_transaction()
 arch.enqueue = enqueue_then_mark
 arch.db.transaction = fail_ack_transaction
 try:
  assert await ManagedArchiveWorker(arch).run_once() is False
 finally:
  arch.db.transaction = original_transaction
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_archive_signals WHERE turn_id='fault-turn'").fetchone()[0] == 1
