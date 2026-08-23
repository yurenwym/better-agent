import pytest
from app.conversation import ConversationService
from app.db import Database
from app.memory_archive import ConversationArchiver
from app.memory_v2 import MemoryStore

@pytest.mark.asyncio
async def test_archiver_keeps_recent_window_and_is_idempotent(tmp_path):
 db=Database(tmp_path/"a.db");conv=ConversationService(db);thread=conv.create_thread();now="2026-01-01T00:00:00+00:00"
 with db.transaction() as c:
  for i in range(4):
   turn=f"t{i}";c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,'COMPLETED',?,?)",(turn,thread.id,turn,now,now))
   for role in ("user","assistant"):
    seq=i*2+(1 if role=="user" else 2);c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,1,?,?,?)",(f"m{seq}",thread.id,turn,role,f"内容{seq}",seq,now,now))
 arch=ConversationArchiver(db,MemoryStore(db,tmp_path/"memory"),keep_messages=4)
 episode=await arch.archive_thread(thread.id);assert episode and episode.start_message_seq==1 and episode.end_message_seq==4
 assert await arch.archive_thread(thread.id) is None
