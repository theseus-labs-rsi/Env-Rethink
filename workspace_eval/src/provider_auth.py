from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.parse
from typing import Any, Mapping, MutableMapping, Optional


Json = Any


class ProviderAuthError(ValueError):
    """Raised when an API provider authentication configuration is incomplete."""


LEGACY_APP_AUTH_TYPES = {"app_credentials", "app-credentials", "app"}
#: **带缓存键的 APP_ID:APP_KEY**：凭据里附一个稳定的 cache_task_id。
CACHED_APP_AUTH_TYPES = {
    "cached_app_credentials",
    "cached-app-credentials",
}
#: **Anthropic 形状的 APP_ID:APP_KEY**：走 Anthropic 消息协议；部分模型要求凭据带
#: timeout 查询串（见 _needs_timeout_query）。
ANTHROPIC_APP_AUTH_TYPES = {
    "anthropic_app_credentials",
    "anthropic-app-credentials",
    "anthropic_app",
}
#: **纯 bearer key**：没有 APP_ID:APP_KEY 那套，直接一个个人 API key。
#: 归入 app-credentials 家族以触发 ClaudeCode 的本地 bridge（chat/completions 转换），
#: 凭据解析走 config.apiKey 兜底分支。
PLAIN_KEY_AUTH_TYPES = {
    "api_key",
    "api-key",
    "plain_key",
}

#: 别名表：`alias -> 规范名`。**平台专有的历史名字由插件注册**，不进本仓（见 docs）。
_CANONICAL_ALIASES: dict[str, str] = {}


def register_auth_alias(alias: str, canonical: str) -> None:
    """把一个别名注册到某个规范凭据方案。

    给插件用：某个平台的历史 `auth_type` 取值可以在插件里注册回来，
    这样存量配置不用改，而公开树里只出现与厂商无关的规范名。
    """
    key = str(alias or "").strip().lower()
    value = str(canonical or "").strip().lower()
    if key and value:
        _CANONICAL_ALIASES[key] = value


def expand_provider_value(value: Json) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    fallback_re = re.compile(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:-]-(\$\{[A-Za-z_][A-Za-z0-9_]*\}|[^}]*)\}"
    )
    while True:
        match = fallback_re.search(value)
        if not match:
            break
        primary = os.environ.get(match.group(1), "")
        fallback = match.group(2)
        replacement = primary if primary else os.path.expandvars(fallback)
        value = value[: match.start()] + replacement + value[match.end() :]
    value = os.path.expandvars(value).strip()
    if not value or re.search(r"\$\{[^}]+\}", value):
        return None
    return value


def first_provider_value(*values: Json) -> Optional[str]:
    for value in values:
        expanded = expand_provider_value(value)
        if expanded:
            return expanded
    return None


def provider_auth_type(config: Mapping[str, Json]) -> str:
    """凭据方案的**规范名**。别名（含插件注册的平台历史名字）在这里归一。"""
    raw = str(
        first_provider_value(config.get("authType"), config.get("auth_type")) or "bearer"
    ).strip().lower()
    return _CANONICAL_ALIASES.get(raw, raw)


def provider_uses_app_credentials(config: Mapping[str, Json]) -> bool:
    return provider_auth_type(config) in (
        LEGACY_APP_AUTH_TYPES
        | CACHED_APP_AUTH_TYPES
        | ANTHROPIC_APP_AUTH_TYPES
        | PLAIN_KEY_AUTH_TYPES
    )


def provider_uses_cached_app_auth(config: Mapping[str, Json]) -> bool:
    return provider_auth_type(config) in CACHED_APP_AUTH_TYPES


def provider_uses_anthropic_app_auth(config: Mapping[str, Json]) -> bool:
    return provider_auth_type(config) in ANTHROPIC_APP_AUTH_TYPES


def load_dotenv(*paths: str) -> None:
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as stream:
                lines = stream.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _positive_int(value: Json, *, default: int) -> int:
    expanded = (
        str(value).strip()
        if isinstance(value, int) and not isinstance(value, bool)
        else expand_provider_value(value)
    )
    try:
        result = int(expanded) if expanded is not None else int(default)
    except (TypeError, ValueError):
        result = int(default)
    return max(1, result)


