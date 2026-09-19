"""Check the formal instance without truncating or replacing user data."""
import argparse
import json
from pathlib import Path
import time
import uuid

import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--url",default="http://127.0.0.1:8000")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    with httpx.Client(base_url=args.url,timeout=15,trust_env=False) as client:
        ready=client.get("/api/model-readiness").json()
        assert ready["ready"] and ready["cost_mode"]=="observe"
        csrf=client.get("/api/bootstrap").json()["csrf_token"]
        client.headers.update({"origin":args.url,"x-csrf-token":csrf})
        thread=client.post("/api/threads",json={"title":"[验收] 费用仅统计，不拦截"})
        thread.raise_for_status();thread_id=thread.json()["id"]
        response=client.post(f"/api/threads/{thread_id}/turns",json={"content":"这是一条功能验收消息，不是个人偏好，请不要记忆。只回复：收到。","client_turn_id":uuid.uuid4().hex,"skill_names":[]})
        response.raise_for_status();turn_id=response.json()["turn_id"]
        deadline=time.monotonic()+90
        while True:
            turn=next(item for item in client.get(f"/api/threads/{thread_id}").json()["turns"] if item["id"]==turn_id)
            if turn["status"] in {"COMPLETED","FAILED","CANCELLED","PARTIAL","AWAITING_INPUT"}:break
            if time.monotonic()>deadline:
                client.post(f"/api/turns/{turn_id}/cancel",json={})
                raise RuntimeError("Acceptance timed out; cancelled")
            time.sleep(.3)
        # Terminal text can precede final telemetry; allow its write to settle.
        time.sleep(1)
        report={"instance":args.url,"thread_id":thread_id,"turn_id":turn_id,"status":turn["status"],"cost_mode":ready["cost_mode"],
                "messages":client.get(f"/api/threads/{thread_id}/messages").json(),
                "events":client.get(f"/api/threads/{thread_id}/events").json()}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({key:report[key] for key in ("instance","thread_id","turn_id","status","cost_mode")},ensure_ascii=False))
        assert turn["status"]=="COMPLETED"


if __name__=="__main__":main()
