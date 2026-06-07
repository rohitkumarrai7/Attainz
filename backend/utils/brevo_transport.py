"""Brevo email transport — REST API or SMTP relay depending on key type."""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from utils.config import Settings
from utils.http_client import create_http_client
from utils.retry import RateLimiter, request_with_retry

logger = logging.getLogger("outreach_engine.brevo")

BREVO_BASE = "https://api.brevo.com"
SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587


def is_smtp_key(api_key: str) -> bool:
    return api_key.startswith("xsmtpsib-")


def check_brevo_rest(api_key: str, sender_email: str) -> tuple[bool, str]:
    if not api_key:
        return False, "BREVO_API_KEY not set"

    try:
        with httpx.Client(timeout=15) as client:
            headers = {"api-key": api_key}
            account = client.get(f"{BREVO_BASE}/v3/account", headers=headers)
            if account.status_code != 200:
                return False, f"Account check failed: HTTP {account.status_code}"

            if not sender_email:
                return False, "SENDER_EMAIL not set in .env"

            senders = client.get(f"{BREVO_BASE}/v3/senders", headers=headers)
            if senders.status_code != 200:
                return False, f"Senders check failed: HTTP {senders.status_code}"

            sender_list = senders.json().get("senders", [])
            matched = next(
                (s for s in sender_list if s.get("email", "").lower() == sender_email.lower()),
                None,
            )
            if not matched:
                available = ", ".join(s.get("email", "") for s in sender_list[:5])
                return False, f"Sender {sender_email} not found. Available: {available or 'none'}"

            if matched.get("active", False):
                return True, f"REST API OK — sender {sender_email} verified and active"
            return False, f"Sender {sender_email} exists but is NOT verified yet"
    except Exception as exc:
        return False, str(exc)


def check_brevo_smtp(api_key: str, smtp_login: str, sender_email: str) -> tuple[bool, str]:
    if not api_key:
        return False, "BREVO_API_KEY not set"
    if not sender_email:
        return False, "SENDER_EMAIL not set in .env"
    if not smtp_login:
        return (
            False,
            "BREVO_SMTP_LOGIN not set — copy it from Brevo → SMTP & API → SMTP tab (e.g. 7abcde@smtp-brevo.com)",
        )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_login, api_key)
        return True, f"SMTP relay OK — will send from {sender_email}"
    except smtplib.SMTPAuthenticationError as exc:
        msg = str(exc).lower()
        if "unauthorized ip" in msg or "525" in msg:
            return (
                False,
                "SMTP blocked: unauthorized IP — add your IP in Brevo → SMTP & API → Authorized IPs, or disable IP restriction",
            )
        return (
            False,
            "SMTP auth failed — verify BREVO_SMTP_LOGIN (xxxxx@smtp-brevo.com) and BREVO_API_KEY, or use a REST API key (xkeysib-...)",
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "timed out" in msg or "timeout" in msg:
            return (
                False,
                "SMTP connection timed out — cloud hosts often block port 587. "
                "Use a Brevo REST API key (xkeysib-...) in BREVO_API_KEY on Render instead of the SMTP key.",
            )
        return False, f"SMTP connection failed: {exc}"


def check_brevo(settings: Settings) -> tuple[bool, str, bool]:
    """Returns (ok, detail, is_smtp_key)."""
    api_key = settings.brevo_api_key
    smtp_style = is_smtp_key(api_key)

    if smtp_style:
        ok, detail = check_brevo_smtp(api_key, settings.brevo_smtp_login, settings.sender_email)
        return ok, detail, True

    ok, detail = check_brevo_rest(api_key, settings.sender_email)
    return ok, detail, False


def send_via_rest(
    settings: Settings,
    *,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    rate_limiter: RateLimiter,
    jsonl_logger,
    max_attempts: int,
) -> dict:
    headers = {
        "api-key": settings.brevo_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "sender": {"name": settings.sender_name, "email": settings.sender_email},
        "to": [{"email": to_email, "name": to_name}],
        "subject": subject,
        "htmlContent": html_body,
    }

    with create_http_client() as client:
        response = request_with_retry(
            client,
            "POST",
            f"{BREVO_BASE}/v3/smtp/email",
            stage="brevo",
            logger=logger,
            jsonl_logger=jsonl_logger,
            rate_limiter=rate_limiter,
            max_attempts=max_attempts,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def send_via_smtp(
    settings: Settings,
    *,
    to_email: str,
    subject: str,
    html_body: str,
    max_attempts: int = 5,
) -> dict:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.sender_name} <{settings.sender_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))
    raw = msg.as_string()

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(settings.brevo_smtp_login, settings.brevo_api_key)
                server.sendmail(settings.sender_email, [to_email], raw)
            return {"messageId": None, "transport": "smtp"}
        except (smtplib.SMTPException, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            delay = 2 ** attempt
            logger.warning("SMTP attempt %d/%d failed, retry in %ds: %s", attempt, max_attempts, delay, exc)
            time.sleep(delay)

    raise last_exc or RuntimeError("SMTP send failed")
