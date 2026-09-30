"""Generate one real workspace snapshot's synthetic, auditable event log."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


EVALUATION_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = EVALUATION_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.event_generator import EventGenerationError, generate_snapshot_event_log, write_generated_event_log


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a metadata-only synthetic observation log from a workspace snapshot. "
            "It never reads task, rubric, reference, or important-file-label assets."
        )
    )
    parser.add_argument("--workspace-root", required=True, help="workspace snapshot directory")
    parser.add_argument("--output-root", required=True, help="new artifact directory outside the workspace")
    parser.add_argument("--deletion-rate", type=float, default=0.0, help="private condition parameter in [0, 1]")
    parser.add_argument("--deletion-seed", type=int, default=0, help="private deterministic selection seed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        generated = generate_snapshot_event_log(
            args.workspace_root,
            deletion_rate=args.deletion_rate,
            deletion_seed=args.deletion_seed,
        )
        output = write_generated_event_log(args.output_root, generated)
    except (EventGenerationError, ValueError, OSError) as exc:
        print(f"event-log generation failed: {exc}", file=sys.stderr)
        return 2
    print(output)
    print(f"files={len(generated.inventory.files)} canonical_events={len(generated.canonical_events)} visible_events={len(generated.visible_events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
