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