def build_app_credential(
    *,
    app_id: str,
    app_key: str,
    provider: str,
    model: str,
    timeout_seconds: int,
    cache_task_id: Optional[str] = None,
) -> str:
    app_id = str(app_id or "").strip()
    app_key = str(app_key or "").strip()
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not app_id or not app_key:
        raise ProviderAuthError("APP_ID and APP_KEY are required")
    if not provider:
        raise ProviderAuthError("authProvider is required")
    if not model:
        raise ProviderAuthError("model is required for app credential authentication")
    task_id = str(cache_task_id or "").strip()
    if not task_id:
        task_id = hashlib.md5(f"{time.time()}{app_id}".encode()).hexdigest()
    query = urllib.parse.urlencode(
        {
            "provider": provider,
            "model": model,
            "timeout": max(1, int(timeout_seconds)),
            "cache_task_id": task_id,
        }
    )
    return f"{app_id}:{app_key}?{query}"


def build_cached_app_credential(
    *,
    app_id: str,
    app_key: str,
    timeout_seconds: int,
    cache_task_id: Optional[str] = None,
) -> str:
    """Build the 网关standard-protocol Authorization bearer value.

    Unlike the legacy compatible-mode credential, the 网关standard
    Chat Completions protocol must not include provider/model query values.
    The model marker is sent in the JSON request body.
    """
    app_id = str(app_id or "").strip()
    app_key = str(app_key or "").strip()
    if not app_id or not app_key:
        raise ProviderAuthError("APP_ID and APP_KEY are required")
    task_id = str(cache_task_id or "").strip()
    if not task_id:
        task_id = hashlib.md5(f"{time.time()}{app_id}".encode()).hexdigest()
    query = urllib.parse.urlencode(
        {
            "cache_task_id": task_id,
            "timeout": max(1, int(timeout_seconds)),
        }
    )
    return f"{app_id}:{app_key}?{query}"


def build_anthropic_app_credential(
    *,
    app_id: str,
    app_key: str,
    timeout_seconds: int,
    timeout_query: bool = True,
) -> str:
    """Build 网关native Anthropic Messages credentials.

    DeepSeek's standard /v1/messages endpoint uses only the positive timeout
    query and does not require provider/model/cache_task_id query values.
    Other models on the same gateway (e.g. Gemini) reject the query-suffixed
    bearer token with HTTP 500, so they must opt out via ``timeout_query``.
    """
    app_id = str(app_id or "").strip()
    app_key = str(app_key or "").strip()
    if not app_id or not app_key:
        raise ProviderAuthError("APP_ID and APP_KEY are required")
    if not timeout_query:
        return f"{app_id}:{app_key}"
    query = urllib.parse.urlencode(
        {"timeout": max(1, int(timeout_seconds))}
    )
    return f"{app_id}:{app_key}?{query}"


def _stable_cache_task_id(
    config: Mapping[str, Json],
    explicit: Optional[str],
) -> Optional[str]:
    configured = first_provider_value(
        explicit,
        config.get("cacheTaskId"),
        config.get("cache_task_id"),
        config.get("__resolved_cache_task_id__"),
    )
    if configured:
        return configured
    app_id = first_provider_value(
        config.get("appId"),
        config.get("app_id"),
        os.environ.get("APP_ID"),
    )
    if not app_id:
        return None
    generated = hashlib.md5(f"{time.time()}{app_id}".encode()).hexdigest()
    if isinstance(config, MutableMapping):
        config["__resolved_cache_task_id__"] = generated
    return generated


