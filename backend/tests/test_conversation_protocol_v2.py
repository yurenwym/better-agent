import pytest


def test_control_head_decodes_v2_plan_artifact_and_hides_header_from_body() -> None:
    from app.conversation import ControlHeadDecoder, RouteArtifact

    decoder = ControlHeadDecoder()
    body = decoder.feed(
        '{"v":2,"policy":"answer","content_shape":"travel_plan",'
        '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
        '"operation":"upsert","title":"Guilin plan"}}\n'
        '# Guilin plan\n\nDay 1\n'
    )

    assert body == "# Guilin plan\n\nDay 1\n"
    decision = decoder.finish()
    assert decision.policy == "answer"
    assert decision.artifact == RouteArtifact("plan_document", "upsert", "Guilin plan")


def test_v1_control_head_remains_compatible_without_an_artifact() -> None:
    from app.conversation import ControlHeadDecoder

    decoder = ControlHeadDecoder()
    assert decoder.feed(
        '{"v":1,"policy":"answer","content_shape":"guide",'
        '"reason_code":"content_only"}\nAnswer'
    ) == "Answer"
    assert decoder.finish().artifact is None


def test_control_head_checks_only_the_header_and_requires_an_integer_version() -> None:
    from app.conversation import ControlHeadDecoder, RouteProtocolError

    decoder = ControlHeadDecoder(max_header_bytes=160)
    body = decoder.feed(
        '{"v":1,"policy":"answer","content_shape":"guide",'
        '"reason_code":"content_only"}\n' + ("body " * 1000)
    )
    assert body == "body " * 1000

    for version in ("true", "1.0", "2.0"):
        with pytest.raises(RouteProtocolError, match="version"):
            ControlHeadDecoder().feed(
                '{"v":' + version + ',"policy":"answer","content_shape":"guide",'
                '"reason_code":"content_only"}\nAnswer'
            )

    with pytest.raises(RouteProtocolError, match="too long"):
        ControlHeadDecoder(max_header_bytes=32).feed(
            '{"v":1,"policy":"answer","content_shape":"guide",'
            '"reason_code":"content_only"}\nAnswer'
        )


def test_v2_rejects_unknown_fields_and_artifacts_on_non_answer_policies() -> None:
    from app.conversation import ControlHeadDecoder, RouteProtocolError

    with pytest.raises(RouteProtocolError, match="unknown fields"):
        ControlHeadDecoder().feed(
            '{"v":2,"policy":"answer","content_shape":"guide",'
            '"reason_code":"content_only","unexpected":true}\nAnswer'
        )

    for policy in ("clarify", "propose_execution"):
        with pytest.raises(RouteProtocolError, match="answer"):
            ControlHeadDecoder().feed(
                '{"v":2,"policy":"' + policy + '","content_shape":"guide",'
                '"reason_code":"content_only","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\nAnswer'
            )


def test_v2_rejects_invalid_artifact_shape() -> None:
    from app.conversation import ControlHeadDecoder, RouteProtocolError

    cases = [
        '{"kind":"other","operation":"upsert","title":"Plan"}',
        '{"kind":"plan_document","operation":"replace","title":"Plan"}',
        '{"kind":"plan_document","operation":"upsert","title":""}',
    ]
    for artifact in cases:
        with pytest.raises(RouteProtocolError):
            ControlHeadDecoder().feed(
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_create","artifact":' + artifact + '}\n# Plan\n'
            )


