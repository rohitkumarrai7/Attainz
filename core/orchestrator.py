"""Pipeline orchestrator — shared by CLI and web UI."""

from collections.abc import Callable
from typing import Any

from models.schemas import PipelineContext, PipelineStats, RunMode, SeedDomain
from stages.brevo import BrevoStage
from stages.eazyreach import EmailResolutionStage
from stages.ocean import OceanStage
from stages.prospeo import ProspeoStage
from storage.database import Database
from utils.config import Settings, get_settings
from utils.export import export_csvs
from utils.logger import JsonlRequestLogger, setup_logging

ProgressCallback = Callable[[dict[str, Any]], None]

STAGES = [
    ("ocean", "Stage 1: Ocean.io lookalike search"),
    ("prospeo", "Stage 2: Prospeo decision-makers"),
    ("email_resolution", "Stage 3: Prospeo email enrichment"),
    ("brevo", "Stage 4: Brevo outreach"),
]


class PipelineOrchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.logger = setup_logging(self.settings.log_level)
        self.jsonl_logger = JsonlRequestLogger(self.settings.log_dir)
        self.db = Database(self.settings.database_path)
        self.stages = [
            OceanStage(self.settings, self.db, self.jsonl_logger),
            ProspeoStage(self.settings, self.db, self.jsonl_logger),
            EmailResolutionStage(self.settings, self.db, self.jsonl_logger),
            BrevoStage(self.settings, self.db, self.jsonl_logger),
        ]

    def _emit(self, callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
        if callback:
            callback(payload)

    def run_pipeline(
        self,
        domain: str,
        mode: RunMode,
        confirm_send: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[int, PipelineStats, list]:
        seed = SeedDomain(domain=domain)
        run_id = self.db.create_run(seed.domain, mode)
        stats = PipelineStats(seed_domain=seed.domain)
        context = PipelineContext(
            run_id=run_id,
            seed=seed,
            mode=mode,
            confirm_send=confirm_send,
            stats=stats,
        )

        ocean_stage, prospeo_stage, email_stage, brevo_stage = self.stages
        enriched: list = []

        self._emit(on_progress, {
            "run_id": run_id,
            "status": "running",
            "stage": 0,
            "stage_name": STAGES[0][0],
            "stage_label": STAGES[0][1],
            "stats": stats.model_dump(),
        })

        companies = ocean_stage.run(seed, context)
        self._emit(on_progress, {
            "run_id": run_id,
            "status": "running",
            "stage": 1,
            "stage_name": STAGES[1][0],
            "stage_label": STAGES[1][1],
            "stats": stats.model_dump(),
        })

        contacts = prospeo_stage.run(companies, context)
        self._emit(on_progress, {
            "run_id": run_id,
            "status": "running",
            "stage": 2,
            "stage_name": STAGES[2][0],
            "stage_label": STAGES[2][1],
            "stats": stats.model_dump(),
        })

        enriched = email_stage.run(contacts, context)
        self._emit(on_progress, {
            "run_id": run_id,
            "status": "running",
            "stage": 3,
            "stage_name": STAGES[3][0],
            "stage_label": STAGES[3][1],
            "stats": stats.model_dump(),
        })

        brevo_stage.run(enriched, context)

        awaiting = (
            mode == RunMode.RUN
            and not confirm_send
            and stats.emails_ready_to_send > 0
        )
        final_status = "awaiting_confirmation" if awaiting else "completed"

        self.db.finish_run(run_id, stats.model_dump())
        export_csvs(self.db, self.settings.output_dir)

        self._emit(on_progress, {
            "run_id": run_id,
            "status": final_status,
            "stage": 4,
            "stage_name": "done",
            "stage_label": "Pipeline complete",
            "stats": stats.model_dump(),
            "awaiting_confirmation": awaiting,
        })

        return run_id, stats, enriched

    def confirm_send(self, run_id: int, on_progress: ProgressCallback | None = None) -> PipelineStats:
        run = self.db.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        contacts = self.db.get_enriched_contacts_for_linkedin_urls(
            [c["linkedin_url"] for c in self.db.get_all_contacts()]
        )
        if not contacts:
            raise ValueError("No enriched contacts to send")

        stats = PipelineStats(seed_domain=run["seed_domain"])
        if run.get("stats"):
            stats = PipelineStats(**run["stats"])

        context = PipelineContext(
            run_id=run_id,
            seed=SeedDomain(domain=run["seed_domain"]),
            mode=RunMode.RUN,
            confirm_send=True,
            stats=stats,
        )

        self._emit(on_progress, {
            "run_id": run_id,
            "status": "sending",
            "stage": 3,
            "stage_name": "brevo",
            "stage_label": "Sending emails...",
            "stats": stats.model_dump(),
        })

        self.stages[3].run(contacts, context)
        self.db.finish_run(run_id, stats.model_dump())
        export_csvs(self.db, self.settings.output_dir)

        self._emit(on_progress, {
            "run_id": run_id,
            "status": "completed",
            "stage": 4,
            "stage_name": "done",
            "stage_label": "Emails sent",
            "stats": stats.model_dump(),
        })

        return stats

    def get_email_previews(self, enriched: list, limit: int = 5) -> list[dict]:
        brevo = self.stages[3]
        previews = []
        for i, contact in enumerate(enriched[:limit]):
            subject, body = brevo.render_preview(contact, i)
            previews.append({
                "to": contact.email,
                "name": contact.full_name or contact.first_name,
                "company": contact.company_name or contact.company_domain,
                "subject": subject,
                "body_html": body,
            })
        return previews