def resolve_provider_api_key(
    config: Mapping[str, Json],
    *,
    model: Optional[str] = None,
    cache_task_id: Optional[str] = None,
) -> Optional[str]:
    auth_type = provider_auth_type(config)
    if auth_type in ANTHROPIC_APP_AUTH_TYPES:
        app_id = first_provider_value(
            config.get("appId"),
            config.get("app_id"),
            os.environ.get("APP_ID"),
        )
        app_key = first_provider_value(
            config.get("appKey"),
            config.get("app_key"),
            os.environ.get("APP_KEY"),
        )
        if not app_id or not app_key:
            return None
        model_id = str(model or "").strip().lower()
        # DeepSeek models require the timeout query; other models on the
        # gateway (e.g. Gemini) reject the query-suffixed bearer token.
        timeout_query = "deepseek" in model_id
        return build_anthropic_app_credential(
            app_id=app_id,
            app_key=app_key,
            timeout_seconds=_positive_int(
                config.get("authTimeoutSec")
                if config.get("authTimeoutSec") is not None
                else config.get("auth_timeout_sec"),
                default=120,
            ),
            timeout_query=timeout_query,
        )
    if auth_type in CACHED_APP_AUTH_TYPES:
        app_id = first_provider_value(
            config.get("appId"),
            config.get("app_id"),
            os.environ.get("APP_ID"),
        )
        app_key = first_provider_value(
            config.get("appKey"),
            config.get("app_key"),
            os.environ.get("APP_KEY"),
        )
        if not app_id or not app_key:
            return None
        return build_cached_app_credential(
            app_id=app_id,
            app_key=app_key,
            timeout_seconds=_positive_int(
                config.get("authTimeoutSec")
                if config.get("authTimeoutSec") is not None
                else config.get("auth_timeout_sec"),
                default=120,
            ),
            cache_task_id=_stable_cache_task_id(config, cache_task_id),
        )
    if auth_type in LEGACY_APP_AUTH_TYPES:
        app_id = first_provider_value(
            config.get("appId"),
            config.get("app_id"),
            os.environ.get("APP_ID"),
        )
        app_key = first_provider_value(
            config.get("appKey"),
            config.get("app_key"),
            os.environ.get("APP_KEY"),
        )
        auth_provider = first_provider_value(
            config.get("authProvider"),
            config.get("auth_provider"),
            "ali",
        )
        auth_model = first_provider_value(
            config.get("authModel"),
            config.get("auth_model"),
            model,
            config.get("model"),
        )
        if not app_id or not app_key:
            return None
        return build_app_credential(
            app_id=app_id,
            app_key=app_key,
            provider=auth_provider or "ali",
            model=auth_model or "",
            timeout_seconds=_positive_int(
                config.get("authTimeoutSec")
                if config.get("authTimeoutSec") is not None
                else config.get("auth_timeout_sec"),
                default=60,
            ),
            cache_task_id=_stable_cache_task_id(config, cache_task_id),
        )
    return first_provider_value(
        config.get("apiKey"),
        config.get("api_key"),
    )


def provider_has_credentials(config: Mapping[str, Json]) -> bool:
    auth_type = provider_auth_type(config)
    if auth_type in (
        LEGACY_APP_AUTH_TYPES
        | CACHED_APP_AUTH_TYPES
        | ANTHROPIC_APP_AUTH_TYPES
    ):
        return bool(
            first_provider_value(
                config.get("appId"),
                config.get("app_id"),
                os.environ.get("APP_ID"),
            )
            and first_provider_value(
                config.get("appKey"),
                config.get("app_key"),
                os.environ.get("APP_KEY"),
            )
        )
    return bool(
        first_provider_value(
            config.get("apiKey"),
            config.get("api_key"),
        )
    )


def sanitized_provider_config(config: Mapping[str, Json]) -> dict[str, Json]:
    sensitive = {
        "apikey",
        "api_key",
        "appid",
        "app_id",
        "appkey",
        "app_key",
        "authorization",
        "token",
        "__resolved_cache_task_id__",
    }
    out: dict[str, Json] = {}
    for key, value in config.items():
        normalized = str(key).replace("-", "_").lower()
        if normalized in sensitive:
            continue
        if isinstance(value, dict):
            out[str(key)] = sanitized_provider_config(value)
        elif isinstance(value, list):
            out[str(key)] = [
                sanitized_provider_config(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[str(key)] = value
    return out
