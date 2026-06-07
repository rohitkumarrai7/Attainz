import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.config import get_settings

SENSITIVE_HEADERS = {
    "x-api-token",
    "x-key",
    "api-key",
    "authorization",
    "x-api-key",
}


def _redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def _safe_body(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, (dict, list, str, int, float, bool)):
        return body
    return str(body)


class JsonlRequestLogger:
    def __init__(self, log_dir: Path | None = None) -> None:
        settings = get_settings()
        self.log_dir = log_dir or settings.log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.log_path = self.log_dir / f"requests-{date_str}.jsonl"

    def log_request(
        self,
        *,
        stage: str,
        method: str,
        url: str,
        status_code: int | None,
        duration_ms: float,
        request_headers: dict[str, Any] | None = None,
        request_body: Any = None,
        response_headers: dict[str, Any] | None = None,
        response_body: Any = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "method": method,
            "url": url,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "request": {
                "headers": _redact_headers(request_headers or {}),
                "body": _safe_body(request_body),
            },
            "response": {
                "headers": response_headers or {},
                "body": _safe_body(response_body),
            },
            "error": error,
            "extra": extra or {},
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger("outreach_engine")
