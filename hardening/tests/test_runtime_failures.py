"""Regressions for infrastructure failures masquerading as successful runs."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


HARDENING_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARDENING_ROOT))

import cli  # noqa: E402
import eval_task  # noqa: E402
import gen_task  # noqa: E402
import image  # noqa: E402
from agentkit import AgentResult, CommandResult  # noqa: E402


def result(rc: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(return_code=rc, stdout=stdout, stderr=stderr)


class VerifierFailureTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *responses: CommandResult):
        return SimpleNamespace(run_command=AsyncMock(side_effect=responses), put_text=AsyncMock())

    async def verify(self, runtime):
        return await eval_task.run_verifier(
            runtime, test_timeout=10, workdir="/workspace", allow_net=False, log=lambda _: None,
        )

    async def test_missing_pytest_is_an_infrastructure_error(self):
        runtime = self.runtime(result(stdout="# pip install pytest"), result(1, stderr="No module named pytest"))
        with self.assertRaisesRegex(RuntimeError, "pytest 不可用"):
            await self.verify(runtime)
        runtime.put_text.assert_not_awaited()

    async def test_pytest_pass_and_assertion_failure_have_valid_rewards(self):
        for rc, reward in ((0, 1.0), (1, 0.0)):
            with self.subTest(rc=rc):
                runtime = self.runtime(result(stdout="# uvx"), result(), result(rc, "pytest output"))
                self.assertEqual(await self.verify(runtime), (reward, "pytest output", "direct-pytest"))
                runtime.put_text.assert_awaited_once_with(f"{eval_task.VERIFIER_DIR}/reward.txt", f"{reward:g}\n")

    async def test_pytest_collection_usage_and_timeout_errors_are_not_scores(self):
        for rc in (2, 3, 4, 5, 124):
            with self.subTest(rc=rc):
                runtime = self.runtime(result(stdout="# uvx"), result(), result(rc, "infrastructure failed"))
                with self.assertRaisesRegex(RuntimeError, f"rc={rc}"):
                    await self.verify(runtime)
                runtime.put_text.assert_not_awaited()

    async def test_script_collection_error_cannot_be_hidden_by_zero_reward(self):
        runtime = self.runtime(
            result(), result(stdout="________________ ERROR collecting /tests/test_outputs.py ________________\n"),
            result(stdout="0\n"),
        )
        with self.assertRaisesRegex(RuntimeError, "未正常运行"):
            await self.verify(runtime)

    async def test_script_dependency_error_cannot_be_hidden_by_zero_reward(self):
        for output in (
            "/usr/bin/python3: No module named pytest\n",
            "/tests/test.sh: line 18: uvx: command not found\n",
            "error: Failed to download pytest\n",
        ):
            with self.subTest(output=output):
                runtime = self.runtime(result(), result(stdout=output), result(stdout="0\n"))
                with self.assertRaisesRegex(RuntimeError, "未正常运行"):
                    await self.verify(runtime)

    async def test_script_assertion_failure_remains_a_valid_zero_reward(self):
        runtime = self.runtime(result(), result(1, "FAILED test_outputs.py::test_answer - AssertionError"), result(stdout="0\n"))
        reward, _, path = await self.verify(runtime)
        self.assertEqual((reward, path), (0.0, "test.sh"))

    async def test_script_without_reward_falls_back_to_pytest(self):
        runtime = self.runtime(result(), result(stdout="no score"), result(3), result(), result())
        reward, output, path = await self.verify(runtime)
        self.assertEqual((reward, path), (1.0, "direct-pytest(fallback)"))
        self.assertIn("fallback: direct pytest", output)

    async def test_script_timeout_never_starts_fallback(self):
        runtime = self.runtime(result(), result(124, "timed out"))
        with self.assertRaisesRegex(RuntimeError, "超时"):
            await self.verify(runtime)
        self.assertEqual(runtime.run_command.await_count, 2)

    async def test_invalid_existing_reward_never_scores_or_starts_fallback(self):
        for raw in ("NaN", "Inf", "-Inf", "-0.1", "1.1", "not-a-number", "", "invalid 1"):
            with self.subTest(raw=raw):
                runtime = self.runtime(result(), result(), result(stdout=raw + "\n"))
                with self.assertRaises(RuntimeError):
                    await self.verify(runtime)
                self.assertEqual(runtime.run_command.await_count, 3)
                runtime.put_text.assert_not_awaited()

    async def test_script_zero_and_one_rewards_remain_valid(self):
        for raw, expected in (("0", 0.0), ("1", 1.0)):
            with self.subTest(raw=raw):
                runtime = self.runtime(result(), result(), result(stdout=raw + "\n"))
                reward, _, path = await self.verify(runtime)
                self.assertEqual((reward, path), (expected, "test.sh"))


class CliFailureTests(unittest.TestCase):
    def batch_args(self, out: str, attempts: int = 1):
        return SimpleNamespace(
            arms="seed=unused", discover="", out=out, concurrency=1, attempts=attempts,
            agent="codex", mode="oracle", base_url="", api_key="", model="", reasoning_effort="",
            tag="test", agent_timeout=10, test_timeout=10, network="none", extra_cli_arg=[],
            display="", keep_container=False,
        )

    def batch(self, responses):
        with tempfile.TemporaryDirectory() as out:
            args = self.batch_args(out, len(responses))
            output = io.StringIO()
            with patch.object(eval_task, "run_eval", AsyncMock(side_effect=responses)), contextlib.redirect_stdout(output):
                rc = asyncio.run(cli._run_batch(args))
            payloads = [json.loads(line) for line in (Path(out) / "results.jsonl").read_text(encoding="utf-8").splitlines()]
            return rc, output.getvalue(), payloads

    def test_batch_returns_failure_and_excludes_errors_from_accuracy(self):
        rc, output, payloads = self.batch([
            eval_task.EvalResult(arm="seed__codex__a1", mode="oracle", reward=1.0),
            eval_task.EvalResult(arm="seed__codex__a2", mode="oracle", status="error", error="missing pytest"),
        ])
        self.assertEqual(rc, 1)
        self.assertIn("accuracy=100%", output)
        self.assertIn("[error] 1", output)
        self.assertEqual([p["status"] for p in payloads], ["ok", "error"])

    def test_valid_zero_reward_does_not_fail_cli(self):
        rc, output, _ = self.batch([eval_task.EvalResult(arm="seed__codex__a1", mode="oracle", reward=0.0)])
        self.assertEqual(rc, 0)
        self.assertIn("accuracy=0%", output)

    def test_eval_returns_failure_for_exception(self):
        with tempfile.TemporaryDirectory() as out:
            args = self.batch_args(out)
            args.arm, args.task_dir = "seed", "unused"
            with patch.object(eval_task, "run_eval", AsyncMock(side_effect=RuntimeError("missing image"))), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_eval(args), 1)

    def test_rounds_stops_at_failed_mechanical_gate(self):
        with tempfile.TemporaryDirectory() as out:
            args = SimpleNamespace(
                task="seed", task_name="", task_dir=out, runs_root=out, round=1, vid="v1-test", axis="",
                agent="codex", base_url="", api_key="", model="", reasoning_effort="", failure_samples="",
                extra_instruction="", agent_timeout=10, test_timeout=10, network="none", keep_container=False,
                tag="test", oracle=True,
            )
            generated = gen_task.GenResult(task="seed", vid="v1-test", out_dir=out)
            completed = [subprocess.CompletedProcess([], 0, "assembled", ""), subprocess.CompletedProcess([], 1, "gate failed", "")]
            with patch.object(gen_task, "run_generation", AsyncMock(return_value=generated)), \
                 patch.object(cli.subprocess, "run", side_effect=completed) as run, \
                 patch.object(image, "task_agent_image") as build_image, \
                 patch.object(eval_task, "run_eval", AsyncMock()) as oracle, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_rounds(args), 1)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.args[0][0] == sys.executable for call in run.call_args_list))
            build_image.assert_not_called()
            oracle.assert_not_awaited()


class GenerationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_history_merge_marks_generation_as_error_and_stops_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = SimpleNamespace(mkdirs=AsyncMock(), stop=AsyncMock())
            harness = SimpleNamespace(run=AsyncMock(return_value=AgentResult(agent="codex", status="ok")))
            gateway = SimpleNamespace(redacted=lambda: {})
            collected = {"files": ["plan.yaml"], "vid": "v1-test", "out_dir": root}
            with patch.object(gen_task.DockerRuntime, "start", AsyncMock(return_value=runtime)), \
                 patch.object(gen_task, "upload_dir", AsyncMock(return_value=1)), \
                 patch.object(gen_task, "answer_digest", return_value=None), \
                 patch.object(gen_task, "upload_workflow", AsyncMock(return_value={})), \
                 patch.object(gen_task.C, "gateway_from_env", return_value=gateway), \
                 patch.object(gen_task, "build_harness", return_value=harness), \
                 patch.object(gen_task, "collect_out", AsyncMock(return_value=collected)), \
                 patch.object(gen_task, "fix_base_marker"), \
                 patch.object(gen_task, "merge_history", return_value={"ok": False, "stdout": "broken causal link"}):
                generated = await gen_task.run_generation(task_dir=root, task_name="seed", runs_root=root, log=lambda _: None)
            self.assertEqual(generated.status, "error")
            self.assertFalse(generated.event_history["ok"])
            self.assertIn("broken causal link", generated.error)
            runtime.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
