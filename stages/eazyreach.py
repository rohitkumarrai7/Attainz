import logging
from abc import ABC, abstractmethod
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


class EmailEnrichmentProvider(ABC):
    name: str

    @abstractmethod
    def resolve_batch(self, contacts: list[Contact]) -> list[EnrichedContact]:
        raise NotImplementedError


class EazyReachProvider(EmailEnrichmentProvider):
    name = "eazyreach"

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
            "Authorization": f"Bearer {self.settings.eazyreach_api_key}",
            "Content-Type": "application/json",
        }

        with create_http_client() as client:
            for contact in contacts:
                try:
                    email = self._resolve_one(client, headers, contact)
                    enriched.append(
                        self._to_enriched(contact, email, "eazyreach", "verified" if email else "unresolved")
                    )
                except Exception as exc:
                    logger.warning(
                        "EazyReach failed for %s: %s",
                        contact.linkedin_url,
                        exc,
                    )
                    enriched.append(
                        self._to_enriched(contact, None, "eazyreach", "failed")
                    )
        return enriched

    def _resolve_one(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        contact: Contact,
    ) -> str | None:
        payload = {"linkedin_url": contact.linkedin_url}
        response = request_with_retry(
            client,
            "POST",
            f"{self.settings.eazyreach_base_url.rstrip('/')}/v1/enrich/linkedin",
            stage="eazyreach",
            logger=logger,
            jsonl_logger=self.jsonl_logger,
            rate_limiter=self.rate_limiter,
            max_attempts=self.settings.retry_max_attempts,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        email = (
            data.get("email")
            or data.get("work_email")
            or (data.get("data") or {}).get("email")
        )
        if email and data.get("verified", True):
            return str(email).lower()
        return None

    def _to_enriched(
        self,
        contact: Contact,
        email: str | None,
        provider: str,
        status: str,
    ) -> EnrichedContact:
        return EnrichedContact(
            **contact.model_dump(),
            email=email,
            email_status=status,
            provider=provider,
        )


class ProspeoEnrichProvider(EmailEnrichmentProvider):
    name = "prospeo_fallback"

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
                        email = self._enrich_one(client, headers, contact)
                        enriched.append(
                            self._to_enriched(
                                contact,
                                email,
                                "prospeo_fallback",
                                "verified" if email else "unresolved",
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "Prospeo enrich failed for %s: %s",
                            contact.linkedin_url,
                            exc,
                        )
                        enriched.append(
                            self._to_enriched(contact, None, "prospeo_fallback", "failed")
                        )
        return enriched

    def _enrich_one(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        contact: Contact,
    ) -> str | None:
        payload = {
            "only_verified_email": True,
            "data": {"linkedin_url": contact.linkedin_url},
        }
        response = request_with_retry(
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
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            return None

        person = data.get("person") or data.get("data") or {}
        if not isinstance(person, dict):
            return None

        email_obj = person.get("email")
        if isinstance(email_obj, dict):
            status = str(email_obj.get("status", "")).upper()
            address = email_obj.get("email") or email_obj.get("address")
            if address and status in ("VERIFIED", "VALID", "DELIVERABLE", ""):
                return str(address).lower()
            return None

        if isinstance(email_obj, str) and "@" in email_obj:
            return email_obj.lower()

        return None

    def _to_enriched(
        self,
        contact: Contact,
        email: str | None,
        provider: str,
        status: str,
    ) -> EnrichedContact:
        return EnrichedContact(
            **contact.model_dump(),
            email=email,
            email_status=status,
            provider=provider,
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
        self.provider = self._select_provider()

    def _select_provider(self) -> EmailEnrichmentProvider:
        if self.settings.has_eazyreach:
            return EazyReachProvider(self.settings, self.jsonl_logger)
        logger.info("EAZYREACH_API_KEY not set — using Prospeo enrich-person fallback")
        return ProspeoEnrichProvider(self.settings, self.jsonl_logger)

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

        enriched = self.provider.resolve_batch(contacts)

        resolved: list[EnrichedContact] = []
        errors: list[str] = []
        for contact in enriched:
            if not contact.email:
                continue
            if not self.db.email_exists(contact.email):
                self.db.save_email(contact, context.run_id)
            resolved.append(contact)

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
            errors=errors,
        )
