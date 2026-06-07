import csv
from pathlib import Path

from storage.database import Database


def export_csvs(db: Database, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    exports = {
        "companies.csv": (
            ["domain", "name", "company_size", "country", "industries"],
            db.get_all_companies(),
            lambda row: {
                **row,
                "industries": row.get("industries", "[]"),
            },
        ),
        "contacts.csv": (
            [
                "person_id",
                "linkedin_url",
                "first_name",
                "last_name",
                "full_name",
                "job_title",
                "company_domain",
                "company_name",
            ],
            db.get_all_contacts(),
            lambda row: row,
        ),
        "emails.csv": (
            [
                "email",
                "provider",
                "verified",
                "full_name",
                "job_title",
                "company_domain",
                "linkedin_url",
            ],
            db.get_all_emails(),
            lambda row: row,
        ),
        "sent_emails.csv": (
            ["email", "message_id", "subject", "status", "sent_at"],
            db.get_all_sent_emails(),
            lambda row: row,
        ),
    }

    for filename, (fieldnames, rows, transform) in exports.items():
        path = output_dir / filename
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(transform(row))
        paths[filename] = path

    return paths
