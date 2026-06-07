#!/usr/bin/env python3
"""Outreach Engine — automated cold-outreach pipeline CLI."""

import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.schemas import PipelineContext, PipelineStats, RunMode, SeedDomain  # noqa: E402
from stages.brevo import BrevoStage  # noqa: E402
from stages.eazyreach import EmailResolutionStage  # noqa: E402
from stages.ocean import OceanStage  # noqa: E402
from stages.prospeo import ProspeoStage  # noqa: E402
from storage.database import Database  # noqa: E402
from utils.config import get_settings  # noqa: E402
from utils.export import export_csvs  # noqa: E402
from utils.logger import JsonlRequestLogger, setup_logging  # noqa: E402

app = typer.Typer(
    name="outreach-engine",
    help="Automated cold-outreach pipeline: Ocean.io → Prospeo → EazyReach → Brevo",
    add_completion=False,
)
console = Console()


class PipelineOrchestrator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.logger = setup_logging(self.settings.log_level)
        self.jsonl_logger = JsonlRequestLogger(self.settings.log_dir)
        self.db = Database(self.settings.database_path)
        self.stages = [
            OceanStage(self.settings, self.db, self.jsonl_logger),
            ProspeoStage(self.settings, self.db, self.jsonl_logger),
            EmailResolutionStage(self.settings, self.db, self.jsonl_logger),
            BrevoStage(self.settings, self.db, self.jsonl_logger),
        ]

    def run_pipeline(
        self,
        domain: str,
        mode: RunMode,
        confirm_send: bool = False,
    ) -> tuple[PipelineStats, list]:
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

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Stage 1: Ocean.io lookalike search...", total=4)

            companies = ocean_stage.run(seed, context)
            progress.advance(task)

            progress.update(task, description="Stage 2: Prospeo decision-makers...")
            contacts = prospeo_stage.run(companies, context)
            progress.advance(task)

            progress.update(task, description="Stage 3: Email resolution...")
            enriched = email_stage.run(contacts, context)
            progress.advance(task)

            progress.update(task, description="Stage 4: Brevo outreach...")
            brevo_stage.run(enriched, context)
            progress.advance(task)

        self.db.finish_run(run_id, stats.model_dump())
        export_csvs(self.db, self.settings.output_dir)
        return stats, enriched


