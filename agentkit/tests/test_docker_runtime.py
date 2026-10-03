"""Opt-in process-group timeout regressions using an existing small image."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import unittest
import uuid

from agentkit import DockerRuntime


@unittest.skipUnless(os.environ.get("AGENTKIT_TEST_DOCKER") == "1", "set AGENTKIT_TEST_DOCKER=1 for real Docker tests")
class DockerCommandLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not shutil.which("docker") or subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim-bookworm"], capture_output=True,
        ).returncode:
            self.skipTest("existing python:3.12-slim-bookworm image is required")
        self.runtime = await DockerRuntime.start(
            image="python:3.12-slim-bookworm", name=f"agentkit-timeout-test-{uuid.uuid4().hex}",
            workdir="/tmp", network="none",
        )

    async def asyncTearDown(self):
        await self.runtime.stop()

    async def assert_no_late_write(self):
        await asyncio.sleep(1.2)
        checked = await self.runtime.run_command("test ! -e /tmp/late-write && printf clean", timeout=10)
        self.assertEqual(checked.return_code, 0, checked.stdout + checked.stderr)
        self.assertEqual(checked.stdout, "clean")

    async def test_timeout_stops_nested_command_processes(self):
        command = "printf started; bash -c '(sleep 1; printf leaked > /tmp/late-write) & wait'; sleep 10"
        interrupted = await self.runtime.run_command(command, timeout=0.4)
        self.assertEqual(interrupted.return_code, 124, interrupted.stdout + interrupted.stderr)
        self.assertIn("started", interrupted.stdout)
        await self.assert_no_late_write()

    async def test_timeout_stops_background_child_after_command_shell_exits(self):
        interrupted = await self.runtime.run_command("(sleep 1; printf leaked > /tmp/late-write) &", timeout=0.4)
        self.assertEqual(interrupted.return_code, 124, interrupted.stdout + interrupted.stderr)
        await self.assert_no_late_write()

    async def test_cancellation_stops_command_process_group(self):
        running = asyncio.create_task(self.runtime.run_command("(sleep 1; touch /tmp/late-write) & wait", timeout=10))
        await asyncio.sleep(0.4)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await self.assert_no_late_write()

    async def test_normal_command_preserves_output_and_exit_code(self):
        finished = await self.runtime.run_command("printf stdout; printf stderr >&2; exit 7", timeout=10)
        self.assertEqual((finished.stdout, finished.return_code), ("stdout", 7))
        self.assertIn("stderr", finished.stderr)
        leftovers, _, _ = await self.runtime._docker([
            "exec", self.runtime.name, "find", "/tmp", "-maxdepth", "1", "-name", "tb-command-*.pid", "-print",
        ])
        self.assertEqual(leftovers, b"")

    async def test_immediate_timeout_never_leaves_a_running_command(self):
        try:
            interrupted = await self.runtime.run_command("sleep 1; touch /tmp/late-write", timeout=0.001)
        except RuntimeError:
            inspected = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", self.runtime.name], capture_output=True, text=True)
            self.assertEqual(inspected.stdout.strip(), "false")
        else:
            self.assertEqual(interrupted.return_code, 124)
            await self.assert_no_late_write()

    async def test_missing_process_metadata_stops_the_owned_container(self):
        with self.assertRaises(RuntimeError):
            await self.runtime.run_command("rm -f /tmp/tb-command-*.pid; sleep 2; touch /tmp/late-write", timeout=0.4)
        inspected = subprocess.run([
            "docker", "inspect", "--format", "{{.State.Running}}", self.runtime.name,
        ], capture_output=True, text=True)
        self.assertEqual(inspected.stdout.strip(), "false")


if __name__ == "__main__":
    unittest.main()
