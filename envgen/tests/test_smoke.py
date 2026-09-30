"""Codex-free smoke checks for envgen-kit.

These checks need no Codex CLI, no credentials, no network and no benchmark data.
They cover the deterministic parts of both pipelines:

* the workspace catalog (task-independent inventory of an immutable snapshot),
* the collection-cover partition (every file covered exactly once),
* the snapshot event log, including its private/public split and file modes.

Anything that needs a real Codex run is out of scope here by design; see
``README.md`` and ``docs/RUNNER_CONTRACT.md``.
"""

from __future__ import annotations

import base64
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from workspace_env.collection_map import build_workspace_catalog, sha256_file
from workspace_env.event_generator import generate_snapshot_event_log, write_generated_event_log
from workspace_env.integration import workspace_snapshot_hash
from workspace_env.runner import RunnerContractError, load_runner
from workspace_env.workspace_collection_cover import partition_catalog

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

PUBLIC_LOG_FORBIDDEN_KEYS = ("deletion_rate", "deletion_seed", "canonical_sequence", "causal_links")


class EnvgenKitSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "下载").mkdir(parents=True)
        (self.workspace / "桌面").mkdir(parents=True)
        for index in range(5):
            (self.workspace / "下载" / f"报表_{index}.txt").write_text(f"行 {index}\n", encoding="utf-8")
        (self.workspace / "桌面" / "说明.md").write_text("# 说明\n", encoding="utf-8")
        (self.workspace / "桌面" / "截图.png").write_bytes(PNG_1X1)

    def test_workspace_catalog_is_sorted_and_reusable(self) -> None:
        snapshot = workspace_snapshot_hash(str(self.workspace))
        catalog, catalog_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=snapshot,
            catalog_root=self.root / "catalog",
        )
        paths = [entry.path for entry in catalog.files]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(catalog.workspace_snapshot_hash, snapshot)
        self.assertTrue(catalog_path.is_file())

        # Rebuilding for the same snapshot reuses the cached catalog verbatim.
        second, second_path = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=snapshot,
            catalog_root=self.root / "catalog",
        )
        self.assertEqual(second_path, catalog_path)
        self.assertEqual(second.model_dump(mode="json"), catalog.model_dump(mode="json"))
        self.assertEqual(sha256_file(catalog_path), sha256_file(catalog_path))

    def test_partition_covers_every_file_exactly_once(self) -> None:
        snapshot = workspace_snapshot_hash(str(self.workspace))
        catalog, _ = build_workspace_catalog(
            self.workspace,
            workspace_snapshot_hash=snapshot,
            catalog_root=self.root / "catalog",
        )
        buckets = partition_catalog(catalog, minimum=2, target=3, maximum=4)
        self.assertTrue(buckets)
        covered = [path for bucket in buckets for path in bucket.paths]
        self.assertEqual(sorted(covered), sorted(entry.path for entry in catalog.files))
        self.assertEqual(len(covered), len(set(covered)), "a file must appear in exactly one bucket")
        bucket_ids = [bucket.bucket_id for bucket in buckets]
        self.assertEqual(len(bucket_ids), len(set(bucket_ids)))

    def test_snapshot_event_log_splits_public_and_private(self) -> None:
        generated = generate_snapshot_event_log(self.workspace, deletion_rate=0.0, deletion_seed=0)
        output = write_generated_event_log(self.root / "event_log", generated)

        public_log = output / "events.public.jsonl"
        self.assertTrue(public_log.is_file())
        rows = [json.loads(line) for line in public_log.read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(rows)
        for row in rows:
            for key in PUBLIC_LOG_FORBIDDEN_KEYS:
                self.assertNotIn(key, row, f"public event must not expose {key}")

        for name in ("canonical.private.jsonl", "audit.private.json", "generation.private.json"):
            path = output / name
            self.assertTrue(path.is_file(), f"missing private artifact {name}")
        self.assertEqual(stat.S_IMODE((output / "audit.private.json").stat().st_mode), 0o600)

    def test_runner_contract_fails_closed(self) -> None:
        with self.assertRaises(RunnerContractError):
            load_runner("")
        with self.assertRaises(RunnerContractError):
            load_runner("not-a-spec")
        with self.assertRaises(RunnerContractError):
            load_runner("module_that_does_not_exist_xyz:run")
        with self.assertRaises(RunnerContractError):
            load_runner("workspace_env.runner:missing_attribute")
        # A well-formed spec resolves to a callable; the kit itself ships none.
        self.assertTrue(callable(load_runner("workspace_env.runner:load_runner")))


if __name__ == "__main__":
    unittest.main()
