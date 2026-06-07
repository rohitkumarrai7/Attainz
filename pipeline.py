#!/usr/bin/env python3
"""Outreach Engine — automated cold-outreach pipeline CLI."""

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import PipelineOrchestrator  # noqa: E402
from core.validation import run_validation  # noqa: E402
from models.schemas import PipelineStats, RunMode  # noqa: E402
from storage.database import Database  # noqa: E402
from utils.config import get_settings  # noqa: E402

app = typer.Typer(
    name="outreach-engine",
    help="Automated cold-outreach pipeline: Ocean.io → Prospeo → Prospeo Enrich → Brevo",
    add_completion=False,
)
console = Console()


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


def _render_email_previews(contacts: list, brevo_stage, limit: int = 3) -> None:
    if not contacts:
        return
    table = Table(title="Email Preview (first 3)", show_header=True, header_style="bold yellow")
    table.add_column("To")
    table.add_column("Subject")
    table.add_column("Body Preview", max_width=60)

    for i, contact in enumerate(contacts[:limit]):
        subject, body = brevo_stage.render_preview(contact, i)
        preview = body[:120].replace("\n", " ").replace("<", " ").replace(">", " ")
        preview = preview[:120] + ("..." if len(body) > 120 else "")
        table.add_row(contact.email or "—", subject, preview)

    console.print(table)


def _run_with_progress(orchestrator: PipelineOrchestrator, domain: str, mode: RunMode, confirm_send: bool):
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Stage 1: Ocean.io lookalike search...", total=4)

        def on_progress(payload: dict) -> None:
            label = payload.get("stage_label", "")
            if label:
                progress.update(task, description=label)
            stage = payload.get("stage", 0)
            if stage > 0:
                progress.update(task, completed=stage)

        run_id, stats, enriched = orchestrator.run_pipeline(
            domain, mode, confirm_send=confirm_send, on_progress=on_progress
        )
        progress.update(task, completed=4)

    return run_id, stats, enriched


@app.command("validate")
def validate() -> None:
    """Validate environment variables and API connectivity."""
    settings = get_settings()
    console.print(Panel.fit("[bold]Outreach Engine — Validation[/bold]", border_style="cyan"))

    result = run_validation(settings)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Details")

    for check in result["checks"]:
        ok = check["ok"]
        status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        table.add_row(check["service"], status, check["detail"])
        if check.get("smtp_warning"):
            console.print("[yellow]SMTP-style key detected. Testing REST API compatibility...[/yellow]")

    console.print(table)

    config = result["config"]
    config_table = Table(title="Configuration", show_header=True)
    config_table.add_column("Setting")
    config_table.add_column("Value")
    config_table.add_row("MAX_COMPANIES", str(config["max_companies"]))
    config_table.add_row("MAX_CONTACTS_PER_COMPANY", str(config["max_contacts_per_company"]))
    config_table.add_row("SENDER_NAME", config["sender_name"] or "(not set)")
    config_table.add_row("DATABASE", str(settings.database_path))
    console.print(config_table)

    if result["all_ok"]:
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
    run_id, stats, enriched = _run_with_progress(orchestrator, domain, RunMode.DRY_RUN, False)
    _render_dashboard(stats, dry_run=True)
    _render_email_previews(enriched, orchestrator.stages[3])

    console.print(
        f"\n[bold yellow]Would send {stats.would_send or stats.emails_ready_to_send} emails.[/bold yellow]"
    )
    console.print(f"Run ID: [cyan]{run_id}[/cyan]")
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
    run_id, stats, enriched = _run_with_progress(
        orchestrator, domain, RunMode.RUN, confirm_send=confirm_send
    )
    _render_dashboard(stats)
    _render_email_previews(enriched, orchestrator.stages[3])

    ready = stats.emails_ready_to_send
    if not confirm_send and ready > 0:
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
        console.print(f"[yellow]Run ID {run_id} — stopped before sending. Use --confirm-send to deliver.[/yellow]")
        raise typer.Exit(0)

    if confirm_send:
        console.print(f"\n[green]Sent {stats.emails_sent} emails.[/green]")
    console.print(f"CSV exports written to [cyan]{orchestrator.settings.output_dir}[/cyan]")


@app.command("report")
def report(
    run_id: int = typer.Option(..., "--run-id", help="Pipeline run ID to report on"),
) -> None:
    """Show business metrics for a completed pipeline run."""
    settings = get_settings()
    db = Database(settings.database_path)
    data = db.get_run_report(run_id)

    if not data:
        console.print(f"[red]Run ID {run_id} not found.[/red]")
        recent = db.list_runs(5)
        if recent:
            console.print("\nRecent runs:")
            for r in recent:
                console.print(f"  Run {r['id']}: {r['seed_domain']} ({r['mode']}) — {r['started_at']}")
        raise typer.Exit(1)

    table = Table(title=f"Pipeline Report — Run #{run_id}", header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")

    table.add_row("Seed Domain", data["seed_domain"])
    table.add_row("Mode", data["mode"])
    table.add_row("Started", data["started_at"] or "—")
    table.add_row("Finished", data["finished_at"] or "—")
    table.add_row("Companies Discovered", str(data["companies_discovered"]))
    table.add_row("Contacts Enriched", str(data["contacts_enriched"]))
    table.add_row("Emails Resolved", str(data["emails_resolved"]))
    table.add_row("Emails Sent", str(data["emails_sent"]))
    table.add_row("Emails Failed", str(data["emails_failed"]))
    table.add_row("Deliverability Rate", f"{data['deliverability_rate']}%")
    table.add_row("Est. Cost Per Lead", f"${data['estimated_cost_per_lead']}")

    console.print(table)

    variants = data.get("subject_variants") or []
    if variants:
        variant_table = Table(title="A/B Subject Line Distribution", header_style="bold yellow")
        variant_table.add_column("Subject")
        variant_table.add_column("Sent", justify="right")
        for v in variants:
            variant_table.add_row(v["subject"][:60], str(v["count"]))
        console.print(variant_table)


if __name__ == "__main__":
    app()
