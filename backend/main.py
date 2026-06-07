"""FastAPI backend — serves API + frontend static assets."""

import sys
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.orchestrator import PipelineOrchestrator  # noqa: E402
from core.validation import run_validation  # noqa: E402
from job_store import job_store  # noqa: E402
from models.schemas import RunMode  # noqa: E402
from storage.database import Database  # noqa: E402
from utils.config import get_settings  # noqa: E402

app = FastAPI(
    title="Outreach Engine",
    description="Automated cold-outreach pipeline API",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    domain: str = Field(..., examples=["stripe.com"])
    mode: str = Field("dry_run", pattern="^(dry_run|run)$")
    confirm_send: bool = False


def _run_pipeline_task(job_id: str, domain: str, mode: RunMode, confirm_send: bool) -> None:
    orchestrator = PipelineOrchestrator()

    def on_progress(payload: dict) -> None:
        job_store.update(
            job_id,
            run_id=payload.get("run_id"),
            status=payload.get("status", "running"),
            stage=payload.get("stage", 0),
            stage_name=payload.get("stage_name", ""),
            stage_label=payload.get("stage_label", ""),
            stats=payload.get("stats", {}),
            awaiting_confirmation=payload.get("awaiting_confirmation", False),
        )

    try:
        job_store.update(job_id, status="running")
        run_id, stats, enriched = orchestrator.run_pipeline(
            domain, mode, confirm_send=confirm_send, on_progress=on_progress
        )
        job_store.update(
            job_id,
            run_id=run_id,
            status="awaiting_confirmation" if (
                mode == RunMode.RUN and not confirm_send and stats.emails_ready_to_send > 0
            ) else "completed",
            stats=stats.model_dump(),
            enriched=enriched,
            awaiting_confirmation=(
                mode == RunMode.RUN and not confirm_send and stats.emails_ready_to_send > 0
            ),
        )
    except Exception as exc:
        job_store.update(job_id, status="failed", error=str(exc))


def _confirm_send_task(job_id: str, run_id: int) -> None:
    orchestrator = PipelineOrchestrator()

    def on_progress(payload: dict) -> None:
        job_store.update(
            job_id,
            status=payload.get("status", "sending"),
            stage=payload.get("stage", 3),
            stage_label=payload.get("stage_label", ""),
            stats=payload.get("stats", {}),
        )

    try:
        job_store.update(job_id, status="sending", run_id=run_id)
        stats = orchestrator.confirm_send(run_id, on_progress=on_progress)
        job_store.update(job_id, status="completed", stats=stats.model_dump())
    except Exception as exc:
        job_store.update(job_id, status="failed", error=str(exc))


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "outreach-engine"}


@app.get("/api/validate")
async def validate():
    return run_validation(get_settings())


@app.get("/api/runs")
async def list_runs(limit: int = 20):
    db = Database(get_settings().database_path)
    return {"runs": db.list_runs(limit)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    db = Database(get_settings().database_path)
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {"run": run, "report": db.get_run_report(run_id)}


@app.get("/api/runs/{run_id}/data")
async def get_run_data(run_id: int):
    db = Database(get_settings().database_path)
    if not db.get_run(run_id):
        raise HTTPException(404, "Run not found")
    return db.get_display_data_for_run(run_id)


@app.get("/api/runs/{run_id}/previews")
async def get_previews(run_id: int, limit: int = 100):
    db = Database(get_settings().database_path)
    if not db.get_run(run_id):
        raise HTTPException(404, "Run not found")
    orchestrator = PipelineOrchestrator()
    enriched = db.get_enriched_contacts_for_run(run_id)
    return {
        "previews": orchestrator.get_email_previews(enriched, limit=limit),
        "total": len(enriched),
    }


@app.post("/api/runs")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    domain = req.domain.strip().lower()
    if not domain:
        raise HTTPException(400, "Domain is required")
    mode = RunMode.DRY_RUN if req.mode == "dry_run" else RunMode.RUN
    job_id = str(uuid.uuid4())
    job_store.create(job_id)
    background_tasks.add_task(_run_pipeline_task, job_id, domain, mode, req.confirm_send)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    state = job_store.to_dict(job_id)
    if not state:
        raise HTTPException(404, "Job not found")
    return state


@app.post("/api/jobs/{job_id}/confirm-send")
async def confirm_send(job_id: str, background_tasks: BackgroundTasks):
    state = job_store.get(job_id)
    if not state or not state.run_id:
        raise HTTPException(404, "Job or run not found")
    background_tasks.add_task(_confirm_send_task, job_id, state.run_id)
    return {"job_id": job_id, "status": "sending"}


@app.post("/api/runs/{run_id}/confirm-send")
async def confirm_send_by_run(run_id: int, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    job_store.create(job_id)
    job_store.update(job_id, run_id=run_id)
    background_tasks.add_task(_confirm_send_task, job_id, run_id)
    return {"job_id": job_id, "status": "sending"}


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
