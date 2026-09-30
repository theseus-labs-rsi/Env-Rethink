# envgen-kit image: Node 24 (Codex CLI) + Python (kit).
#
# The Codex CLI version is pinned exactly.  Every orchestrator records the version
# it ran with and refuses a trace produced by a different CLI, so a drifting
# `npm install -g` must never be used for comparable runs.
FROM node:24-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/envgen-kit/.cache/tiktoken

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      python3-pip \
      git \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex@0.144.5

WORKDIR /opt/envgen-kit
COPY requirements.lock.txt ./
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock.txt
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/opt/envgen-kit/src

COPY src ./src
COPY scripts ./scripts
COPY tests ./tests
COPY docs ./docs
COPY examples ./examples
COPY README.md PROVENANCE.json ./

# Mount the benchmark workspace and the artifact root from the host; the kit
# never writes inside the workspace it is given.
WORKDIR /work
CMD ["python3", "/opt/envgen-kit/scripts/generate_workspace_event_log.py", "--help"]
