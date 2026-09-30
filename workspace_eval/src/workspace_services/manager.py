from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from workspace_env.manifest_staging import (
    SURFACE_COLLECTION_MAP,
    SURFACE_HISTORY,
    ManifestStagingError,
    enabled_surfaces,
)

from .mail.core import FixtureError as MailFixtureError
from .mail.core import validate_fixture as validate_mail_fixture
from .mail.server import client_environment as mail_client_environment
from .wecom.core import FixtureError as WeComFixtureError
from .wecom.core import validate_fixture as validate_wecom_fixture


Json = Any
SCHEMA_VERSION = 1


class WorkspaceServiceError(RuntimeError):
    """Raised when task-scoped workspace services cannot be used safely."""


class HarnessExecutionGate:
    """A writer-preferring shared/exclusive gate around process environment use."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._shared = 0
        self._exclusive = False
        self._exclusive_waiters = 0

    @contextmanager
    def acquire(self, *, exclusive: bool) -> Iterator[None]:
        with self._condition:
            if exclusive:
                self._exclusive_waiters += 1
                try:
                    while self._exclusive or self._shared:
                        self._condition.wait()
                    self._exclusive = True
                finally:
                    self._exclusive_waiters -= 1
            else:
                while self._exclusive or self._exclusive_waiters:
                    self._condition.wait()
                self._shared += 1
        try:
            yield
        finally:
            with self._condition:
                if exclusive:
                    self._exclusive = False
                else:
                    self._shared -= 1
                self._condition.notify_all()


HARNESS_EXECUTION_GATE = HarnessExecutionGate()


WECOM_PROMPTS = {
    "cn": (
        "企业微信访问已预授权，无需执行 init 或扫码登录。\n"
        "所有服务数据只能通过 wecom-cli 获取，禁止直接读取任务配置目录"
        "（/workspace/strict/tasks/*/services/）下的 wecom.json / mail.json 等文件。\n"
        "可用命令：\n"
        "- wecom-cli auth show --auth-status\n"
        "- wecom-cli contact get_userlist '{}'\n"
        "- wecom-cli msg get_msg_chat_list '{...}'\n"
        "- wecom-cli msg get_message '{...}'\n"
        "- wecom-cli msg get_msg_media '{\"media_id\":\"...\"}'\n"
        "- wecom-cli doc get_doc_content '{\"url\":\"...\",\"type\":2}'\n\n"
        "若 wecom-cli 不可用，用等价命令兜底：\n"
        "PYTHONPATH=/workspace/Workspace-Bench/evaluation/src python3 -m workspace_services.wecom.cli <同上参数>\n\n"
        "消息附件需先取得 media_id，再下载并读取 local_path。\n"
        "在线文档可能返回 task_done=false，需携带 task_id 继续轮询。"
    ),
    "en": (
        "WeCom access is pre-authorized; do not run init or attempt QR-code login.\n"
        "All service data must be retrieved via wecom-cli. Do NOT read the service "
        "config files (wecom.json / mail.json) under /workspace/strict/tasks/*/services/ directly.\n"
        "Available commands:\n"
        "- wecom-cli auth show --auth-status\n"
        "- wecom-cli contact get_userlist '{}'\n"
        "- wecom-cli msg get_msg_chat_list '{...}'\n"
        "- wecom-cli msg get_message '{...}'\n"
        "- wecom-cli msg get_msg_media '{\"media_id\":\"...\"}'\n"
        "- wecom-cli doc get_doc_content '{\"url\":\"...\",\"type\":2}'\n\n"
        "If wecom-cli is unavailable, fall back to:\n"
        "PYTHONPATH=/workspace/Workspace-Bench/evaluation/src python3 -m workspace_services.wecom.cli <same args>\n\n"
        "For message attachments, obtain the media_id first, then download and read local_path.\n"
        "Online documents may return task_done=false; continue polling with the returned task_id."
    ),
}

MAIL_PROMPTS = {
    "cn": (
        "邮箱访问已预配置为任务级 Mock；不要运行 setup，也不要连接真实邮箱服务。\n"
        "所有邮件数据只能通过标准 IMAP/SMTP 客户端获取，禁止直接读取任务配置目录"
        "（/workspace/strict/tasks/*/services/）下的 wecom.json / mail.json 等文件。\n"
        "连接所需的 IMAP_* 和 SMTP_* 环境变量已经注入（IMAP_HOST/IMAP_PORT/IMAP_USER/"
        "IMAP_PASS/SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS 等），可用 python3 imaplib/smtplib "
        "标准库直接连接（IMAP_TLS=false 明文即可）。下载附件请写到工作目录内路径。"
    ),
    "en": (
        "Email access is preconfigured as a task-scoped mock. Do not run setup or connect to a real mail service.\n"
        "All mail data must be retrieved via a standard IMAP/SMTP client. Do NOT read the service "
        "config files (wecom.json / mail.json) under /workspace/strict/tasks/*/services/ directly.\n"
        "The required IMAP_* and SMTP_* environment variables are already injected "
        "(IMAP_HOST/IMAP_PORT/IMAP_USER/IMAP_PASS/SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS). "
        "Use python3 imaplib/smtplib to connect (plaintext, IMAP_TLS=false). Download attachments "
        "to a path inside the working directory."
    ),
}


WORKSPACE_ENV_PROMPTS = {
    "cn": (
        "工作区环境层已就绪：可调用 MCP 工具 workspace_map / workspace_search / event_search "
        "浏览「集合地图」和「工作区历史」。\n"
        "workspace_map 返回完整集合卡目录（只有 card_id、标题、简述与文件数，不含成员路径）；"
        "workspace_search 用 card_id 展开某集合的成员路径，用 path 精确定位一个文件名/路径，"
        "用 query 作为兜底关键词检索（三者互斥，续页只传 cursor）；"
        "event_search 按路径/关键词/操作类别/时间范围检索可见的工作历史，detail=true 才返回公开摘录。\n"
        "这些工具是只读的，返回内容有 token 上限；历史记录是推断出的合成记录，不作为事实来源的替代，"
        "关键结论仍须结合文件本身核对。不要直接读取任务配置目录"
        "（/workspace/strict/tasks/*/services/）下的 fixture.json 或 blobs/。"
    ),
    "en": (
        "A workspace environment layer is available: call the MCP tools workspace_map / workspace_search / "
        "event_search to browse the collection map and the visible work history.\n"
        "workspace_map lists every collection card (card_id, title, description, file count — never member "
        "paths); workspace_search expands one card's member paths with card_id, locates a concrete file with "
        "path, and falls back to keywords with query (mutually exclusive; continue with cursor only); "
        "event_search looks up visible work history by path, keywords, action class or time range, and returns "
        "public excerpts only with detail=true.\n"
        "The tools are read-only and token-bounded. The history is synthesized inference, not a substitute for "
        "the files themselves; verify important conclusions against the files. Do not read fixture.json or "
        "blobs/ under /workspace/strict/tasks/*/services/ directly."
    ),
}


def _read_json(path: Path) -> Json:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _resolve_task_path(task_dir: Path, value: Json, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceServiceError(f"{label} must be a non-empty relative path")
    raw = Path(value)
    if raw.is_absolute():
        raise WorkspaceServiceError(f"{label} must be relative to the task directory")
    task_root = task_dir.resolve()
    resolved = (task_root / raw).resolve()
    try:
        resolved.relative_to(task_root)
    except ValueError as exc:
        raise WorkspaceServiceError(f"{label} escapes the task directory") from exc
    return resolved


def _metadata_task_dir(meta: dict[str, Json]) -> Path:
    metadata_path = meta.get("__metadata_path")
    if not isinstance(metadata_path, str) or not metadata_path.strip():
        raise WorkspaceServiceError("workspace services require __metadata_path")
    path = Path(metadata_path).resolve()
    if not path.is_file():
        raise WorkspaceServiceError(f"metadata file not found: {path}")
    return path.parent


def has_workspace_services(meta: dict[str, Json]) -> bool:
    value = meta.get("workspace_services")
    return isinstance(value, dict) and bool(value)


def workspace_service_prompt(meta: dict[str, Json], *, language: str) -> str:
    services = meta.get("workspace_services")
    if not isinstance(services, dict):
        return ""
    prompt_language = "cn" if language == "cn" else "en"
    sections: list[str] = []
    if "wecom" in services:
        sections.append(WECOM_PROMPTS[prompt_language])
    if "mail" in services:
        sections.append(MAIL_PROMPTS[prompt_language])
    if "workspace_env" in services:
        sections.append(WORKSPACE_ENV_PROMPTS[prompt_language])
    return "\n\n".join(sections)


def _validate_expectations(meta: dict[str, Json], task_dir: Path) -> Path | None:
    raw = meta.get("service_expectations")
    if raw in (None, ""):
        return None
    path = _resolve_task_path(task_dir, raw, "service_expectations")
    if not path.is_file():
        raise WorkspaceServiceError(f"service expectations file not found: {path}")
    return path


class _ProcessWorkspaceService:
    """Shared lifecycle for task-scoped local workspace service processes."""

    name = ""
    display_name = "workspace"

    def __init__(
        self,
        *,
        task_dir: Path,
        config: dict[str, Json],
        work_dir: Path,
        raw_dir: Path,
        runner_python: str,
    ) -> None:
        unknown = sorted(set(config) - {"fixture", "blobs"})
        if unknown:
            raise WorkspaceServiceError(
                f"workspace_services.{self.name} contains unknown field(s): {', '.join(unknown)}"
            )
        self.fixture_path = _resolve_task_path(
            task_dir, config.get("fixture"), f"workspace_services.{self.name}.fixture"
        )
        self.blobs_dir = _resolve_task_path(
            task_dir, config.get("blobs"), f"workspace_services.{self.name}.blobs"
        )
        self.work_dir = work_dir.resolve()
        self.raw_dir = raw_dir.resolve()
        self.state_dir = self.raw_dir / "workspace-services-private" / self.name
        self.ready_file = self.state_dir / "ready.json"
        self.runner_python = runner_python
        self.installed_dependencies = False
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout_handle: Any = None
        self.stderr_handle: Any = None
        self.ready: dict[str, Json] | None = None
        self.stop_forced = False
        self.stop_failed = False
        self.unexpected_exit = False

    @property
    def stdout_path(self) -> Path:
        return self.raw_dir / f"{self.name}-service-stdout.txt"

    @property
    def stderr_path(self) -> Path:
        return self.raw_dir / f"{self.name}-service-stderr.txt"

    def validate(self) -> None:
        if not self.work_dir.is_dir():
            raise WorkspaceServiceError(f"workspace directory not found: {self.work_dir}")
        if not self.fixture_path.is_file():
            raise WorkspaceServiceError(f"{self.display_name} fixture not found: {self.fixture_path}")
        if not self.blobs_dir.is_dir():
            raise WorkspaceServiceError(
                f"{self.display_name} blob directory not found: {self.blobs_dir}"
            )
        try:
            self._validate_fixture()
        except (MailFixtureError, WeComFixtureError) as exc:
            raise WorkspaceServiceError(f"invalid {self.display_name} fixture: {exc}") from exc

    def _validate_fixture(self) -> None:
        raise NotImplementedError

    def _command(self) -> list[str]:
        raise NotImplementedError

    def _prepare_service_environment(self, env: dict[str, str]) -> dict[str, str]:
        """Hook for services that must prepare their own process environment.

        Called after ``state_dir`` has been recreated and before the service
        process starts; implementations may install runtime dependencies or
        extend ``PYTHONPATH``.
        """

        return env

    def _close_logs(self) -> None:
        for handle_name in ("stdout_handle", "stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)

    def _load_ready(self) -> dict[str, Json]:
        try:
            value = _read_json(self.ready_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError(f"invalid {self.display_name} ready file: {exc}") from exc
        if not isinstance(value, dict):
            raise WorkspaceServiceError(
                f"invalid {self.display_name} ready file: expected an object"
            )
        base_url = value.get("base_url")
        token = value.get("token")
        instance_id = value.get("instance_id")
        if (
            not isinstance(base_url, str)
            or not base_url.startswith("http://127.0.0.1:")
            or not isinstance(token, str)
            or not token
            or not isinstance(instance_id, str)
            or not instance_id
        ):
            raise WorkspaceServiceError(f"invalid {self.display_name} ready file contents")
        self._validate_ready(value)
        return value

    def _validate_ready(self, value: dict[str, Json]) -> None:
        del value

    def _health_request(self) -> dict[str, Json]:
        if not isinstance(self.ready, dict):
            raise WorkspaceServiceError(f"{self.display_name} ready data is unavailable")
        request = urllib.request.Request(
            str(self.ready["base_url"]).rstrip("/") + "/health",
            headers={"Authorization": f"Bearer {self.ready['token']}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError(f"{self.display_name} health check failed: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("errcode") != 0
            or value.get("status") != "ready"
            or value.get("instance_id") != self.ready.get("instance_id")
        ):
            raise WorkspaceServiceError(
                f"{self.display_name} health check returned an invalid response"
            )
        return value

    def start(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.stdout_handle = self.stdout_path.open("wb")
        self.stderr_handle = self.stderr_path.open("wb")
        try:
            self.validate()
            env = os.environ.copy()
            src_root = str(Path(__file__).resolve().parents[1])
            pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                src_root if not pythonpath else src_root + os.pathsep + pythonpath
            )
            env = self._prepare_service_environment(env)
            self.process = subprocess.Popen(
                self._command(),
                stdout=self.stdout_handle,
                stderr=self.stderr_handle,
                env=env,
            )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise WorkspaceServiceError(
                        f"{self.display_name} service exited during startup with code "
                        f"{self.process.returncode}"
                    )
                if self.ready_file.is_file():
                    self.ready = self._load_ready()
                    self._health_request()
                    return
                time.sleep(0.05)
            raise WorkspaceServiceError(
                f"timed out waiting for {self.display_name} ready file"
            )
        except BaseException:
            self.stop()
            raise

    def check_running(self) -> None:
        if self.process is None:
            raise WorkspaceServiceError(f"{self.display_name} service was not started")
        if self.process.poll() is not None:
            raise WorkspaceServiceError(
                f"{self.display_name} service exited unexpectedly with code "
                f"{self.process.returncode}"
            )
        self._health_request()

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is not None:
            self.unexpected_exit = True
        elif process is not None:
            try:
                process.send_signal(signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.stop_forced = True
                try:
                    process.send_signal(signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.stop_failed = True
        self._close_logs()

    @property
    def evaluation_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _agent_path(self) -> str:
        bin_dir = str(self.evaluation_root / "bin")
        current_path = os.environ.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []
        return current_path if bin_dir in path_parts else (
            bin_dir if not current_path else bin_dir + os.pathsep + current_path
        )

    def _agent_pythonpath(self) -> str:
        src_root = str(self.evaluation_root / "src")
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        pythonpath_parts = current_pythonpath.split(os.pathsep) if current_pythonpath else []
        return current_pythonpath if src_root in pythonpath_parts else (
            src_root
            if not current_pythonpath
            else src_root + os.pathsep + current_pythonpath
        )

    def environment(self) -> dict[str, str]:
        raise NotImplementedError

    def _copy_artifact(self, source_name: str, target_name: str) -> Path | None:
        source = self.state_dir / source_name
        target = self.raw_dir / target_name
        if not source.is_file():
            return None
        shutil.copy2(source, target)
        return target

    @staticmethod
    def _public_manifest(value: Json) -> dict[str, Json]:
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def _artifact_name(self, suffix: str) -> str:
        return f"{self.name}-service-{suffix}"

    def collect(self) -> dict[str, Json]:
        manifest_name = self._artifact_name("manifest.json")
        events_name = self._artifact_name("events.jsonl")
        health_name = self._artifact_name("health.json")
        manifest_path = self._copy_artifact(manifest_name, manifest_name)
        events_path = self._copy_artifact(events_name, events_name)
        health_path = self._copy_artifact(health_name, health_name)
        if events_path is None and self.state_dir.exists():
            events_path = self.raw_dir / events_name
            events_path.touch()
        if manifest_path is not None:
            try:
                _write_json(manifest_path, self._public_manifest(_read_json(manifest_path)))
            except (OSError, json.JSONDecodeError):
                pass

        health: dict[str, Json] = {}
        if health_path is not None:
            try:
                value = _read_json(health_path)
                if isinstance(value, dict):
                    health = value
            except (OSError, json.JSONDecodeError):
                pass
        events: list[dict[str, Json]] = []
        if events_path is not None:
            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
            except (OSError, json.JSONDecodeError):
                events = []
        operations = Counter(
            str(event.get("operation"))
            for event in events
            if isinstance(event.get("operation"), str)
        )
        errors: list[str] = []
        status = str(health.get("status") or "")
        if self.stop_forced:
            errors.append(
                f"{self.display_name} service did not stop within 5 seconds and was killed"
            )
        if self.stop_failed:
            errors.append(f"{self.display_name} service could not be stopped")
        if self.unexpected_exit:
            errors.append(
                f"{self.display_name} service exited before shutdown was requested"
            )
        if status != "stopped":
            errors.append(
                f"{self.display_name} final health status is {status or 'missing'}, "
                "expected stopped"
            )
        if self.process is not None and self.process.returncode not in (0, None):
            errors.append(
                f"{self.display_name} service exited with code {self.process.returncode}"
            )
        if manifest_path is None or events_path is None or health_path is None:
            errors.append(f"one or more {self.display_name} audit artifacts are missing")

        return {
            "summary": {
                "status": status or ("error" if errors else "stopped"),
                "instanceId": health.get("instanceId")
                or (self.ready.get("instance_id") if isinstance(self.ready, dict) else None),
                "requestCount": int(health.get("requestCount") or len(events)),
                "errorCount": int(
                    health.get("errorCount")
                    or sum(1 for event in events if event.get("status") == "error")
                ),
                "operations": dict(sorted(operations.items())),
                "resourceIds": [],
                "artifacts": {
                    "manifest": f"raw/{manifest_name}",
                    "events": f"raw/{events_name}",
                    "health": f"raw/{health_name}",
                },
            },
            "errors": errors,
        }

    def cleanup_private(self, *, retain: bool) -> bool:
        if retain:
            return self.state_dir.exists()
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        private_root = self.state_dir.parent
        try:
            private_root.rmdir()
        except OSError:
            pass
        return False


class WeComService(_ProcessWorkspaceService):
    name = "wecom"
    display_name = "WeCom"

    def _validate_fixture(self) -> None:
        validate_wecom_fixture(self.fixture_path, self.blobs_dir, self.work_dir)

    def _command(self) -> list[str]:
        return [
            self.runner_python,
            "-m",
            "workspace_services.wecom.server",
            "serve",
            "--fixture",
            str(self.fixture_path),
            "--blobs",
            str(self.blobs_dir),
            "--workspace-root",
            str(self.work_dir),
            "--state-dir",
            str(self.state_dir),
            "--ready-file",
            str(self.ready_file),
            "--port",
            "0",
        ]

    def environment(self) -> dict[str, str]:
        if not self.ready_file.is_file():
            raise WorkspaceServiceError("WeCom ready file is unavailable")
        return {
            "WECOM_MOCK_CONFIG": str(self.ready_file),
            "PATH": self._agent_path(),
            "PYTHONPATH": self._agent_pythonpath(),
        }


class MailService(_ProcessWorkspaceService):
    name = "mail"
    display_name = "mail"

    def _validate_fixture(self) -> None:
        validate_mail_fixture(self.fixture_path, self.blobs_dir, self.work_dir)

    def _command(self) -> list[str]:
        return [
            self.runner_python,
            "-m",
            "workspace_services.mail.server",
            "serve",
            "--fixture",
            str(self.fixture_path),
            "--blobs",
            str(self.blobs_dir),
            "--workspace-root",
            str(self.work_dir),
            "--state-dir",
            str(self.state_dir),
            "--ready-file",
            str(self.ready_file),
            "--imap-port",
            "0",
            "--smtp-port",
            "0",
            "--health-port",
            "0",
        ]

    def _validate_ready(self, value: dict[str, Json]) -> None:
        if value.get("host") != "127.0.0.1":
            raise WorkspaceServiceError("invalid mail ready file contents")
        for key in ("imap_port", "smtp_port"):
            port = value.get(key)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise WorkspaceServiceError("invalid mail ready file contents")
        for key in ("login", "password", "address"):
            if not isinstance(value.get(key), str) or not str(value[key]).strip():
                raise WorkspaceServiceError("invalid mail ready file contents")
        environment = value.get("environment")
        if not isinstance(environment, dict):
            raise WorkspaceServiceError("invalid mail ready file contents")
        expected = mail_client_environment(value)
        if environment != expected:
            raise WorkspaceServiceError("invalid mail ready file contents")

    def environment(self) -> dict[str, str]:
        if not isinstance(self.ready, dict):
            raise WorkspaceServiceError("mail ready data is unavailable")
        environment = self.ready.get("environment")
        if not isinstance(environment, dict):
            raise WorkspaceServiceError("invalid mail ready file contents")
        out = {str(key): str(value) for key, value in environment.items()}
        out["MAIL_DOWNLOAD_DIR"] = ".mail/downloads"
        out["PATH"] = self._agent_path()
        return out


# The sidecar needs these at import time.  Versions are pinned to the ones the
# benchmark image ships (``mcp`` in particular: the server reads the SDK's tool
# manager, so an unexpected minor upgrade could change the tool schema).
WORKSPACE_ENV_RUNTIME_REQUIREMENTS = (
    "mcp==1.27.0",
    "uvicorn==0.53.0",
    "jsonschema==4.26.0",
    "tiktoken==0.13.0",
)
# ``tiktoken`` resolves ``cl100k_base`` from a BPE file it downloads once and
# caches by URL hash; the image preheats that cache at build time, so a remote
# sandbox has to warm it too (the download itself is reachable from there).
WORKSPACE_ENV_TOKENIZER_ENCODING = "cl100k_base"
WORKSPACE_ENV_INSTALL_TIMEOUT_SECONDS = 600


class WorkspaceEnvService(_ProcessWorkspaceService):
    """MCP sidecar serving the generated collection map and work history."""

    name = "workspace_env"
    display_name = "workspace_env"

    @property
    def deps_dir(self) -> Path:
        return self.state_dir / "python-deps"

    def _dependencies_available(self, extra_pythonpath: str | None) -> bool:
        env = os.environ.copy()
        if extra_pythonpath:
            env["PYTHONPATH"] = extra_pythonpath
        probe = subprocess.run(
            [
                self.runner_python,
                "-c",
                "import mcp, uvicorn, jsonschema, tiktoken",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        return probe.returncode == 0

    def _install_dependencies(self) -> None:
        """Install the sidecar's own runtime dependencies when they are missing.

        The benchmark image already ships them; a bare remote sandbox image does
        not, and there is no image rebuild in the loop there.  ``uv`` (present in
        both) resolves through the image's package index, and the result lands in
        a task-private directory that only this service's ``PYTHONPATH`` sees.
        """

        if self._dependencies_available(str(self.deps_dir)):
            self.installed_dependencies = False
            return
        self.deps_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.state_dir / "deps-install.log"
        candidates: list[list[str]] = []
        if shutil.which("uv"):
            candidates.append(
                [
                    "uv",
                    "pip",
                    "install",
                    "--system",
                    "--target",
                    str(self.deps_dir),
                    *WORKSPACE_ENV_RUNTIME_REQUIREMENTS,
                ]
            )
        candidates.append(
            [
                self.runner_python,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--target",
                str(self.deps_dir),
                *WORKSPACE_ENV_RUNTIME_REQUIREMENTS,
            ]
        )
        errors: list[str] = []
        for command in candidates:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=WORKSPACE_ENV_INSTALL_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{command[0]}: {type(exc).__name__}: {exc}")
                continue
            log_path.write_text(
                "$ " + " ".join(command) + "\n" + (completed.stdout or "") + (completed.stderr or ""),
                encoding="utf-8",
            )
            if completed.returncode == 0 and self._dependencies_available(str(self.deps_dir)):
                self.installed_dependencies = True
                self._warm_tokenizer_cache()
                return
            errors.append(
                f"{command[0]} exited {completed.returncode}: "
                + (completed.stderr or completed.stdout or "").strip()[-500:]
            )
        raise WorkspaceServiceError(
            "workspace_env sidecar dependencies are unavailable and could not be "
            "installed: " + " | ".join(errors)
        )

    def _warm_tokenizer_cache(self) -> None:
        """Fetch the tokenizer encoding once, under the service's own cache dir."""

        cache_dir = self.state_dir / "tiktoken-cache"
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.deps_dir)
        env["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
        try:
            completed = subprocess.run(
                [
                    self.runner_python,
                    "-c",
                    (
                        "import tiktoken; "
                        f"tiktoken.get_encoding({WORKSPACE_ENV_TOKENIZER_ENCODING!r})"
                    ),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=WORKSPACE_ENV_INSTALL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceServiceError(
                f"workspace_env tokenizer cache is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise WorkspaceServiceError(
                "workspace_env tokenizer cache is unavailable: "
                + (completed.stderr or completed.stdout or "").strip()[-500:]
            )

    def _prepare_service_environment(self, env: dict[str, str]) -> dict[str, str]:
        self._install_dependencies()
        existing = env.get("PYTHONPATH")
        deps = str(self.deps_dir)
        env["PYTHONPATH"] = deps if not existing else deps + os.pathsep + existing
        if self.installed_dependencies:
            # Only override the image's cache when the dependencies (and hence
            # the cache location) came from the task-private install.
            env["TIKTOKEN_CACHE_DIR"] = str(self.state_dir / "tiktoken-cache")
        return env

    def _validate_fixture(self) -> None:
        fixture = _read_json(self.fixture_path)
        if not isinstance(fixture, dict) or fixture.get("schema_version") != 1:
            raise WorkspaceServiceError("invalid workspace_env fixture schema version")
        artifacts = fixture.get("artifacts")
        if not isinstance(artifacts, dict):
            raise WorkspaceServiceError("invalid workspace_env fixture artifacts")
        try:
            surfaces = enabled_surfaces(fixture)
        except ManifestStagingError as exc:
            raise WorkspaceServiceError(f"invalid workspace_env fixture surfaces: {exc}") from exc
        required: list[str] = []
        if SURFACE_COLLECTION_MAP in surfaces:
            required += ["collection_set", "member_index"]
        if SURFACE_HISTORY in surfaces:
            required += ["events"]
        for key in required:
            relative = artifacts.get(key)
            if not isinstance(relative, str) or not relative.strip():
                raise WorkspaceServiceError(f"invalid workspace_env fixture artifact: {key}")
            candidate = (self.blobs_dir / relative).resolve()
            try:
                candidate.relative_to(self.blobs_dir)
            except ValueError as exc:
                raise WorkspaceServiceError(
                    f"workspace_env artifact escapes the blob directory: {relative}"
                ) from exc
            if not candidate.is_file():
                raise WorkspaceServiceError(f"workspace_env artifact not found: {candidate}")

    def _command(self) -> list[str]:
        return [
            self.runner_python,
            "-m",
            "workspace_env.server",
            "--fixture",
            str(self.fixture_path),
            "--blobs",
            str(self.blobs_dir),
            "--workspace-root",
            str(self.work_dir),
            "--state-dir",
            str(self.state_dir),
            "--ready-file",
            str(self.ready_file),
            "--port",
            "0",
        ]

    def _validate_ready(self, value: dict[str, Json]) -> None:
        if value.get("mcp_path") != "/mcp":
            raise WorkspaceServiceError("invalid workspace_env ready file contents")

    def hook_environment(self) -> dict[str, str]:
        """Paths a native hook (``workspace_env.agent_context_hook``) needs.

        The hook is a separate short-lived process started by the *agent's* CLI,
        not by this service, so it cannot inherit the service's environment.  It
        needs exactly two things beyond the repo on ``PYTHONPATH``: the
        task-private dependency directory (``tiktoken`` is not in every image)
        and, when the dependencies came from that install, the matching
        tokenizer cache — a cold ``cl100k_base`` lookup goes to the network.

        Only paths are advertised; the hook command itself is assembled inside
        the sandbox, because the artifact root only exists once the service has
        staged it.
        """

        pythonpath = str(self.deps_dir)
        agent_pythonpath = self._agent_pythonpath()
        if agent_pythonpath:
            pythonpath += os.pathsep + agent_pythonpath
        environment = {
            "WORKSPACE_BENCH_WORKSPACE_ENV_ARTIFACT_ROOT": str(self.state_dir / "artifacts"),
            "WORKSPACE_BENCH_WORKSPACE_ENV_HOOK_PYTHONPATH": pythonpath,
        }
        if self.installed_dependencies:
            environment["WORKSPACE_BENCH_WORKSPACE_ENV_TIKTOKEN_CACHE_DIR"] = str(
                self.state_dir / "tiktoken-cache"
            )
        return environment

    def environment(self) -> dict[str, str]:
        if not isinstance(self.ready, dict):
            raise WorkspaceServiceError("workspace_env ready data is unavailable")
        return {
            "WORKSPACE_BENCH_MCP_URL": str(self.ready["base_url"]).rstrip("/") + "/mcp",
            "WORKSPACE_BENCH_MCP_TOKEN": str(self.ready["token"]),
            "PATH": self._agent_path(),
            "PYTHONPATH": self._agent_pythonpath(),
            # 原生 hook（答题 agent 的 CLI 自己拉起的进程）拿不到本服务的环境，
            # 所以要把它需要的路径一并交出去；由沙盒内的驱动拼成 hook 命令。
            **self.hook_environment(),
        }


WORKSPACE_SERVICE_PROVIDERS = {
    "wecom": WeComService,
    "mail": MailService,
    "workspace_env": WorkspaceEnvService,
}


class WorkspaceServiceManager:
    def __init__(
        self,
        *,
        meta: dict[str, Json],
        work_dir: str,
        raw_dir: str,
        runner_python: str | None = None,
    ) -> None:
        self.meta = meta
        self.work_dir = Path(work_dir).resolve()
        self.raw_dir = Path(raw_dir).resolve()
        self.runner_python = str(runner_python or sys.executable)
        self.services: list[_ProcessWorkspaceService] = []
        self.start_error: str | None = None
        self.runtime_errors: list[str] = []
        raw_config = meta.get("workspace_services")
        self._configured = raw_config not in (None, {}, "")
        if not self._configured:
            return

        task_dir = _metadata_task_dir(meta)
        _validate_expectations(meta, task_dir)
        raw_services = raw_config
        if not isinstance(raw_services, dict):
            raise WorkspaceServiceError("workspace_services must be an object")
        unknown = sorted(set(raw_services) - set(WORKSPACE_SERVICE_PROVIDERS))
        if unknown:
            raise WorkspaceServiceError(
                f"unsupported workspace service provider(s): {', '.join(unknown)}"
            )
        for provider_name, provider_config in raw_services.items():
            if not isinstance(provider_config, dict):
                raise WorkspaceServiceError(
                    f"workspace_services.{provider_name} must be an object"
                )
            provider = WORKSPACE_SERVICE_PROVIDERS[provider_name]
            self.services.append(
                provider(
                    task_dir=task_dir,
                    config=provider_config,
                    work_dir=self.work_dir,
                    raw_dir=self.raw_dir,
                    runner_python=self.runner_python,
                )
            )

    @property
    def enabled(self) -> bool:
        return bool(self.services)

    @property
    def service_names(self) -> list[str]:
        return [service.name for service in self.services]

    def validate(self) -> None:
        for service in self.services:
            service.validate()

    def start(self) -> None:
        started: list[_ProcessWorkspaceService] = []
        try:
            for service in self.services:
                service.start()
                started.append(service)
        except BaseException as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            for service in reversed(started):
                service.stop()
            if isinstance(exc, WorkspaceServiceError):
                raise
            raise WorkspaceServiceError(
                f"failed to start workspace service: {type(exc).__name__}: {exc}"
            ) from exc

    def environment(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for service in self.services:
            out.update(service.environment())
        return out

    def check_running(self) -> None:
        for service in self.services:
            service.check_running()

    def stop(self) -> None:
        for service in reversed(self.services):
            service.stop()

    def collect(self) -> dict[str, Json]:
        summaries: dict[str, Json] = {}
        errors = list(self.runtime_errors)
        for service in self.services:
            result = service.collect()
            summaries[service.name] = result["summary"]
            errors.extend(str(item) for item in result.get("errors", []) if str(item))
        return {
            "schemaVersion": SCHEMA_VERSION,
            "services": summaries,
            "healthy": not errors,
            "errors": errors,
        }

    def finalize_private_state(
        self, summary: dict[str, Json], *, retain: bool
    ) -> dict[str, Json]:
        services = summary.get("services") if isinstance(summary.get("services"), dict) else {}
        for service in self.services:
            if retain and not service.state_dir.exists():
                service.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            retained = service.cleanup_private(retain=retain)
            item = services.get(service.name)
            if isinstance(item, dict):
                item["privateStateRetained"] = retained
                if retained:
                    item["privateStatePath"] = (
                        f"raw/workspace-services-private/{service.name}"
                    )
        return summary


def write_workspace_service_error(
    raw_dir: str | Path,
    *,
    message: str,
    phase: str,
    service_names: Iterable[str] = (),
) -> None:
    raw_path = Path(raw_dir)
    raw_path.mkdir(parents=True, exist_ok=True)
    known_names = set(WORKSPACE_SERVICE_PROVIDERS)
    for name in dict.fromkeys(str(item) for item in service_names):
        if name not in known_names:
            continue
        (raw_path / f"{name}-service-stdout.txt").touch(exist_ok=True)
        (raw_path / f"{name}-service-stderr.txt").touch(exist_ok=True)
    _write_json(
        raw_path / "workspace-service-error.json",
        {
            "schemaVersion": SCHEMA_VERSION,
            "errorType": "WorkspaceServiceError",
            "phase": phase,
            "message": str(message),
        },
    )
