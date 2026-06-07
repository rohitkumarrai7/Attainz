import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _parse_float_header(headers: httpx.Headers, *names: str) -> float | None:
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def compute_retry_delay(
    response: httpx.Response | None,
    attempt: int,
    *,
    base_delay: float = 1.0,
    max_delay: float = 120.0,
) -> float:
    if response is not None:
        headers = response.headers

        retry_after = _parse_float_header(headers, "Retry-After", "retry-after")
        if retry_after is not None:
            return min(max(retry_after, 0.0), max_delay)

        minute_reset = _parse_float_header(
            headers,
            "x-minute-reset-seconds",
            "X-Minute-Reset-Seconds",
        )
        if minute_reset is not None:
            return min(max(minute_reset, 0.0), max_delay)

        sib_reset = _parse_float_header(
            headers,
            "x-sib-ratelimit-reset",
            "X-Sib-Ratelimit-Reset",
        )
        if sib_reset is not None:
            return min(max(sib_reset, 0.0), max_delay)

        daily_reset = _parse_float_header(
            headers,
            "dailyLimitRateSecondsToReset",
            "X-RateLimit-Reset",
        )
        if daily_reset is not None:
            return min(max(daily_reset, 0.0), max_delay)

    exponential = base_delay * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.5)
    return min(exponential + jitter, max_delay)


class RateLimiter:
    def __init__(self) -> None:
        self._next_allowed_at = 0.0

    def throttle_from_response(self, response: httpx.Response) -> None:
        remaining = _parse_float_header(
            response.headers,
            "x-minute-request-left",
            "X-Minute-Request-Left",
            "x-sib-ratelimit-remaining",
            "X-Sib-Ratelimit-Remaining",
            "dailyLimitRateLeft",
        )
        if remaining is not None and remaining <= 1:
            delay = compute_retry_delay(response, attempt=1)
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + delay)

    def wait_if_needed(self) -> None:
        now = time.monotonic()
        if now < self._next_allowed_at:
            time.sleep(self._next_allowed_at - now)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    stage: str,
    logger: Any,
    jsonl_logger: Any,
    rate_limiter: RateLimiter | None = None,
    max_attempts: int = 5,
    **kwargs: Any,
) -> httpx.Response:
    last_response: httpx.Response | None = None
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        if rate_limiter:
            rate_limiter.wait_if_needed()

        start = time.perf_counter()
        try:
            response = client.request(method, url, **kwargs)
            duration_ms = (time.perf_counter() - start) * 1000

            response_body: Any
            try:
                response_body = response.json()
            except Exception:
                response_body = response.text[:2000]

            jsonl_logger.log_request(
                stage=stage,
                method=method,
                url=url,
                status_code=response.status_code,
                duration_ms=duration_ms,
                request_headers=dict(kwargs.get("headers") or {}),
                request_body=kwargs.get("json") or kwargs.get("data"),
                response_headers=dict(response.headers),
                response_body=response_body,
                extra={"attempt": attempt},
            )

            if response.status_code not in RETRYABLE_STATUS:
                if rate_limiter:
                    rate_limiter.throttle_from_response(response)
                return response

            last_response = response
            if rate_limiter:
                rate_limiter.throttle_from_response(response)

            if attempt < max_attempts:
                delay = compute_retry_delay(response, attempt)
                logger.warning(
                    "%s %s returned %s — retrying in %.1fs (attempt %d/%d)",
                    method,
                    url,
                    response.status_code,
                    delay,
                    attempt,
                    max_attempts,
                )
                time.sleep(delay)

        except httpx.RequestError as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            last_error = exc
            jsonl_logger.log_request(
                stage=stage,
                method=method,
                url=url,
                status_code=None,
                duration_ms=duration_ms,
                request_headers=dict(kwargs.get("headers") or {}),
                request_body=kwargs.get("json") or kwargs.get("data"),
                error=str(exc),
                extra={"attempt": attempt},
            )
            if attempt < max_attempts:
                delay = compute_retry_delay(None, attempt)
                logger.warning(
                    "%s %s failed: %s — retrying in %.1fs (attempt %d/%d)",
                    method,
                    url,
                    exc,
                    delay,
                    attempt,
                    max_attempts,
                )
                time.sleep(delay)

    if last_response is not None:
        last_response.raise_for_status()
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Request failed after {max_attempts} attempts: {method} {url}")
