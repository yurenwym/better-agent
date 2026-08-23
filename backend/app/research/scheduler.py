from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ResearchSchedule:
    id: str; name: str; thread_id: str; topic: str; source_scopes: tuple[str, ...]
    trigger_type: str; trigger_time: str | None; trigger_weekday: int | None; interval_hours: int | None
    timezone: str; enabled: bool; notify_enabled: bool; next_run_at: str | None; last_run_at: str | None
    last_job_id: str | None; created_at: str; updated_at: str


class _UsEastern(tzinfo):
    def utcoffset(self, value): return self.dst(value) + timedelta(hours=-5)
    def dst(self, value):
        if value is None: return timedelta(0)
        year=value.year
        march=date(year,3,8); start=march+timedelta(days=(6-march.weekday())%7)
        november=date(year,11,1); end=november+timedelta(days=(6-november.weekday())%7)
        naive=value.replace(tzinfo=None)
        return timedelta(hours=1) if datetime.combine(start,datetime.min.time()).replace(hour=2) <= naive < datetime.combine(end,datetime.min.time()).replace(hour=2) else timedelta(0)
    def tzname(self,value): return "EDT" if self.dst(value) else "EST"


def _zone(name: str):
    try: return ZoneInfo(name)
    except Exception:
        if name == "Asia/Shanghai": return timezone(timedelta(hours=8), "Asia/Shanghai")
        if name == "America/New_York": return _UsEastern()
        raise ValueError(f"timezone data is unavailable for {name}")


def next_run(trigger_type: str, trigger_time: str | None, weekday: int | None, interval_hours: int | None, timezone_name: str, after: datetime) -> datetime:
    zone = _zone(timezone_name); after = after.astimezone(timezone.utc)
    if trigger_type == "interval_hours": return after + timedelta(hours=int(interval_hours or 1))
    hour, minute = map(int, (trigger_time or "00:00").split(":"))
    local = after.astimezone(zone)
    day = local.date()
    if trigger_type == "weekly": day += timedelta(days=((int(weekday or 0) - day.weekday()) % 7))
    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    if candidate.astimezone(timezone.utc) <= after:
        candidate += timedelta(days=7 if trigger_type == "weekly" else 1)
    return candidate.astimezone(timezone.utc)


