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
                '# Guilin plan\n\nDay 1  with spaces  \n'
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
