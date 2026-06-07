import logging
from typing import Any

import httpx

from models.schemas import Company, PipelineContext, PipelineStage, SeedDomain, StageResult
from storage.database import Database
from utils.config import Settings
from utils.http_client import create_http_client
from utils.logger import JsonlRequestLogger
from utils.retry import RateLimiter, request_with_retry

logger = logging.getLogger("outreach_engine.ocean")

OCEAN_BASE = "https://api.ocean.io"


class OceanStage(PipelineStage):
    name = "ocean"

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

    def run(self, input_data: SeedDomain, context: PipelineContext) -> list[Company]:
        result = self._execute(input_data, context)
        if not result.success:
            raise RuntimeError(f"Ocean stage failed: {'; '.join(result.errors)}")
        return result.data or []

    def _execute(self, seed: SeedDomain, context: PipelineContext) -> StageResult:
        errors: list[str] = []
        companies: list[Company] = []

        headers = {
            "X-Api-Token": self.settings.ocean_io_api_key,
            "Content-Type": "application/json",
        }

        with create_http_client() as client:
            try:
                self._warmup(client, headers, seed.domain)
            except Exception as exc:
                logger.warning("Ocean warmup failed for %s: %s", seed.domain, exc)
                errors.append(f"warmup: {exc}")

            search_after: list[str] | None = None
            remaining = self.settings.max_companies

            while remaining > 0:
                page_size = min(remaining, 50)
                payload: dict[str, Any] = {
                    "size": page_size,
                    "companiesFilters": {
                        "lookalikeDomains": [seed.domain],
                        "excludeDomains": [seed.domain],
                        "companyMatchingMode": "precise",
                    },
                }
                if search_after:
                    payload["searchAfter"] = search_after

                try:
                    response = request_with_retry(
                        client,
                        "POST",
                        f"{OCEAN_BASE}/v3/search/companies",
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
                except Exception as exc:
                    errors.append(str(exc))
                    break

                batch = self._parse_companies(data)
                for company in batch:
                    if self.db.company_exists(company.domain):
                        continue
                    companies.append(company)
                    remaining -= 1
                    if remaining <= 0:
                        break

                search_after = data.get("searchAfter")
                if not search_after or not batch:
                    break

        self.db.save_companies(companies, context.run_id)
        context.stats.companies_found = len(companies)

        return StageResult(
            stage_name=self.name,
            success=True,
            items_processed=len(companies),
            items_failed=len(errors),
            data=companies,
            errors=errors,
        )

    def _warmup(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        domain: str,
    ) -> None:
        request_with_retry(
            client,
            "POST",
            f"{OCEAN_BASE}/v2/warmup/companies",
            stage=self.name,
            logger=logger,
            jsonl_logger=self.jsonl_logger,
            rate_limiter=self.rate_limiter,
            max_attempts=self.settings.retry_max_attempts,
            headers=headers,
            json={"domains": [domain]},
        )

    def _parse_companies(self, data: dict[str, Any]) -> list[Company]:
        companies: list[Company] = []
        for item in data.get("companies", []):
            company_data = item.get("company") if isinstance(item, dict) else None
            if not isinstance(company_data, dict):
                company_data = item if isinstance(item, dict) else {}

            domain = (company_data.get("domain") or "").strip().lower()
            if not domain:
                continue
            companies.append(
                Company(
                    domain=domain,
                    name=company_data.get("name"),
                    company_size=company_data.get("companySize"),
                    country=company_data.get("primaryCountry"),
                    industries=company_data.get("industries") or [],
                )
            )
        return companies
