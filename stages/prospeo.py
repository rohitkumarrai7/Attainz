import logging
from typing import Any

import httpx

from models.schemas import Company, Contact, PipelineContext, PipelineStage, StageResult
from storage.database import Database
from utils.config import Settings
from utils.http_client import create_http_client
from utils.logger import JsonlRequestLogger
from utils.retry import RateLimiter, request_with_retry

logger = logging.getLogger("outreach_engine.prospeo")

PROSPEO_BASE = "https://api.prospeo.io"
SENIORITY_FILTER = ["C-Suite", "Founder/Owner", "Vice President"]
BATCH_SIZE = 50


class ProspeoStage(PipelineStage):
    name = "prospeo"

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

    def run(self, input_data: list[Company], context: PipelineContext) -> list[Contact]:
        result = self._execute(input_data, context)
        return result.data or []

    def _execute(
        self,
        companies: list[Company],
        context: PipelineContext,
    ) -> StageResult:
        if not companies:
            return StageResult(stage_name=self.name, data=[], items_processed=0)

        headers = {
            "X-KEY": self.settings.prospeo_api_key,
            "Content-Type": "application/json",
        }

        all_contacts: list[Contact] = []
        errors: list[str] = []
        domains = [c.domain for c in companies]

        with create_http_client() as client:
            for i in range(0, len(domains), BATCH_SIZE):
                batch_domains = domains[i : i + BATCH_SIZE]
                try:
                    batch_contacts = self._search_domains(
                        client, headers, batch_domains, context
                    )
                    all_contacts.extend(batch_contacts)
                except Exception as exc:
                    msg = f"batch {batch_domains[:3]}...: {exc}"
                    logger.error("Prospeo search failed: %s", msg)
                    errors.append(msg)

        self.db.save_contacts(all_contacts, context.run_id)
        context.stats.decision_makers_found = len(all_contacts)

        return StageResult(
            stage_name=self.name,
            success=True,
            items_processed=len(all_contacts),
            items_failed=len(errors),
            data=all_contacts,
            errors=errors,
        )

    def _search_domains(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        domains: list[str],
        context: PipelineContext,
    ) -> list[Contact]:
        contacts: list[Contact] = []
        page = 1
        max_pages = 20

        while page <= max_pages:
            payload = {
                "page": page,
                "filters": {
                    "company": {"websites": {"include": domains}},
                    "person_seniority": {"include": SENIORITY_FILTER},
                    "max_person_per_company": self.settings.max_contacts_per_company,
                },
            }

            response = request_with_retry(
                client,
                "POST",
                f"{PROSPEO_BASE}/search-person",
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

            if data.get("error"):
                raise RuntimeError(data.get("message", "Prospeo search error"))

            for item in data.get("results", []):
                contact = self._parse_contact(item)
                if contact and not self.db.contact_exists(contact.linkedin_url):
                    contacts.append(contact)

            pagination = data.get("pagination") or {}
            total_page = pagination.get("total_page", 1)
            if page >= total_page:
                break
            page += 1

        return contacts

    def _parse_contact(self, item: dict[str, Any]) -> Contact | None:
        person = item.get("person") or {}
        company = item.get("company") or {}
        linkedin_url = (person.get("linkedin_url") or "").strip()
        if not linkedin_url:
            return None

        company_domain = (company.get("website") or "").strip().lower()
        if not company_domain:
            return None

        return Contact(
            person_id=person.get("person_id"),
            first_name=person.get("first_name"),
            last_name=person.get("last_name"),
            full_name=person.get("full_name"),
            job_title=person.get("current_job_title") or person.get("headline"),
            linkedin_url=linkedin_url,
            company_domain=company_domain,
            company_name=company.get("name"),
        )
