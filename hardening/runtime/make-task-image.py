#!/usr/bin/env python3
"""从题目自己的 environment/Dockerfile 推导「每题镜像 + 布局映射」。

用法（宿主）：
    python3 runtime/make-task-image.py <task> [<task> ...]        # 只生成文件
    python3 runtime/make-task-image.py --build <task> [...]        # 生成并本地构建
    python3 runtime/make-task-image.py --build --push <task> [...] # 再推送

做三件事：
  1. runtime/task-images/<task>.Dockerfile —— FROM wb-tb-base:<tag> + 题目 Dockerfile 的其余步骤；
  2. runtime/task-layouts/<task>.yaml      —— 把题目 Dockerfile 里的 `COPY <src> <dst>` 转成
     `environment/<src>: <dst>`（runner 据此把环境文件铺到容器里，agent/oracle 都能看到）；
  3. 打印自检提示（多阶段 / USER 切换 / apt 联网 等需要人看一眼的地方）。

只支持单阶段 Dockerfile（多阶段会拒绝并说明原因）——复杂题请手写 task-images/<task>.Dockerfile。
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "tasks"          # 可用 --tasks-root 换成 tasks-tb21 等
OUT_IMG = REPO / "runtime/task-images"
OUT_LAYOUT = REPO / "runtime/task-layouts"
BASE_REPO = os.environ.get("TB_IMAGE_REPO", "").rstrip("/")   # 空 = 只用本地镜像名
BASE_TAG = "20260916"


def parse_dockerfile(text: str) -> tuple[int, list[tuple[str, str]], list[str]]:
    """返回 (FROM 段数, [(src, dst)], 备注列表)。"""
    froms = re.findall(r"(?mi)^FROM\s+(\S+)", text)
    notes: list[str] = []
    if "USER " in text:
        notes.append("题目切了 USER —— 单容器里 agent 以 root 跑，检查权限是否仍可写")
    if "apt-get install" in text:
        notes.append("有 apt-get install —— 构建时需要能连内网 apt 源")
    copies: list[tuple[str, str]] = []
    for m in re.finditer(r"(?mi)^COPY\s+(?!--from)(\S+)\s+(\S+)\s*$", text):
        copies.append((m.group(1), m.group(2)))
    return len(froms), copies, notes


APT_MIRROR_RUN = (
    # 构建期源修复（实测：公网 deb.debian.org 40s/8MB、archive.ubuntu.com 125KB/s，都会拖到构建失败）
    "# 构建期源修复（实测：公网 deb.debian.org 40s/8MB、archive.ubuntu.com 125KB/s；EOL 发行版的安全池已移到 archive）\n"
    # 逐文件扫：debian 用 debian.sources、ubuntu 用 ubuntu.sources —— 只认 debian 那一个文件名会漏掉 ubuntu（mailman 就是）
    "RUN for S in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do [ -f \"$S\" ] || continue; "
    "sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|archive.debian.org|g; "
    "s|//archive.ubuntu.com/ubuntu|//mirrors.cloud.tencent.com/ubuntu|g; "
    "s|//security.ubuntu.com/ubuntu|//mirrors.cloud.tencent.com/ubuntu|g; "
    "s|//ports.ubuntu.com/ubuntu-ports|//mirrors.cloud.tencent.com/ubuntu-ports|g' \"$S\"; "
    # bullseye/buster（EOL）：安全池只有 archive 有，主仓仍在腾讯镜像上
    # EOL 发行版（bullseye/buster）的 security 套件哪儿都拉不到（安全池已下线）→ 直接注释掉，主仓够用
    "if grep -qE 'bullseye|buster' \"$S\" 2>/dev/null; then sed -i '/security/ s|^|# EOL: |' \"$S\"; fi; done; "
    "echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99no-valid-until"
)


def inject_apt_mirror_after_every_from(text: str) -> str:
    """在每个 FROM 之后插入内网 apt 源替换。

    --keep-from 保留题目原 Dockerfile（可能是 debian:bullseye-slim、也可能是多阶段），
    题目自己的 apt 步骤在 FROM 之后立刻执行 —— 只把替换追加到文件末尾等于没生效（实测 qemu-* 仍超时）。
    """
    out = []
    for line in text.splitlines():
        out.append(line)
        if re.match(r"(?i)^FROM\s", line):
            out.append(APT_MIRROR_RUN)
    return "\n".join(out) + "\n"


def strip_apt_lists_cleanup(text: str) -> str:
    """删掉题目 Dockerfile 里的 `rm -rf /var/lib/apt/lists/*`。

    踩过（qemu-alpine-ssh/qemu-startup）：题目把 apt 拆成多步，第一步 update+install 后清了包索引，
    后面还有 `apt install -y telnet netcat expect tmux asciinema` → 找不到包，exit 100，镜像建不出来
    （这几道题的**种子镜像也因此从未建成**）。索引留着只多几十 MB，换来多步 apt 可用。
    """
    # 跨行写法（`... \` + 换行 + `&& rm -rf ...`）必须连着续行符一起去掉，
    # 否则行尾的反斜杠会把下一行（如 WORKDIR）吞进这条 RUN —— 实测过，Dockerfile 直接被改坏。
    text = re.sub(r"\\\s*\n\s*&&\s*rm -rf /var/lib/apt/lists/\*", "", text)
    return re.sub(r"\s*&&\s*rm -rf /var/lib/apt/lists/\*", "", text)


def strip_first_from(text: str, base_image: str) -> str:
    """把第一个 FROM 换成底座镜像（其余步骤原样保留；ENV/WORKDIR/RUN/COPY 都跟着走）。

    换完立刻插一条 apt 源替换（走内网镜像）——否则题目 Dockerfile 里的 apt-get install
    会去拉公网，实测慢到构建失败（exit 100），且题目镜像与种子镜像都建不出来。
    """
    out, done = [], False
    for line in text.splitlines():
        if not done and re.match(r"(?i)^FROM\s", line):
            out.append(f"FROM {base_image}")
            out.append(APT_MIRROR_RUN)
            done = True
            continue
        out.append(line)
    return "\n".join(out) + "\n"


NODE_VERSION = "20.18.1"


def tools_layer(need_python: bool = False) -> str:
    """--keep-from 模式追加的工具层：node + 基础工具（agent/判分要用）+（必要时）python3。

    为什么：FROM 替换会把 OS/Python 版本漂移（python:3.11→3.13 后 pyarrow 无 cp313 wheel；
    bookworm→trixie 后 libgl1-mesa-glx / telnet 等包名失效）。保留原 FROM 就得自己补工具层。
    """
    pkgs = "ca-certificates curl xz-utils patch procps jq"
    if need_python:
        pkgs += " python3 python3-pip python3-venv"
    return (
        "\n# --- wb 工具层（--keep-from：保留原 FROM，避免 OS/Python 版本漂移）---\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        f"        {pkgs} \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f'RUN curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz" \\\n'
        "    | tar xJ -C /usr/local --strip-components=1\n"
    )


PIP_SKIP = {"pytest", "pytest-json-ctrf", "jsonschema"}     # 底座已带


def logical_lines(text: str) -> list[str]:
    """折行合成逻辑行（行尾反斜杠），跳过注释行 —— 注释里常提到 tests/ 之类，不能当指令解析。"""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def tests_requirements(tests_dockerfile: Path) -> list[str]:
    """从 tests/Dockerfile 抽出 pip 包（我们单容器跑判分，测试依赖必须烘进镜像）。

    只取依赖，**绝不取 COPY** —— 判分资产（ground_truth / 判分器）任何时候都不进 agent 容器。
    """
    if not tests_dockerfile.exists():
        return []
    pkgs: list[str] = []
    for line in logical_lines(tests_dockerfile.read_text(encoding="utf-8", errors="replace")):
        m = re.match(r"(?i)^\s*RUN\s+.*?(?:uv\s+pip\s+install|pip\s+install)\s+(.+)$", line)
        if not m:
            continue
        for tok in m.group(1).split():
            if tok.startswith("-") or "$" in tok or "=" in tok and "/" in tok:
                continue
            name = re.split(r"[=<>!~\[;]", tok)[0]
            if not name or not re.match(r"^[A-Za-z][\w.\-]*$", name):
                continue
            if name.lower() not in PIP_SKIP:
                pkgs.append(tok)
    return sorted(set(pkgs))


def judge_python(text: str) -> str:
    """判分器该装到哪个 python：题目若建了 venv 并把它设成默认，就必须装进那个 venv。

    踩过：glycan / gsea 在容器里 `uv venv /opt/venv` 且 `ENV PATH=/opt/venv/bin` ——
    判分依赖装进系统 python 后，verifier 报 `No module named pytest`。
    """
    m = re.search(r'(?mi)^ENV\s+VIRTUAL_ENV=["\']?([^"\'\s]+)', text)
    if m:
        return f"{m.group(1).rstrip('/')}/bin/python"
    m = re.search(r"(?mi)^RUN\s+uv\s+venv\s+(\S+)", text)
    if m:
        return f"{m.group(1).rstrip('/')}/bin/python"
    return ""


def pip_install_cmd(py: str, pkgs: list[str], has_uv: bool, break_system: bool = False) -> str:
    """给 venv 装包时用 uv —— `uv venv` 建出来的 venv 里没有 pip（踩过：No module named pip）。

    break_system=True（--keep-from 模式）：系统 python 可能带 PEP 668 externally-managed 标记，
    普通 pip install 会被拒 → 失败后自动用 --break-system-packages 重试。
    """
    if not pkgs:
        return ""
    if py != "python3" and has_uv:
        return f"uv pip install --python {py} --no-cache {' '.join(shlex.quote(p) for p in pkgs)}"
    specs = ' '.join(shlex.quote(p) for p in pkgs)   # 版本区间含 > <，必须加引号（实测被当重定向）
    base = f"{py} -m pip install --no-cache-dir --ignore-installed {specs}"
    if break_system:
        return (
            f"( {base} || {py} -m pip install --no-cache-dir --ignore-installed "
            f"--break-system-packages {specs} )"
        )
    return base


def solution_pip_requirements(solution: Path) -> list[str]:
    """从 solution/solve.sh 或 solution/*.sh 里抽出 pip 依赖 —— 沙盒无外网，这些必须烘进镜像。

    踩过：protein-autointerp-disulfide 的 solve.sh 里 `pip install requests==2.32.4 biopython==1.85`，
    沙盒里装不动 → oracle 直接 rc=1。
    """
    pkgs: list[str] = []
    for sh in sorted(solution.glob("*.sh")):
        for line in logical_lines(sh.read_text(encoding="utf-8", errors="replace")):
            m = re.search(r"(?:python[0-9.]*\s+-m\s+)?pip\d?\s+install\s+(.+)$", line)
            if not m:
                continue
            for tok in m.group(1).split():
                if tok.startswith("-") or "$" in tok or "/" in tok:
                    continue
                if tok.endswith(".txt") or tok.endswith(".toml"):
                    continue
                name = re.split(r"[=<>!~\[;]", tok)[0]
                if name and re.match(r"^[A-Za-z][\w.\-]*$", name):
                    pkgs.append(tok)
    return sorted(set(pkgs))


# 参考解/判分常用 CLI 工具的探测表（TB 官方 harness 自带 tmux/asciinema；我们底座没有，
# 参照解脚本会直接 rc=127 —— 实测 git-multibranch `line 54: tmux: command not found`）
TOOL_PROBES = {
    "tmux": "tmux",
    "asciinema": "asciinema",
    "expect": "expect",
    "screen": "screen",
    "nc": "netcat-openbsd",
    "netcat": "netcat-openbsd",
    "telnet": "inetutils-telnet",
    "socat": "socat",
    "rsync": "rsync",
    "supervisorctl": "supervisor",
    "sqlite3": "sqlite3",
    "redis-cli": "redis-tools",
    "jq": "jq",
}


def solution_tool_requirements(solution: Path, tests: Path) -> list[str]:
    """从 solve.sh / tests 脚本里探测 CLI 工具依赖（tmux 等），按包名返回。

    宽松匹配（宁可多装）：文本里出现命令名就装 —— 多装无害，少装直接 rc=127。
    """
    blobs: list[str] = []
    for sh in sorted(solution.glob("*")):
        if sh.suffix in (".sh", ".bash") or sh.name in ("solve.sh", "solution.sh"):
            blobs.append(sh.read_text(encoding="utf-8", errors="replace"))
    if tests.exists():
        for f in tests.rglob("*"):
            if f.is_file() and f.suffix in (".sh", ".bash", ".py"):
                try:
                    blobs.append(f.read_text(encoding="utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    pass
    text = "\n".join(blobs)
    pkgs: set[str] = set()
    for cmd, pkg in TOOL_PROBES.items():
        if re.search(rf"(?m)(?:^|[|&;(]\s*|sudo\s+){re.escape(cmd)}(?:\s|$|[;|&)])", text) or f"/usr/bin/{cmd}" in text:
            pkgs.add(pkg)
    return sorted(pkgs)


def leak_check(text: str) -> list[str]:
    """生成的镜像 Dockerfile 里不得出现判分资产（我们评测时另外上传 /tests）。只看真实指令行。"""
    bad = []
    for line in logical_lines(text):
        flat = line.strip()
        if not flat or flat.startswith("#"):
            continue
        if re.match(r"(?i)^(COPY|ADD)\b", flat) and re.search(r"/tests\b", flat):
            bad.append(flat[:80])
        if re.search(r"ground_truth|answer_key", flat):
            bad.append(flat[:80])
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--tag", default=BASE_TAG)
    ap.add_argument("--tasks-root", default="tasks", help="题目根目录（如 tasks-tb21）")
    ap.add_argument("--task-dir", default="", help="直接指定题目目录（如某个变体的 build/）——与 --tasks-root 二选一")
    ap.add_argument("--name", default="", help="配合 --task-dir：镜像/布局用的题目名（默认取 task-dir 名）")
    ap.add_argument("--keep-from", action="store_true",
                    help="保留题目原 FROM（不换 wb-tb-base），改为在末尾追加工具层（node+基础工具+判分依赖）。"
                         "用于 FROM 替换会漂移 OS/Python 版本的任务（pyarrow 无 cp313 wheel、trixie 缺包名等）")
    args = ap.parse_args()

    global TASKS
    TASKS = (REPO / args.tasks_root).resolve()
    if args.task_dir:            # 变体镜像：直接指向 <variant>/build（其下有 environment/ tests/）
        TASKS = Path(args.task_dir).resolve().parent
        args.tasks = [Path(args.task_dir).resolve().name]
        args.name = args.name or ""
    OUT_IMG.mkdir(parents=True, exist_ok=True)
    OUT_LAYOUT.mkdir(parents=True, exist_ok=True)
    base_image = f"{BASE_REPO}/wb-tb-base:{args.tag}" if BASE_REPO else f"wb-tb-base:{args.tag}"
    ok: list[str] = []

    if not args.tasks:
        raise SystemExit("给题目名，或用 --task-dir 指定变体 build 目录")
    for task in args.tasks:
        display = args.name or task
        env = TASKS / task / "environment"
        dockerfile = env / "Dockerfile"
        if not dockerfile.exists():
            print(f"[skip] {task}: 没有 environment/Dockerfile")
            continue
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        stages, copies, notes = parse_dockerfile(text)
        if stages != 1 and not args.keep_from:
            print(f"[skip] {task}: {stages} 个 FROM（多阶段），请手写 task-images/{task}.Dockerfile")
            print(f"       或者用 --keep-from（保留原 Dockerfile 的多个 stage 原样构建）")
            continue
        if stages != 1:
            print(f"[note] {task}: {stages} 个 FROM（多阶段）—— 用 --keep-from 原样保留构建")

        img_path = OUT_IMG / f"{display}.Dockerfile"
        text = strip_apt_lists_cleanup(text)      # 多步 apt 的前置修复（见函数注释）
        if args.keep_from:
            need_py = not re.search(r"(?i)python", text)
            body = inject_apt_mirror_after_every_from(text).rstrip() + "\n" + tools_layer(need_python=need_py)
        else:
            body = strip_first_from(text, base_image)

        # 判分依赖：tests/Dockerfile 的 pip 包（我们单容器跑判分）+ 判分器自身的 pytest 三件套
        reqs = tests_requirements(TASKS / task / "tests/Dockerfile")
        judge_pkgs = ["pytest>=7.4,<10", "pytest-json-ctrf>=0.3,<0.6", "jsonschema>=4.17,<5"] + reqs
        # 参考解在 solve.sh 里 pip install 的包（沙盒无外网）也要烘进来
        sol_pkgs = solution_pip_requirements(TASKS / task / "solution")
        # 参考解/判分要用的 CLI 工具（tmux 等；TB 官方 harness 自带，我们底座没有）
        tool_pkgs = [] if args.keep_from else solution_tool_requirements(
            TASKS / task / "solution", TASKS / task / "tests")
        if tool_pkgs:
            body += "\n# 参考解/判分工具依赖（探测自 solve.sh / tests；TB 官方 harness 自带 tmux，我们底座没有）\n"
            body += ("RUN apt-get update && apt-get install -y --no-install-recommends "
                     + " ".join(tool_pkgs) + " \\\n    && rm -rf /var/lib/apt/lists/*\n")
        target = judge_python(text)
        py = target or "python3"
        has_uv = bool(re.search(r"(?mi)^(ENV .*\.local/bin|RUN .*uv\s|RUN .*uv==)", text))
        if target:
            print(f"      · 判分器 python = 题目 venv：{py}（uv={has_uv}）")
        cmd = pip_install_cmd(py, judge_pkgs, has_uv, break_system=args.keep_from)
        if cmd:
            body += "\n# 判分器依赖（来自 tests/Dockerfile + 判分器三件套；判分资产本身不进镜像）\n"
            body += f"RUN {cmd}\n"
        scmd = pip_install_cmd(py, sol_pkgs, has_uv, break_system=args.keep_from)
        if scmd:
            body += "\n# 参考解依赖（来自 solution/*.sh 的 pip install；沙盒无外网，必须烘进镜像）\n"
            body += f"RUN {scmd}\n"
        leaks = leak_check(body)
        if leaks:
            print(f"[FAIL] {task}: 生成的 Dockerfile 命中判分资产关键词 {leaks} —— 请手写")
            continue
        img_path.write_text(body, encoding="utf-8")

        workdirs = re.findall(r"(?mi)^WORKDIR\s+(\S+)", text)
        workdir = workdirs[-1] if workdirs else "/workspace"
        lines = [f"# 由 runtime/make-task-image.py 生成：照 {task}/environment/Dockerfile（单阶段）",
                 f"task: {task}", f"workdir: {workdir}      # 题目 Dockerfile 的最后一个 WORKDIR（agent/参考解/判分的 cwd）",
                 "map:"]
        for src, dst in copies:
            src_clean = src.rstrip("/")
            if (env / src_clean).exists():
                lines.append(f"  environment/{src_clean}: {dst}")
        (OUT_LAYOUT / f"{display}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(
            f"[ok] {task}: → {img_path.name} + {task}.yaml"
            f"（COPY {len(copies)} 条；判分依赖 {len(judge_pkgs)} 个；参考解依赖 {sol_pkgs}）"
        )
        for n in notes:
            print(f"     ⚠ {n}")
        ok.append(task)

    if args.build and ok:
        cmd = ["bash", str(REPO / "runtime/build-task-images.sh")]
        if args.push:
            cmd.append("--push")
        cmd += ok
        print("=== 构建 ===", " ".join(cmd))
        return subprocess.call(cmd, cwd=str(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
