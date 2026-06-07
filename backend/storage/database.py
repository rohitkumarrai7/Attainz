import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models.schemas import Company, Contact, EnrichedContact, RunMode, SendResult
from utils.domain import normalize_domain


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seed_domain TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    stats_json TEXT
                );

                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL UNIQUE,
                    name TEXT,
                    company_size TEXT,
                    country TEXT,
                    industries TEXT,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT,
                    linkedin_url TEXT NOT NULL UNIQUE,
                    first_name TEXT,
                    last_name TEXT,
                    full_name TEXT,
                    job_title TEXT,
                    company_domain TEXT NOT NULL,
                    company_name TEXT,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    contact_id INTEGER,
                    provider TEXT,
                    verified INTEGER DEFAULT 0,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (contact_id) REFERENCES contacts(id),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS sent_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    message_id TEXT,
                    subject TEXT,
                    status TEXT,
                    run_id INTEGER,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
                """
            )

    def create_run(self, seed_domain: str, mode: RunMode) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (seed_domain, mode, started_at) VALUES (?, ?, ?)",
                (seed_domain, mode.value, now),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, stats_json = ? WHERE id = ?",
                (now, json.dumps(stats), run_id),
            )

    def company_exists(self, domain: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM companies WHERE domain = ?",
                (domain.lower(),),
            ).fetchone()
            return row is not None

    def save_companies(self, companies: list[Company], run_id: int) -> list[Company]:
        saved: list[Company] = []
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            for company in companies:
                domain = company.domain.lower()
                if self.company_exists(domain):
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO companies
                    (domain, name, company_size, country, industries, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        domain,
                        company.name,
                        company.company_size,
                        company.country,
                        json.dumps(company.industries),
                        run_id,
                        now,
                    ),
                )
                saved.append(company)
        return saved

    def contact_exists(self, linkedin_url: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM contacts WHERE linkedin_url = ?",
                (linkedin_url,),
            ).fetchone()
            return row is not None

    def save_contacts(self, contacts: list[Contact], run_id: int) -> list[Contact]:
        saved: list[Contact] = []
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            for contact in contacts:
                if self.contact_exists(contact.linkedin_url):
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contacts
                    (person_id, linkedin_url, first_name, last_name, full_name,
                     job_title, company_domain, company_name, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contact.person_id,
                        contact.linkedin_url,
                        contact.first_name,
                        contact.last_name,
                        contact.full_name,
                        contact.job_title,
                        contact.company_domain,
                        contact.company_name,
                        run_id,
                        now,
                    ),
                )
                saved.append(contact)
        return saved

    def email_exists(self, email: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM emails WHERE email = ?",
                (email.lower(),),
            ).fetchone()
            return row is not None

    def sent_email_exists(self, email: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_emails WHERE email = ?",
                (email.lower(),),
            ).fetchone()
            return row is not None

    def get_contact_id(self, linkedin_url: str) -> int | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM contacts WHERE linkedin_url = ?",
                (linkedin_url,),
            ).fetchone()
            return int(row["id"]) if row else None

    def save_email(
        self,
        contact: EnrichedContact,
        run_id: int,
        contact_row_id: int | None = None,
    ) -> bool:
        if not contact.email:
            return False
        email = contact.email.lower()
        if self.email_exists(email):
            return False
        if contact_row_id is None:
            contact_row_id = self.get_contact_id(contact.linkedin_url)
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO emails
                (email, contact_id, provider, verified, run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    contact_row_id,
                    contact.provider,
                    1 if contact.email_status == "verified" else 0,
                    run_id,
                    now,
                ),
            )
        return True

    def save_sent_email(self, result: SendResult, run_id: int) -> bool:
        email = result.email.lower()
        if self.sent_email_exists(email):
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_emails
                (email, message_id, subject, status, run_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    result.message_id,
                    result.subject,
                    result.status,
                    run_id,
                    now,
                ),
            )
        return True

    def get_all_companies(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT domain, name, company_size, country, industries FROM companies ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_all_contacts(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT person_id, linkedin_url, first_name, last_name, full_name,
                       job_title, company_domain, company_name
                FROM contacts ORDER BY id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_all_emails(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT e.email, e.provider, e.verified, c.full_name, c.job_title,
                       c.company_domain, c.linkedin_url
                FROM emails e
                LEFT JOIN contacts c ON c.id = e.contact_id
                ORDER BY e.id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_all_sent_emails(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT email, message_id, subject, status, sent_at FROM sent_emails ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_companies_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT domain, name, company_size, country, industries
                FROM companies WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_contacts_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT person_id, linkedin_url, first_name, last_name, full_name,
                       job_title, company_domain, company_name
                FROM contacts WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_emails_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT e.email, e.provider, e.verified, c.full_name, c.job_title,
                       c.company_domain, c.linkedin_url
                FROM emails e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.run_id = ? ORDER BY e.id
                """,
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_sent_emails_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT s.email, s.message_id, s.subject, s.status, s.sent_at,
                       c.full_name, c.company_domain
                FROM sent_emails s
                LEFT JOIN emails e ON e.email = s.email AND e.run_id = s.run_id
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE s.run_id = ?
                ORDER BY s.id
                """,
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_enriched_contacts_for_run(self, run_id: int) -> list[EnrichedContact]:
        contacts = self.get_contacts_for_run(run_id)
        if not contacts:
            data = self.get_display_data_for_run(run_id)
            contacts = data.get("contacts", [])
        urls = [c["linkedin_url"] for c in contacts if c.get("linkedin_url")]
        return self.get_enriched_contacts_for_linkedin_urls(urls)

    def get_display_data_for_run(self, run_id: int) -> dict[str, Any]:
        """Run-scoped data with fallback when dedup stores records under earlier runs."""
        run = self.get_run(run_id)
        if not run:
            return {}

        companies = self.get_companies_for_run(run_id)
        if not companies:
            companies = [
                {
                    "domain": c.domain,
                    "name": c.name,
                    "company_size": c.company_size,
                    "country": c.country,
                    "industries": json.dumps(c.industries),
                }
                for c in self.get_companies_for_seed(run["seed_domain"], limit=100)
            ]
        if not companies:
            companies = self.get_all_companies()

        domains = [c["domain"] for c in companies if c.get("domain")]
        contacts = self.get_contacts_for_run(run_id)
        if not contacts and domains:
            contacts = [
                {
                    "person_id": c.person_id,
                    "linkedin_url": c.linkedin_url,
                    "first_name": c.first_name,
                    "last_name": c.last_name,
                    "full_name": c.full_name,
                    "job_title": c.job_title,
                    "company_domain": c.company_domain,
                    "company_name": c.company_name,
                }
                for c in self.get_contacts_for_domains(domains)
            ]

        emails = self.get_emails_for_run(run_id)
        if not emails and contacts:
            all_emails = self.get_all_emails()
            contact_urls = {c["linkedin_url"] for c in contacts if c.get("linkedin_url")}
            emails = [e for e in all_emails if e.get("linkedin_url") in contact_urls]

        sent_emails = self.get_sent_emails_for_run(run_id)

        return {
            "run_id": run_id,
            "companies": companies,
            "contacts": contacts,
            "emails": emails,
            "sent_emails": sent_emails,
        }

    def get_companies_for_seed(self, seed_domain: str, limit: int) -> list[Company]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.domain, c.name, c.company_size, c.country, c.industries
                FROM companies c
                JOIN runs r ON c.run_id = r.id
                WHERE r.seed_domain = ?
                ORDER BY c.id DESC
                LIMIT ?
                """,
                (seed_domain.lower(), limit),
            ).fetchall()
        return [
            Company(
                domain=row["domain"],
                name=row["name"],
                company_size=row["company_size"],
                country=row["country"],
                industries=json.loads(row["industries"] or "[]"),
            )
            for row in rows
        ]

    def get_contacts_for_domains(self, domains: list[str]) -> list[Contact]:
        if not domains:
            return []
        normalized = [normalize_domain(d) for d in domains]
        placeholders = ",".join("?" * len(normalized))
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT person_id, linkedin_url, first_name, last_name, full_name,
                       job_title, company_domain, company_name
                FROM contacts
                WHERE REPLACE(REPLACE(LOWER(company_domain), 'https://', ''), 'http://', '')
                      IN ({placeholders})
                ORDER BY id
                """,
                normalized,
            ).fetchall()
        return [
            Contact(
                person_id=row["person_id"],
                linkedin_url=row["linkedin_url"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                full_name=row["full_name"],
                job_title=row["job_title"],
                company_domain=normalize_domain(row["company_domain"]),
                company_name=row["company_name"],
            )
            for row in rows
        ]

    def get_enriched_contacts_for_linkedin_urls(
        self, linkedin_urls: list[str]
    ) -> list[EnrichedContact]:
        if not linkedin_urls:
            return []
        placeholders = ",".join("?" * len(linkedin_urls))
        with self.connection() as conn:
            contact_rows = conn.execute(
                f"""
                SELECT id, person_id, linkedin_url, first_name, last_name,
                       full_name, job_title, company_domain, company_name
                FROM contacts
                WHERE linkedin_url IN ({placeholders})
                ORDER BY id
                """,
                linkedin_urls,
            ).fetchall()

            enriched: list[EnrichedContact] = []
            for contact in contact_rows:
                email_row = conn.execute(
                    """
                    SELECT email, provider, verified FROM emails
                    WHERE contact_id = ?
                    LIMIT 1
                    """,
                    (contact["id"],),
                ).fetchone()

                if not email_row:
                    domain = normalize_domain(contact["company_domain"])
                    email_row = conn.execute(
                        """
                        SELECT email, provider, verified FROM emails
                        WHERE email LIKE ? AND contact_id IS NULL
                        LIMIT 1
                        """,
                        (f"%@{domain}",),
                    ).fetchone()
                    if email_row:
                        conn.execute(
                            "UPDATE emails SET contact_id = ? WHERE email = ?",
                            (contact["id"], email_row["email"]),
                        )

                if not email_row:
                    continue

                enriched.append(
                    EnrichedContact(
                        person_id=contact["person_id"],
                        linkedin_url=contact["linkedin_url"],
                        first_name=contact["first_name"],
                        last_name=contact["last_name"],
                        full_name=contact["full_name"],
                        job_title=contact["job_title"],
                        company_domain=normalize_domain(contact["company_domain"]),
                        company_name=contact["company_name"],
                        email=email_row["email"],
                        email_status="verified" if email_row["verified"] else "unresolved",
                        provider=email_row["provider"] or "prospeo",
                    )
                )
        return enriched

    def get_company_by_domain(self, domain: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT domain, name, industries FROM companies WHERE domain = ?",
                (domain.lower(),),
            ).fetchone()
            return dict(row) if row else None

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id, seed_domain, mode, started_at, finished_at, stats_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if result.get("stats_json"):
                result["stats"] = json.loads(result["stats_json"])
            return result

    def get_run_report(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            return {}

        with self.connection() as conn:
            companies = conn.execute(
                "SELECT COUNT(*) as n FROM companies WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
            contacts = conn.execute(
                "SELECT COUNT(*) as n FROM contacts WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
            emails = conn.execute(
                "SELECT COUNT(*) as n FROM emails WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
            sent = conn.execute(
                "SELECT COUNT(*) as n FROM sent_emails WHERE run_id = ? AND status = 'sent'",
                (run_id,),
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) as n FROM sent_emails WHERE run_id = ? AND status = 'failed'",
                (run_id,),
            ).fetchone()["n"]
            subjects = conn.execute(
                """
                SELECT subject, COUNT(*) as count
                FROM sent_emails WHERE run_id = ? AND status = 'sent'
                GROUP BY subject ORDER BY count DESC
                """,
                (run_id,),
            ).fetchall()

        stats = run.get("stats") or {}
        emails_resolved = stats.get("emails_resolved", emails)
        deliverability = (
            round(sent / emails_resolved * 100, 1) if emails_resolved else 0.0
        )
        cost_per_lead = round(
            (companies * 0.2 + contacts * 0.04 + emails * 1.0) / max(sent, 1), 2
        )

        return {
            "run_id": run_id,
            "seed_domain": run["seed_domain"],
            "mode": run["mode"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "companies_discovered": companies,
            "contacts_enriched": contacts,
            "emails_resolved": emails_resolved,
            "emails_sent": sent,
            "emails_failed": failed,
            "deliverability_rate": deliverability,
            "estimated_cost_per_lead": cost_per_lead,
            "subject_variants": [dict(s) for s in subjects],
            "stats": stats,
        }

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, seed_domain, mode, started_at, finished_at FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
