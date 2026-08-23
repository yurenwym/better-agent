from __future__ import annotations

import os,re,uuid
from dataclasses import dataclass
from datetime import datetime,timezone

import httpx

ENV_RE=re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
@dataclass(frozen=True)
class NotificationChannel:id:str;name:str;channel_type:str;secret_env_name:str;enabled:bool;configured:bool
@dataclass(frozen=True)
class Delivery:id:str;job_id:str;channel_id:str;attempt:int;status:str;http_status:int|None;provider_code:str|None;error:str|None

class NotificationService:
 def __init__(self,db,transport=None):self.db=db;self.transport=transport
 def create(self,name,channel_type,secret_env_name,enabled=True):
  if channel_type not in {"serverchan","wecom","dingtalk","webhook"} or not ENV_RE.fullmatch(secret_env_name):raise ValueError("invalid notification channel")
  now=_now();cid=f"channel_{uuid.uuid4().hex}"
  with self.db.transaction() as c:c.execute("INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(cid,name,channel_type,secret_env_name,int(enabled),now,now))
  return self.get(cid)
 def get(self,cid):
  with self.db.connection() as c:r=c.execute("SELECT * FROM notification_channels WHERE id=?",(cid,)).fetchone()
  if not r:raise KeyError(cid)
  return NotificationChannel(r["id"],r["name"],r["channel_type"],r["secret_env_name"],bool(r["enabled"]),bool(os.getenv(r["secret_env_name"])))
 def list(self):
  with self.db.connection() as c:ids=[r[0] for r in c.execute("SELECT id FROM notification_channels ORDER BY created_at,id")]
  return [self.get(x) for x in ids]
 async def test(self,cid):return await self.send(cid,"test","Better Agent notification test",job_id="test")
 async def deliver_completed(self,job_id,title,content):
  with self.db.connection() as c:
   row=c.execute("SELECT s.notify_enabled FROM research_jobs j LEFT JOIN research_schedules s ON s.id=j.schedule_id WHERE j.id=?",(job_id,)).fetchone()
  if not row or not row[0]:return []
  results=[]
  for channel in self.list():
   if channel.enabled:results.append(await self.send(channel.id,title,content[:3000],job_id))
  return results
 async def send(self,cid,title,content,job_id):
  channel=self.get(cid);secret=os.getenv(channel.secret_env_name);attempt=1;did=f"delivery_{uuid.uuid4().hex}";now=_now()
  with self.db.transaction() as c:
   if job_id!="test":c.execute("INSERT INTO notification_deliveries(id,job_id,channel_id,attempt,status,created_at) VALUES (?,?,?,?,'PENDING',?)",(did,job_id,cid,attempt,now))
  status="FAILED";http_status=None;code=None;error=None
  try:
   if not secret:raise ValueError("notification secret is not configured")
   url=secret if channel.channel_type!="serverchan" else f"https://sctapi.ftqq.com/{secret}.send"
   if channel.channel_type=="serverchan":kwargs={"data":{"title":title[:64],"desp":content[:8000]}}
   elif channel.channel_type in {"wecom","dingtalk"}:kwargs={"json":{"msgtype":"markdown","markdown":{"content":f"{title}\n\n{content}"}}}
   else:kwargs={"json":{"title":title,"content":content,"job_id":job_id,"report_url":f"/api/research/jobs/{job_id}/report"}}
   async with httpx.AsyncClient(transport=self.transport,timeout=10,follow_redirects=False) as client:response=await client.post(url,**kwargs)
   http_status=response.status_code;response.raise_for_status()
   try:data=response.json()
   except ValueError:data={}
   code=data.get("code") if channel.channel_type=="serverchan" else data.get("errcode") if channel.channel_type in {"wecom","dingtalk"} else None
   if code not in {None,0,"0"} or data.get("ok") is False:raise ValueError("provider rejected notification")
   status="SENT"
  except Exception as exc:error=str(exc)[:300].replace(secret or "", "[REDACTED]")
  if job_id!="test":
   with self.db.transaction() as c:c.execute("UPDATE notification_deliveries SET status=?,http_status=?,provider_code=?,error=?,finished_at=? WHERE id=?",(status,http_status,str(code) if code is not None else None,error,_now(),did))
  return Delivery(did,job_id,cid,attempt,status,http_status,str(code) if code is not None else None,error)
def _now():return datetime.now(timezone.utc).isoformat()
