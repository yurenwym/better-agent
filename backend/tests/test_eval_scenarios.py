import pytest


def test_deterministic_v1_suite_contains_twelve_passing_scenarios() -> None:
    from app.evals import run_deterministic_suite

    report = run_deterministic_suite()

    assert len(report.results) == 12
    assert [result.name for result in report.results] == [
        "normal-loop",
        "clarification",
        "plan-revision",
        "step-cancel",
        "write-rejection",
        "unknown-tool",
        "rate-limit-retry",
        "authentication-blocked",
        "budget-recovery",
        "tool-failure",
        "memory-confirmation",
        "restart-recovery",
    ]
    assert report.passed == 12
    assert all(result.passed for result in report.results)


def test_eval_command_writes_json_and_markdown_report_without_runtime_data(tmp_path) -> None:
    from app.eval import run_evaluation

    result = run_evaluation("v1", "deterministic", results_dir=tmp_path)

    assert result.report.passed == 12
    assert result.json_path.exists()
    assert result.markdown_path.exists()
    assert "AGENT_MODEL_API_KEY" not in result.json_path.read_text(encoding="utf-8")


def test_live_cli_smoke_is_an_explicit_offline_path(monkeypatch) -> None:
    import asyncio

    from app.eval import _run_live_smoke
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    profile = ModelProfile("https://provider.invalid/v1", "demo", "MISSING_KEY")
    # The default unbound gateway is refused before any credential or network use.
    with pytest.raises(GatewayError) as refused:
        asyncio.run(ModelGateway(profile).complete(ModelRequest(messages=[{"role": "user", "content": "x"}])))
    assert refused.value.kind == "configuration"
    assert "offline" in str(refused.value)
    # The CLI smoke opts in explicitly, so it reaches the credential check instead.
    monkeypatch.delenv("MISSING_KEY", raising=False)
    report = _run_live_smoke(profile)
    assert report.failed == 1
    assert "gateway=configuration" in report.results[0].detail
