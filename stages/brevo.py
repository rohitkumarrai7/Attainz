import hashlib
import logging
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from models.schemas import EnrichedContact, PipelineContext, PipelineStage, RunMode, SendResult, StageResult
from storage.database import Database
from utils.config import Settings
from utils.http_client import create_http_client
from utils.logger import JsonlRequestLogger
from utils.retry import RateLimiter, request_with_retry

logger = logging.getLogger("outreach_engine.brevo")

BREVO_BASE = "https://api.brevo.com"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

SUBJECT_VARIANTS = [
    "Quick thought on {{ company_name }}'s outreach stack",
    "{{ first_name }}, idea for {{ company_name }}",
    "For {{ job_title }}s scaling outbound at {{ company_name }}",
]


class BrevoStage(PipelineStage):
    name = "brevo"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        jsonl_logger: JsonlRequestLogger,
    ) -> None:
        self.settings = settings
        self.db = db
        self.jsonl_logger = jsonl_logger
        self.rate_limiter = RateLimiter()
        self.jinja_env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def run(
        self,
        input_data: list[EnrichedContact],
        context: PipelineContext,
    ) -> list[SendResult]:
        result = self._execute(input_data, context)
        return result.data or []

    def render_preview(self, contact: EnrichedContact, index: int = 0) -> tuple[str, str]:
        subject = self._render_subject(contact, index)
        body = self._render_body(contact)
        return subject, body

    def _execute(
        self,
        contacts: list[EnrichedContact],
        context: PipelineContext,
    ) -> StageResult:
        sendable = [
            c
            for c in contacts
            if c.email and not self.db.sent_email_exists(c.email)
        ]
        context.stats.would_send = len(sendable)

        if context.mode == RunMode.DRY_RUN:
            context.stats.emails_ready_to_send = len(sendable)
            return StageResult(
                stage_name=self.name,
                success=True,
                items_processed=len(sendable),
                data=[],
            )

        if not context.confirm_send:
            context.stats.emails_ready_to_send = len(sendable)
            return StageResult(
                stage_name=self.name,
                success=True,
                items_processed=0,
                data=[],
            )

        results: list[SendResult] = []
        errors: list[str] = []
        headers = {
            "api-key": self.settings.brevo_api_key,
            "Content-Type": "application/json",
        }

        with create_http_client() as client:
            for index, contact in enumerate(sendable):
                if not contact.email:
                    continue
                subject = self._render_subject(contact, index)
                html_body = self._render_body_html(contact)

                payload = {
                    "sender": {
                        "name": self.settings.sender_name,
                        "email": self.settings.sender_email,
                    },
                    "to": [
                        {
                            "email": contact.email,
                            "name": contact.full_name or contact.first_name or "",
                        }
                    ],
                    "subject": subject,
                    "htmlContent": html_body,
                }

                try:
                    response = request_with_retry(
                        client,
                        "POST",
                        f"{BREVO_BASE}/v3/smtp/email",
                        stage=self.name,
                        logger=logger,
                        jsonl_logger=self.jsonl_logger,
                        rate_limiter=self.rate_limiter,
                        max_attempts=self.settings.retry_max_attempts,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    result = SendResult(
                        email=contact.email,
                        contact_name=contact.full_name,
                        company_domain=contact.company_domain,
                        subject=subject,
                        message_id=data.get("messageId"),
                        status="sent",
                    )
                    self.db.save_sent_email(result, context.run_id)
                    results.append(result)
                    context.stats.emails_sent += 1
                except Exception as exc:
                    msg = f"{contact.email}: {exc}"
                    logger.error("Brevo send failed: %s", msg)
                    errors.append(msg)
                    results.append(
                        SendResult(
                            email=contact.email or "",
                            contact_name=contact.full_name,
                            company_domain=contact.company_domain,
                            subject=subject,
                            status="failed",
                            error=str(exc),
                        )
                    )
                    context.stats.emails_failed += 1

        context.stats.emails_ready_to_send = len(sendable)
        return StageResult(
            stage_name=self.name,
            success=True,
            items_processed=context.stats.emails_sent,
            items_failed=context.stats.emails_failed,
            data=results,
            errors=errors,
        )

    def _contact_vars(self, contact: EnrichedContact) -> dict[str, str]:
        return {
            "first_name": contact.first_name or (contact.full_name or "there").split()[0],
            "company_name": contact.company_name or contact.company_domain,
            "job_title": contact.job_title or "leader",
            "sender_name": self.settings.sender_name,
        }

    def _subject_index(self, contact: EnrichedContact, index: int) -> int:
        key = contact.email or contact.linkedin_url or str(index)
        digest = int(hashlib.md5(key.encode()).hexdigest(), 16)
        return digest % len(SUBJECT_VARIANTS)

    def _render_subject(self, contact: EnrichedContact, index: int) -> str:
        vars_ = self._contact_vars(contact)
        variant_idx = self._subject_index(contact, index)
        template = self.jinja_env.from_string(SUBJECT_VARIANTS[variant_idx])
        return template.render(**vars_)

    def _render_body(self, contact: EnrichedContact) -> str:
        template = self.jinja_env.get_template("outreach.j2")
        return template.render(**self._contact_vars(contact))

    def _render_body_html(self, contact: EnrichedContact) -> str:
        body = self._render_body(contact)
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        html_parts = []
        for p in paragraphs:
            if p.startswith("Best,"):
                html_parts.append(f"<p>{p.replace(chr(10), '<br>')}</p>")
            else:
                html_parts.append(f"<p>{p}</p>")
        return "\n".join(html_parts)