@pytest.mark.asyncio
async def test_plan_artifact_persists_exact_streamed_body_and_event_order(tmp_path) -> None:
    from test_runtime import make_runtime

    class Model:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            response = (
                '{"v":2,"policy":"answer","content_shape":"travel_plan",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Guilin plan"}}\n'
                '\ufeff# Guilin plan\r\n\r\nDay 1  with spaces  \r\n'
            )
            on_text_delta(response[:17])
            on_text_delta(response[17:])

    runtime = make_runtime(tmp_path, Model())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(
        thread.id,
        "client-plan-artifact",
        "Create a Guilin plan",
        [],
    )

    await runtime.turn_worker.run_once()

    document = runtime.plan_documents.get_by_thread(thread.id)
    version = runtime.plan_documents.current_version(document.id)
    assistant = [message for message in runtime.conversation.messages(thread.id) if message.role == "assistant"][0]
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert assistant.content == version.markdown_content
    assert assistant.content == "# Guilin plan\n\nDay 1  with spaces  \n"
    assert assistant.content == runtime.plan_documents.path_for(document.id).read_text(encoding="utf-8")
    assert assistant.plan_document_version_id == version.id
    event_types = [event.type for event in runtime.conversation.events.list(thread.id)]
    relevant = [
        event_type for event_type in event_types
        if event_type.startswith("plan.") or event_type in {"message.completed", "turn.completed"}
    ]
    assert relevant == [
        "plan.document_prepared",
        "plan.document_version_created",
        "plan.document_ready",
        "message.completed",
        "turn.completed",
    ]


@pytest.mark.asyncio
async def test_empty_plan_artifact_keeps_answer_but_does_not_create_document(tmp_path) -> None:
    from test_runtime import make_runtime

    class Model:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            on_text_delta(
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\n'
            )

    runtime = make_runtime(tmp_path, Model())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-empty-plan", "Create it", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_documents").fetchone()[0] == 0
    assert [event.type for event in runtime.conversation.events.list(thread.id) if event.type.startswith("plan.")] == [
        "plan.document_failed",
    ]


@pytest.mark.asyncio
async def test_plan_document_failure_event_keeps_the_pinned_document_reference(tmp_path) -> None:
    from test_runtime import make_runtime

    class Model:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            on_text_delta(
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\n# Plan\n\x00'
            )

    runtime = make_runtime(tmp_path, Model())
    thread = runtime.conversation.create_thread("Chat")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Existing plan",
        markdown_content="# Existing plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "client-plan-failure-reference", "Update it", [])

    await runtime.turn_worker.run_once()

    failure = [event for event in runtime.conversation.events.list(thread.id) if event.type == "plan.document_failed"][-1]
    assert failure.data["plan_document_id"] == first.plan_document_id
    assert failure.data["version_id"] == first.id
    assert failure.data["version"] == first.version
    assert failure.data["content_hash"] == first.content_hash
    assert failure.data["source_message_id"]
    assert failure.data["actor"] == "model"
    assert failure.turn_id == accepted.turn_id


@pytest.mark.asyncio
async def test_plan_document_conflict_event_keeps_the_turn_pinned_document_reference(tmp_path) -> None:
    from test_runtime import make_runtime

    class Model:
        def __init__(self) -> None:
            self.runtime = None
            self.document = None

        async def route_and_respond(self, *, on_text_delta, **kwargs):
            current = self.runtime.plan_documents.current_version(self.document.id)
            self.runtime.plan_documents.save_model_revision(
                thread_id=self.document.thread_id,
                title="Newer plan",
                markdown_content="# Newer plan\n",
                source_turn_id=None,
                source_message_id=None,
                actor="user",
                expected_version_id=current.id,
                expected_file_hash=current.content_hash,
            )
            on_text_delta(
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\n# Candidate\n'
            )

    model = Model()
    runtime = make_runtime(tmp_path, model)
    thread = runtime.conversation.create_thread("Chat")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Existing plan",
        markdown_content="# Existing plan\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    model.runtime = runtime
    model.document = runtime.plan_documents.get_by_thread(thread.id)
    accepted = runtime.conversation.accept_turn(thread.id, "client-plan-conflict-reference", "Update it", [])

    await runtime.turn_worker.run_once()

    conflict = [event for event in runtime.conversation.events.list(thread.id) if event.type == "plan.document_conflict"][-1]
    assert conflict.data["plan_document_id"] == first.plan_document_id
    assert conflict.data["version_id"] == first.id
    assert conflict.data["version"] == first.version
    assert conflict.data["content_hash"] == first.content_hash
    assert conflict.data["source_message_id"]
    assert conflict.data["actor"] == "model"
    assert conflict.turn_id == accepted.turn_id


