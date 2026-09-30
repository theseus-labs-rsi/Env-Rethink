"""Tests for the author-pinned targeted overlay and the public-artifact leak gate."""

from __future__ import annotations

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

from workspace_env.artifact_gate import normalize_name, run_gate
from workspace_env.event_generator import generate_snapshot_event_log, write_generated_event_log
from workspace_env.targeted_overlay import (
    AUDIT_KEYS,
    TargetedOverlayError,
    build_overlay,
)


class NameNormalizationTests(unittest.TestCase):
    def test_version_tails_and_extensions_are_stripped(self) -> None:
        cases = {
            "周报_v2.xlsx": "周报",
            "周报(1).xlsx": "周报",
            "周报_副本.xlsx": "周报",
            "周报-终版.xlsx": "周报",
            "Report_final_v3.csv": "report",
            "发布检查单_2022.md": "发布检查单",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_name(raw), expected)


class TargetedOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "桌面").mkdir(parents=True)
        self.note = self.workspace / "桌面" / "先记一下.txt"
        self.note.write_text(
            "2025-11-03 交接备忘\n"
            "1. 巡检脚本放到运维目录，周五前跑一次。\n"
            "2. 旧版台账先别删，等月度核对完再归档。\n"
            "3. 下周把接口字段对照表补上渠道一的那一列。\n",
            encoding="utf-8",
        )

    def _spec(self, *, excerpt: str | None = None, start: int = 1, end: int = 4, path: str | None = None) -> dict:
        return {
            "format": "envgen-kit.author-pinned-overlay-spec.v1",
            "run_label": "overlay-test-r1",
            "targeted_task_ids": ["probe-task"],
            "sessions": [
                {
                    "label": "handover-note",
                    "started_at": "2025-11-03T09:12:00Z",
                    "ended_at": "2025-11-03T09:31:00Z",
                    "title": "交接备忘复核",
                    "narrative": "复核交接便笺里提到的三件事，确认台账与脚本的去处。",
                    "application_context": ["workspace_snapshot_inventory"],
                    "reads": [
                        {
                            "path": path or "桌面/先记一下.txt",
                            "locator": {"kind": "line", "start": start, "end": end},
                            "excerpt": excerpt if excerpt is not None else "旧版台账先别删，等月度核对完再归档。",
                            "purpose": "确认台账归档时机",
                            "observation": "便笺注明月度核对后再归档，属于交接约定。",
                        }
                    ],
                }
            ],
        }

    def _write_spec(self, payload: dict, name: str = "spec.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_overlay_is_synthetic_audited_and_merges_with_a_base_log(self) -> None:
        generated = generate_snapshot_event_log(self.workspace, deletion_rate=0.0, deletion_seed=0)
        base_log = write_generated_event_log(self.root / "base", generated) / "events.public.jsonl"
        result = build_overlay(
            spec_path=self._write_spec(self._spec()),
            workspace_root=self.workspace,
            output_root=self.root / "out",
            base_public_log=base_log,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["event_count"], 3)  # session.start + one read + session.end

        overlay_rows = [
            json.loads(line)
            for line in Path(result["overlay_event_log"]).read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual([row["action"] for row in overlay_rows], ["session.start", "file.read", "session.end"])
        for row in overlay_rows:
            self.assertTrue(row["provenance"]["synthetic"])
            self.assertEqual(row["provenance"]["generation_method"], "agent_inference")
            self.assertEqual(row["provenance"]["temporal_basis"], "synthetic_timestamp")

        audit_path = Path(result["targeted_audit"])
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(set(audit), AUDIT_KEYS)
        self.assertEqual(audit["construction_kind"], "targeted_context_construction")
        self.assertEqual(audit["targeted_task_ids"], ["probe-task"])
        self.assertEqual(stat.S_IMODE(audit_path.stat().st_mode), 0o600)
        self.assertEqual(audit["construction_scope"]["read_count"], 1)
        self.assertEqual(len(audit["construction_scope"]["source_identities"]), 1)

        # The merged stream must keep one workspace identity and stay sorted.
        merged = [
            json.loads(line)
            for line in Path(result["final_event_log"]).read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len({row["workspace_id"] for row in merged}), 1)
        self.assertEqual(len(merged), result["final_event_count"])

    def test_unverifiable_excerpt_fails_closed(self) -> None:
        with self.assertRaises(TargetedOverlayError):
            build_overlay(
                spec_path=self._write_spec(self._spec(excerpt="这句在文件里根本不存在。")),
                workspace_root=self.workspace,
                output_root=self.root / "out-bad",
            )

    def test_locator_out_of_range_fails_closed(self) -> None:
        with self.assertRaises(TargetedOverlayError):
            build_overlay(
                spec_path=self._write_spec(self._spec(start=10, end=12)),
                workspace_root=self.workspace,
                output_root=self.root / "out-range",
            )

    def test_non_text_source_fails_closed(self) -> None:
        binary = self.workspace / "桌面" / "台账.xlsx"
        binary.write_bytes(b"PK\x03\x04 not really a workbook")
        with self.assertRaises(TargetedOverlayError):
            build_overlay(
                spec_path=self._write_spec(self._spec(path="桌面/台账.xlsx")),
                workspace_root=self.workspace,
                output_root=self.root / "out-binary",
            )

    def test_path_escape_fails_closed(self) -> None:
        with self.assertRaises(TargetedOverlayError):
            build_overlay(
                spec_path=self._write_spec(self._spec(path="../outside.txt")),
                workspace_root=self.workspace,
                output_root=self.root / "out-escape",
            )


class ArtifactGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "桌面" / "材料").mkdir(parents=True)
        (self.workspace / "桌面" / "先记一下.txt").write_text("随便记一笔。\n", encoding="utf-8")
        self.metadata = self.root / "metadata.json"
        self.metadata.write_text(
            json.dumps(
                {
                    "task": "汇总胜业电气的年度数据",
                    "data_manifest": [{"filename": "周报.xlsx", "target_path": "桌面/材料/周报.xlsx"}],
                    "output_files": ["汇总.md"],
                    "rubrics": ["输出必须引用「胜业电气」的年度营收口径", "表格需包含同比列"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _artifact(self, rows: list[dict], name: str = "artifact.jsonl") -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_clean_artifact_passes(self) -> None:
        artifact = self._artifact(
            [
                {"object": {"path_at_event": "桌面/先记一下.txt"}, "payload": {"excerpt": "随手记一笔"}},
                {"payload": {"title": "交接备忘复核"}},
            ]
        )
        report = run_gate(workspace=self.workspace, metadata_path=self.metadata, artifact_paths=[artifact])
        self.assertFalse(report.failed, report.to_json())

    def test_material_filename_collision_fails(self) -> None:
        artifact = self._artifact([{"object": {"path_at_event": "下载/周报_v2.xlsx"}}])
        report = run_gate(workspace=self.workspace, metadata_path=self.metadata, artifact_paths=[artifact])
        self.assertTrue(report.failed)
        self.assertIn("filename_intersection", {finding.check for finding in report.findings})

    def test_rubric_keyword_hit_fails(self) -> None:
        artifact = self._artifact([{"payload": {"observation": "这份材料写的是胜业电气的口径。"}}])
        report = run_gate(workspace=self.workspace, metadata_path=self.metadata, artifact_paths=[artifact])
        self.assertTrue(report.failed)
        self.assertIn("rubric_keyword", {finding.check for finding in report.findings})

    def test_quoted_phrase_in_path_fails(self) -> None:
        artifact = self._artifact([{"object": {"path_at_event": "下载/胜业电气/草稿.txt"}}])
        report = run_gate(workspace=self.workspace, metadata_path=self.metadata, artifact_paths=[artifact])
        self.assertTrue(report.failed)
        self.assertIn("rubric_keyword", {finding.check for finding in report.findings})

    def test_unquoted_common_vocabulary_does_not_fire(self) -> None:
        """Guard against the false positives the ambient pipeline measured."""

        metadata = self.root / "metadata-plain.json"
        metadata.write_text(
            json.dumps(
                {
                    "data_manifest": [{"filename": "周报.xlsx", "target_path": "桌面/材料/周报.xlsx"}],
                    "rubrics": ["表格需包含同比列，并说明口径来源", "输出文件需可正常读取"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        artifact = self._artifact([{"payload": {"observation": "表格按渠道拆分，口径见备注。"}}])
        report = run_gate(workspace=self.workspace, metadata_path=metadata, artifact_paths=[artifact])
        self.assertFalse(report.failed, report.to_json())

    def test_missing_referenced_path_fails(self) -> None:
        artifact = self._artifact([{"object": {"path_at_event": "桌面/并不存在.txt"}}])
        report = run_gate(workspace=self.workspace, metadata_path=self.metadata, artifact_paths=[artifact])
        self.assertTrue(report.failed)
        self.assertIn("reference_alignment", {finding.check for finding in report.findings})


if __name__ == "__main__":
    unittest.main()
