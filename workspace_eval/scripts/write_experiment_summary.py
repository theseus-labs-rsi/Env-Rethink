#!/usr/bin/env python3
"""Build a deterministic experiment summary TSV from per-case status files."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


STATUS_NAME_RE = re.compile(r"^task(?P<task_id>\d+)(?:_(?P<condition>[^.]+))?$")


def read_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def previous_return_codes(
    output_path: Path,
    *,
    with_condition: bool,
) -> dict[tuple[str, str], str]:
    if not output_path.is_file():
        return {}
    try:
        with output_path.open(encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle, delimiter="\t")
            return {
                (
                    str(row.get("task_id") or ""),
                    str(row.get("condition") or "") if with_condition else "",
                ): str(row.get("return_code") or "")
                for row in rows
                if str(row.get("task_id") or "").isdigit()
            }
    except (OSError, csv.Error):
        return {}


def judge_fields(raw_summary: str) -> dict[str, str]:
    if not raw_summary:
        return {
            "passed": "",
            "failed": "",
            "total": "",
            "pass_rate": "",
            "pass_rate_pct": "",
        }
    try:
        summary: Any = json.loads(raw_summary)
    except json.JSONDecodeError:
        summary = None
    if not isinstance(summary, dict):
        return {
            "passed": "",
            "failed": "",
            "total": "",
            "pass_rate": "",
            "pass_rate_pct": "",
        }
    try:
        passed = int(summary["passed"])
        total = int(summary["total"])
        failed = int(summary.get("failed", total - passed))
    except (KeyError, TypeError, ValueError):
        return {
            "passed": "",
            "failed": "",
            "total": "",
            "pass_rate": "",
            "pass_rate_pct": "",
        }
    if total <= 0:
        return {
            "passed": str(passed),
            "failed": str(failed),
            "total": str(total),
            "pass_rate": "",
            "pass_rate_pct": "",
        }
    rate = passed / total
    return {
        "passed": str(passed),
        "failed": str(failed),
        "total": str(total),
        "pass_rate": f"{rate:.6f}",
        "pass_rate_pct": f"{rate * 100:.2f}%",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--condition-column",
        choices=("yes", "no", "auto"),
        default="auto",
    )
    args = parser.parse_args()

    status_paths = sorted(args.status_dir.glob("task*.status"))
    parsed_names: list[tuple[Path, re.Match[str]]] = []
    for path in status_paths:
        match = STATUS_NAME_RE.fullmatch(path.stem)
        if match:
            parsed_names.append((path, match))

    with_condition = args.condition_column == "yes" or (
        args.condition_column == "auto"
        and any(match.group("condition") for _, match in parsed_names)
    )
    old_return_codes = previous_return_codes(
        args.output,
        with_condition=with_condition,
    )

    rows: list[dict[str, str]] = []
    for path, match in parsed_names:
        values = read_status(path)
        task_id = match.group("task_id")
        condition = match.group("condition") or ""
        status = values.get("status") or values.get("phase") or "unknown"
        raw_summary = values.get("judge_summary", "")
        score = judge_fields(raw_summary)
        return_code = values.get("return_code", "")
        if not return_code:
            return_code = old_return_codes.get(
                (task_id, condition if with_condition else ""),
                "",
            )
        if not return_code and status == "judged":
            return_code = "0"
        row = {
            "task_id": task_id,
            "status": status,
            **score,
            "judge_summary": raw_summary,
            "return_code": return_code,
        }
        if with_condition:
            row["condition"] = condition
        rows.append(row)

    rows.sort(
        key=lambda row: (
            int(row["task_id"]),
            row.get("condition", ""),
        )
    )
    fields = ["task_id"]
    if with_condition:
        fields.append("condition")
    fields.extend(
        [
            "status",
            "passed",
            "failed",
            "total",
            "pass_rate",
            "pass_rate_pct",
            "judge_summary",
            "return_code",
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
