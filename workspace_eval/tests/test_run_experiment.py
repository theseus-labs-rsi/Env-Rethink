"""Regression checks for generated configs and valid/invalid judge scores."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT / "scripts"))
import run_experiment as runner
from judge_results import is_valid_judge_result


def score(*, passed: bool = False) -> dict:
    return {
        "status": "ok",
        "rubrics": [{"index": 0, "passed": passed}],
        "summary": {"total": 1, "passed": int(passed), "failed": int(not passed)},
        "judge": {"status": "ok", "error": None},
    }


class ExperimentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.case = runner.CaseSpec(case_id="task1-r1", task_id="1", repeat=1)
        config = {
            "name": "audit",
            "condition": "task_files",
            "agent": {"model": "local-test", "harness": "ClaudeCode", "attempts": 1},
            "judge": {"model": "local-test", "attempts": 1},
            "runtime": {"resources": {}},
        }
        self.prepared = runner.PreparedRun(
            config=config, config_path=root / "config.yaml", run_id="audit",
            runtime_root=root / "runtime", persistent_root=root / "results",
            runtime_eval_root=root / "runtime" / "evaluation",
            runtime_task_root=root / "tasks", suite_root=root / "suite",
            env_file=None, cases=[self.case], judge_config_path=root / "judge.yaml",
            image="workspace-bench:local", image_id="sha256:offline-test",
        )
        self.prepared.runtime_eval_root.mkdir(parents=True)
        runner._write_case_configs(
            self.prepared,
            {"1": {"file_system": "dataseed"}},
            root / "workspaces",
        )

    def test_harness_selection_is_written_to_real_case_config(self):
        path = self.prepared.suite_root / "configs" / f"{self.case.case_id}.yaml"
        self.assertEqual(yaml.safe_load(path.read_text())['agent_name'], "ClaudeCode")
        del self.prepared.config['agent']['harness']
        self.prepared.config['harness'] = "claude-code"
        runner._write_case_configs(self.prepared, {"1": {"file_system": "dataseed"}}, Path(self.temp.name))
        self.assertEqual(yaml.safe_load(path.read_text())['agent_name'], "ClaudeCode")
        with self.assertRaises(SystemExit):
            runner._agent_harness({"agent": {"harness": "missing-harness"}})

    def test_provider_presets_and_empty_model_id(self):
        for name in ("deepseek-v4-flash", "deepseek-v4-pro"):
            model = runner._model_config({"model": name}, default_effort="max")
            self.assertEqual(model['api_key_env'], "DEEPSEEK_API_KEY")
            overridden = runner._model_config({"model": name, "api_key_env": "CUSTOM_KEY"}, default_effort="max")
            self.assertEqual(overridden['api_key_env'], "CUSTOM_KEY")
        model = runner._model_config({"model": "curator-model", "model_id": ""}, default_effort="max")
        self.assertEqual(runner._api_provider(model)['model'], "curator-model")

    def test_judge_does_not_mount_a_missing_dependency_directory(self):
        case_dir = self.prepared.suite_root / "judge_input" / self.case.case_id / "1"
        command = runner._judge_command(self.prepared, self.case, case_dir)
        volumes = [command[i + 1] for i, word in enumerate(command[:-1]) if word == '-v']
        self.assertFalse(any('/evaluation/node_modules:ro' in volume for volume in volumes))
        modules = self.prepared.runtime_eval_root / "node_modules"
        modules.mkdir()
        (modules / "fixture.js").write_text("// local dependency\n")
        command = runner._judge_command(self.prepared, self.case, case_dir)
        self.assertIn(f"{modules}:/workspace/Workspace-Bench/evaluation/node_modules:ro", command)

    def test_zero_score_is_valid_but_infrastructure_errors_and_bad_counts_are_not(self):
        zero = score()
        self.assertTrue(is_valid_judge_result(zero, 1))
        legacy = copy.deepcopy(zero)
        del legacy['status']
        del legacy['judge']['status']
        self.assertTrue(is_valid_judge_result(legacy, 1))
        for mutation in (
            lambda x: x['judge'].update(error="SDK import failed"),
            lambda x: x.update(status="error"),
            lambda x: x.update(summary=None),
            lambda x: x['summary'].update(passed=1),
            lambda x: x['summary'].update(total="1"),
            lambda x: x['rubrics'][0].update(passed="false"),
            lambda x: x['rubrics'][0].update(index=True),
            lambda x: x.update(rubrics=[]),
        ):
            bad = copy.deepcopy(zero)
            mutation(bad)
            with self.subTest(result=bad):
                self.assertFalse(is_valid_judge_result(bad, 1))

    def run_fake_case(self, judge_result: dict, returncode: int):
        def fake_run(command, **kwargs):
            if command[0] != 'docker':
                agent_case = runner._case_run_root(self.prepared, self.case)
                (agent_case / "output").mkdir(parents=True)
                (agent_case / "output" / "answer.txt").write_text("fixture answer")
                runner._write_json(agent_case / "metadata.json", {"rubrics": ["fixture rubric"]})
                runner._write_json(agent_case / "agent.json", {"status": "passed"})
                return subprocess.CompletedProcess(command, 0)
            judge_case = self.prepared.suite_root / "judge_input" / self.case.case_id / self.case.task_id
            runner._write_json(judge_case / "rubrics_judge--fixture.json", judge_result)
            return subprocess.CompletedProcess(command, returncode)

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            return runner._run_case(self.prepared, self.case)

    def test_valid_all_failed_rubrics_are_scored(self):
        status = self.run_fake_case(score(), 0)
        self.assertEqual(status['status'], "judged")
        self.assertEqual(status['judge_summary'], {"total": 1, "passed": 0, "failed": 1})

    def test_judge_infrastructure_error_is_excluded_from_the_score_summary(self):
        result = score()
        result['judge']['error'] = "fixture SDK failure"
        status = self.run_fake_case(result, 0)
        self.assertEqual(status['status'], "failed")
        self.assertEqual(status['phase'], "judge")
        self.assertNotIn('judge_summary', status)
        runner._write_summary(self.prepared.persistent_root, [status])
        with (self.prepared.persistent_root / "summary.tsv").open(newline='') as handle:
            rows = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['total'], '')

    def test_nonzero_judge_exit_is_not_accepted_even_with_valid_rows(self):
        status = self.run_fake_case(score(passed=True), 1)
        self.assertEqual(status['status'], "failed")
        self.assertNotIn('judge_summary', status)


if __name__ == "__main__":
    unittest.main()