class ScheduleService:
    def __init__(self, db, conversation, research) -> None: self.db=db; self.conversation=conversation; self.research=research

    def create(self, *, name, topic, source_scopes, trigger_type, timezone_name, trigger_time=None, trigger_weekday=None, interval_hours=None, enabled=True, notify_enabled=True):
        _zone(timezone_name)
        if trigger_type not in {"daily","weekly","interval_hours"}: raise ValueError("invalid trigger type")
        thread = self.conversation.create_thread(f"定时研究 · {name}"); now = datetime.now(timezone.utc); schedule_id=f"schedule_{uuid.uuid4().hex}"
        upcoming = next_run(trigger_type, trigger_time, trigger_weekday, interval_hours, timezone_name, now).isoformat() if enabled else None
        stamp = now.isoformat()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO research_schedules(id,name,thread_id,topic,source_scopes_json,trigger_type,trigger_time,trigger_weekday,interval_hours,timezone,enabled,notify_enabled,next_run_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (schedule_id,name.strip(),thread.id,topic.strip(),json.dumps(source_scopes),trigger_type,trigger_time,trigger_weekday,interval_hours,timezone_name,int(enabled),int(notify_enabled),upcoming,stamp,stamp))
        return self.get(schedule_id)

    def update(self, schedule_id: str, **changes):
        current=self.get(schedule_id); data=current.__dict__.copy(); data.update(changes); enabled=bool(data["enabled"]); now=datetime.now(timezone.utc)
        upcoming=next_run(data["trigger_type"],data["trigger_time"],data["trigger_weekday"],data["interval_hours"],data["timezone"],now).isoformat() if enabled else None
        with self.db.transaction() as connection:
            connection.execute("UPDATE research_schedules SET name=?,topic=?,source_scopes_json=?,trigger_type=?,trigger_time=?,trigger_weekday=?,interval_hours=?,timezone=?,enabled=?,notify_enabled=?,next_run_at=?,updated_at=? WHERE id=?", (data["name"],data["topic"],json.dumps(data["source_scopes"]),data["trigger_type"],data["trigger_time"],data["trigger_weekday"],data["interval_hours"],data["timezone"],int(enabled),int(data["notify_enabled"]),upcoming,now.isoformat(),schedule_id))
        return self.get(schedule_id)

    def tick(self, now: datetime | None = None):
        now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc); created=[]
        with self.db.connection() as connection:
            due=[row[0] for row in connection.execute("SELECT id FROM research_schedules WHERE enabled=1 AND deleted_at IS NULL AND next_run_at<=? ORDER BY next_run_at,id",(now.isoformat(),))]
        for schedule_id in due:
            schedule=self.get(schedule_id)
            with self.db.connection() as connection:
                active=connection.execute("SELECT 1 FROM research_jobs WHERE schedule_id=? AND status IN ('QUEUED','RUNNING')",(schedule_id,)).fetchone()
            scheduled_for=schedule.next_run_at or now.isoformat()
            if not active:
                key=f"schedule:{schedule.id}:{scheduled_for}"
                job=self.research._create_anchor_job(schedule.thread_id,schedule.topic,schedule.source_scopes,"scheduled",key,f"schedule:{scheduled_for}",schedule_id=schedule.id); created.append(job)
            upcoming=next_run(schedule.trigger_type,schedule.trigger_time,schedule.trigger_weekday,schedule.interval_hours,schedule.timezone,now).isoformat()
            with self.db.transaction() as connection:
                connection.execute("UPDATE research_schedules SET next_run_at=?,last_run_at=?,last_job_id=COALESCE(?,last_job_id),updated_at=? WHERE id=?",(upcoming,scheduled_for,created[-1].id if created and created[-1].schedule_id==schedule.id else None,now.isoformat(),schedule.id))
        return created

    def run_now(self,schedule_id,key):
        schedule=self.get(schedule_id); occurrence=f"run-now:{schedule_id}:{key}"
        with self.db.connection() as connection:
            prior=connection.execute("SELECT id FROM research_jobs WHERE occurrence_key=?",(occurrence,)).fetchone()
        if prior:return self.research.get(prior["id"])
        return self.research._create_anchor_job(schedule.thread_id,schedule.topic,schedule.source_scopes,"run_now",occurrence,f"run-now:{key}",schedule_id=schedule.id)

    def delete(self,schedule_id):
        self.get(schedule_id); now=datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as connection: connection.execute("UPDATE research_schedules SET enabled=0,next_run_at=NULL,deleted_at=?,updated_at=? WHERE id=?",(now,now,schedule_id))

    def get(self,schedule_id):
        with self.db.connection() as connection: row=connection.execute("SELECT * FROM research_schedules WHERE id=?",(schedule_id,)).fetchone()
        if not row:raise KeyError(schedule_id)
        return _schedule(row)

    def list(self):
        with self.db.connection() as connection: rows=connection.execute("SELECT * FROM research_schedules WHERE deleted_at IS NULL ORDER BY created_at DESC,id").fetchall()
        return [_schedule(row) for row in rows]


class ManagedScheduler:
    def __init__(self,service,poll_seconds=30):self.service=service;self.poll_seconds=poll_seconds;self._task=None;self._stop=None
    async def start(self):
        if self._task:return
        self._stop=asyncio.Event();self._task=asyncio.create_task(self._loop())
    async def stop(self):
        if not self._task:return
        self._stop.set();task,self._task=self._task,None
        with contextlib.suppress(asyncio.CancelledError):await task
    async def _loop(self):
        while not self._stop.is_set():
            self.service.tick()
            try:await asyncio.wait_for(self._stop.wait(),self.poll_seconds)
            except asyncio.TimeoutError:pass


def _schedule(row):
    return ResearchSchedule(row["id"],row["name"],row["thread_id"],row["topic"],tuple(json.loads(row["source_scopes_json"])),row["trigger_type"],row["trigger_time"],row["trigger_weekday"],row["interval_hours"],row["timezone"],bool(row["enabled"]),bool(row["notify_enabled"]),row["next_run_at"],row["last_run_at"],row["last_job_id"],row["created_at"],row["updated_at"])
