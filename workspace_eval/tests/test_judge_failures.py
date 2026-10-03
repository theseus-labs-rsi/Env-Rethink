"""Judge failures must not become benchmark scores; no model calls are made."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
import yaml


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent_as_a_judge as judge


def response(rows=None, *, status="ok", text=None, error=None, usage=None):
    return {
        "status": status,
        "errorMessage": error,
        "trace": {
            "lastText": json.dumps({"rubrics": rows}) if text is None else text,
            "usageTotal": usage,
        },
    }


def rubric_rows(*passed):
    return [
        {"index": i, "passed": value, "confidence": 0.9, "evidence": "Inspected the report."}
        for i, value in enumerate(passed)
    ]


@pytest.fixture
def case(tmp_path, monkeypatch):
    task_dir = tmp_path / "1"
    (task_dir / "output").mkdir(parents=True)
    (task_dir / "output" / "report.txt").write_text("A report.", encoding="utf-8")
    (task_dir / "metadata.json").write_text(
        json.dumps({
            "id": "1",
            "task": "Write a report.",
            "rubrics": ["The report is complete.", "The report is accurate."],
            "rubric_types": ["output", "output"],
            "data_manifest": [],
        }),
        encoding="utf-8",
    )
    config = tmp_path / "judge.yaml"
    config.write_text(yaml.safe_dump({
        "baseUrl": "http://127.0.0.1:1",
        "apiKey": "offline-test-placeholder",
        "model": "offline-model",
        "model_name": "offline-judge",
    }), encoding="utf-8")
    monkeypatch.setattr(judge, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(judge.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(judge._claudecode, "run", Mock(side_effect=AssertionError("No model calls allowed")))
    return task_dir, config


def evaluate(case, *, retries=1, overwrite=True):
    task_dir, config = case
    result = judge.evaluate_task(
        str(task_dir), eval_yaml_path=str(config), max_retries=retries, overwrite=overwrite,
    )
    artifact = json.loads((task_dir / "rubrics_judge--offline-judge.json").read_text(encoding="utf-8"))
    return result, artifact


@pytest.mark.parametrize("run_result", [
    response(status="error", error="SDK failed to load", text=""),
    response(status="timeout", error="Timeout after 60s", text=""),
    response(text="This is not JSON"),
    response(rubric_rows(True)),
    response(rubric_rows(True, False), status="error", error="Runner crashed after producing output"),
    response(rubric_rows(True, False), status="timeout"),
    response([{"index": 0, "passed": False}, {"index": 0, "passed": True}]),
    response([{"index": 0}, {"index": 1, "passed": False}]),
    response([{"index": False, "passed": False}, {"index": 1, "passed": False}]),
    None,
], ids=[
    "infrastructure", "timeout", "malformed-json", "incomplete-rubrics",
    "complete-output-from-failed-run", "complete-output-from-timeout",
    "duplicate-indices", "missing-verdict", "boolean-index", "invalid-run-result",
])
def test_invalid_judgment_is_unscored(case, monkeypatch, run_result):
    monkeypatch.setattr(judge._claudecode, "run", Mock(return_value=run_result))
    result, artifact = evaluate(case)
    assert result["success"] is False
    assert result["error"]
    assert result["rubricsSummary"] is None
    assert artifact["status"] == "error"
    assert artifact["judge"]["status"] == "error"
    assert artifact["judge"]["error"]
    assert artifact["summary"] is None


def test_harness_exception_is_a_failed_judgment(case, monkeypatch):
    monkeypatch.setattr(judge._claudecode, "run", Mock(side_effect=TimeoutError("Local runner timed out")))
    result, artifact = evaluate(case)
    assert result["success"] is False
    assert "TimeoutError" in artifact["judge"]["error"]
    assert artifact["summary"] is None


def test_successful_zero_score_remains_valid(case, monkeypatch):
    monkeypatch.setattr(judge._claudecode, "run", Mock(return_value=response(rubric_rows(False, False))))
    result, artifact = evaluate(case)
    assert result["success"] is True
    assert result["rubricsSummary"] == {"total": 2, "passed": 0, "failed": 2}
    assert artifact["status"] == artifact["judge"]["status"] == "ok"
    assert artifact["judge"]["error"] is None
    assert artifact["summary"] == result["rubricsSummary"]


def test_retry_success_drops_previous_error_rows_and_usage(case, monkeypatch):
    runner = Mock(side_effect=[
        response(rubric_rows(True), usage={"totalTokens": 17}),
        response(rubric_rows(False, False)),
    ])
    monkeypatch.setattr(judge._claudecode, "run", runner)
    result, artifact = evaluate(case, retries=2)
    assert runner.call_count == 2
    assert result["success"] is True
    assert artifact["judge"]["error"] is None
    assert artifact["judge"]["usage"] is None
    assert artifact["summary"] == {"total": 2, "passed": 0, "failed": 2}


def test_failed_retry_drops_partial_rows_from_previous_attempt(case, monkeypatch):
    monkeypatch.setattr(judge._claudecode, "run", Mock(side_effect=[
        response(rubric_rows(True)), response(text="No structured response"),
    ]))
    result, artifact = evaluate(case, retries=2)
    assert result["success"] is False
    assert artifact["judge"]["error"] == "Judge output parse failed"
    assert artifact["rubrics"] == []
    assert artifact["summary"] is None


def test_failed_run_with_complete_rows_retries_until_runner_succeeds(case, monkeypatch):
    runner = Mock(side_effect=[
        response(rubric_rows(True, True), status="error"),
        response(rubric_rows(False, True)),
    ])
    monkeypatch.setattr(judge._claudecode, "run", runner)
    result, artifact = evaluate(case, retries=2)
    assert runner.call_count == 2
    assert result["success"] is True
    assert artifact["summary"] == {"total": 2, "passed": 1, "failed": 1}


@pytest.mark.parametrize("cached", [
    {"rubrics": rubric_rows(False, False), "summary": {"total": 2, "passed": 0, "failed": 2}, "judge": {"error": "Previous SDK failure"}},
    {"rubrics": rubric_rows(False, False), "summary": None, "status": "error", "judge": {"status": "error", "error": "Timeout"}},
    {"rubrics": rubric_rows(False), "summary": {"total": 2, "passed": 0, "failed": 2}, "judge": {"error": None}},
], ids=["legacy-failure-with-score", "new-unscored-failure", "incomplete-cache"])
def test_invalid_cache_is_rejudged(case, monkeypatch, cached):
    (case[0] / "rubrics_judge--offline-judge.json").write_text(json.dumps(cached), encoding="utf-8")
    runner = Mock(return_value=response(rubric_rows(True, False)))
    monkeypatch.setattr(judge._claudecode, "run", runner)
    result, artifact = evaluate(case, overwrite=False)
    assert runner.call_count == 1
    assert not result.get("rubricsSkipped")
    assert result["success"] is True
    assert artifact["summary"] == {"total": 2, "passed": 1, "failed": 1}


def test_valid_legacy_zero_score_can_be_reused(case):
    cached = {"rubrics": rubric_rows(False, False), "summary": {"total": 2, "passed": 0, "failed": 2}, "judge": {"error": None}}
    (case[0] / "rubrics_judge--offline-judge.json").write_text(json.dumps(cached), encoding="utf-8")
    result, artifact = evaluate(case, overwrite=False)
    judge._claudecode.run.assert_not_called()
    assert result["success"] is True
    assert result["rubricsSkipped"] is True
    assert result["rubricsSummary"] == artifact["summary"] == cached["summary"]


@pytest.mark.parametrize("succeeded,expected_exit", [(False, 1), (True, 0)])
def test_cli_distinguishes_judge_failure_and_real_zero_score(case, monkeypatch, succeeded, expected_exit):
    run_result = response(rubric_rows(False, False)) if succeeded else response(status="error", error="SDK unavailable", text="")
    monkeypatch.setattr(judge._claudecode, "run", Mock(return_value=run_result))
    assert judge.main([
        "--task-dir", str(case[0]), "--eval-yaml", str(case[1]), "--max-retries", "1",
    ]) == expected_exit


def test_parallel_cli_propagates_a_single_failure(case, monkeypatch):
    second = case[0].parent / "2"
    second.mkdir()
    (second / "metadata.json").write_text("{}", encoding="utf-8")
    def local_result(task_dir, **kwargs):
        return {"success": Path(task_dir).name == "1", "error": "Local test failure"}
    monkeypatch.setattr(judge, "evaluate_task", local_result)
    assert judge.main([
        "--task-dir", str(case[0].parent), "--eval-yaml", str(case[1]), "--parallel",
    ]) == 1


def test_script_process_exits_nonzero_for_missing_task(tmp_path):
    result = subprocess.run([
        sys.executable, str(SRC_ROOT / "agent_as_a_judge.py"),
        "--task-dir", str(tmp_path / "missing"), "--eval-yaml", str(tmp_path / "missing.yaml"),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=tmp_path)
    assert result.returncode == 1
    assert "Task directory not found" in result.stderr
