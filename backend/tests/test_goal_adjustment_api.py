from fastapi.testclient import TestClient

from test_goal_program_api import headers, setup_app


def test_adjustment_http_reject_and_conflict_contract(tmp_path,monkeypatch) -> None:
    monkeypatch.setattr("app.goal_adjustments._local_date",lambda _:"2026-09-01")
    runtime,app,client,version=setup_app(tmp_path)
    payload={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60}
    draft=client.post(f"/api/plans/{version.plan_document_id}/program-preview",json=payload,headers=headers(app,"preview")).json()
    active=client.post(f"/api/programs/{draft['id']}/activate",json={"expected_version":draft["version"]},headers=headers(app,"activate")).json()
    proposed=client.post(f"/api/programs/{draft['id']}/adjustments",json={"reason":"降低难度","expected_version":active["version"]},headers=headers(app,"propose"))
    assert proposed.status_code==200
    proposal=proposed.json()
    rejected=client.post(f"/api/adjustments/{proposal['id']}/reject",json={"expected_version":proposal["version"]},headers=headers(app,"reject"))
    conflict=client.post(f"/api/adjustments/{proposal['id']}/accept",json={"expected_version":proposal["version"]},headers=headers(app,"accept"))
    assert rejected.status_code==200 and rejected.json()["status"]=="REJECTED"
    assert conflict.status_code==409 and "current" in conflict.json()