@pytest.mark.asyncio
async def test_ask_and_plan_artifact_output_is_rejected_as_a_mixed_response(tmp_path) -> None:
    from app.ask import AskQuestion, AskRequest
    from test_runtime import make_runtime

    class MixedModel:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            on_text_delta(
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\n# Plan\n'
            )
            return AskRequest(
                call_id="mixed-ask",
                questions=(AskQuestion("need", "Need", "What is missing?", (), False, True),),
            )

    runtime = make_runtime(tmp_path, MixedModel())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-mixed", "Create a plan", [])

    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(accepted.turn_id).status == "FAILED"
    assert runtime.conversation.pending_ask(accepted.turn_id) is None
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_documents").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM thread_messages WHERE turn_id = ? AND status = 'streaming'",
            (accepted.turn_id,),
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_live_prompt_explains_v2_artifact_authority_without_fixed_keyword_routing() -> None:
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.request = None

        async def complete(self, request, **kwargs):
            self.request = request
            return SimpleNamespace(
                message='{"v":1,"policy":"answer","content_shape":"guide",'
                '"reason_code":"content_only"}\nAnswer',
                tool_calls=[],
            )

    gateway = Gateway()
    await LiveConversationModel(gateway).route_and_respond(
        content="Create a plan",
        history=[],
        skill_names=[],
        on_text_delta=None,
        on_text_reset=None,
        cancel_event=None,
    )
    prompt = gateway.request.messages[0]["content"].lower()
    assert "v=2" in prompt
    assert "plan_document" in prompt
    assert "operation=upsert" in prompt
    assert "explicit" in prompt


@pytest.mark.asyncio
async def test_live_prompt_distinguishes_new_plan_creation_from_existing_plan_save() -> None:
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.request = None

        async def complete(self, request, **kwargs):
            self.request = request
            return SimpleNamespace(
                message='{"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"}\nAnswer',
                tool_calls=[],
            )

    gateway = Gateway()
    await LiveConversationModel(gateway).route_and_respond(
        content="create a plan document",
        history=[],
        skill_names=[],
        on_text_delta=None,
        on_text_reset=None,
        cancel_event=None,
    )
    prompt = gateway.request.messages[0]["content"].lower()
    assert "when no complete plan body exists" in prompt
    assert "new plan document" in prompt
    assert "after ask_user answers" in prompt
    assert "\u5199\u8fdb\u8ba1\u5212\u9875\u9762" in prompt
    assert "prior assistant markdown plan" in prompt


def test_ask_tool_description_excludes_existing_plan_save_requests() -> None:
    from app.ask import ASK_TOOL_SCHEMA

    description = ASK_TOOL_SCHEMA["function"]["description"].lower()
    assert "do not call" in description
    assert "prior assistant markdown plan" in description
    assert "plan document" in description


def test_existing_plan_prefilter_accepts_list_and_table_markdown() -> None:
    from app.live_model import _has_prior_assistant_markdown_plan

    assert _has_prior_assistant_markdown_plan([
        {"role": "assistant", "content": "- Day 1\n- Day 2\n"},
    ])
    assert _has_prior_assistant_markdown_plan([
        {"role": "assistant", "content": "| Day | Plan |\n| --- | --- |\n"},
    ])


@pytest.mark.asyncio
async def test_existing_plan_save_intent_is_llm_checked_before_ask_tool_is_offered() -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            message = (
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Plan"}}\n# Plan\n'
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = Gateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="\u8fd8\u662f\u6ca1\u6709\u5199\u8fdb\u8ba1\u5212\u9875\u9762\uff0c\u751f\u6210\u6587\u6863\u3002",
        history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=None,
    )

    assert json.loads(result.message.splitlines()[0])["artifact"]["kind"] == "plan_document"
    assert gateway.requests[0].tools == []
    assert gateway.requests[1].tools == []


