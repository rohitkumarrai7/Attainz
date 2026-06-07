"""In-memory job state for live UI progress tracking."""

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobState:
    run_id: int | None = None
    status: str = "idle"
    stage: int = 0
    stage_name: str = ""
    stage_label: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    enriched: list = field(default_factory=list)
    awaiting_confirmation: bool = False


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str) -> JobState:
        with self._lock:
            state = JobState()
            self._jobs[job_id] = state
            return state

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in kwargs.items():
                setattr(job, key, value)

    def to_dict(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if not job:
            return None
        return {
            "job_id": job_id,
            "run_id": job.run_id,
            "status": job.status,
            "stage": job.stage,
            "stage_name": job.stage_name,
            "stage_label": job.stage_label,
            "stats": job.stats,
            "error": job.error,
            "awaiting_confirmation": job.awaiting_confirmation,
        }


job_store = JobStore()
