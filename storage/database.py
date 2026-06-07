import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models.schemas import Company, Contact, EnrichedContact, RunMode, SendResult


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
