"""Run real Codex A/B construction of one workspace-scoped collection map."""

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
from workspace_env.runner import RunnerContractError, load_runner
from workspace_env.workspace_collection_synthesis import (
    WorkspaceCollectionSynthesisConfig,
    WorkspaceCollectionSynthesisError,
    WorkspaceCollectionSynthesisOrchestrator,
)
from workspace_env.workspace_collection_cover import (
    WorkspaceCollectionCoverConfig,
    WorkspaceCollectionCoverError,
    WorkspaceCollectionCoverOrchestrator,
    WorkspaceCollectionContinuationConfig,
    WorkspaceCollectionContinuationOrchestrator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real Codex A/B construction of one workspace-scoped collection map.")
    parser.add_argument("--config", required=True, help="path to WorkspaceCollectionSynthesisConfig JSON; credentials stay in the environment")
    parser.add_argument("--runner", help="Codex runner spec 'module:attr' (default: $ENVGEN_RUNNER); see docs/RUNNER_CONTRACT.md")
    args = parser.parse_args()
    try:
        runner = load_runner(args.runner)
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("schema_version") == 3:
            continuation_config = WorkspaceCollectionContinuationConfig.model_validate(raw)
            result = WorkspaceCollectionContinuationOrchestrator(
                config=continuation_config,
                codex_runner=runner,
            ).run()
        elif isinstance(raw, dict) and raw.get("schema_version") == 2:
            cover_config = WorkspaceCollectionCoverConfig.model_validate(raw)
            result = WorkspaceCollectionCoverOrchestrator(config=cover_config, codex_runner=runner).run()
        else:
            config = WorkspaceCollectionSynthesisConfig.model_validate(raw)
            result = WorkspaceCollectionSynthesisOrchestrator(config=config, codex_runner=runner).run()
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        CollectionMapError,
        WorkspaceCollectionSynthesisError,
        WorkspaceCollectionCoverError,
        RunnerContractError,
    ) as exc:
        print(f"workspace collection-map synthesis failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
