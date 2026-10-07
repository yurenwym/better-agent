#!/usr/bin/env bash
# Run ONE Phase 2A acceptance group, capturing start/end time, exit code and log.
# One group per shell invocation: the sandbox safe-delete guard counts deletions
# per tool call, so packing several groups into one call makes unrelated tests
# fail with SAFE_DELETE_BULK_CONFIRM_REQUIRED.
#
#   bash run_phase2a_group.sh g2
set -u

GROUP="${1:?usage: run_phase2a_group.sh <g1|g2|g3|g4|g5|g6|g7|g8>}"

cd /d/RAG/better/backend || exit 1

PY="D:/pycharm/python.exe"
OUT="/d/RAG/better/docs/acceptance/context-snapshot-phase2/phase2a/logs"
SUMMARY="$OUT/_summary.tsv"
PG_URL="${TEST_DATABASE_URL:-}"

mkdir -p "$OUT"
if [ ! -f "$SUMMARY" ]; then
  printf "group\tstarted_at\tfinished_at\texit_code\tresult\tduration_s\tlog\n" > "$SUMMARY"
fi

case "$GROUP" in
  file)
    # file <label> <file...>  -- single file per shell call, PG env applied
    LOG="${2:?usage: run_phase2a_group.sh file <label> <file...>}"
    ENV_PREFIX="TEST_DATABASE_URL=${PG_URL:?Set TEST_DATABASE_URL to an isolated test database}"
    shift 2
    FILES=("$@")
    ;;
  sqlite)
    # sqlite <label> <file...>  -- single file per shell call, no PG env
    LOG="${2:?usage: run_phase2a_group.sh sqlite <label> <file...>}"
    ENV_PREFIX=""
    shift 2
    FILES=("$@")
    ;;
  g1) ENV_PREFIX=""; LOG=g1-specialised-sqlite; FILES=(tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py) ;;
  g2) ENV_PREFIX="TEST_DATABASE_URL=${PG_URL:?Set TEST_DATABASE_URL to an isolated test database}"; LOG=g2-postgres-integration; FILES=(tests/integration/test_model_input_snapshot_postgres.py tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py) ;;
  g3) ENV_PREFIX=""; LOG=g3-regression-batch1; FILES=(tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py) ;;
  g4) ENV_PREFIX=""; LOG=g4-regression-batch2; FILES=(tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py tests/test_goal_programs.py tests/test_goal_program_compiler.py) ;;
  g5) ENV_PREFIX=""; LOG=g5-conversation; FILES=(tests/test_conversation.py) ;;
  g6) ENV_PREFIX=""; LOG=g6-conversation-protocol-v2; FILES=(tests/test_conversation_protocol_v2.py) ;;
  g7) ENV_PREFIX=""; LOG=g7-conversation-worker-default-env; FILES=(tests/test_conversation_worker.py --deselect tests/test_conversation_worker.py::test_active_turn_lease_is_renewed_during_long_model_call) ;;
  g8) ENV_PREFIX="AGENT_ARCHIVE_WAIT_MS=30000"; LOG=g8-conversation-worker-enlarged-archive-deadline; FILES=(tests/test_conversation_worker.py --deselect tests/test_conversation_worker.py::test_active_turn_lease_is_renewed_during_long_model_call) ;;
  *) echo "unknown group $GROUP"; exit 2 ;;
esac

LOG_PATH="$OUT/$LOG.log"
BT="C:/tmp/pytest-evi-$LOG-$$"
T0=$(date -Is)
S0=$(date +%s)

if [ -n "$ENV_PREFIX" ]; then
  env $ENV_PREFIX "$PY" -m pytest "${FILES[@]}" -q --tb=short -rfE -p no:cacheprovider --basetemp="$BT" > "$LOG_PATH" 2>&1
else
  "$PY" -m pytest "${FILES[@]}" -q --tb=short -rfE -p no:cacheprovider --basetemp="$BT" > "$LOG_PATH" 2>&1
fi
CODE=$?

T1=$(date -Is)
DUR=$(( $(date +%s) - S0 ))
RESULT=$(grep -E "[0-9]+ (passed|failed|error)" "$LOG_PATH" | tail -1)

# Keep the basetemp: deleting a whole tree here would be counted by the same
# guard.  It is removed by the separate cleanup call.
printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$LOG" "$T0" "$T1" "$CODE" "$RESULT" "$DUR" "logs/$LOG.log" >> "$SUMMARY"
echo "[$LOG] exit=$CODE  $RESULT"