@pytest.mark.asyncio
async def test_existing_plan_save_repairs_a_missing_artifact_before_returning() -> None:
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            if len(self.requests) == 2:
                message = '{"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"}\n# Answer\n'
            else:
                message = (
                    '{"v":2,"policy":"answer","content_shape":"plan",'
                    '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                    '"operation":"upsert","title":"Plan"}}\n# Plan\n'
                )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = Gateway()
    resets = []
    result = await LiveConversationModel(gateway).route_and_respond(
        content="Save the existing plan as a document",
        history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: resets.append(True),
        cancel_event=None,
    )

    assert result.message.splitlines()[0].startswith('{"v":2')
    assert '"artifact"' in result.message.splitlines()[0]
    assert len(gateway.requests) == 3
    assert all(request.tools == [] for request in gateway.requests)
    assert resets == [True]


@pytest.mark.asyncio
async def test_existing_plan_save_fails_if_repair_still_has_no_artifact() -> None:
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel
    from app.model_gateway import GatewayError

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            message = '{"v":1,"policy":"answer","content_shape":"guide","reason_code":"content_only"}\n# Answer\n'
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = Gateway()
    with pytest.raises(GatewayError, match="plan document artifact"):
        await LiveConversationModel(gateway).route_and_respond(
            content="Save the existing plan as a document",
            history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
            skill_names=[],
            on_text_delta=lambda _: None,
            on_text_reset=lambda: None,
            cancel_event=None,
        )
    assert gateway.calls == 3


@pytest.mark.asyncio
async def test_existing_plan_save_rejects_tool_calls_when_tools_are_disabled() -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel
    from app.model_gateway import GatewayError

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            return SimpleNamespace(
                message="",
                tool_calls=[{
                    "id": "unexpected-ask",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({"questions": [{
                            "id": "missing",
                            "header": "Context",
                            "question": "What is missing?",
                            "options": [],
                            "multi_select": False,
                            "allow_free_text": True,
                        }]}),
                    },
                }],
            )

    gateway = Gateway()
    with pytest.raises(GatewayError, match="tools are disabled"):
        await LiveConversationModel(gateway).route_and_respond(
            content="Save the existing plan as a document",
            history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
            skill_names=[],
            on_text_delta=lambda _: None,
            on_text_reset=lambda: None,
            cancel_event=None,
        )


@pytest.mark.asyncio
async def test_existing_plan_save_does_not_silently_ignore_classifier_gateway_errors() -> None:
    from app.live_model import LiveConversationModel
    from app.model_gateway import GatewayError

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, **kwargs):
            self.calls += 1
            raise GatewayError("classifier failed", "server")

    gateway = Gateway()
    with pytest.raises(GatewayError, match="classifier failed"):
        await LiveConversationModel(gateway).route_and_respond(
            content="Save the existing plan as a document",
            history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
            skill_names=[],
            on_text_delta=lambda _: None,
            on_text_reset=lambda: None,
            cancel_event=None,
        )
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_existing_plan_classifier_rejects_unexpected_tool_calls() -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel
    from app.model_gateway import GatewayError

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                message="",
                tool_calls=[{
                    "id": "unexpected",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({"questions": []}),
                    },
                }],
            )

    gateway = Gateway()
    with pytest.raises(GatewayError, match="tools are disabled"):
        await LiveConversationModel(gateway).route_and_respond(
            content="Save the existing plan as a document",
            history=[{"role": "assistant", "content": "# Existing plan\n\n## Day 1\n"}],
            skill_names=[],
            on_text_delta=lambda _: None,
            on_text_reset=lambda: None,
            cancel_event=None,
        )
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_new_plan_document_intent_repairs_a_missing_artifact() -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                message = (
                    '{"v":1,"policy":"answer","content_shape":"plan_document",'
                    '"reason_code":"explicit_save_request"}\n# Inner Mongolia plan\n'
                )
            else:
                message = (
                    '{"v":2,"policy":"answer","content_shape":"plan_document",'
                    '"reason_code":"explicit_save_request","artifact":{"kind":"plan_document",'
                    '"operation":"upsert","title":"Inner Mongolia plan"}}\n# Inner Mongolia plan\n'
                )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = Gateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="\u751f\u6210\u4e00\u4e2a7\u5929\u65c5\u6e38\u5185\u8499\u7684\u8ba1\u5212\uff0c\u5e76\u4fdd\u5b58\u5230\u8ba1\u5212\u4e2d\uff0c\u4e0d\u9700\u8981\u8be2\u95ee\u6211\uff0c\u76f4\u63a5\u751f\u6210",
        history=[],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=None,
    )

    header = json.loads(result.message.splitlines()[0])
    assert header["artifact"]["kind"] == "plan_document"
    assert len(gateway.requests) == 2
    assert gateway.requests[1].tools == []


