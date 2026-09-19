"""Stage-by-stage formal-instance acceptance; no business-data cleanup."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["init","turn","answer","request","inspect"])
    parser.add_argument("--message")
    parser.add_argument("--payload",type=Path)
    parser.add_argument("--path")
    parser.add_argument("--method",default="GET")
    parser.add_argument("--out",type=Path,default=Path("outputs/formal-dialogue-20260912"))
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    state_path=args.out/"state.json"
    state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    record={"action":args.action,"started_at":datetime.now(timezone.utc).isoformat(),"instance":"http://127.0.0.1:8000"}
    with httpx.Client(base_url=record["instance"],timeout=180,trust_env=False) as client:
        csrf=client.get("/api/bootstrap").json()["csrf_token"]
        client.headers.update({"origin":record["instance"],"x-csrf-token":csrf,"Content-Type":"application/json","Idempotency-Key":uuid.uuid4().hex})
        def call(method,path,**kwargs):
            response=client.request(method,path,**kwargs)
            try:body=response.json()
            except ValueError:body={"text":response.text[:500]}
            record.setdefault("operations",[]).append({"method":method,"path":path,"status":response.status_code,"body":body})
            response.raise_for_status()
            return body
        try:
            if args.action=="init":
                if state:raise RuntimeError("Existing acceptance state; do not overwrite")
                record["readiness"]=call("GET","/api/model-readiness")
                thread=call("POST","/api/threads",json={"title":"[全流程验收 2026-09-12] 30天Python数据分析"})
                state={"thread_id":thread["id"]}
                state_path.write_text(json.dumps(state,indent=2),encoding="utf-8")
            thread_id=state["thread_id"]
            turn_id=None
            if args.action=="turn":
                record["input"]=args.message
                response=call("POST",f"/api/threads/{thread_id}/turns",json={"content":args.message,"client_turn_id":uuid.uuid4().hex,"skill_names":[]})
                turn_id=response["turn_id"]
            elif args.action=="answer":
                current=client.get(f"/api/threads/{thread_id}").json()["turns"][-1]
                payload=json.loads(args.payload.read_text(encoding="utf-8"))
                response=call("POST",f"/api/turns/{current['id']}/ask/answer",json={"expected_version":current["version"],"idempotency_key":uuid.uuid4().hex,"answers":payload})
                turn_id=response["turn"]["id"]
            elif args.action=="request":
                payload=json.loads(args.payload.read_text(encoding="utf-8")) if args.payload else None
                call(args.method,args.path,**({"json":payload} if payload is not None else {}))
            if turn_id:
                deadline=time.monotonic()+150
                while True:
                    turns=client.get(f"/api/threads/{thread_id}").json()["turns"]
                    current=next(item for item in turns if item["id"]==turn_id)
                    if current["status"] in {"COMPLETED","FAILED","PARTIAL","CANCELLED","AWAITING_INPUT","AWAITING_DIRECTION"}:break
                    if time.monotonic()>deadline:
                        call("POST",f"/api/turns/{turn_id}/cancel",json={})
                        raise RuntimeError("Turn timeout; cancelled")
                    time.sleep(.3)
                record["turn"]=current
                if current["status"] in {"FAILED","PARTIAL","CANCELLED"}:record["error"]="turn ended "+current["status"]
            thread=client.get(f"/api/threads/{thread_id}").json()
            record["thread"]=thread
            record["messages"]=client.get(f"/api/threads/{thread_id}/messages").json()
            record["events"]=client.get(f"/api/threads/{thread_id}/events").json()
            record["plan"]=client.get(f"/api/threads/{thread_id}/plan").json()
            if thread["turns"] and thread["turns"][-1]["status"]=="AWAITING_INPUT":
                record["ask"]=client.get(f"/api/turns/{thread['turns'][-1]['id']}/ask").json()
        except Exception as exc:
            record["error"]=str(exc).replace(csrf,"<redacted>")[:1500]
        record["finished_at"]=datetime.now(timezone.utc).isoformat()
        destination=args.out/f"{args.action}-{time.time_ns()}.json"
        destination.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
        messages=record.get("messages",{}).get("messages",[])
        latest=[item for item in messages if item["role"]=="assistant"][-1:]
        result={"file":str(destination),"thread_id":state.get("thread_id"),"turn":record.get("turn"),"error":record.get("error"),"latest_assistant":latest,"ask":record.get("ask"),"plan_id":record.get("plan",{}).get("plan",{}).get("id") if record.get("plan",{}).get("plan") else None}
        if args.action=="request":result["operations"]=record.get("operations")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return int(bool(record.get("error")))


if __name__=="__main__":raise SystemExit(main())
