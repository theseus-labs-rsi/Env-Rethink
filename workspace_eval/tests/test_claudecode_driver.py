"""Exercise the real Node driver against a local SDK fixture, without API calls."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


EVAL_ROOT = Path(__file__).resolve().parents[1]
DRIVER = EVAL_ROOT / "baselines" / "ClaudeCode.js"
NODE = shutil.which("node")

SDK_FIXTURE = r"""
import fs from 'fs';
import path from 'path';
export async function* query({ prompt, options }) {
  const check = (tool, input) => options.canUseTool(tool, input, {});
  const permissions = {};
  for (const [name, tool, input] of [
    ['localRead', 'Read', { file_path: 'input.txt' }],
    ['externalRead', 'Read', { file_path: '/private/outside.txt' }],
    ['externalWrite', 'Write', { file_path: '/private/outside.txt' }],
    ['venv', 'Bash', { command: '/opt/workspace-bench/evaluation-venv/bin/python script.py' }],
    ['venvRead', 'Read', { file_path: '/opt/workspace-bench/evaluation-venv/bin/python' }],
    ['venvAsData', 'Bash', { command: 'cat /opt/workspace-bench/evaluation-venv/bin/python' }],
    ['otherOpt', 'Bash', { command: '/opt/workspace-bench/evaluation-venv/bin/other script.py' }],
    ['venvExternalOutput', 'Bash', { command: '/opt/workspace-bench/evaluation-venv/bin/python script.py /private/output.txt' }],
    ['externalTemp', 'Bash', { command: 'soffice --convert-to csv --outdir /tmp input.xlsx' }],
    ['localScratch', 'Bash', { command: 'soffice --convert-to csv --outdir .file-review-scratch input.xlsx' }],
    ['managedScratch', 'Bash', { command: `soffice --convert-to csv --outdir "${options.env.TMPDIR}" input.xlsx` }],
    ['mcp', 'mcp__workspace_env__search', { query: 'fixture' }],
  ]) permissions[name] = (await check(tool, input)).behavior;
  fs.writeFileSync(path.join(options.cwd, 'captured.json'), JSON.stringify({
    cliPath: options.pathToClaudeCodeExecutable,
    maxTurns: options.maxTurns,
    mcpServers: options.mcpServers,
    model: options.env.ANTHROPIC_MODEL,
    tmpdir: options.env.TMPDIR,
    temp: options.env.TEMP,
    tmp: options.env.TMP,
    permissions,
  }));
  if (prompt === 'empty') return;
  yield { type: 'system', subtype: 'init', session_id: 'fixture-session' };
  if (prompt === 'init-only') return;
  if (prompt === 'sdk-error') {
    yield { type: 'result', subtype: 'error_max_turns', is_error: true, errors: ['Exceeded max turns'] };
    return;
  }
  if (prompt === 'error-flag') {
    yield { type: 'result', subtype: 'success', is_error: true, result: 'Provider refused the request' };
    return;
  }
  yield { type: 'assistant', message: { id: 'msg1', content: [
    { type: 'text', text: '["model_output/answer.txt"]' },
    { type: 'tool_use', id: 'call1', name: 'Read', input: { file_path: 'input.txt' } },
  ] } };
  if (prompt === 'truncated') return;
  yield { type: 'user', message: { content: [
    { type: 'tool_result', tool_use_id: 'call1', content: 'fixture input content' },
  ] } };
  yield { type: 'result', subtype: 'success', is_error: false,
    usage: { input_tokens: 10, output_tokens: 5 } };
}
"""


@unittest.skipUnless(NODE, "Node.js is required for the driver regression checks")
class ClaudeCodeDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claudecode_driver_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.eval_dir = self.root / "workspace_eval"
        self.driver = self.eval_dir / "baselines" / "ClaudeCode.js"
        self.driver.parent.mkdir(parents=True)
        shutil.copy2(DRIVER, self.driver)
        (self.driver.parent / "package.json").write_text(
            '{"type":"module"}', encoding="utf-8"
        )
        self.sdk = self.install_sdk(self.eval_dir / "node_modules")
        self.work = self.root / "work"
        self.work.mkdir()

    @staticmethod
    def install_sdk(modules_dir):
        sdk = modules_dir / "@anthropic-ai" / "claude-agent-sdk"
        sdk.mkdir(parents=True)
        (sdk / "sdk.mjs").write_text(SDK_FIXTURE, encoding="utf-8")
        (sdk / "cli.js").write_text("// fixture CLI is never executed", encoding="utf-8")
        return sdk

    def run_driver(self, prompt="success", modules_dir=None):
        config = self.root / "config.json"
        report = self.root / "report.json"
        config.write_text(json.dumps({"tasks": [{
            "id": "fixture", "prompt": prompt, "cwd": str(self.work),
            "timeout": 5, "maxTurns": 7,
            "mcpServers": {"workspace_env": {"type": "http", "url": "http://127.0.0.1:1/mcp"}},
            "customProvider": {"baseUrl": "http://127.0.0.1:1", "apiKey": "fixture", "modelName": "fixture-model"},
        }]}), encoding="utf-8")
        env = dict(os.environ)
        env.pop("WORKSPACE_BENCH_NODE_MODULES", None)
        if modules_dir is not None:
            env["WORKSPACE_BENCH_NODE_MODULES"] = str(modules_dir)
        proc = subprocess.run(
            [NODE, str(self.driver), str(config), "-o", str(report)],
            cwd=self.root, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        self.assertTrue(report.is_file(), proc.stderr)
        record = json.loads(report.read_text(encoding="utf-8"))["tasks"][0]
        capture = json.loads((self.work / "captured.json").read_text(encoding="utf-8"))
        return proc, record, capture

    def test_success_preserves_report_tools_and_options(self):
        proc, record, capture = self.run_driver()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(record["status"], "passed")
        self.assertIsNone(record["errorMessage"])
        self.assertEqual(record["textOutputs"], ['["model_output/answer.txt"]'])
        self.assertEqual(record["toolCalls"][0]["output"], "fixture input content")
        self.assertEqual(capture["maxTurns"], 7)
        self.assertEqual(capture["model"], "fixture-model")
        self.assertIn("workspace_env", capture["mcpServers"])

    def test_explicit_sdk_errors_remain_failed(self):
        for prompt, expected in [
            ("sdk-error", "Exceeded max turns"),
            ("error-flag", "Provider refused the request"),
        ]:
            with self.subTest(prompt=prompt):
                proc, record, _ = self.run_driver(prompt)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(record["status"], "failed")
                self.assertIn(expected, record["errorMessage"])

    def test_stream_without_terminal_result_fails(self):
        for prompt in ["empty", "init-only", "truncated"]:
            with self.subTest(prompt=prompt):
                proc, record, _ = self.run_driver(prompt)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(record["status"], "failed")
                self.assertIn("without a terminal result", record["errorMessage"])

    def test_permission_fence_and_task_scratch(self):
        _, _, capture = self.run_driver()
        permissions = capture["permissions"]
        for name in ["localRead", "venv", "localScratch", "managedScratch", "mcp"]:
            self.assertEqual(permissions[name], "allow", name)
        for name in ["externalRead", "externalWrite", "venvRead", "venvAsData",
                     "otherOpt", "venvExternalOutput", "externalTemp"]:
            self.assertEqual(permissions[name], "deny", name)
        scratch = Path(capture["tmpdir"])
        self.assertEqual(scratch.parent, self.work)
        self.assertEqual(capture["temp"], str(scratch))
        self.assertEqual(capture["tmp"], str(scratch))
        self.assertFalse(scratch.exists(), "scratch must be removed after the task")

    def test_sdk_layouts_and_image_modules_precedence(self):
        _, _, capture = self.run_driver()
        self.assertEqual(Path(capture["cliPath"]), self.sdk / "cli.js")
        staged = self.root / "evaluation"
        self.eval_dir.rename(staged)
        self.eval_dir = staged
        self.driver = staged / "baselines" / "ClaudeCode.js"
        self.sdk = staged / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"
        _, _, capture = self.run_driver()
        self.assertEqual(Path(capture["cliPath"]), self.sdk / "cli.js")
        image_modules = self.root / "image-node" / "node_modules"
        image_sdk = self.install_sdk(image_modules)
        _, _, capture = self.run_driver(modules_dir=image_modules)
        self.assertEqual(Path(capture["cliPath"]), image_sdk / "cli.js")
        _, _, capture = self.run_driver(modules_dir=self.root / "missing")
        self.assertEqual(Path(capture["cliPath"]), self.sdk / "cli.js")

    def test_python_adapter_propagates_sdk_failure(self):
        with mock.patch.object(sys, "path", [str(EVAL_ROOT / "src"), *sys.path]):
            adapter = importlib.import_module("agents.claudecode")
        with mock.patch.object(adapter, "CLAUDECODE_JS", str(self.driver)), \
                mock.patch.object(adapter, "BASELINES_DIR", str(self.driver.parent)), \
                mock.patch.object(adapter, "load_dotenv"), \
                mock.patch.object(adapter, "_workspace_mcp_servers", return_value={}), \
                mock.patch.dict(os.environ, {"WORKSPACE_BENCH_NODE_MODULES": str(self.eval_dir / "node_modules")}):
            result = adapter.run(
                prompt="sdk-error", work_dir=str(self.work),
                sandbox_dir=str(self.root / "sandbox"), timeout_s=5,
                api_provider={"provider_type": "anthropic", "model": "fixture-model",
                              "baseUrl": "http://127.0.0.1:1", "apiKey": "fixture"},
            )
        self.assertEqual(result["status"], "error")
        self.assertIn("Exceeded max turns", result["errorMessage"])

    def test_python_adapter_process_failure_overrides_passed_report(self):
        with mock.patch.object(sys, "path", [str(EVAL_ROOT / "src"), *sys.path]):
            adapter = importlib.import_module("agents.claudecode")
        for exit_code, expected_status in [(1, "error"), (124, "timeout")]:
            with self.subTest(exit_code=exit_code):
                def popen(command, **kwargs):
                    report = Path(command[command.index("-o") + 1])
                    report.write_text(json.dumps({"tasks": [{
                        "status": "passed", "textOutputs": [], "stdout": "",
                    }]}), encoding="utf-8")
                    process = mock.Mock(returncode=exit_code)
                    process.communicate.return_value = (b"", b"")
                    return process

                with mock.patch.object(adapter, "CLAUDECODE_JS", str(self.driver)), \
                        mock.patch.object(adapter, "BASELINES_DIR", str(self.driver.parent)), \
                        mock.patch.object(adapter, "load_dotenv"), \
                        mock.patch.object(adapter, "_workspace_mcp_servers", return_value={}), \
                        mock.patch.object(adapter.subprocess, "Popen", side_effect=popen):
                    result = adapter.run(
                        prompt="fixture", work_dir=str(self.work),
                        sandbox_dir=str(self.root / f"sandbox_exit_{exit_code}"), timeout_s=5,
                        api_provider={"provider_type": "anthropic", "model": "fixture-model",
                                      "baseUrl": "http://127.0.0.1:1", "apiKey": "fixture"},
                    )
                self.assertEqual(result["status"], expected_status)
                self.assertTrue(result["errorMessage"])


if __name__ == "__main__":
    unittest.main()
