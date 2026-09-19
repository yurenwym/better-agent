"""Shared durable canary admission and per-attempt reservations."""
from datetime import datetime, timezone
import json


def remaining_capacity(connection, deployment_id):
    row = connection.execute(
        "SELECT COALESCE(SUM(COALESCE(a.cost_microusd,r.amount_microusd)),0) amount,COUNT(*) calls "
        "FROM canary_attempt_reservations r LEFT JOIN model_attempts a ON a.id=r.attempt_id WHERE r.deployment_id=?",
        (deployment_id,),
    ).fetchone()
    return int(row["amount"]), int(row["calls"])


def authorization_valid(connection, deployment):
    from .evolution_contract import VERSION
    if deployment["release_contract_version"] != VERSION:
        return False
    now = datetime.now(timezone.utc).isoformat()
    candidate = connection.execute("SELECT * FROM evolution_candidates WHERE id=?", (deployment["candidate_id"],)).fetchone()
    approval = connection.execute("SELECT * FROM evolution_decisions WHERE id=?", (deployment["approval_id"],)).fetchone()
    if not candidate or not approval or not approval["expires_at"] or approval["expires_at"] <= now:
        return False
    if any(candidate[left] != approval[right] for left, right in (
        ("proposed_digest", "candidate_digest"), ("target_bundle_digest", "target_bundle_digest"),
        ("permission_diff_digest", "permission_diff_digest"),
    )):
        return False
    for source_id in json.loads(candidate["experience_ids_json"]):
        source = connection.execute("SELECT source_state FROM evolution_experiences WHERE id=? AND owner_id=?",
                                    (source_id, candidate["owner_id"])).fetchone()
        if not source or source["source_state"] != "ACTIVE":
            return False
    auth_id = json.loads(candidate["contract_json"] or "{}").get("content_authorization_id")
    if auth_id:
        auth = connection.execute("SELECT * FROM evolution_content_authorizations WHERE id=? AND owner_id=?",
                                  (auth_id, candidate["owner_id"])).fetchone()
        if not auth or auth["revoked_at"] or auth["expires_at"] <= now:
            return False
    return True


def stop_deployment(connection, deployment, reason):
    now = datetime.now(timezone.utc).isoformat()
    changed = connection.execute("UPDATE canary_deployments SET status='STOPPED',stop_reason=?,updated_at=? WHERE id=? AND status='ACTIVE'",
                                 (reason, now, deployment["id"])).rowcount
    if not changed:
        return
    # Existing assignments remain pinned; new tasks use stable. Never write stable.
    connection.execute(
        "UPDATE runtime_channels SET bundle_id=(SELECT bundle_id FROM runtime_channels WHERE name='stable'),version=version+1,updated_at=? "
        "WHERE name='canary' AND bundle_id=?", (now, deployment["challenger_bundle_id"]),
    )


def reserve_canary_attempt(connection, handle, attempt_id, worst_cost):
    from .costs import BudgetExceeded, monetary_limits_enabled
    deployment = connection.execute(
        "SELECT d.* FROM canary_deployments d JOIN canary_exposures e ON e.deployment_id=d.id "
        "WHERE e.run_id=? AND d.release_contract_version<>''", (handle.context.run_id,),
    ).fetchone()
    if deployment is None:
        return
    # This write serializes competing reservations in PostgreSQL and SQLite.
    connection.execute("UPDATE canary_deployments SET updated_at=updated_at WHERE id=?", (deployment["id"],))
    deployment = connection.execute("SELECT * FROM canary_deployments WHERE id=?", (deployment["id"],)).fetchone()
    if connection.execute("SELECT 1 FROM canary_attempt_reservations WHERE attempt_id=?", (attempt_id,)).fetchone():
        raise BudgetExceeded("canary attempt cannot be replayed")
    if deployment["status"] != "ACTIVE":
        raise BudgetExceeded("canary is stopped")
    if not authorization_valid(connection, deployment):
        raise BudgetExceeded("canary authorization or source is invalid")
    if not deployment["deadline_at"] or datetime.fromisoformat(deployment["deadline_at"]) <= datetime.now(timezone.utc):
        raise BudgetExceeded("canary approval window expired")
    amount, calls = remaining_capacity(connection, deployment["id"])
    if (monetary_limits_enabled() and (worst_cost is None or amount + worst_cost > int(deployment["budget_microusd"] or 0))) or calls >= int(deployment["max_calls"] or 0):
        raise BudgetExceeded("canary budget or call cap exhausted")
    connection.execute("INSERT INTO canary_attempt_reservations(attempt_id,deployment_id,amount_microusd,created_at) VALUES (?,?,?,?)",
                       (attempt_id, deployment["id"], worst_cost or 0, datetime.now(timezone.utc).isoformat()))
