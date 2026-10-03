"""本地 docker runtime：在常驻容器里跑命令、传文件。

对外就三个动作，够加难管线用：

    runtime.run_command(command, timeout)   -> .stdout / .stderr / .return_code
    runtime.upload_file(files=[FileItem], overwrite=True)
    runtime.download_file([FileItem])       -> .files / .errors

几个刻意的选择：
  · 容器用 `docker run -d ... sleep infinity` 常驻，后续全部走 `docker exec`；
    比每次 `docker run` 一次性执行便宜得多（题目容器动辄几个 GB，起来要十几秒）。
  · 上传/下载走 **tar over exec**，不走 `docker cp` 逐文件 —— 一个变体几百个文件，
    逐个 cp 是分钟级，打一个 tar 是秒级。
  · 容器**默认禁网**（`--network none`），需要时显式打开。很多加难轴的成立前提是
    "离线唯一来源"，默认放网会把测量变成"会不会上网对答案"。
"""

from __future__ import annotations

import asyncio
import base64
import io
import posixpath
import shlex
import tarfile
import time
import uuid

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


# ── 传输用的几个小类型 ────────────────────────────────────────────────

@dataclass
class FileItem:
    """一个待传/已传的文件。"""

    path: str
    content: str = ""
    encoding: str = ""          # "" = 文本；"base64" = 二进制


@dataclass
class CommandResult:
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    duration: float = 0.0


@dataclass
class DownloadResponse:
    files: list[FileItem] = field(default_factory=list)
    errors: list[Any] = field(default_factory=list)


@dataclass
class DownloadError:
    path: str
    message: str


# ── 运行时 ────────────────────────────────────────────────────────────

