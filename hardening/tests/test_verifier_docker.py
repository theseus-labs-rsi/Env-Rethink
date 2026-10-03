"""Opt-in verifier integration test; installs only pytest in a disposable container."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eval_task  # noqa: E402
from agentkit import DockerRuntime  # noqa: E402


@unittest.skipUnless(os.environ.get("HARDENING_TEST_DOCKER") == "1", "set HARDENING_TEST_DOCKER=1 for real verifier tests")
class VerifierDockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_verifier_distinguishes_assertions_and_infrastructure(self):
        if not shutil.which("docker") or subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim-bookworm"], capture_output=True,
        ).returncode:
            self.skipTest("existing python:3.12-slim-bookworm image is required")
        runtime = await DockerRuntime.start(
            image="python:3.12-slim-bookworm", name=f"hardening-verifier-test-{uuid.uuid4().hex}",
            workdir="/tmp", network="default", env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            await runtime.put_text("/tests/test.sh", "# pip install deliberately omitted by this offline verifier\n")
            await runtime.put_text("/tests/test_outputs.py", "def test_answer():\n    assert True\n")

            async def verify():
                return await eval_task.run_verifier(
                    runtime, test_timeout=30, workdir="/tmp", allow_net=False, log=lambda _: None,
                )

            with self.assertRaisesRegex(RuntimeError, "pytest 不可用"):
                await verify()
            self.assertIsNone(await runtime.read_text(f"{eval_task.VERIFIER_DIR}/reward.txt"))
            installed = await runtime.run_command(
                "python3 -m pip install --disable-pip-version-check --no-cache-dir pytest==8.4.1 pytest-json-ctrf==0.3.5",
                timeout=120,
            )
            self.assertEqual(installed.return_code, 0, installed.stdout + installed.stderr)

            reward, _, path = await verify()
            self.assertEqual((reward, path), (1.0, "direct-pytest"))
            await runtime.put_text("/tests/test_outputs.py", "def test_answer():\n    assert False\n")
            reward, _, _ = await verify()
            self.assertEqual(reward, 0.0)
            await runtime.put_text("/tests/test_outputs.py", "import missing_verifier_dependency\n")
            with self.assertRaisesRegex(RuntimeError, "rc=2"):
                await verify()
            self.assertIsNone(await runtime.read_text(f"{eval_task.VERIFIER_DIR}/reward.txt"))

            # The task script's usual reward wrapper can suppress pytest's rc;
            # collection errors must still be rejected when it writes zero.
            await runtime.put_text(
                "/tests/test.sh",
                "python3 -m pytest /tests/test_outputs.py -rA\nprintf '0\\n' > /logs/verifier/reward.txt\n",
            )
            with self.assertRaisesRegex(RuntimeError, "未正常运行"):
                await verify()
            await runtime.put_text("/tests/test_outputs.py", "def test_answer():\n    assert False\n")
            reward, _, path = await verify()
            self.assertEqual((reward, path), (0.0, "test.sh"))
            for raw in ("NaN", "Inf", "-Inf", "-0.1", "1.1", "not-a-number", ""):
                with self.subTest(raw=raw):
                    await runtime.put_text("/tests/test.sh", f"printf '%s\\n' '{raw}' > /logs/verifier/reward.txt\n")
                    with self.assertRaises(RuntimeError):
                        await verify()
            for raw, expected in (("0", 0.0), ("1", 1.0)):
                with self.subTest(raw=raw):
                    await runtime.put_text("/tests/test.sh", f"printf '%s\\n' '{raw}' > /logs/verifier/reward.txt\n")
                    reward, _, path = await verify()
                    self.assertEqual((reward, path), (expected, "test.sh"))
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main()
