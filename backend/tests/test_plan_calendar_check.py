import pytest

from app.plan_calendar_check import PlanCalendarMismatch,validate_plan_calendar


def plan(count):
    return f"""# 2026-09-14 Python plan
| 阶段 | 日期 | 学习日 |
| --- | --- | --- |
| 入门 | 9/14-9/15 | {count} 天 |

| 日 | 日期 | 任务 |
| --- | --- | --- |
| 1 | 9/14 一 | pandas |
| 2 | 9/15 二 | Jupyter |
"""


def test_explicit_stage_count_must_match_daily_table():
    validate_plan_calendar(plan(2))
    with pytest.raises(PlanCalendarMismatch,match="实际为2"):
        validate_plan_calendar(plan(1))


def test_calendar_check_does_not_parse_code_fences_or_prose():
    validate_plan_calendar("```markdown\n"+plan(1)+"\n```")
    validate_plan_calendar("学习日由用户决定，日期稍后确认。")


def test_bad_calendar_cannot_create_document(tmp_path):
    from app.db import Database
    from app.conversation import ConversationService
    db=Database(tmp_path/"agent.db")
    conversation=ConversationService(db)
    thread=conversation.create_thread("test")
    with pytest.raises(PlanCalendarMismatch):
        conversation.plan_documents.save_model_revision(thread_id=thread.id,title="Plan",markdown_content=plan(1),source_turn_id=None,source_message_id=None,actor="model")
    assert conversation.plan_documents.list_documents()==[]
