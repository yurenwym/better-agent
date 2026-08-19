from app.context_policy import requires_context_collection


def test_personalized_training_plan_requires_context_collection() -> None:
    assert requires_context_collection("\u6211\u60f3\u5236\u4f5c\u4e00\u4e2a\u957f\u671f\u7684\u8bad\u7ec3\u8ba1\u5212\uff0c\u5b66\u4e60\u9a91\u884c", []) is True
    assert requires_context_collection("help me create a six-week English learning plan", []) is True


def test_ordinary_content_requests_do_not_require_context_collection() -> None:
    assert requires_context_collection("what is bicycle FTP?", []) is False
    assert requires_context_collection("give me a seven-day Guilin travel guide", []) is False
    assert requires_context_collection("write a training plan template", []) is False


def test_answered_ask_continuation_is_not_asked_again() -> None:
    history = [
        {"role": "assistant", "content": "Please provide a little more information."},
        {"role": "tool", "tool_call_id": "call-1", "content": '{"answers": []}'},
    ]

    assert requires_context_collection("I provided the information, please continue", history) is False
