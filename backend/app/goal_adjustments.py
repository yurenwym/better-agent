from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from .goal_program_compiler import GoalCompiler, validate_program_structure
from .goal_programs import (
    OWNER_ID, GoalProgramConflict, GoalProgramNotFound, GoalProgramService,
    _hash, _json, _now, _timezone,
)
from .plan_documents import PlanDocumentConflict


class GoalAdjustmentService:
    def __init__(self, programs: GoalProgramService, compiler: GoalCompiler, plan_documents) -> None:
        self.programs = programs
        self.compiler = compiler
        self.plan_documents = plan_documents
        self.db = programs.db

    async def propose(self, program_id: str, *, reason: str, expected_version: int, idempotency_key: str,
                      owner_id: str = OWNER_ID, actor: str = "user", root_budget_id: str | None = None) -> dict[str, Any]:
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason or len(reason) > 2000: raise ValueError("reason is required and must be at most 2000 characters")
        request_hash = _hash({"program_id":program_id,"reason":reason,"expected_version":expected_version})
        cached = self.programs._receipt(owner_id,idempotency_key,request_hash)
        if cached is not None:return cached
        with self.db.connection() as connection:
            program=self.programs._program_row(connection,program_id,owner_id)
            if program["status"] not in {"ACTIVE","PAUSED"} or program["version"]!=expected_version or not program["current_program_version_id"]:
                raise GoalProgramConflict("program cannot be adjusted",self.programs._program_json(connection,program_id,owner_id))
            current=self.programs._structure(connection,program["current_program_version_id"])
            live_actions=connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status!='CANCELLED' ORDER BY scheduled_date,position,id",(program_id,)).fetchall()
            fields=("logical_key","scheduled_date","position","title","description","estimated_minutes","completion_criteria")
            current["actions"]=[{**{field:row[field] for field in fields},"required":bool(row["required"])} for row in live_actions]
            source=connection.execute("SELECT v.id,v.content_hash FROM plan_documents d JOIN plan_document_versions v ON v.id=d.current_version_id WHERE d.id=? AND d.deleted_at IS NULL AND v.status='committed'",(program["source_plan_document_id"],)).fetchone()
            if source is None:raise GoalProgramConflict("source plan snapshot is invalid")
            local_today=_local_date(program["timezone"])
            affected=connection.execute("SELECT id,version,status,logical_key,scheduled_date FROM goal_actions WHERE program_id=? AND status='SCHEDULED' AND scheduled_date>=? ORDER BY scheduled_date,position,id",(program_id,local_today)).fetchall()
            snapshot=[dict(row) for row in affected]
            protected_keys={row["logical_key"] for row in connection.execute("SELECT logical_key FROM goal_actions WHERE program_id=? AND status IN ('COMPLETED','SKIPPED','DEFERRED')",(program_id,)).fetchall()}
            # Past/partially executed work is evidence, not a fresh model draft.
            protected_keys.update(row["logical_key"] for row in live_actions if row["scheduled_date"]<local_today or json.loads(row["progress_json"]))
            historical_keys={row["logical_key"] for row in live_actions if row["status"] in {"DEFERRED","SKIPPED"} or row["scheduled_date"]<local_today}
            constraints=json.loads(program["schedule_constraints_json"])
        if actor == "model" and not any(item["logical_key"] not in protected_keys for item in snapshot):
            raise GoalAdjustmentNoChange("no remaining actions can be adjusted")
        async def generate(adjustment_reason: str):
            context=self.programs._model_context(program_id,"planner","adjust_goal_program",operation_id=f"{program_id}:{idempotency_key}:{request_hash}",root_budget_id=root_budget_id)
            model_current={**current,"execution_context":{"daily_minutes":program["daily_minutes"],"schedule_constraints":constraints,"historical_keys":sorted(historical_keys),"protected_keys":sorted(protected_keys)}}
            raw_candidate=await self.programs._call_model(context,self.compiler.adjust(model_current,adjustment_reason))
            if isinstance(raw_candidate,dict) and isinstance(raw_candidate.get("actions"),list):
                raw_candidate={key:value for key,value in raw_candidate.items() if key!="execution_context"}
                original={item["logical_key"]:item for item in current["actions"]}
                candidate_by_key={item.get("logical_key"):item for item in raw_candidate["actions"] if isinstance(item,dict)}
                # When a plan has no remaining scheduled actions, keep the
                # candidate shape intact so validation can produce a useful
                # proposal (completed history is still protected below).
                earliest_future=min((item["scheduled_date"] for item in snapshot),default=program["start_date"])
                candidate_by_key={key:item for key,item in candidate_by_key.items() if key in protected_keys or item.get("scheduled_date","")>=earliest_future}
                for key in protected_keys:
                    if key in original:candidate_by_key[key]=original[key]
                raw_candidate={**raw_candidate,"actions":list(candidate_by_key.values())}
            candidate=validate_program_structure(raw_candidate,program["start_date"],program["end_date"],program["daily_minutes"],constraints=constraints,historical_keys=historical_keys)
            candidate["coach"]=current.get("coach","")
            return candidate

        candidate=await generate(reason)
        diff=deterministic_diff(current,candidate)
        if not any(diff.values()):
            raise GoalAdjustmentNoChange("adjustment has no effective changes")
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        proposal_id=f"adjustment_{uuid.uuid4().hex}"; now=_now()
        with self.db.transaction() as connection:
            current_program=self.programs._program_row(connection,program_id,owner_id)
            if current_program["version"]!=expected_version or current_program["current_program_version_id"]!=program["current_program_version_id"]:
                raise GoalProgramConflict("program changed while adjustment was generated",self.programs._program_json(connection,program_id,owner_id))
            connection.execute("INSERT INTO goal_adjustment_proposals(id,owner_id,program_id,base_program_version_id,expected_plan_document_version_id,expected_plan_content_hash,affected_actions_json,candidate_structure_json,diff_json,reason,status,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',0,?,?)",
                               (proposal_id,owner_id,program_id,program["current_program_version_id"],source["id"],source["content_hash"],_json(snapshot),_json(candidate),_json(diff),reason,now,now))
            self.programs._event(connection,program_id,None,"adjustment.proposed",actor,{"proposal_id":proposal_id,"changed":len(diff["changed"]),"added":len(diff["added"]),"removed":len(diff["removed"])})
            response=self._proposal_json(connection,proposal_id,owner_id)
            self.programs._save_receipt(connection,owner_id,"adjustment",proposal_id,"propose",idempotency_key,request_hash,response)
        return response

    def get(self, proposal_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:return self._proposal_json(connection,proposal_id,owner_id)

    def accept(self, proposal_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        try:
            return self._accept(proposal_id, expected_version=expected_version, idempotency_key=idempotency_key, owner_id=owner_id)
        except _StaleProposal as exc:
            with self.db.transaction() as connection:
                connection.execute("UPDATE goal_adjustment_proposals SET status='STALE',version=version+1,updated_at=? WHERE id=? AND owner_id=? AND status='PENDING'",(_now(),proposal_id,owner_id))
            exc.current=self.get(proposal_id,owner_id)
            raise

    def _accept(self, proposal_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash=_hash({"proposal_id":proposal_id,"expected_version":expected_version})
        cached=self.programs._receipt(owner_id,idempotency_key,request_hash,identity=("adjustment",proposal_id,"accept"))
        if cached is not None:return cached
        with self.db.transaction() as connection:
            self.programs._lock_command(connection,owner_id,idempotency_key)
            cached=self.programs._receipt(owner_id,idempotency_key,request_hash,identity=("adjustment",proposal_id,"accept"),connection=connection)
            if cached is not None:return cached
            proposal=self._proposal_row(connection,proposal_id,owner_id)
            program=self.programs._program_row(connection,proposal["program_id"],owner_id,lock=True)
            proposal=self._proposal_row(connection,proposal_id,owner_id)
            if proposal["status"]!="PENDING" or proposal["version"]!=expected_version:
                raise GoalProgramConflict("proposal cannot be accepted",self._proposal_json(connection,proposal_id,owner_id))
            if program["current_program_version_id"]!=proposal["base_program_version_id"]:
                raise _StaleProposal("proposal is stale",self._proposal_json(connection,proposal_id,owner_id))
            affected=json.loads(proposal["affected_actions_json"])
            current_future=[dict(row) for row in connection.execute(
                "SELECT id,version,status,logical_key,scheduled_date FROM goal_actions "
                "WHERE program_id=? AND status='SCHEDULED' AND scheduled_date>=? ORDER BY scheduled_date,position,id",
                (program["id"],_local_date(program["timezone"])),
            ).fetchall()]
            if current_future!=affected:
                raise _StaleProposal("proposal action snapshot is stale",self._proposal_json(connection,proposal_id,owner_id))
            for snapshot in affected:
                row=connection.execute("SELECT version,status FROM goal_actions WHERE id=? AND program_id=?",(snapshot["id"],program["id"])).fetchone()
                if row is None or row["version"]!=snapshot["version"] or row["status"]!=snapshot["status"]:
                    raise _StaleProposal("proposal action snapshot is stale",self._proposal_json(connection,proposal_id,owner_id))
            candidate=json.loads(proposal["candidate_structure_json"])
            candidate_by_key={item["logical_key"]:item for item in candidate["actions"]}
            fields=("scheduled_date","position","title","description","estimated_minutes","completion_criteria")
            retained=set()
            for action in connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status!='CANCELLED'",(program["id"],)).fetchall():
                item=candidate_by_key.get(action["logical_key"])
                unchanged=item is not None and all(item[field]==action[field] for field in fields) and bool(item["required"])==bool(action["required"])
                if action["status"]=="SCHEDULED" and unchanged:
                    retained.add(action["id"])
                if action["status"]=="SCHEDULED" and json.loads(action["progress_json"]) and not unchanged:
                    raise _StaleProposal("candidate attempts to rewrite partially executed work",self._proposal_json(connection,proposal_id,owner_id))
            removed_keys={item["logical_key"] for item in json.loads(proposal["diff_json"])["removed"]}
            if any(item["logical_key"] not in candidate_by_key and item["logical_key"] not in removed_keys for item in affected):
                raise _StaleProposal("candidate removes work not disclosed in the preview",self._proposal_json(connection,proposal_id,owner_id))
            next_version=int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM goal_program_versions WHERE program_id=?",(program["id"],)).fetchone()[0])
            version_id=f"programv_{uuid.uuid4().hex}"; now=_now()
            connection.execute("INSERT INTO goal_program_versions(id,program_id,version,base_version_id,source_plan_document_version_id,structure_json,change_summary,actor,created_at) VALUES (?,?,?,?,?,?,?,'user',?)",
                               (version_id,program["id"],next_version,proposal["base_program_version_id"],program["source_plan_document_version_id"],_json(candidate),proposal["reason"][:500],now))
            changed_actions=[item for item in affected if item["id"] not in retained]
            if changed_actions:
                ids=[item["id"] for item in changed_actions]
                placeholders=",".join("?" for _ in ids)
                connection.execute(f"UPDATE goal_actions SET status='CANCELLED',cancel_reason='SUPERSEDED',cancelled_at=?,updated_at=?,version=version+1 WHERE id IN ({placeholders}) AND status='SCHEDULED'",(now,now,*ids))
                earliest=min(item["scheduled_date"] for item in affected)
            else:
                earliest=_local_date(program["timezone"])
            protected_keys={row["logical_key"] for row in connection.execute("SELECT logical_key FROM goal_actions WHERE program_id=? AND status IN ('COMPLETED','SKIPPED','DEFERRED')",(program["id"],)).fetchall()}
            retained_keys={item["logical_key"] for item in affected if item["id"] in retained}
            candidate_by_key={item["logical_key"]:item for item in candidate["actions"]}
            for historical in connection.execute("SELECT * FROM goal_actions WHERE program_id=? AND status IN ('COMPLETED','SKIPPED','DEFERRED')",(program["id"],)).fetchall():
                proposed=candidate_by_key.get(historical["logical_key"])
                if proposed is None or any(proposed[field]!=historical[field] for field in ("scheduled_date","position","title","description","estimated_minutes","completion_criteria")) or bool(proposed["required"])!=bool(historical["required"]):
                    raise _StaleProposal("candidate attempts to rewrite action history",self._proposal_json(connection,proposal_id,owner_id))
            for item in candidate["actions"]:
                if item["scheduled_date"]<earliest or item["logical_key"] in protected_keys or item["logical_key"] in retained_keys:continue
                connection.execute("INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?)",
                                   (f"action_{uuid.uuid4().hex}",program["id"],version_id,item["logical_key"],item["scheduled_date"],item["position"],item["title"],item["description"],item["estimated_minutes"],item["completion_criteria"],int(item["required"]),now,now))
            connection.execute("UPDATE goal_programs SET objective_title=?,objective_summary=?,current_program_version_id=?,version=version+1,updated_at=? WHERE id=? AND current_program_version_id=?",
                               (candidate["objective_title"],candidate["objective_summary"],version_id,now,program["id"],proposal["base_program_version_id"]))
            if self.programs.reviews is not None:
                dates = {item["scheduled_date"] for item in changed_actions}
                dates.update(item["scheduled_date"] for item in candidate["actions"] if item["scheduled_date"] >= earliest)
                for local_date in dates:
                    self.programs.reviews.refresh_date_evidence(connection, program["id"], local_date)
            connection.execute("UPDATE goal_adjustment_proposals SET status='ACCEPTED',accepted_program_version_id=?,version=version+1,decided_at=?,updated_at=? WHERE id=? AND status='PENDING' AND version=?",
                               (version_id,now,now,proposal_id,expected_version))
            self.programs._event(connection,program["id"],None,"adjustment.accepted","user",{"proposal_id":proposal_id,"program_version_id":version_id})
            response={"proposal":self._proposal_json(connection,proposal_id,owner_id),"program":self.programs._program_json(connection,program["id"],owner_id)}
            self.programs._save_receipt(connection,owner_id,"adjustment",proposal_id,"accept",idempotency_key,request_hash,response)
        return response

    def reject(self, proposal_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash=_hash({"proposal_id":proposal_id,"expected_version":expected_version});cached=self.programs._receipt(owner_id,idempotency_key,request_hash,identity=("adjustment",proposal_id,"reject"))
        if cached is not None:return cached
        with self.db.transaction() as connection:
            self.programs._lock_command(connection,owner_id,idempotency_key)
            cached=self.programs._receipt(owner_id,idempotency_key,request_hash,identity=("adjustment",proposal_id,"reject"),connection=connection)
            if cached is not None:return cached
            proposal=self._proposal_row(connection,proposal_id,owner_id)
            self.programs._program_row(connection,proposal["program_id"],owner_id,lock=True)
            proposal=self._proposal_row(connection,proposal_id,owner_id)
            if proposal["status"]!="PENDING" or proposal["version"]!=expected_version:raise GoalProgramConflict("proposal cannot be rejected",self._proposal_json(connection,proposal_id,owner_id))
            connection.execute("UPDATE goal_adjustment_proposals SET status='REJECTED',version=version+1,decided_at=?,updated_at=? WHERE id=? AND status='PENDING' AND version=?",(_now(),_now(),proposal_id,expected_version))
            self.programs._event(connection,proposal["program_id"],None,"adjustment.rejected","user",{"proposal_id":proposal_id})
            response=self._proposal_json(connection,proposal_id,owner_id)
            self.programs._save_receipt(connection,owner_id,"adjustment",proposal_id,"reject",idempotency_key,request_hash,response)
        return response

    def sync_plan_document(self, proposal_id: str, *, expected_version: int, rebase_to_current: bool = False, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_hash=_hash({"proposal_id":proposal_id,"expected_version":expected_version,"rebase_to_current":rebase_to_current})
        cached=self.programs._receipt(owner_id,idempotency_key,request_hash)
        if cached is not None:return cached
        with self.db.connection() as connection:
            proposal=self._proposal_row(connection,proposal_id,owner_id)
            program=self.programs._program_row(connection,proposal["program_id"],owner_id)
            if proposal["status"]!="ACCEPTED" or proposal["version"]!=expected_version:raise GoalProgramConflict("only the current accepted proposal can be synchronized",self._proposal_json(connection,proposal_id,owner_id))
            if proposal["plan_sync_status"]=="COMMITTED" and proposal["plan_sync_version_id"]:
                return {"proposal":self._proposal_json(connection,proposal_id,owner_id),"plan_document_version_id":proposal["plan_sync_version_id"]}
            version_id=proposal["expected_plan_document_version_id"]
            expected_hash=proposal["expected_plan_content_hash"]
            if rebase_to_current:
                current=connection.execute("SELECT v.* FROM goal_programs p JOIN plan_documents d ON d.id=p.source_plan_document_id JOIN plan_document_versions v ON v.id=d.current_version_id WHERE p.id=? AND p.owner_id=? AND v.status='committed'",(proposal["program_id"],owner_id)).fetchone()
                if current is None:raise GoalProgramConflict("current plan document head is unavailable",self._proposal_json(connection,proposal_id,owner_id))
                version_id=current["id"];expected_hash=current["content_hash"]
            expected=connection.execute("SELECT * FROM plan_document_versions WHERE id=?",(version_id,)).fetchone()
            candidate=json.loads(proposal["candidate_structure_json"])
        markdown=render_execution_markdown(expected["markdown_content"],candidate)
        change_summary=f"Sync execution adjustment {proposal_id}"
        recovered=self._recover_plan_sync(proposal_id,owner_id,program["source_plan_document_id"],version_id,markdown,change_summary,request_hash,idempotency_key)
        if recovered is not None:return recovered
        try:
            revision=self.plan_documents.save_model_revision(thread_id=program["source_thread_id"],title=expected["title"],markdown_content=markdown,
                source_turn_id=None,source_message_id=None,actor="user",expected_version_id=version_id,
                expected_file_hash=expected_hash,change_summary=change_summary)
        except (PlanDocumentConflict,OSError,UnicodeError):
            recovered=self._recover_plan_sync(proposal_id,owner_id,program["source_plan_document_id"],version_id,markdown,change_summary,request_hash,idempotency_key)
            if recovered is not None:return recovered
            with self.db.transaction() as connection:connection.execute("UPDATE goal_adjustment_proposals SET plan_sync_status='CONFLICT',updated_at=? WHERE id=?",(_now(),proposal_id))
            raise GoalProgramConflict("execution version is active; plan document was not synchronized",self.get(proposal_id))
        with self.db.transaction() as connection:
            connection.execute("UPDATE goal_adjustment_proposals SET plan_sync_status='COMMITTED',plan_sync_version_id=?,updated_at=? WHERE id=?",(revision.id,_now(),proposal_id))
            response={"proposal":self._proposal_json(connection,proposal_id,owner_id),"plan_document_version_id":revision.id}
            self.programs._save_receipt(connection,owner_id,"adjustment",proposal_id,"sync-plan-document",idempotency_key,request_hash,response)
        return response

    def _recover_plan_sync(self,proposal_id,owner_id,document_id,base_version_id,markdown,change_summary,request_hash,idempotency_key):
        from .plan_documents import content_hash
        with self.db.transaction() as connection:
            revision=connection.execute(
                "SELECT id FROM plan_document_versions WHERE plan_document_id=? AND base_version_id=? "
                "AND content_hash=? AND change_summary=? AND status='committed' ORDER BY version DESC LIMIT 1",
                (document_id,base_version_id,content_hash(markdown),change_summary),
            ).fetchone()
            if revision is None:return None
            connection.execute(
                "UPDATE goal_adjustment_proposals SET plan_sync_status='COMMITTED',plan_sync_version_id=?,updated_at=? WHERE id=? AND owner_id=?",
                (revision["id"],_now(),proposal_id,owner_id),
            )
            response={"proposal":self._proposal_json(connection,proposal_id,owner_id),"plan_document_version_id":revision["id"]}
            self.programs._save_receipt(connection,owner_id,"adjustment",proposal_id,"sync-plan-document",idempotency_key,request_hash,response)
            return response

    def _proposal_row(self, connection, proposal_id, owner_id):
        row=connection.execute("SELECT * FROM goal_adjustment_proposals WHERE id=? AND owner_id=?",(proposal_id,owner_id)).fetchone()
        if row is None:raise GoalProgramNotFound(proposal_id)
        return row

    def _proposal_json(self, connection, proposal_id, owner_id):
        row=self._proposal_row(connection,proposal_id,owner_id)
        return {"id":row["id"],"program_id":row["program_id"],"base_program_version_id":row["base_program_version_id"],
                "expected_plan_document_version_id":row["expected_plan_document_version_id"],"expected_plan_content_hash":row["expected_plan_content_hash"],
                "candidate":json.loads(row["candidate_structure_json"]),"diff":json.loads(row["diff_json"]),"reason":row["reason"],"status":row["status"],
                "version":row["version"],"accepted_program_version_id":row["accepted_program_version_id"],"plan_sync_status":row["plan_sync_status"],
                "plan_sync_version_id":row["plan_sync_version_id"],"created_at":row["created_at"],"decided_at":row["decided_at"]}


def deterministic_diff(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    before={item["logical_key"]:item for item in current["actions"]};after={item["logical_key"]:item for item in candidate["actions"]}
    added=[after[key] for key in sorted(after.keys()-before.keys())]
    removed=[before[key] for key in sorted(before.keys()-after.keys())]
    changed=[]
    for key in sorted(before.keys()&after.keys()):
        fields={name:{"before":before[key][name],"after":after[key][name]} for name in sorted(before[key]) if before[key][name]!=after[key][name]}
        if fields:changed.append({"logical_key":key,"fields":fields})
    return {"added":added,"removed":removed,"changed":changed}


def render_execution_markdown(original: str, structure: dict[str, Any]) -> str:
    marker="\n\n## 当前执行版本\n"
    base=original.split(marker,1)[0].rstrip()
    lines=[base,"","## 当前执行版本","",f"> {structure['start_date']} 至 {structure['end_date']}；此章节由用户显式同步。",""]
    for action in structure["actions"]:
        check=" " if action["required"] else "-"
        lines.append(f"- [{check}] {action['scheduled_date']} · {action['title']}（{action['estimated_minutes']} 分钟）")
    return "\n".join(lines).rstrip()+"\n"


def _local_date(timezone_name: str) -> str:
    return datetime.now(_timezone(timezone_name)).date().isoformat()


class _StaleProposal(GoalProgramConflict):
    pass


class GoalAdjustmentNoChange(GoalProgramConflict):
    code = "ADJUSTMENT_NO_CHANGE"
