"""Gate public environment artifacts against one task's materials and rubrics.

Runs the three ambient-workspace checks (filename intersection, rubric keyword hits,
reference alignment) against the public artifacts this kit produces, and reports the
verdict as JSON. Findings quote rubric material, so the report is written 0600.

Exit codes: 0 = pass, 1 = findings (fail), 2 = invalid inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.artifact_gate import ArtifactGateError, run_gate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="workspace snapshot the artifacts were generated from")
    parser.add_argument("--task-metadata", required=True, help="path to the task metadata.json (materials + rubrics)")
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        help="public artifact to gate (.jsonl event log or .json collection map); repeatable",
    )
    parser.add_argument("--no-reference-alignment", action="store_true", help="skip the workspace path existence check")
    parser.add_argument(
        "--scan-mode",
        choices=("quoted", "tokens"),
        default="quoted",
        help="quoted: scan rubric quoted phrases only (low false positives); tokens: also sweep long rubric tokens",
    )
    parser.add_argument("--extra-keyword", action="append", default=[], help="extra sensitive keyword; repeatable")
    parser.add_argument("--json-out", help="write the full report to this path (mode 0600)")
    args = parser.parse_args()
    try:
        report = run_gate(
            workspace=args.workspace,
            metadata_path=args.task_metadata,
            artifact_paths=args.artifact,
            include_reference_alignment=not args.no_reference_alignment,
            scan_mode=args.scan_mode,
            extra_keywords=args.extra_keyword,
        )
    except (OSError, json.JSONDecodeError, ArtifactGateError) as exc:
        print(f"artifact gate could not run: {exc}", file=sys.stderr)
        return 2
    payload = report.to_json()
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    failures = [finding for finding in report.findings if finding.severity == "fail"]
    for finding in report.findings:
        print(f"[{finding.severity}] {finding.check}: {finding.detail}")
    print(f"verdict={payload['verdict']} findings={len(report.findings)} failures={len(failures)}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
