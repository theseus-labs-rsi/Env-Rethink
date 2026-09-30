"""Run real Codex A/B v1 map construction for every task in one persona."""

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

from workspace_env.collection_map import CollectionMapError
from workspace_env.collection_synthesis import CollectionSynthesisError
from workspace_env.runner import RunnerContractError, load_runner
from workspace_env.task_input_collection_batch import (
    TaskInputCollectionBatchConfig,
    TaskInputCollectionBatchError,
    TaskInputCollectionBatchOrchestrator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real Codex A/B collection-map batch for one persona.")
    parser.add_argument("--config", required=True, help="path to TaskInputCollectionBatchConfig JSON; credentials stay in the environment")
    parser.add_argument("--runner", help="Codex runner spec 'module:attr' (default: $ENVGEN_RUNNER); see docs/RUNNER_CONTRACT.md")
    args = parser.parse_args()
    try:
        runner = load_runner(args.runner)
        config = TaskInputCollectionBatchConfig.model_validate(
            json.loads(Path(args.config).read_text(encoding="utf-8"))
        )
        result = TaskInputCollectionBatchOrchestrator(config=config, codex_runner=runner).run()
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        CollectionMapError,
        CollectionSynthesisError,
        TaskInputCollectionBatchError,
        RunnerContractError,
    ) as exc:
        print(f"task-input collection-map batch failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