class DockerRuntime:
    """一个常驻容器的句柄。

    典型用法：

        rt = await DockerRuntime.start(image="tb-agent-base:20260916", workdir="/workspace")
        try:
            await rt.upload_file([FileItem(path="/app/x.py", content=src)])
            r = await rt.run_command("python3 -c 'print(1)'")
            ...
        finally:
            await rt.stop()
    """

    def __init__(
        self,
        *,
        image: str,
        name: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        network: str = "none",
        mounts: Sequence[tuple[str, str]] | None = None,
        user: str = "",
        cpus: str = "",
        memory: str = "",
        keep: bool = False,
    ) -> None:
        self.image = image
        self.name = name
        self.workdir = workdir
        self.env = dict(env or {})
        self.network = network
        self.mounts = list(mounts or [])
        self.user = user
        self.cpus = cpus
        self.memory = memory
        self.keep = keep
        self._started = False
        self._log_tail: list[str] = []

    # ── 生命周期 ──────────────────────────────────────────────────────

    @classmethod
    async def start(cls, **kwargs: Any) -> "DockerRuntime":
        rt = cls(**kwargs)
        await rt._start_container()
        return rt

    async def _start_container(self) -> None:
        await self._docker(["rm", "-f", self.name], check=False)
        cmd = ["docker", "run", "-d", "--name", self.name]
        if self.network == "none":
            cmd += ["--network", "none"]
        elif self.network != "default":
            cmd += ["--network", self.network]
        if self.user:
            cmd += ["--user", self.user]
        if self.cpus:
            cmd += ["--cpus", self.cpus]
        if self.memory:
            cmd += ["--memory", self.memory]
        for host, cont in self.mounts:
            cmd += ["-v", f"{host}:{cont}"]
        for k, v in self.env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += ["-w", self.workdir, self.image, "sleep", "infinity"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run 失败（{self.image}）：{(err or b'').decode('utf-8', 'replace')[-800:]}"
            )
        self._started = True

    async def stop(self) -> None:
        if self.keep:
            return
        await self._docker(["rm", "-f", self.name], check=False)
        self._started = False

    async def __aenter__(self) -> "DockerRuntime":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ── docker 底座 ───────────────────────────────────────────────────

    async def _docker(
        self, args: Sequence[str], *, check: bool = True, stdin: bytes | None = None
    ) -> tuple[bytes, bytes, int]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate(stdin)
        except BaseException:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.communicate()
            raise
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"docker {' '.join(args[:3])} 失败 rc={proc.returncode}："
                f"{((err or b'') + (out or b'')).decode('utf-8', 'replace')[-800:]}"
            )
        return out or b"", err or b"", proc.returncode or 0

    # ── 1) run_command ────────────────────────────────────────────────

    async def _terminate_command_group(self, pid_path: str) -> None:
        """Stop the container process group, including command subprocesses."""
        script = f"""for attempt in 1 2 3 4 5; do
  [ -f {shlex.quote(pid_path)} ] && break
  sleep 0.1
done
[ -f {shlex.quote(pid_path)} ] || exit 3
read -r command_pid < {shlex.quote(pid_path)}
case "$command_pid" in ''|*[!0-9]*) exit 2;; esac
[ "$command_pid" -gt 1 ] || exit 2
kill -TERM -- "-$command_pid" 2>/dev/null || true
for attempt in 1 2 3 4 5; do
  kill -0 -- "-$command_pid" 2>/dev/null || break
  sleep 0.1
done
kill -KILL -- "-$command_pid" 2>/dev/null || true
rm -f {shlex.quote(pid_path)}
"""
        await asyncio.wait_for(
            self._docker(["exec", self.name, "bash", "-c", script]), timeout=30
        )

    async def _interrupt_command(self, proc, communication, pid_path: str) -> tuple[bytes, bytes]:
        try:
            await self._terminate_command_group(pid_path)
        except Exception:
            # Missing process metadata is also a cleanup failure: exec may not
            # have started yet. Stop our container before tests can be uploaded.
            await self._docker(["stop", "-t", "0", self.name], check=False)
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=30)
            except Exception:
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
        return communication.result()

    async def run_command(self, command: str, timeout: int | float = 600) -> CommandResult:
        """在容器内跑一条 shell 命令。

        超时返回 124，并先终止容器内命令的整个进程组，避免它与后续判分重叠。
        终止失败时抛异常，调用方必须把它作为基础设施错误处理。
        """
        started = time.monotonic()
        pid_path = f"/tmp/tb-command-{uuid.uuid4().hex}.pid"
        # Bash job control gives this one child its own process group without
        # requiring setsid in every task image. Its nested shells inherit that
        # group, so terminating it also stops agent tools and verifier children.
        launcher = (
            "set -m\n"
            f"bash -c {shlex.quote(command)} &\n"
            "command_pid=$!\n"
            f"printf '%s\\n' \"$command_pid\" > {shlex.quote(pid_path)}\n"
            "wait \"$command_pid\"\n"
            "command_rc=$?\n"
            "exit \"$command_rc\"\n"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.name, "bash", "-c", launcher,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communication = asyncio.create_task(proc.communicate())
        try:
            out, err = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
            rc = proc.returncode or 0
        except asyncio.TimeoutError:
            out, err = await self._interrupt_command(proc, communication, pid_path)
            rc = 124
            err = (err or b"") + f"\n[本地 runtime] 命令超过 {timeout}s 被中断".encode()
        except asyncio.CancelledError:
            await self._interrupt_command(proc, communication, pid_path)
            raise
        else:
            # Keep the group id while communicate waits for inherited pipes;
            # the command shell can exit before its background children do.
            await self._docker(["exec", self.name, "rm", "-f", pid_path])
        result = CommandResult(
            command=command,
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=(err or b"").decode("utf-8", "replace"),
            return_code=rc,
            duration=time.monotonic() - started,
        )
        self._log_tail.append(f"$ {command[:200]}  -> rc={rc} ({result.duration:.1f}s)")
        del self._log_tail[:-50]
        return result

    async def run_script(self, script: str, timeout: int | float = 600) -> CommandResult:
        """跑一段脚本（写进临时文件再执行，避免 shell 引号地狱）。"""
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return await self.run_command(
            f"printf %s {b64!r} | base64 -d > /tmp/tb-script.sh && bash /tmp/tb-script.sh",
            timeout=timeout,
        )

    # ── 2) upload_file ────────────────────────────────────────────────

    async def upload_file(self, files: Sequence[FileItem], overwrite: bool = True) -> None:
        """按 tar 批量写进容器（绝对路径 → 容器根下的相对路径）。"""
        if not files:
            return
        buf = io.BytesIO()
        dirs: set[str] = set()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for item in files:
                raw = self._decode(item)
                parent = posixpath.dirname(item.path)
                if parent and parent not in dirs:
                    dirs.add(parent)
                info = tarfile.TarInfo(name=item.path.lstrip("/"))
                info.size = len(raw)
                info.mode = 0o644
                info.mtime = int(time.time())
                tf.addfile(info, io.BytesIO(raw))
        # 先建目录（tar 里只有文件，父目录不存在会解不出来）
        if dirs:
            # 走 argv 而不是拼 shell：路径是任意文件名（中文 / 空格 / 可能有引号），
            # 拼进命令行会被打断，argv 不会。
            await self._docker(["exec", self.name, "mkdir", "-p", *sorted(dirs)])
        await self._docker(["exec", "-i", self.name, "tar", "-x", "-C", "/"], stdin=buf.getvalue())

    @staticmethod
    def _decode(item: FileItem) -> bytes:
        if item.encoding == "base64":
            return base64.b64decode(item.content or "")
        return (item.content or "").encode("utf-8")

    # ── 3) download_file ──────────────────────────────────────────────

    async def download_file(self, files: Sequence[FileItem]) -> DownloadResponse:
        """把容器里的若干路径捞出来。

        不存在的路径**不报错退出**，而是记进 `errors`：
        artifacts 回收时一半的路径本来就是可选的（ctrf.json 可能没生成）。
        """
        want = [f.path for f in files]
        if not want:
            return DownloadResponse()
        # 路径经 "$@" 传进 shell，**不拼进脚本文本** —— 任意文件名都安全，且只往返一次
        out, _err, _rc = await self._docker([
            "exec", self.name, "sh", "-c",
            'for p in "$@"; do [ -e "$p" ] && printf "%s\\n" "$p"; done',
            "sh", *want,
        ], check=False)
        present = [line for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
        missing = [p for p in want if p not in set(present)]
        resp = DownloadResponse(errors=[DownloadError(p, "not found in container") for p in missing])
        if not present:
            return resp

        rel = [p.lstrip("/") for p in present]
        out, err, rc = await self._docker(
            ["exec", self.name, "tar", "-c", "-C", "/", *rel], check=False, stdin=b""
        )
        if rc != 0:
            resp.errors.append(DownloadError("<tar>", (err or b"").decode("utf-8", "replace")[-400:]))
            return resp

        found: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(out), mode="r") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                fh = tf.extractfile(member)
                found["/" + member.name] = fh.read() if fh else b""
        for path in present:
            raw = found.get(path)
            if raw is None:
                resp.errors.append(DownloadError(path, "tar 里没有这个文件"))
                continue
            resp.files.append(
                FileItem(path=path, content=base64.b64encode(raw).decode("ascii"), encoding="base64")
            )
        return resp

    # ── 便利方法 ──────────────────────────────────────────────────────

    async def put_text(self, path: str, text: str, mode: str = "644") -> None:
        await self.upload_file([FileItem(path=path, content=text)])
        if mode != "644":
            await self._docker(["exec", self.name, "chmod", mode, path])

    async def read_text(self, path: str) -> str | None:
        resp = await self.download_file([FileItem(path=path)])
        for item in resp.files:
            return base64.b64decode(item.content or "").decode("utf-8", "replace")
        return None

    async def exists(self, path: str) -> bool:
        _out, _err, rc = await self._docker(["exec", self.name, "test", "-e", path], check=False)
        return rc == 0

    async def mkdirs(self, *paths: str) -> None:
        if paths:
            await self._docker(["exec", self.name, "mkdir", "-p", *paths])


def read_local_directory_to_file_items(
    *, local_dir: str | Path, container_base_path: str
) -> list[FileItem]:
    """把宿主一个目录整棵读成 FileItem 列表，`container_base_path` 是容器里的对应根。
    二进制安全（base64），保持相对目录结构。
    """
    root = Path(local_dir)
    items: list[FileItem] = []
    if not root.is_dir():
        return items
    base = container_base_path.rstrip("/")
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        items.append(
            FileItem(
                path=f"{base}/{rel}",
                content=base64.b64encode(raw).decode("ascii"),
                encoding="base64",
            )
        )
    return items
