def test_control_plane_exports_are_redacted_and_omit_private_content(tmp_path, monkeypatch) -> None:
    from app.control_exports import cost_export, invocation_export, skill_audit_export
    from app.db import Database
    from app.skill_platform import SkillPlatform
    from test_skill_platform import _skill_zip

    secret = "sk-live-secret-value"
    monkeypatch.setenv("MODEL_EXPORT_KEY", secret)
    db = Database(tmp_path / "agent.db")
    platform = SkillPlatform(db, tmp_path / "skills")
    preview = platform.preview_install(_skill_zip(document="# Private\n\nuser memory body"))
    platform.confirm_install(preview["install_token"], granted_tools=[], idempotency_key="install")
    with db.transaction() as connection:
        connection.execute("INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES ('p','local-user','P','ACTIVE','now','now')")
        connection.execute(
            "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at) "
            "VALUES ('pv','p',1,'openai_compatible','provider','https://example.com','model','MODEL_EXPORT_KEY','{}',1,1,1,1,'cfg','now')"
        )
        connection.execute(
            "INSERT INTO model_invocations(id,owner_id,role,purpose,routing_policy_digest,route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at) "
            "VALUES ('inv','local-user','planner','plan','route','{}','request-secret-digest','tools','context','FAILED','inv','now')"
        )
    exported = cost_export(db) + invocation_export(db) + skill_audit_export(db)
    assert secret not in exported
    assert "Authorization" not in exported
    assert "user memory body" not in exported
    assert "request-secret-digest" in exported
    assert preview["package_digest"] in exported
