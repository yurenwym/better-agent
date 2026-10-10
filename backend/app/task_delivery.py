"""Validate persisted model dependencies before committing an asynchronous result."""
from .model_control import ModelCallContext
from .learning import LearningConflict


def require_delivery_sources(db, connection, *, run_id, agent_task_id=None, expert=False, control_store=None):
    # Expert synthesis also depends on the child results in the same run.
    predicate, identity = "run_id=?", run_id
    if expert:
        predicate = "agent_task_id IN (SELECT id FROM agent_tasks WHERE agent_run_id=?)"
        if agent_task_id is not None:
            predicate, identity = "agent_task_id=?", agent_task_id
    invocations = connection.execute(
        f"SELECT id,owner_id,runtime_bundle_id,role,purpose FROM model_invocations WHERE {predicate}",
        (identity,),
    ).fetchall()
    lock = " FOR UPDATE" if db.backend == "postgresql" else ""
    for invocation in invocations:
        for table, key in (("learning_snapshots", "task_id"), ("memory_context_pins", "model_invocation_id")):
            predicate = " AND task_kind='invocation'" if table == "learning_snapshots" else ""
            rows = connection.execute(
                f"SELECT invalidated_at FROM {table} WHERE {key}=?" + predicate + lock,
                (invocation["id"],),
            ).fetchall()
            if any(row["invalidated_at"] for row in rows):
                raise LearningConflict("task result source was revoked")
        if control_store is not None:
            control_store.assert_sources_active(invocation["id"], ModelCallContext(
                role=invocation["role"], purpose=invocation["purpose"],
                owner_id=invocation["owner_id"], runtime_bundle_id=invocation["runtime_bundle_id"],
            ))