def _render_dashboard(stats: PipelineStats, *, dry_run: bool = False) -> None:
    table = Table(title="Outreach Engine Dashboard", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")

    table.add_row("Seed Domain", stats.seed_domain)
    table.add_row("Companies Found", str(stats.companies_found))
    table.add_row("Decision Makers Found", str(stats.decision_makers_found))
    table.add_row("Emails Resolved", str(stats.emails_resolved))
    if dry_run:
        table.add_row("Would Send", str(stats.would_send or stats.emails_ready_to_send))
    else:
        table.add_row("Emails Ready To Send", str(stats.emails_ready_to_send))
        table.add_row("Emails Sent", str(stats.emails_sent))
        if stats.emails_failed:
            table.add_row("Emails Failed", str(stats.emails_failed))

    console.print(table)


def _render_email_previews(contacts: list, brevo_stage: BrevoStage, limit: int = 3) -> None:
    if not contacts:
        return
    table = Table(title="Email Preview (first 3)", show_header=True, header_style="bold yellow")
    table.add_column("To")
    table.add_column("Subject")
    table.add_column("Body Preview", max_width=60)

    for i, contact in enumerate(contacts[:limit]):
        subject, body = brevo_stage.render_preview(contact, i)
        preview = body[:120].replace("\n", " ") + ("..." if len(body) > 120 else "")
        table.add_row(contact.email or "—", subject, preview)

    console.print(table)


def _check_ocean(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "OCEAN_IO_API_KEY not set"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                "https://api.ocean.io/v2/credits/balance",
                headers={"X-Api-Token": api_key},
            )
            if r.status_code == 200:
                data = r.json()
                credits = data.get("credits", data.get("balance", "ok"))
                return True, f"Connected (credits: {credits})"
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as exc:
        return False, str(exc)


def _check_prospeo(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "PROSPEO_API_KEY not set"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                "https://api.prospeo.io/search-person",
                headers={"X-KEY": api_key, "Content-Type": "application/json"},
                json={
                    "page": 1,
                    "filters": {
                        "company": {"websites": {"include": ["stripe.com"]}},
                        "person_seniority": {"include": ["C-Suite"]},
                        "max_person_per_company": 1,
                    },
                },
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("error"):
                    return False, data.get("message", "API error")
                return True, "Connected"
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as exc:
        return False, str(exc)


def _check_eazyreach(api_key: str, base_url: str) -> tuple[bool, str]:
    if not api_key:
        return True, "Not configured — Prospeo enrich fallback active"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{base_url.rstrip('/')}/v1/health",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code in (200, 404):
                return True, "API key configured"
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as exc:
        return True, f"Key set (connectivity unverified: {exc})"


def _check_brevo(api_key: str, sender_email: str) -> tuple[bool, str]:
    if not api_key:
        return False, "BREVO_API_KEY not set (use REST API key, not SMTP)"
    if api_key.startswith("xsmtpsib-"):
        return False, "SMTP key detected — create a REST API key in Brevo dashboard"

    try:
        with httpx.Client(timeout=15) as client:
            headers = {"api-key": api_key}
            account = client.get("https://api.brevo.com/v3/account", headers=headers)
            if account.status_code != 200:
                return False, f"Account check failed: HTTP {account.status_code}"

            senders = client.get("https://api.brevo.com/v3/senders", headers=headers)
            if senders.status_code != 200:
                return False, f"Senders check failed: HTTP {senders.status_code}"

            sender_list = senders.json().get("senders", [])
            if not sender_email:
                return False, "SENDER_EMAIL not set in .env"

            matched = next(
                (s for s in sender_list if s.get("email", "").lower() == sender_email.lower()),
                None,
            )
            if not matched:
                available = ", ".join(s.get("email", "") for s in sender_list[:5])
                return False, f"Sender {sender_email} not found. Available: {available or 'none'}"

            active = matched.get("active", False)
            if active:
                return True, f"Sender {sender_email} is verified and active"
            return False, f"Sender {sender_email} exists but is NOT verified yet"
    except Exception as exc:
        return False, str(exc)


@app.command("validate")
def validate() -> None:
    """Validate environment variables and API connectivity."""
    settings = get_settings()
    console.print(Panel.fit("[bold]Outreach Engine — Validation[/bold]", border_style="cyan"))

    checks = [
        ("Ocean.io", *_check_ocean(settings.ocean_io_api_key)),
        ("Prospeo", *_check_prospeo(settings.prospeo_api_key)),
        ("EazyReach", *_check_eazyreach(settings.eazyreach_api_key, settings.eazyreach_base_url)),
        ("Brevo", *_check_brevo(settings.brevo_api_key, settings.sender_email)),
    ]

    table = Table(show_header=True, header_style="bold")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Details")

    all_ok = True
    for name, ok, detail in checks:
        status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        if not ok and name != "EazyReach":
            all_ok = False
        table.add_row(name, status, detail)

    console.print(table)

    config_table = Table(title="Configuration", show_header=True)
    config_table.add_column("Setting")
    config_table.add_column("Value")
    config_table.add_row("MAX_COMPANIES", str(settings.max_companies))
    config_table.add_row("MAX_CONTACTS_PER_COMPANY", str(settings.max_contacts_per_company))
    config_table.add_row("SENDER_NAME", settings.sender_name or "(not set)")
    config_table.add_row("DATABASE", str(settings.database_path))
    console.print(config_table)

    if all_ok:
        console.print("\n[green]All critical services validated.[/green]")
        raise typer.Exit(0)
    console.print("\n[yellow]Fix the issues above before running the pipeline.[/yellow]")
    raise typer.Exit(1)


@app.command("dry-run")
def dry_run(
    domain: str = typer.Argument(..., help="Seed company domain, e.g. stripe.com"),
) -> None:
    """Run all stages except actual email sending."""
    console.print(Panel.fit(f"[bold]Dry Run[/bold] — {domain}", border_style="yellow"))
    orchestrator = PipelineOrchestrator()
    stats, enriched = orchestrator.run_pipeline(domain, RunMode.DRY_RUN)
    _render_dashboard(stats, dry_run=True)
    _render_email_previews(enriched, orchestrator.stages[3])

    console.print(
        f"\n[bold yellow]Would send {stats.would_send or stats.emails_ready_to_send} emails.[/bold yellow]"
    )
    console.print(f"CSV exports written to [cyan]{orchestrator.settings.output_dir}[/cyan]")
    console.print(f"Logs written to [cyan]{orchestrator.settings.log_dir}[/cyan]")


@app.command("run")
def run(
    domain: str = typer.Argument(..., help="Seed company domain, e.g. stripe.com"),
    confirm_send: bool = typer.Option(
        False,
        "--confirm-send",
        help="Explicitly confirm sending emails. Without this flag, NO emails are sent.",
    ),
) -> None:
    """Run the full pipeline with safety checkpoint before sending."""
    orchestrator = PipelineOrchestrator()
    console.print(Panel.fit(f"[bold]Pipeline Run[/bold] — {domain}", border_style="green"))
    stats, enriched = orchestrator.run_pipeline(
        domain, RunMode.RUN, confirm_send=confirm_send
    )
    _render_dashboard(stats)
    _render_email_previews(enriched, orchestrator.stages[3])

    ready = stats.emails_ready_to_send
    if not confirm_send:
        console.print(
            Panel(
                f"Companies: {stats.companies_found}\n"
                f"Contacts: {stats.decision_makers_found}\n"
                f"Emails: {ready}\n\n"
                f"[bold]No emails have been sent.[/bold]\n"
                f"Re-run with [cyan]--confirm-send[/cyan] to deliver.",
                title="Safety Checkpoint",
                border_style="red",
            )
        )
        console.print("[yellow]Stopped before sending. Use --confirm-send to deliver emails.[/yellow]")
        raise typer.Exit(0)

    console.print(f"\n[green]Sent {stats.emails_sent} emails.[/green]")
    console.print(f"CSV exports written to [cyan]{orchestrator.settings.output_dir}[/cyan]")


if __name__ == "__main__":
    app()
