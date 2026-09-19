import json

import pytest


def _call(name: str, payload: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(payload, ensure_ascii=False)}}


def _questions() -> dict:
    return {
        "questions": [{
            "id": "feeling", "header": "今天的状态", "question": "今天训练下来感觉如何？",
            "options": [
                {"label": "轻松", "description": "还能再加一点"},
                {"label": "吃力", "description": "勉强完成"},
            ],
            "multi_select": False, "allow_free_text": True,
        }]
    }


def test_review_tool_is_exposed_alongside_ask_user() -> None:
    from app.ask import CONVERSATION_TOOL_NAMES, CONVERSATION_TOOL_SCHEMAS

    assert CONVERSATION_TOOL_NAMES == {"ask_user", "review_check_in"}
    assert [schema["function"]["name"] for schema in CONVERSATION_TOOL_SCHEMAS] == ["ask_user", "review_check_in"]


def test_parser_accepts_the_review_tool_call() -> None:
    from app.ask import parse_ask_tool_call

    request = parse_ask_tool_call(_call("review_check_in", _questions()))

    assert [question.id for question in request.questions] == ["feeling"]
    assert request.questions[0].header == "今天的状态"


def test_parser_still_rejects_unknown_tools() -> None:
    from app.ask import AskValidationError, parse_ask_tool_call

    with pytest.raises(AskValidationError):
        parse_ask_tool_call(_call("delete_everything", _questions()))
