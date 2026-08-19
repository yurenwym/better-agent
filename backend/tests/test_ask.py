import json

import pytest


def _call(payload: dict, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "function": {
            "name": "ask_user",
            "arguments": json.dumps(payload, ensure_ascii=False),
        },
    }


def _request_payload() -> dict:
    return {
        "questions": [
            {
                "id": "training_level",
                "header": "训练水平",
                "question": "你目前的训练水平是什么？",
                "options": [
                    {"label": "新手", "description": "刚开始训练"},
                    {"label": "有基础", "description": "已有规律训练习惯"},
                ],
                "multi_select": False,
                "allow_free_text": True,
            }
        ]
    }


def test_ask_tool_schema_has_bounded_questions_and_no_unknown_fields():
    from app.ask import ASK_TOOL_SCHEMA

    schema = ASK_TOOL_SCHEMA["function"]["parameters"]
    assert schema["properties"]["questions"]["maxItems"] == 4
    assert schema["additionalProperties"] is False
    assert schema["properties"]["questions"]["items"]["additionalProperties"] is False


def test_parse_ask_tool_call_normalizes_questions():
    from app.ask import parse_ask_tool_call

    request = parse_ask_tool_call(_call(_request_payload()))

    assert request.call_id == "call-1"
    assert request.questions[0].id == "training_level"
    assert request.questions[0].options[0]["label"] == "新手"


def test_parse_ask_tool_call_rejects_duplicate_question_ids_and_bad_option_counts():
    from app.ask import AskValidationError, parse_ask_tool_call

    duplicate = {
        "questions": [
            {"id": "same", "header": "A", "question": "A?", "options": [], "multi_select": False, "allow_free_text": True},
            {"id": "same", "header": "B", "question": "B?", "options": [], "multi_select": False, "allow_free_text": True},
        ]
    }
    bad_options = {
        "questions": [
            {"id": "only", "header": "A", "question": "A?", "options": [{"label": "一个", "description": ""}], "multi_select": False, "allow_free_text": False},
        ]
    }

    with pytest.raises(AskValidationError):
        parse_ask_tool_call(_call(duplicate))
    with pytest.raises(AskValidationError):
        parse_ask_tool_call(_call(bad_options))


def test_normalize_answers_rejects_unknown_options_and_accepts_free_text():
    from app.ask import normalize_answers, parse_ask_tool_call

    request = parse_ask_tool_call(_call(_request_payload()))

    normalized = normalize_answers(
        request.questions,
        [{"question_id": "training_level", "selected_options": [], "free_text": "我每周训练三次"}],
    )

    assert normalized[0]["free_text"] == "我每周训练三次"
    with pytest.raises(ValueError):
        normalize_answers(
            request.questions,
            [{"question_id": "training_level", "selected_options": ["未知"], "free_text": ""}],
        )


def test_normalize_answers_rejects_missing_question_and_multiple_single_choice_options():
    from app.ask import normalize_answers, parse_ask_tool_call

    request = parse_ask_tool_call(_call(_request_payload()))

    with pytest.raises(ValueError):
        normalize_answers(request.questions, [])
    with pytest.raises(ValueError):
        normalize_answers(
            request.questions,
            [{"question_id": "training_level", "selected_options": ["新手", "有基础"], "free_text": ""}],
        )


def test_answer_helpers_are_user_readable_and_model_safe():
    from app.ask import format_answer_message, parse_ask_tool_call, tool_result_payload

    request = parse_ask_tool_call(_call(_request_payload()))
    answers = [{"question_id": "training_level", "selected_options": ["有基础"], "free_text": ""}]

    assert format_answer_message(request.questions, answers) == "我补充了以下信息：\n- 训练水平：有基础"
    result = tool_result_payload(request.questions, answers)
    assert result["answers"][0]["selected_options"] == ["有基础"]
    assert "call-1" not in format_answer_message(request.questions, answers)


def test_database_initializes_durable_turn_asks_table(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(turn_asks)").fetchall()
        }
        assert {"id", "turn_id", "call_id", "questions_json", "status", "answer_json", "continuation_turn_id"} <= columns