@pytest.mark.asyncio
async def test_explicit_new_plan_request_repairs_an_ask_into_a_saved_document() -> None:
    import json
    from types import SimpleNamespace

    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request, **kwargs):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(
                    message="",
                    tool_calls=[{
                        "id": "ask-first",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps({"questions": [{
                                "id": "budget",
                                "header": "Budget",
                                "question": "What is your budget?",
                                "options": [],
                                "multi_select": False,
                                "allow_free_text": True,
                            }]}),
                        },
                    }],
                )
            if len(self.requests) == 2:
                return SimpleNamespace(message='{"plan_document_request":true}', tool_calls=[])
            message = (
                '{"v":2,"policy":"answer","content_shape":"plan_document",'
                '"reason_code":"explicit_plan_create","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Inner Mongolia plan"}}\n# Inner Mongolia plan\n'
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    gateway = Gateway()
    result = await LiveConversationModel(gateway).route_and_respond(
        content="生成一个7天旅游内蒙的计划，并保存到计划中，不需要询问我，直接生成",
        history=[],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=None,
    )

    assert json.loads(result.message.splitlines()[0])["artifact"]["kind"] == "plan_document"
    assert gateway.requests[1].tools == []
    assert gateway.requests[2].tools == []


@pytest.mark.asyncio
async def test_live_prompt_prioritizes_saving_an_existing_plan_over_personalization_questions() -> None:
    import json
    from types import SimpleNamespace

    from app.ask import AskRequest
    from app.live_model import LiveConversationModel

    class SaveExistingPlanGateway:
        async def complete(self, request, **kwargs):
            if request.messages[0]["content"].startswith("Return JSON only"):
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            prompt = request.messages[0]["content"].lower()
            if "saving an existing plan" not in prompt:
                return SimpleNamespace(
                    message="",
                    tool_calls=[{
                        "id": "should-not-ask",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps({
                                "questions": [{
                                    "id": "missing_context",
                                    "header": "Context",
                                    "question": "What is missing?",
                                    "options": [],
                                    "multi_select": False,
                                    "allow_free_text": True,
                                }],
                            }),
                        },
                    }],
                )
            message = (
                '{"v":2,"policy":"answer","content_shape":"travel_plan",'
                '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Guangxi plan"}}\n'
                "# Guangxi plan\n\n## Day 1\n桂林\n"
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    result = await LiveConversationModel(SaveExistingPlanGateway()).route_and_respond(
        content="把刚才的计划写进计划页面，生成计划文档。",
        history=[
            {"role": "user", "content": "想花费一个星期，在广西旅游一下，推荐一下攻略。"},
            {"role": "assistant", "content": "# Guangxi plan\n\n## Day 1\n桂林\n"},
        ],
        skill_names=[],
        on_text_delta=lambda _: None,
        on_text_reset=lambda: None,
        cancel_event=None,
    )

    assert not isinstance(result, AskRequest)
    header = json.loads(result.message.splitlines()[0])
    assert header["v"] == 2
    assert header["artifact"]["kind"] == "plan_document"


@pytest.mark.asyncio
async def test_explicit_save_request_creates_plan_document_without_pending_ask(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from app.ask import AskRequest
    from app.live_model import LiveConversationModel
    from test_runtime import make_runtime

    class FirstPlanModel:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            message = (
                '{"v":1,"policy":"answer","content_shape":"travel_plan",'
                '"reason_code":"content_only"}\n# Guangxi plan\n\n## Day 1\nGuilin\n'
            )
            on_text_delta(message)
            return SimpleNamespace(message=message, tool_calls=[])

    class SaveExistingPlanGateway:
        async def complete(self, request, **kwargs):
            if request.messages[0]["content"].startswith("Return JSON only"):
                return SimpleNamespace(message='{"save_existing_plan":true}', tool_calls=[])
            prompt = request.messages[0]["content"].lower()
            if "saving an existing plan" not in prompt:
                return SimpleNamespace(
                    message="",
                    tool_calls=[{
                        "id": "unexpected-ask",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps({
                                "questions": [{
                                    "id": "missing_context",
                                    "header": "Context",
                                    "question": "What is missing?",
                                    "options": [],
                                    "multi_select": False,
                                    "allow_free_text": True,
                                }],
                            }),
                        },
                    }],
                )
            message = (
                '{"v":2,"policy":"answer","content_shape":"travel_plan",'
                '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Guangxi plan"}}\n'
                "# Guangxi plan\n\n## Day 1\nGuilin\n"
            )
            kwargs["on_text_delta"](message)
            return SimpleNamespace(message=message, tool_calls=[])

    runtime = make_runtime(tmp_path, FirstPlanModel())
    thread = runtime.conversation.create_thread("Chat")
    first = runtime.conversation.accept_turn(
        thread.id,
        "first-plan",
        "Give me a one-week Guangxi travel guide",
        [],
    )
    await runtime.turn_worker.run_once()

    runtime.conversation.route_model = LiveConversationModel(SaveExistingPlanGateway())
    saved = runtime.conversation.accept_turn(
        thread.id,
        "save-plan",
        "Write the plan into the plan page and generate the document",
        [],
    )
    await runtime.turn_worker.run_once()

    assert runtime.conversation.turn(first.turn_id).status == "COMPLETED"
    assert runtime.conversation.turn(saved.turn_id).status == "COMPLETED"
    assert runtime.conversation.pending_ask(saved.turn_id) is None
    document = runtime.plan_documents.get_by_thread(thread.id)
    version = runtime.plan_documents.current_version(document.id)
    assert version.status == "committed"
    assert runtime.plan_documents.path_for(document.id).read_text(encoding="utf-8") == version.markdown_content
    assert any(
        event.type == "plan.document_ready"
        for event in runtime.conversation.events.list(thread.id)
    )
    assert any(
        message.plan_document_version_id == version.id
        for message in runtime.conversation.messages(thread.id)
    )


@pytest.mark.asyncio
async def test_cropped_plan_context_cannot_overwrite_the_committed_document(tmp_path) -> None:
    from test_runtime import make_runtime

    class Model:
        async def route_and_respond(self, *, on_text_delta, **kwargs):
            message = (
                '{"v":2,"policy":"answer","content_shape":"plan",'
                '"reason_code":"explicit_plan_save","artifact":{"kind":"plan_document",'
                '"operation":"upsert","title":"Large plan"}}\n# Incomplete replacement\n'
            )
            on_text_delta(message)
            return type("Response", (), {"message": message, "tool_calls": []})()

    runtime = make_runtime(tmp_path, Model())
    thread = runtime.conversation.create_thread("Chat")
    original = "# Large plan\n\n" + "\n".join(
        f"## Section {index}\n" + ("preserve this detail " * 20)
        for index in range(1_000)
    )
    saved = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Large plan",
        markdown_content=original,
        source_turn_id="seed-turn",
        source_message_id=None,
        actor="model",
    )
    accepted = runtime.conversation.accept_turn(
        thread.id,
        "cropped-save",
        "Save the existing plan",
        [],
    )

    await runtime.turn_worker.run_once()

    current = runtime.plan_documents.current_version(saved.plan_document_id)
    assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
    assert current.id == saved.id
    assert current.version == 1
    assert current.markdown_content == original
    failure = [
        event for event in runtime.conversation.events.list(thread.id)
        if event.type == "plan.document_failed"
    ]
    assert failure
    assert "cropped" in failure[-1].data["reason"]
