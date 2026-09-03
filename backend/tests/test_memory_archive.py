import json

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
 with pytest.raises(ArchiveLeaseLost):
  await arch.process(claim)
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
  job=c.execute("SELECT status FROM memory_archive_jobs WHERE id=?",(job_id,)).fetchone()
  assert job["status"] != "COMPLETED"


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
  assert state["archived_through_seq"] == 0
  job=c.execute("SELECT status,last_error_code FROM memory_archive_jobs").fetchone()
  assert job["status"] == "DEAD_LETTER" and job["last_error_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_archiver_dead_letters_a_single_turn_that_exceeds_summary_budget(tmp_path):
 db, thread = _completed_thread(tmp_path)
 with db.transaction() as c:
  c.execute("UPDATE thread_messages SET content=?,content_length=? WHERE id='fault-user'", ("x" * 300, 300))
 arch=ConversationArchiver(
  db, MemoryStore(db,tmp_path/"memory"), valid_summary, keep_messages=0,
  max_summary_tokens=80, max_attempts=3,
 )
 assert await arch.archive_thread(thread.id) is None
 with db.connection() as c:
  job=c.execute("SELECT status,last_error_code,attempts FROM memory_archive_jobs").fetchone()
  state=c.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",(thread.id,)).fetchone()
  assert job["status"] == "DEAD_LETTER"
  assert job["last_error_code"] == "oversized_input"
  assert job["attempts"] == 1
  assert state["archived_through_seq"] == 0
  assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0


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
