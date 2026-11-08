import pytest

from nova.evaluation import evaluate_catalog, evaluate_retrieval


@pytest.mark.parametrize("size", [1, 51])
def test_catalog_baseline_reaches_tail_and_measures_context(size):
    result = evaluate_catalog(size)
    assert result["discovered_skills"] == size
    assert result["last_skill_found"]
    assert result["prompt_within_budget"]
    assert result["full_tool_schema_tokens"] > 0


def test_catalog_baseline_rejects_unbounded_fixture():
    with pytest.raises(ValueError):
        evaluate_catalog(10001)


@pytest.mark.parametrize("size", [10, 100, 1000])
def test_retrieval_suite_reports_control_and_bounded_selection(size):
    report = evaluate_retrieval(size)
    assert report["dataset_version"] == 1
    assert report["catalog_size"] == size
    assert {case["split"] for case in report["cases"]} == {"tuning", "heldout"}
    assert len({case["case_id"] for case in report["cases"]}) == len(report["cases"])
    for case in report["cases"]:
        assert case["full"]["availability_recall"] == 1
        assert case["selected"]["budget_violations"] == 0
        assert case["selected"]["schema_snapshot_tokens"] > 0
        assert case["selected"]["discovery_calls"] == len(case["steps"])
    if size == 1000:
        assert all(
            case["selected"]["schema_snapshot_tokens"] < case["full"]["schema_snapshot_tokens"]
            for case in report["cases"]
        )


def test_retrieval_suite_keeps_misses_and_multistep_evidence():
    report = evaluate_retrieval(100)
    cases = {case["case_id"]: case for case in report["cases"]}
    miss = cases["heldout-paraphrase"]
    assert miss["selected"]["retrieval_recall"] == 0
    assert miss["selected"]["availability_recall"] == 0
    assert miss["steps"][0]["missing_tools"] == ["mcp__weather__forecast"]
    workflow = cases["tuning-workflow"]
    assert len(workflow["steps"]) == 2
    assert workflow["selected"]["availability_recall"] == 1
    assert {"mcp__issues__search", "mcp__issues__comment"} <= set(
        workflow["steps"][-1]["selected_tools"]
    )
    absent = cases["heldout-absent"]
    assert absent["steps"][0]["retrieved_tools"] == []
    assert absent["selected"]["retrieval_precision"] == 1


def test_retrieval_splits_are_disjoint_and_reproducible():
    tuning = evaluate_retrieval(10, split="tuning")
    heldout = evaluate_retrieval(10, split="heldout")
    assert {case["case_id"] for case in tuning["cases"]}.isdisjoint(
        case["case_id"] for case in heldout["cases"]
    )
    assert evaluate_retrieval(10, split="heldout") == heldout


@pytest.mark.parametrize(
    "kwargs", [{"size": 9}, {"size": True}, {"size": 10001}, {"size": 10, "split": "invalid"}]
)
def test_retrieval_suite_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        evaluate_retrieval(**kwargs)


def test_retrieval_scores_actual_results_instead_of_expected_labels(monkeypatch):
    from nova.tool_catalog import ToolCatalog

    original = ToolCatalog.discover

    def no_matches(self, query, **kwargs):
        result = original(self, "nonexistentcapability", **kwargs)
        return result

    monkeypatch.setattr(ToolCatalog, "discover", no_matches)
    report = evaluate_retrieval(100, split="tuning")
    assert all(case["selected"]["retrieval_recall"] == 0 for case in report["cases"])
    assert all(case["selected"]["availability_recall"] == 0 for case in report["cases"])


def test_retrieval_cli_writes_misses_and_logs_split_metrics(monkeypatch, tmp_path, capsys):
    import json
    import sys
    from unittest.mock import MagicMock

    from nova.evaluation import main

    mlflow = MagicMock()
    monkeypatch.setitem(sys.modules, "mlflow", mlflow)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "--suite",
            "retrieval",
            "--sizes",
            "100",
            "--split",
            "heldout",
            "--output",
            str(output),
            "--tracking-uri",
            "http://127.0.0.1:5000",
        ],
    )
    main()
    report = json.loads(output.read_text())
    assert json.loads(capsys.readouterr().out) == report
    assert report["results"] == []
    assert len(report["retrieval_results"]) == 1
    assert all(case["split"] == "heldout" for case in report["retrieval_results"][0]["cases"])
    logged = mlflow.log_metrics.call_args_list[-1].args[0]
    assert 0 < logged["retrieval_100.heldout.selected.retrieval_recall"] < 1
    assert logged["retrieval_100.heldout.selected.budget_violations"] == 0
    mlflow.log_dict.assert_called_once_with(report, "catalog-results.json")


def test_retrieval_cli_fails_on_budget_invariant(monkeypatch, capsys):
    import sys

    import nova.evaluation as evaluation

    report = evaluation.evaluate_retrieval(10)
    report["cases"][0]["selected"]["budget_violations"] = 1
    monkeypatch.setattr(evaluation, "evaluate_retrieval", lambda *args, **kwargs: report)
    monkeypatch.setattr(sys, "argv", ["evaluation", "--suite", "retrieval", "--sizes", "10"])
    with pytest.raises(SystemExit) as error:
        evaluation.main()
    assert error.value.code == 1


def test_retrieval_reports_budget_blocking_without_schema_truncation():
    from nova.evaluation import evaluate_retrieval
    from nova.tokens import estimate_total_request_tokens
    from nova.tool_catalog import DISCOVERY_SCHEMA

    budget = estimate_total_request_tokens([], tools=[DISCOVERY_SCHEMA])
    report = evaluate_retrieval(100, max_tokens=budget)
    exact = next(case for case in report["cases"] if case["case_id"] == "tuning-exact")
    assert exact["selected"]["retrieval_recall"] == 1
    assert exact["selected"]["availability_recall"] == 0
    assert exact["steps"][0]["budget_blocked_tools"] == ["mcp__warehouse__query"]
    assert exact["selected"]["budget_violations"] == 0


@pytest.mark.parametrize("suite", ["baseline", "all"])
def test_evaluation_cli_preserves_baseline_control(monkeypatch, capsys, suite):
    import json
    import sys

    from nova.evaluation import main

    arguments = ["evaluation", "--sizes", "10"]
    if suite == "all":
        arguments.extend(["--suite", "all"])
    monkeypatch.setattr(sys, "argv", arguments)
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["tail_tool_activated"]
    assert ("retrieval_results" in report) == (suite == "all")
