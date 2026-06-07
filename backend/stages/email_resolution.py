import logging
from typing import Any

import httpx

from models.schemas import Contact, EnrichedContact, PipelineContext, PipelineStage, StageResult
from storage.database import Database
from utils.config import Settings
from utils.http_client import create_http_client
from utils.logger import JsonlRequestLogger
from utils.retry import RateLimiter, request_with_retry

logger = logging.getLogger("outreach_engine.email_resolution")

PROSPEO_BASE = "https://api.prospeo.io"
BULK_BATCH = 50
VERIFIED_STATUSES = {"verified", "valid", "deliverable"}


class ProspeoEnrichProvider:
    """Stage 3: LinkedIn URL → verified work email via Prospeo enrich-person."""

    name = "prospeo"

    def __init__(
        self,
        settings: Settings,
        jsonl_logger: JsonlRequestLogger,
    ) -> None:
        self.settings = settings
        self.jsonl_logger = jsonl_logger
        self.rate_limiter = RateLimiter()

    def resolve_batch(self, contacts: list[Contact]) -> list[EnrichedContact]:
        enriched: list[EnrichedContact] = []
        headers = {
            "X-KEY": self.settings.prospeo_api_key,
            "Content-Type": "application/json",
        }

        with create_http_client() as client:
            for i in range(0, len(contacts), BULK_BATCH):
                batch = contacts[i : i + BULK_BATCH]
                for contact in batch:
                    try:
                        email, status = self._enrich_one(client, headers, contact)
                        enriched.append(
                            self._to_enriched(contact, email, status)
                        )
                    except Exception as exc:
                        logger.warning(
                            "Prospeo enrich failed for %s: %s",
                            contact.linkedin_url,
                            exc,
                        )
                        enriched.append(
                            self._to_enriched(contact, None, "failed")
                        )
        return enriched

    def _enrich_payloads(self, linkedin_url: str) -> list[dict[str, Any]]:
        return [
            {"only_verified_email": True, "data": {"linkedin_url": linkedin_url}},
            {"only_verified_email": True, "linkedin_url": linkedin_url},
        ]

    def _enrich_one(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        contact: Contact,
    ) -> tuple[str | None, str]:
        response: httpx.Response | None = None
        header_sets = [headers]
        if "X-KEY" in headers:
            header_sets.append({
                "X-API-KEY": self.settings.prospeo_api_key,
                "Content-Type": "application/json",
            })

        for hdrs in header_sets:
            for payload in self._enrich_payloads(contact.linkedin_url):
                response = self._enrich_request(client, hdrs, payload)
                if response.status_code == 200:
                    break
                if response.status_code in (401, 403):
                    break
            if response and response.status_code == 200:
                break

        if not response:
            return None, "unresolved"
        if response.status_code in (401, 403):
            response.raise_for_status()
        if response.status_code == 400:
            data = response.json()
            if data.get("error_code") in ("NO_MATCH", "NOT_FOUND"):
                return None, "unresolved"
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            return None, "unresolved"

        person = data.get("person") or data.get("data") or {}
        if not isinstance(person, dict):
            return None, "unresolved"

        email_obj = person.get("email")
        if isinstance(email_obj, dict):
            status = str(email_obj.get("status", "")).lower()
            address = email_obj.get("email") or email_obj.get("address")
            if address and status in VERIFIED_STATUSES | {""}:
                return str(address).lower(), "verified"
            return None, "unresolved"

        if isinstance(email_obj, str) and "@" in email_obj:
            return email_obj.lower(), "verified"

        return None, "unresolved"

    def _enrich_request(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        return request_with_retry(
            client,
            "POST",
            f"{PROSPEO_BASE}/enrich-person",
            stage="prospeo_enrich",
            logger=logger,
            jsonl_logger=self.jsonl_logger,
            rate_limiter=self.rate_limiter,
            max_attempts=self.settings.retry_max_attempts,
            headers=headers,
            json=payload,
        )

    def _to_enriched(
        self,
        contact: Contact,
        email: str | None,
        status: str,
    ) -> EnrichedContact:
        return EnrichedContact(
            **contact.model_dump(),
            email=email,
            email_status=status,
            provider=self.name,
        )


class EmailResolutionStage(PipelineStage):
    name = "email_resolution"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        jsonl_logger: JsonlRequestLogger,
    ) -> None:
        self.settings = settings
        self.db = db
        self.jsonl_logger = jsonl_logger
        self.provider = ProspeoEnrichProvider(settings, jsonl_logger)

    def run(
        self,
        input_data: list[Contact],
        context: PipelineContext,
    ) -> list[EnrichedContact]:
        result = self._execute(input_data, context)
        return result.data or []

    def _execute(
        self,
        contacts: list[Contact],
        context: PipelineContext,
    ) -> StageResult:
        if not contacts:
            return StageResult(stage_name=self.name, data=[], items_processed=0)

        existing_map = {
            c.linkedin_url: c
            for c in self.db.get_enriched_contacts_for_linkedin_urls(
                [c.linkedin_url for c in contacts]
            )
        }
        pending = [c for c in contacts if c.linkedin_url not in existing_map]

        if pending:
            newly_enriched = self.provider.resolve_batch(pending)
        else:
            newly_enriched = []
            logger.info(
                "Resuming: all %d contacts already enriched in DB",
                len(contacts),
            )

        enriched = list(existing_map.values()) + newly_enriched

        for contact in enriched:
            if not contact.email:
                continue
            if contact.email_status not in VERIFIED_STATUSES:
                continue
            if not self.db.email_exists(contact.email):
                self.db.save_email(contact, context.run_id)

        resolved = self.db.get_enriched_contacts_for_linkedin_urls(
            [c.linkedin_url for c in contacts]
        )
        context.stats.emails_resolved = len(resolved)
        context.stats.emails_ready_to_send = len(
            [c for c in resolved if not self.db.sent_email_exists(c.email or "")]
        )

        failed = sum(1 for c in enriched if c.email_status == "failed")
        return StageResult(
            stage_name=self.name,
            success=True,
            items_processed=len(resolved),
            items_failed=failed,
            data=resolved,
            errors=[],
        )
