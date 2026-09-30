"""Run the real Codex A/B/C Context Event Log synthesis loop."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.runner import RunnerContractError, load_runner
from workspace_env.event_synthesis import (
    EventSynthesisError,
    EventSynthesisOrchestrator,
    SynthesisRunConfig,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the configured workspace-inference, rubric-context, or interference-bridge "
            "Codex A/B/C event-log synthesis. Coverage modes stop at their file target; targeted "
            "modes stop at their accepted-candidate target. The config must be JSON and must not "
            "contain API keys."
        )
    )
    parser.add_argument("--config", required=True, help="path to a SynthesisRunConfig JSON file")
    parser.add_argument("--runner", help="Codex runner spec 'module:attr' (default: $ENVGEN_RUNNER); see docs/RUNNER_CONTRACT.md")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an existing interrupted output_root after validating its accepted artifacts and masks",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        runner = load_runner(args.runner)
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        config = SynthesisRunConfig.model_validate(raw)
        result = EventSynthesisOrchestrator(
            config=config,
            codex_runner=runner,
            resume=args.resume,
        ).run()
    except (OSError, json.JSONDecodeError, ValidationError, EventSynthesisError, RunnerContractError) as exc:
        print(f"context event-log synthesis failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
