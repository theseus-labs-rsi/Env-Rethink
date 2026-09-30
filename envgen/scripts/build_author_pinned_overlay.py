"""Build an author-pinned targeted-context overlay from a private spec.

Every published read is verified verbatim against a workspace file; nothing is
invented. The overlay is synthetic targeted context and must be reported separately
from natural-history conditions. The spec must not contain API keys or benchmark
answers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pydantic import ValidationError

from workspace_env.targeted_overlay import TargetedOverlayError, build_overlay


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a spec-driven author-pinned targeted overlay.")
    parser.add_argument("--spec", required=True, help="path to the private overlay spec JSON")
    parser.add_argument("--workspace-root", required=True, help="read-only workspace snapshot the reads are verified against")
    parser.add_argument("--output-root", required=True, help="new artifact directory outside the workspace")
    parser.add_argument(
        "--base-public-log",
        help="optional validated natural-history events.public.jsonl to merge with (writes final/)",
    )
    args = parser.parse_args()
    try:
        result = build_overlay(
            spec_path=args.spec,
            workspace_root=args.workspace_root,
            output_root=args.output_root,
            base_public_log=args.base_public_log,
        )
    except (OSError, json.JSONDecodeError, ValidationError, TargetedOverlayError) as exc:
        print(f"author-pinned overlay failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
