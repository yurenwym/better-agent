from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone


class ConversationArchiver:
    def __init__(self, db, store, summarizer=None, *, keep_messages=50, max_attempts=3, lease_seconds=30):
        self.db=db;self.store=store;self.summarizer=summarizer;self.keep_messages=keep_messages;self.max_attempts=max_attempts;self.lease_seconds=lease_seconds

    async def archive_thread(self, thread_id:str, owner_id="local-user"):
        reserved=self.reserve(thread_id,owner_id)
        if not reserved:return None
        start,end,source_hash,messages,attempts=reserved
        try:
            if self.summarizer: summary=await self.summarizer(messages)
            else: summary="；".join(f"{m['role']}：{m['content']}" for m in messages)[:1800]
            status="ACTIVE"
        except Exception as exc:
            if attempts < self.max_attempts:
                with self.db.transaction() as c:c.execute("UPDATE conversation_archive_state SET state='FAILED',error=?,lease_owner=NULL,lease_until=NULL WHERE owner_id=? AND thread_id=?",(str(exc)[:240],owner_id,thread_id))
                return None
            summary=f"原始消息 {start}–{end} 可追溯，摘要生成失败。";status="RAW_REFERENCE"
        episode=self.store.save_episode(owner_id=owner_id,thread_id=thread_id,project_id=self._project(thread_id),start_message_seq=start,end_message_seq=end,source_hash=source_hash,summary=summary,status=status)
        with self.db.transaction() as c:c.execute("UPDATE conversation_archive_state SET archived_through_seq=?,state='IDLE',reserved_start_seq=NULL,reserved_end_seq=NULL,source_hash=NULL,lease_owner=NULL,lease_until=NULL,error=NULL WHERE owner_id=? AND thread_id=?",(end,owner_id,thread_id))
        return episode

    def reserve(self,thread_id,owner_id):
        now=datetime.now(timezone.utc);until=(now+timedelta(seconds=self.lease_seconds)).isoformat()
        with self.db.transaction() as c:
            c.execute("INSERT OR IGNORE INTO conversation_archive_state(owner_id,thread_id) VALUES (?,?)",(owner_id,thread_id))
            state=c.execute("SELECT * FROM conversation_archive_state WHERE owner_id=? AND thread_id=?",(owner_id,thread_id)).fetchone()
            if state["state"]=="RESERVED" and state["lease_until"] and state["lease_until"]>now.isoformat():return None
            rows=c.execute("SELECT m.message_seq,m.turn_id,m.role,m.content,m.status,t.status turn_status FROM thread_messages m JOIN turns t ON t.id=m.turn_id WHERE m.thread_id=? AND m.message_seq>? ORDER BY m.message_seq",(thread_id,state["archived_through_seq"])).fetchall()
            eligible=[]
            for row in rows:
                if row["turn_status"] not in {"COMPLETED","FAILED","CANCELLED"}:
                    break
                if row["status"]=="ready":eligible.append(row)
            ready=eligible
            if len(ready)<=self.keep_messages:return None
            cutoff=ready[-1]["message_seq"] if self.keep_messages==0 else ready[-self.keep_messages]["message_seq"]-1
            selected=[dict(r) for r in ready if r["message_seq"]<=cutoff]
            if not selected:return None
            start,end=selected[0]["message_seq"],selected[-1]["message_seq"]
            digest="sha256:"+hashlib.sha256(json.dumps(selected,ensure_ascii=False,sort_keys=True).encode()).hexdigest();attempts=int(state["attempts"])+1
            c.execute("UPDATE conversation_archive_state SET state='RESERVED',reserved_start_seq=?,reserved_end_seq=?,source_hash=?,lease_owner=?,lease_until=?,attempts=? WHERE owner_id=? AND thread_id=?",(start,end,digest,f"archiver-{uuid.uuid4().hex}",until,attempts,owner_id,thread_id))
            return start,end,digest,selected,attempts

    def _project(self,thread_id):
        with self.db.connection() as c:r=c.execute("SELECT project_id FROM threads WHERE id=?",(thread_id,)).fetchone()
        return r[0] if r else None
