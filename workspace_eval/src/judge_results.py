"""Validate complete rubric scores separately from judge infrastructure errors."""

from typing import Any


def is_valid_judge_result(value: Any, expected_total: int) -> bool:
    """Accept complete, consistent scores, including legitimate all-false scores.

    Older successful artifacts may omit status fields. Diagnostic artifacts
    with an error or incomplete rubric rows are never usable as scores.
    """
    if not isinstance(value, dict) or expected_total <= 0:
        return False
    if value.get("status") not in (None, "ok") or value.get("success") is False:
        return False
    judge = value.get("judge")
    if not isinstance(judge, dict) or judge.get("error"):
        return False
    if judge.get("status") not in (None, "ok"):
        return False
    summary = value.get("summary")
    if not isinstance(summary, dict):
        return False
    counts = [summary.get(key) for key in ("total", "passed", "failed")]
    if any(type(count) is not int or count < 0 for count in counts):
        return False
    total, passed, failed = counts
    if total != expected_total or passed + failed != total:
        return False
    rows = value.get("rubrics")
    if not isinstance(rows, list) or len(rows) != total:
        return False
    indices = set()
    passed_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            return False
        index = row.get("index")
        if type(index) is not int or index < 0 or index >= total or index in indices:
            return False
        if type(row.get("passed")) is not bool:
            return False
        indices.add(index)
        passed_rows += row["passed"]
    return passed_rows == passed
