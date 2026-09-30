"""模型连接：把 (base_url, api_key, model) 整理成 agent CLI 能直接用的形状。

**不内置任何端点知识** —— 连接三件套由调用方传入（`--base-url/--api-key/--model`
或环境变量 `TB_BASE_URL/TB_API_KEY/TB_MODEL`）。

关于两个 base_url 的差别（这是 SDK 拼路径的方式决定的，不是配置问题）：
    claude code 自己会拼 `/v1/messages`，所以给它的 base 到 `/v1` 之前为止；
    codex      自己会拼 `/responses`，所以给它的 base 要**带着** `/v1`。
    `base_url` 按通用约定传入（OpenAI 风格，通常以 `/v1` 结尾），
    anthropic 那一侧由 `_strip_v1()` 推导。
"""

from __future__ import annotations

import os

from dataclasses import dataclass


def _strip_v1(url: str) -> str:
    """`…/v1` → `…`（claude code 的 base 要吃到这一层，它自己拼 /v1/messages）。"""
    url = url.rstrip("/")
    return url[:-3].rstrip("/") if url.endswith("/v1") else url


@dataclass(frozen=True)
class Gateway:
    """一次模型调用的连接信息。"""

    protocol: str                 # anthropic | openai-responses | openai-chat
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = ""

    def redacted(self) -> dict[str, str]:
        """写进产物用 —— 凭据只留"有没有设"，不留值。"""
        return {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "api_key": "<set>" if self.api_key else "<empty>",
        }


def resolve_gateway(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    reasoning_effort: str = "",
) -> Gateway:
    """把调用方给的连接三件套整理成某个 agent 能用的形状。

    protocol:
      anthropic        —— claude code 用（base 去掉尾部 /v1）
      openai-responses —— codex 用，wire_api=responses
      openai-chat      —— codex 用，wire_api=chat
    """
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    mdl = (model or "").strip()
    missing = [n for n, v in (("base_url", base), ("api_key", key), ("model", mdl)) if not v]
    if missing:
        raise ValueError(
            f"缺少模型连接参数：{', '.join(missing)}。"
            "用 --base-url/--api-key/--model 传入，或设 TB_BASE_URL/TB_API_KEY/TB_MODEL。"
        )
    if protocol == "anthropic":
        resolved = _strip_v1(base)
    elif protocol in ("openai-responses", "openai-chat"):
        resolved = base
    else:
        raise ValueError(f"未知协议：{protocol}")
    return Gateway(
        protocol=protocol,
        base_url=resolved,
        api_key=key,
        model=mdl,
        reasoning_effort=(reasoning_effort or "").strip(),
    )


def gateway_from_env(
    *, protocol: str, base_url: str = "", api_key: str = "",
    model: str = "", reasoning_effort: str = "",
) -> Gateway:
    """命令行参数优先，空的话回落到同名环境变量。"""
    return resolve_gateway(
        base_url=base_url or os.environ.get("TB_BASE_URL", ""),
        api_key=api_key or os.environ.get("TB_API_KEY", ""),
        model=model or os.environ.get("TB_MODEL", ""),
        protocol=protocol,
        reasoning_effort=reasoning_effort or os.environ.get("TB_REASONING_EFFORT", ""),
    )


def agent_protocol(agent: str) -> str:
    """哪个 agent 用哪个协议。claude code 只会说 Anthropic Messages；codex 说 Responses。"""
    key = agent.strip().lower()
    if key in ("claude", "claude_code", "claude-code", "claudecode"):
        return "anthropic"
    if key in ("codex",):
        return "openai-responses"
    raise ValueError(f"未知 agent：{agent!r}（支持 claude_code / codex）")


def canonical_agent(agent: str) -> str:
    key = agent.strip().lower()
    return "claude_code" if key in ("claude", "claude_code", "claude-code", "claudecode") else "codex"
