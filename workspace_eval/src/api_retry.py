from __future__ import annotations

import email.utils
import http.client
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


Json = Any

RETRYABLE_HTTP_STATUSES = frozenset(
    {
        408,
        409,
        425,
        429,
        500,
        502,
        503,
        504,
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    initial_delay_sec: float = 1.0
    max_delay_sec: float = 30.0
    jitter_ratio: float = 0.25
    total_timeout_sec: float = 300.0
    request_timeout_sec: float = 300.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    attempts: int


class RetryRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = dict(headers or {})
        self.body = body
        self.attempts = attempts
        self.retryable = retryable


def _config_value(config: Mapping[str, Json], *names: str) -> Json:
    for name in names:
        value = config.get(name)
        if value is not None:
            return value
    return None


def _bounded_int(value: Json, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    value: Json,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def retry_policy_from_config(
    config: Optional[Mapping[str, Json]],
    *,
    request_timeout_default: float = 300.0,
    total_timeout_default: float = 300.0,
) -> RetryPolicy:
    provider = config if isinstance(config, Mapping) else {}
    return RetryPolicy(
        max_attempts=_bounded_int(
            _config_value(
                provider,
                "apiRetryMaxAttempts",
                "api_retry_max_attempts",
            ),
            6,
            minimum=1,
            maximum=20,
        ),
        initial_delay_sec=_bounded_float(
            _config_value(
                provider,
                "apiRetryInitialDelaySec",
                "api_retry_initial_delay_sec",
            ),
            1.0,
            minimum=0.0,
            maximum=300.0,
        ),
        max_delay_sec=_bounded_float(
            _config_value(
                provider,
                "apiRetryMaxDelaySec",
                "api_retry_max_delay_sec",
            ),
            30.0,
            minimum=0.0,
            maximum=900.0,
        ),
        jitter_ratio=_bounded_float(
            _config_value(
                provider,
                "apiRetryJitterRatio",
                "api_retry_jitter_ratio",
            ),
            0.25,
            minimum=0.0,
            maximum=1.0,
        ),
        total_timeout_sec=_bounded_float(
            _config_value(
                provider,
                "apiRetryTotalTimeoutSec",
                "api_retry_total_timeout_sec",
            ),
            total_timeout_default,
            minimum=1.0,
            maximum=7200.0,
        ),
        request_timeout_sec=_bounded_float(
            _config_value(
                provider,
                "apiRequestTimeoutSec",
                "api_request_timeout_sec",
                "clientTimeoutSec",
                "client_timeout_sec",
            ),
            request_timeout_default,
            minimum=1.0,
            maximum=7200.0,
        ),
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> Optional[float]:
    raw = ""
    for name, value in headers.items():
        if str(name).lower() == "retry-after":
            raw = str(value).strip()
            break
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _backoff_delay(
    policy: RetryPolicy,
    *,
    failed_attempt: int,
    headers: Mapping[str, str],
) -> float:
    exponential = min(
        policy.max_delay_sec,
        policy.initial_delay_sec * (2 ** max(0, failed_attempt - 1)),
    )
    if exponential > 0 and policy.jitter_ratio > 0:
        exponential *= 1.0 + random.uniform(
            -policy.jitter_ratio,
            policy.jitter_ratio,
        )
    retry_after = _retry_after_seconds(headers)
    if retry_after is not None:
        exponential = max(exponential, retry_after)
    return max(0.0, min(exponential, policy.max_delay_sec))


def _headers_dict(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {str(key): str(value) for key, value in headers.items()}
    except Exception:
        return {}


def _retryable_exception(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            OSError,
        ),
    )


def request_with_backoff(
    request_factory: Callable[[], urllib.request.Request],
    *,
    policy: RetryPolicy,
    validate_response: Optional[
        Callable[[int, Mapping[str, str], bytes], Optional[str]]
    ] = None,
    on_attempt: Optional[Callable[[dict[str, Json]], None]] = None,
) -> HttpResponse:
    started = time.monotonic()
    last_error: Optional[RetryRequestError] = None

    for attempt in range(1, policy.max_attempts + 1):
        elapsed = time.monotonic() - started
        remaining = policy.total_timeout_sec - elapsed
        if remaining <= 0:
            break
        request_timeout = max(
            1.0,
            min(policy.request_timeout_sec, remaining),
        )
        attempt_started = time.monotonic()
        status: Optional[int] = None
        headers: dict[str, str] = {}
        body = b""
        error_text = ""
        retryable = False

        try:
            with urllib.request.urlopen(
                request_factory(),
                timeout=request_timeout,
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                headers = _headers_dict(getattr(response, "headers", None))
                body = response.read()
            validation_error = (
                validate_response(status, headers, body)
                if validate_response is not None
                else None
            )
            if not validation_error:
                if on_attempt is not None:
                    on_attempt(
                        {
                            "attempt": attempt,
                            "maxAttempts": policy.max_attempts,
                            "status": status,
                            "durationMs": int(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                            "willRetry": False,
                            "responseBytes": len(body),
                        }
                    )
                return HttpResponse(
                    status=status,
                    headers=headers,
                    body=body,
                    attempts=attempt,
                )
            error_text = validation_error
            retryable = True
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = _headers_dict(exc.headers)
            try:
                body = exc.read()
            except Exception:
                body = b""
            error_text = f"HTTP {status}"
            retryable = status in RETRYABLE_HTTP_STATUSES
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            retryable = _retryable_exception(exc)

        last_error = RetryRequestError(
            error_text or "API request failed",
            status=status,
            headers=headers,
            body=body,
            attempts=attempt,
            retryable=retryable,
        )
        elapsed = time.monotonic() - started
        can_retry = (
            retryable
            and attempt < policy.max_attempts
            and elapsed < policy.total_timeout_sec
        )
        delay = (
            _backoff_delay(
                policy,
                failed_attempt=attempt,
                headers=headers,
            )
            if can_retry
            else 0.0
        )
        remaining = max(0.0, policy.total_timeout_sec - elapsed)
        delay = min(delay, remaining)
        if on_attempt is not None:
            on_attempt(
                {
                    "attempt": attempt,
                    "maxAttempts": policy.max_attempts,
                    "status": status,
                    "error": error_text,
                    "retryable": retryable,
                    "willRetry": can_retry and delay <= remaining,
                    "delaySec": round(delay, 3),
                    "durationMs": int(
                        (time.monotonic() - attempt_started) * 1000
                    ),
                    "responseBytes": len(body),
                }
            )
        if not can_retry:
            raise last_error
        if delay > 0:
            time.sleep(delay)

    if last_error is not None:
        raise last_error
    raise RetryRequestError(
        "API retry timeout exhausted before an attempt could start",
        attempts=0,
        retryable=True,
    )
