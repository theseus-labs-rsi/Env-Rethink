# Self-contained build for the Workspace-Bench evaluation runtime.
#
# This is a trimmed port of the upstream Workspace-Bench
# `evaluation/docker/Dockerfile`, adapted to this checkout's layout and to the
# harness set this repository supports. The image builds entirely from files in
# this repository, so it does not depend on a pre-built `workspace-bench:local`
# image:
#
#     docker compose -f workspace_eval/docker/docker-compose.yaml build
#     # or: docker build -t workspace-bench:local workspace_eval
#
# The build context is `workspace_eval/` (as set in the compose file), so every
# COPY source below is relative to that directory. Container-internal paths keep
# the upstream `evaluation/` naming so the runtime mounts and env vars still line
# up. Removed relative to upstream: the deepagents/openclaw/dsh harness runtimes
# and the Office/mail skill trees that this checkout does not ship.

FROM node:24-bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKSPACE_BENCH_ROOT=/workspace/Workspace-Bench
ENV WORKSPACE_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV RIP_BENCH_ROOT=/workspace/Workspace-Bench
ENV RIP_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV CODEX_SANDBOX_MODE=danger-full-access
ENV WORKSPACE_BENCH_OFFICE_RUNTIME=/opt/workspace-bench/office-runtime
ENV CODEX_HOME=/opt/workspace-bench/agent-homes/codex/.codex
ENV WORKSPACE_BENCH_CODEX_HOME_TEMPLATE=/opt/workspace-bench/agent-homes/codex/.codex
ENV WORKSPACE_BENCH_CLAUDE_CONFIG_DIR=/opt/workspace-bench/agent-homes/claude/.claude
ENV UV_MANAGED_PYTHON=1
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
ENV UV_PROJECT_ENVIRONMENT=/opt/workspace-bench/evaluation-venv
ENV UV_LINK_MODE=copy
ENV PATH=/opt/workspace-bench/evaluation-venv/bin:/root/.local/bin:$PATH
ENV NODE_PATH=/opt/workspace-bench/office-runtime/node_modules

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    bash \
    ca-certificates \
    curl \
    fonts-noto-cjk \
    fonts-noto-core \
    ghostscript \
    git \
    graphviz \
    libreoffice-calc \
    libreoffice-common \
    libreoffice-core \
    libreoffice-impress \
    libreoffice-writer \
    pandoc \
    pdftk-java \
    poppler-utils \
    qpdf \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    unzip \
    wget \
    zip \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN uv python install 3.11 \
    && PYTHON_BIN="$(uv python find 3.11)" \
    && ln -s "$PYTHON_BIN" /usr/local/bin/python \
    && ln -s "$PYTHON_BIN" /usr/local/bin/python3

ENV UV_PYTHON_DOWNLOADS=never

# Evaluation runtime dependencies, installed through the same `uv` project as
# upstream (`uv sync --frozen`) so the launcher scripts that run
# `uv run --project <eval> --frozen --no-sync python ...` keep working. The
# checked-in `pyproject.toml`/`uv.lock` are the upstream manifests with the
# deepagents group/source removed (this checkout does not ship `deepagents/libs`).
COPY pyproject.toml uv.lock .python-version /opt/workspace-bench/evaluation/

RUN uv sync \
      --project /opt/workspace-bench/evaluation \
      --frozen \
      --group download \
      --group parsing

# Shell-based agents use the evaluation virtual-environment Python wrapper
# below. Install Office helpers in that same environment so a rebuilt image
# retains the full document/PDF/PPTX/XLSX capability of workspace-bench:local.
RUN uv pip install --project /opt/workspace-bench/evaluation \
    defusedxml \
    lxml \
    'markitdown[pptx]' \
    numpy \
    openpyxl \
    pandas \
    pdf2image \
    pdfplumber \
    pymupdf \
    pypdfium2 \
    Pillow \
    python-docx \
    pytesseract \
    reportlab

# Codex runs shell commands through `bash -lc`, whose login initialization can
# discard the PATH set above. Use wrappers rather than symlinks: CPython
# resolves an external symlink to its base interpreter and then loses the
# virtual-environment prefix. The wrappers preserve the venv invocation path,
# so document-parsing dependencies remain directly available as
# `python` / `python3`.
RUN rm -f /usr/local/bin/python /usr/local/bin/python3 \
    && printf '%s\n' \
      '#!/bin/sh' \
      'exec /opt/workspace-bench/evaluation-venv/bin/python "$@"' \
      > /usr/local/bin/python \
    && cp /usr/local/bin/python /usr/local/bin/python3 \
    && chmod 0755 /usr/local/bin/python /usr/local/bin/python3 \
    && python -c 'import pypdf, pdfminer, fitz; print("PDF parsing dependencies available")' \
    && command -v pdftotext \
    && command -v pdfinfo

RUN npm install -g @openai/codex@0.130.0

# `workspace_env` (MCP sidecar + generation chains) counts tokens with tiktoken,
# which downloads its BPE file on first use.  A task container has no egress, so
# warm the cache at build time; TIKTOKEN_CACHE_DIR makes every runtime import use
# the baked copy instead of trying to fetch one.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod -R a+rX "$TIKTOKEN_CACHE_DIR"

# Shared Node runtime for Office/PDF artifact tasks.
COPY docker/office-runtime.package.json /opt/workspace-bench/office-runtime/package.json
RUN npm install --prefix "$WORKSPACE_BENCH_OFFICE_RUNTIME" --omit=dev

# Node dependencies for the agent/judge driver scripts. `baselines/ClaudeCode.js`
# imports the Claude Agent SDK from `<eval>/node_modules`, so install the
# evaluation package.json deps into the image's evaluation directory.
COPY package.json package-lock.json /workspace/Workspace-Bench/evaluation/
RUN npm ci --prefix /workspace/Workspace-Bench/evaluation --omit=dev

# Per-run agent home templates. Upstream seeds these from the Office skill tree;
# this checkout does not ship that tree, so the directories start empty and the
# harnesses copy them into a writable per-run home.
RUN mkdir -p "$CODEX_HOME" "$WORKSPACE_BENCH_CLAUDE_CONFIG_DIR"

RUN command -v soffice \
    && command -v pandoc \
    && command -v pdfinfo \
    && command -v pdftoppm \
    && command -v qpdf \
    && command -v tesseract \
    && command -v dot \
    && python - <<'PY'
import importlib

for module in (
    "docx",
    "openpyxl",
    "pandas",
    "numpy",
    "pdf2image",
    "pdfplumber",
    "pypdf",
    "pptx",
    "reportlab",
    "pytesseract",
):
    importlib.import_module(module)
PY
RUN node -e 'for (const name of ["docx", "pdf-lib", "pptxgenjs", "react", "react-dom", "react-icons", "sharp"]) require(name)'

COPY bin/wecom-cli bin/wecom-mock \
     bin/mail-mock bin/mail-imap bin/mail-smtp \
     /usr/local/bin/
RUN chmod 0755 /usr/local/bin/wecom-cli /usr/local/bin/wecom-mock \
    /usr/local/bin/mail-mock /usr/local/bin/mail-imap /usr/local/bin/mail-smtp

WORKDIR /workspace/Workspace-Bench/evaluation

CMD ["/bin/bash"]
