from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RunMode(str, Enum):
    VALIDATE = "validate"
    DRY_RUN = "dry_run"
    RUN = "run"


class SeedDomain(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        domain = value.strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://")
        domain = domain.removeprefix("www.")
        return domain.split("/")[0]


class Company(BaseModel):
    domain: str
    name: str | None = None
    company_size: str | None = None
    country: str | None = None
    industries: list[str] = Field(default_factory=list)


class Contact(BaseModel):
    person_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    linkedin_url: str
    company_domain: str
    company_name: str | None = None


class EnrichedContact(Contact):
    email: str | None = None
    email_status: str = "unresolved"
    provider: str = "unknown"


class SendResult(BaseModel):
    email: str
    contact_name: str | None = None
    company_domain: str
    subject: str
    message_id: str | None = None
    status: str
    error: str | None = None


class PipelineStats(BaseModel):
    seed_domain: str = ""
    companies_found: int = 0
    decision_makers_found: int = 0
    emails_resolved: int = 0
    emails_ready_to_send: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    would_send: int = 0


class PipelineContext(BaseModel):
    run_id: int
    seed: SeedDomain
    mode: RunMode
    confirm_send: bool = False
    stats: PipelineStats = Field(default_factory=PipelineStats)

    class Config:
        arbitrary_types_allowed = True


class StageResult(BaseModel):
    stage_name: str
    success: bool = True
    items_processed: int = 0
    items_failed: int = 0
    data: Any = None
    errors: list[str] = Field(default_factory=list)


class PipelineStage(ABC):
    name: str

    @abstractmethod
    def run(self, input_data: Any, context: PipelineContext) -> Any:
        raise NotImplementedError
