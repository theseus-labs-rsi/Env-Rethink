"""Docker regressions for dependencies hidden by evaluation bind mounts.

Set WORKSPACE_BENCH_TEST_DOCKER=1 to use an existing node:24-alpine image.
The fixture SDK never starts a CLI or calls a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


EVAL_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_EVAL = "/workspace/Workspace-Bench/evaluation"


@unittest.skipUnless(
    os.environ.get("WORKSPACE_BENCH_TEST_DOCKER") == "1",
    "set WORKSPACE_BENCH_TEST_DOCKER=1 for Docker bind regression tests",
)
class RuntimeDependencyMountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker is unavailable")
        inspected = subprocess.run(
            ["docker", "image", "inspect", "node:24-alpine"],
            capture_output=True,
        )
        if inspected.returncode:
            raise unittest.SkipTest("node:24-alpine is not already available locally")
        cls.staging = tempfile.TemporaryDirectory(prefix="workspace-node-bind-")
        root = Path(cls.staging.name)
        cls.image = f"workspace-node-bind-test:{uuid.uuid4().hex}"
        cls.empty_modules = root / "empty-node_modules"
        cls.empty_modules.mkdir()
        dockerfile = (EVAL_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        dependency_env = [
            line for line in dockerfile.splitlines()
            if line.startswith(("ENV WORKSPACE_BENCH_NODE_MODULES=", "ENV NODE_PATH="))
        ]
        shutil.copy2(EVAL_ROOT / "baselines" / "ClaudeCode.js", root / "ClaudeCode.js")
        shutil.copy2(EVAL_ROOT / "baselines" / "package.json", root / "baselines-package.json")
        (root / "sdk.mjs").write_text(
            "export async function* query({options}) {\n"
            "  const expected = process.env.WORKSPACE_BENCH_NODE_MODULES + '/@anthropic-ai/claude-agent-sdk/cli.js';\n"
            "  if (options.pathToClaudeCodeExecutable !== expected) throw new Error('Wrong SDK CLI path');\n"
            "  yield {type:'result',subtype:'success',is_error:false,result:'baked SDK reached',session_id:'fixture'};\n"
            "}\n",
            encoding="utf-8",
        )
        (root / "cli.js").write_text("throw new Error('The fixture CLI must never run');\n", encoding="utf-8")
        (root / "task.json").write_text(
            json.dumps({"tasks": [{"id": "dependency-bind", "prompt": "fixture", "cwd": "/tmp", "timeout": 5}]}),
            encoding="utf-8",
        )
        (root / "Dockerfile").write_text(
            "FROM node:24-alpine\n"
            + "\n".join(dependency_env)
            + "\nCOPY sdk.mjs cli.js ${WORKSPACE_BENCH_NODE_MODULES}/@anthropic-ai/claude-agent-sdk/\n"
            + f"COPY ClaudeCode.js {CONTAINER_EVAL}/baselines/ClaudeCode.js\n"
            + f"COPY baselines-package.json {CONTAINER_EVAL}/baselines/package.json\n"
            + "COPY task.json /fixture/task.json\n",
            encoding="utf-8",
        )
        built = subprocess.run(
            ["docker", "build", "--pull=false", "--network=none", "-t", cls.image, str(root)],
            capture_output=True, text=True, timeout=120,
        )
        if built.returncode:
            subprocess.run(["docker", "image", "rm", cls.image], capture_output=True)
            cls.staging.cleanup()
            raise AssertionError(built.stdout + built.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["docker", "image", "rm", cls.image], capture_output=True)
        cls.staging.cleanup()

    def run_driver(self, source: Path, target: str) -> None:
        command = (
            f"node {CONTAINER_EVAL}/baselines/ClaudeCode.js /fixture/task.json -o /tmp/report.json "
            "&& node -e 'const r = require(\"/tmp/report.json\"); "
            "if (r.summary.passed !== 1) process.exit(1); console.log(\"BAKED_SDK_REACHED\")'"
        )
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--network=none", "--read-only",
                "--tmpfs", "/tmp:rw,size=16m", "--cap-drop=ALL",
                "--mount", f"type=bind,source={source},target={target},readonly",
                self.image, "sh", "-c", command,
            ],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BAKED_SDK_REACHED", result.stdout)

    def test_development_evaluation_bind_keeps_baked_sdk(self) -> None:
        self.run_driver(EVAL_ROOT, CONTAINER_EVAL)

    def test_empty_judge_node_modules_bind_keeps_baked_sdk(self) -> None:
        self.run_driver(self.empty_modules, f"{CONTAINER_EVAL}/node_modules")


if __name__ == "__main__":
    unittest.main()
