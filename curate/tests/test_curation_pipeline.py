"""Offline regression fixtures for curation; assets and outputs stay in Temp."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


CURATE_ROOT = Path(__file__).resolve().parents[1]
RUNNER = CURATE_ROOT.parent / "workspace_eval" / "scripts" / "run_experiment.py"


class CurationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="curation_pipeline_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.curate = self.root / "curate"
        self.curate.mkdir()
        for source in CURATE_ROOT.glob("*.py"):
            shutil.copy2(source, self.curate / source.name)
        self.ws_eval = self.root / "workspace_eval"
        hints = self.ws_eval / "experiments" / "noise-id" / "hints"
        hints.mkdir(parents=True)
        (hints / "L1.md").write_text("审查文件\n## 输出要求\nfiles: []", encoding="utf-8")
        self.task = self.ws_eval / "tasks_hard_v4" / "108"
        (self.task / "data").mkdir(parents=True)
        manifest = []
        for filename, role in [("report.txt", "standard"), ("report_draft.txt", "noise")]:
            (self.task / "data" / filename).write_text("测试内容 " + role, encoding="utf-8")
            manifest.append({"filename": filename, "stored_relpath": f"data/{filename}",
                             "target_path": f"docs/{filename}", "input_role": role})
        (self.task / "metadata.json").write_text(json.dumps({
            "id": "108", "file_system": "dataseed", "language": "cn", "task": "fixture",
            "output_files": ["answer.txt"], "rubrics": ["fixture"], "rubric_types": ["fixture"],
            "data_manifest": manifest,
        }), encoding="utf-8")
        (self.curate / ".env").write_text(
            'export ENV_RETHINK_MODEL = "fixture-model" # display and provider id\n'
            'ENV_RETHINK_BASE_URL="http://127.0.0.1:1"\n'
            'ENV_RETHINK_API_KEY="fixture-key"\n'
            "ENV_RETHINK_QWEN_MODEL='fixture-qwen'\n"
            "ENV_RETHINK_QWEN_BASE_URL=http://127.0.0.1:2\n",
            encoding="utf-8",
        )
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ENV_RETHINK") and key not in {
                        "WS_EVAL_ROOT", "WS_EVAL_RUNNER", "CURATE_TASK_ROOT", "CURATE_RUNTIME_PROVIDER",
                    }}

    def cli(self, script, *args, env=None):
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(self.curate / script), *args],
            cwd=self.root, env=env or self.env, capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def load_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def prepared_config(self, curator="env-rethink", env=None):
        self.cli("curate_workspace.py", "prepare", "--curator", curator,
                 "--tasks", "108", env=env)
        path = self.curate / "experiments" / f"curate-{curator}-local.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_dotenv_model_endpoint_and_model_id_fallback(self):
        config = self.prepared_config()
        self.assertEqual(config["agent"]["model"], "fixture-model")
        self.assertEqual(config["agent"]["model_id"], "fixture-model")
        self.assertEqual(config["agent"]["base_url"], "http://127.0.0.1:1")
        self.assertEqual(Path(config["runtime"]["env_file"]), self.curate / ".env")
        self.assertEqual(config["agent"]["api_key_env"], "ENV_RETHINK_API_KEY")
        self.assertIn('ENV_RETHINK_API_KEY="fixture-key"',
                      Path(config["runtime"]["env_file"]).read_text(encoding="utf-8"))
        qwen = self.prepared_config("qwen")
        self.assertEqual(qwen["agent"]["model_id"], "fixture-qwen")
        self.assertEqual(qwen["agent"]["base_url"], "http://127.0.0.1:2")
        # Run the actual command with only the connectivity probe replaced.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import curate_workspace as c; "
                "c.probe_endpoint=lambda url,key: url == 'http://127.0.0.1:1'; "
                "sys.argv=['curate_workspace.py','run','--curator','env-rethink','--dry-run']; "
                "raise SystemExit(c.main())")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code, str(self.curate)],
                                cwd=self.root, env=self.env, capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_process_environment_overrides_dotenv(self):
        env = {**self.env, "ENV_RETHINK_MODEL": "process-model",
               "ENV_RETHINK_MODEL_ID": "process-id", "ENV_RETHINK_BASE_URL": "http://127.0.0.1:3"}
        config = self.prepared_config(env=env)
        self.assertEqual(config["agent"]["model"], "process-model")
        self.assertEqual(config["agent"]["model_id"], "process-id")
        self.assertEqual(config["agent"]["base_url"], "http://127.0.0.1:3")

    def test_all_baseline_can_emit_valid_downstream(self):
        self.cli("curate_workspace.py", "all", "--tasks", "108")
        models = [
            ("deepseek-v4-flash", "hard-v4-dshflash-max-noise-v2.yaml", "dshflash"),
            ("gpt-5.6-sol", "hard-v4-sol-max-noise-v2.yaml", "sol"),
        ]
        for model, name, _ in models:
            source = self.curate / "experiments" / "tasks_hard_v4" / model / name
            source.parent.mkdir(parents=True)
            source.write_text(yaml.safe_dump({"version": 1, "agent": {"model": "fixture"},
                                              "judge": {"model": "fixture"},
                                              "runtime": {"provider": "local"}}), encoding="utf-8")
        self.cli("curate_workspace.py", "emit-downstream", "--curators", "all", "--tasks", "108")
        downstream = (self.curate / "experiments" / "tasks_hard_v4" / "gpt-5.6-sol" /
                      "hard-v4-sol-max-curated-all.yaml")
        config = yaml.safe_load(downstream.read_text(encoding="utf-8"))
        self.assertEqual(config["condition"], "curated")
        metadata = self.load_json(Path(config["task_dir"]) / "108" / "metadata.json")
        self.assertEqual(len(metadata["data_manifest"]), 2)
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(RUNNER),
                                 "--config", str(downstream), "--validate-only"],
                                cwd=self.root, env=self.env, capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_collect_uses_valid_retry_after_invalid_old_labels(self):
        self.prepared_config()
        old, retry = self.root / "old", self.root / "retry"
        for exp, content in [(old, "invalid JSON"), (retry, json.dumps({"files": [
            {"path": "docs/report.txt", "partition": "standard"},
            {"path": "docs/report_draft.txt", "partition": "noise"},
        ]}))]:
            labels = exp / "cases" / "task108-b01" / "agent" / "output" / "noise_labels.json"
            labels.parent.mkdir(parents=True)
            labels.write_text(content, encoding="utf-8")
        self.cli("curate_workspace.py", "collect", "--curator", "env-rethink", "--tasks", "108",
                 "--exp", str(old), "--exp", str(retry))
        selected = self.load_json(self.curate / ".generated" / "preprocessed" /
                                  "env-rethink" / "108" / "metadata.json")
        self.assertEqual([entry["filename"] for entry in selected["data_manifest"]], ["report.txt"])

    def test_retry_coverage_is_scoped_to_task_and_curator(self):
        batches = self.curate / ".generated" / "curate_batches"
        for task in ["108", "207"]:
            batch = batches / f"{task}-b01"
            batch.mkdir(parents=True)
            (batch / "metadata.json").write_text(json.dumps({"data_manifest": [
                {"target_path": "docs/report.txt"},
            ]}), encoding="utf-8")
        labels = (self.curate / "experiments" / "curate-qwen-local-retry" / "cases" /
                  "task108-b01-sub01" / "agent" / "output" / "noise_labels.json")
        labels.parent.mkdir(parents=True)
        labels.write_text(json.dumps({"files": [{"path": "./docs/report.txt", "partition": "standard"}]}),
                          encoding="utf-8")
        output = self.cli("retry_dead_batches.py", "--dry-run", "--curator", "env-rethink")
        self.assertEqual(ast.literal_eval(output.split("死批:", 1)[1].strip()), ["108-b01", "207-b01"])
        output = self.cli("retry_dead_batches.py", "--dry-run", "--curator", "qwen")
        self.assertEqual(ast.literal_eval(output.split("死批:", 1)[1].strip()), ["207-b01"])

    def test_rule_partition_report_handles_new_and_existing_records(self):
        self.cli("curate_workspace.py", "rule", "--tasks", "108")
        records = self.curate / ".generated" / "preprocessed" / "rule" / "108" / "curation.json"
        data = self.load_json(records)
        self.assertEqual([entry["pred_partition"] for entry in data["files"]], ["standard", "noise"])
        spec = importlib.util.spec_from_file_location("fixture_label_report", self.curate / "gen_label_acc_report.py")
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
        truth = {"108": {"docs/report.txt": {"partition": "standard", "category": "canonical"},
                         "docs/report_draft.txt": {"partition": "noise", "category": "superseded"}}}
        scores, _ = report.compute("rule", truth)
        self.assertEqual(scores["train15"]["p"], [2, 2])
        for entry in data["files"]:
            entry.pop("pred_partition")
        records.write_text(json.dumps(data), encoding="utf-8")
        scores, _ = report.compute("rule", truth)
        self.assertEqual(scores["train15"]["p"], [2, 2])

    def test_create_eval_preserves_nested_same_names_utf8_and_posix_paths(self):
        subenvs = self.root / "subenvs"
        workspace = subenvs / "108-fixture" / "workspace"
        for folder, content in [("甲", "正本"), ("乙", "噪声")]:
            source = workspace / folder / "report.txt"
            source.parent.mkdir(parents=True)
            source.write_text(content, encoding="utf-8")
        (subenvs / "_ocr").mkdir()
        output = self.root / "exports"
        self.cli("create_eval.py", "--subenvs", str(subenvs), "--out", str(output))
        metadata = self.load_json(output / "108-fixture" / "metadata.json")
        self.assertEqual({entry["target_path"] for entry in metadata["data_manifest"]},
                         {"甲/report.txt", "乙/report.txt"})
        for entry in metadata["data_manifest"]:
            copied = (output / "108-fixture" / entry["stored_relpath"]).read_text(encoding="utf-8")
            self.assertEqual(copied, "正本" if entry["target_path"].startswith("甲/") else "噪声")
            self.assertNotIn("\\", entry["stored_relpath"])
        self.assertFalse((output / "_ocr").exists())


if __name__ == "__main__":
    unittest.main()
