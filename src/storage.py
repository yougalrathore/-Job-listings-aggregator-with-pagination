"""
Storage Agent - SQLite persistence and schema management for JobScrape Pro.

Responsibilities:
- Database schema creation and migration
- CRUD operations for job listings
- Indexed querying for deduplication and analysis
- Connection pooling and transaction management
"""

import sqlite3
import logging
import os
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class JobRecord:
    """Structured representation of a job listing."""
    id: Optional[int] = None
    title: str = ""
    company: str = ""
    location: str = ""
    salary_range: str = ""
    date_posted: str = ""
    job_url: str = ""
    source_site: str = ""
    raw_html: str = ""
    scraped_at: Optional[str] = None
    is_active: int = 1


class StorageAgent:
    """
    Production-grade SQLite storage agent with indexed schema,
    connection pooling, and batch operations.
    """

    # Schema version for future migrations
    SCHEMA_VERSION = 1

    def __init__(self, db_path: str = "data/jobs.db"):
        self.db_path = db_path
        self._ensure_directory()
        self._init_schema()
        logger.info(f"StorageAgent initialized with database: {db_path}")

    def _ensure_directory(self) -> None:
        """Ensure the database directory exists."""
        dir_path = os.path.dirname(self.db_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
            logger.info(f"Created database directory: {dir_path}")

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections with proper cleanup."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=30000000")
            yield conn
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            logger.error(f"Database error: {e}", exc_info=True)
            raise
        finally:
            if conn:
                conn.close()

    def _init_schema(self) -> None:
        """Initialize the database schema with indexes for performance."""
        schema_sql = """
        -- Core jobs table with all required fields
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            salary_range TEXT NOT NULL DEFAULT '',
            date_posted TEXT NOT NULL DEFAULT '',
            job_url TEXT NOT NULL UNIQUE,
            source_site TEXT NOT NULL DEFAULT '',
            raw_html TEXT DEFAULT '',
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            week_key TEXT GENERATED ALWAYS AS (
                strftime('%Y-W%W', COALESCE(NULLIF(date_posted, ''), scraped_at))
            ) STORED
        );

        -- Indexes for efficient querying
        CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(job_url);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
        CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
        CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_week_key ON jobs(week_key);
        CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_site);
        CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active);

        -- Composite index for common query patterns
        CREATE INDEX IF NOT EXISTS idx_jobs_company_location ON jobs(company, location);

        -- Schema version tracking for migrations
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Deduplication log for auditing
        CREATE TABLE IF NOT EXISTS dedup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT NOT NULL,
            reason TEXT NOT NULL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Scrape runs log for monitoring
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_site TEXT NOT NULL,
            pages_scraped INTEGER DEFAULT 0,
            jobs_found INTEGER DEFAULT 0,
            jobs_inserted INTEGER DEFAULT 0,
            jobs_deduplicated INTEGER DEFAULT 0,
            errors_encountered INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT DEFAULT 'running',
            error_message TEXT
        );
        """

        with self._get_connection() as conn:
            conn.executescript(schema_sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata (key, value) VALUES (?, ?)",
                ("version", str(self.SCHEMA_VERSION))
            )
            conn.commit()
            logger.info(f"Schema initialized (version {self.SCHEMA_VERSION})")

    def start_scrape_run(self, source_site: str) -> int:
        """Log the start of a scraping run and return its ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO scrape_runs (source_site, status) VALUES (?, ?)",
                (source_site, "running")
            )
            conn.commit()
            run_id = cursor.lastrowid
            logger.info(f"Started scrape run {run_id} for {source_site}")
            return run_id

    def complete_scrape_run(
        self,
        run_id: int,
        pages_scraped: int = 0,
        jobs_found: int = 0,
        jobs_inserted: int = 0,
        jobs_deduplicated: int = 0,
        errors_encountered: int = 0,
        status: str = "completed",
        error_message: str = ""
    ) -> None:
        """Log the completion of a scraping run."""
        with self._get_connection() as conn:
            conn.execute(
                """UPDATE scrape_runs
                   SET pages_scraped = ?, jobs_found = ?, jobs_inserted = ?,
                       jobs_deduplicated = ?, errors_encountered = ?,
                       completed_at = CURRENT_TIMESTAMP, status = ?, error_message = ?
                   WHERE id = ?""",
                (pages_scraped, jobs_found, jobs_inserted,
                 jobs_deduplicated, errors_encountered, status, error_message, run_id)
            )
            conn.commit()
            logger.info(f"Completed scrape run {run_id} with status: {status}")

    def url_exists(self, job_url: str) -> bool:
        """Check if a job URL already exists in the database."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM jobs WHERE job_url = ? LIMIT 1", (job_url,)
            )
            exists = cursor.fetchone() is not None
            if exists:
                logger.debug(f"URL already exists: {job_url[:80]}...")
            return exists

    def bulk_check_urls(self, urls: List[str]) -> set:
        """Efficiently check which URLs already exist. Returns set of existing URLs."""
        if not urls:
            return set()

        # SQLite has a limit of 999 variables, so batch if needed
        BATCH_SIZE = 900
        existing = set()

        with self._get_connection() as conn:
            for i in range(0, len(urls), BATCH_SIZE):
                batch = urls[i:i + BATCH_SIZE]
                placeholders = ",".join(["?"] * len(batch))
                cursor = conn.execute(
                    f"SELECT job_url FROM jobs WHERE job_url IN ({placeholders})",
                    batch
                )
                existing.update(row["job_url"] for row in cursor.fetchall())

        logger.debug(f"Found {len(existing)} existing URLs out of {len(urls)} checked")
        return existing

    def insert_job(self, job: JobRecord) -> Optional[int]:
        """Insert a single job record. Returns the new ID or None on failure."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """INSERT INTO jobs
                       (title, company, location, salary_range, date_posted,
                        job_url, source_site, raw_html, scraped_at, is_active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job.title, job.company, job.location, job.salary_range,
                     job.date_posted, job.job_url, job.source_site, job.raw_html,
                     job.scraped_at or datetime.now().isoformat(), job.is_active)
                )
                conn.commit()
                logger.debug(f"Inserted job: {job.title[:60]}...")
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.debug(f"Duplicate URL prevented insert: {job.job_url[:80]}...")
            return None
        except sqlite3.Error as e:
            logger.error(f"Failed to insert job: {e}", exc_info=True)
            return None

    def insert_jobs_bulk(self, jobs: List[JobRecord]) -> Tuple[int, int]:
        """
        Bulk insert jobs with conflict handling.
        Returns: (inserted_count, skipped_count)
        """
        if not jobs:
            return 0, 0

        inserted = 0
        skipped = 0

        with self._get_connection() as conn:
            for job in jobs:
                try:
                    conn.execute(
                        """INSERT INTO jobs
                           (title, company, location, salary_range, date_posted,
                            job_url, source_site, raw_html, scraped_at, is_active)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (job.title, job.company, job.location, job.salary_range,
                         job.date_posted, job.job_url, job.source_site, job.raw_html,
                         job.scraped_at or datetime.now().isoformat(), job.is_active)
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                    # Log deduplication event
                    conn.execute(
                        "INSERT INTO dedup_log (job_url, reason) VALUES (?, ?)",
                        (job.job_url, "duplicate_url")
                    )

            conn.commit()

        logger.info(f"Bulk insert: {inserted} inserted, {skipped} skipped (duplicates)")
        return inserted, skipped

    def get_all_jobs(
        self,
        source_site: Optional[str] = None,
        week_key: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve jobs with optional filtering."""
        query = "SELECT * FROM jobs WHERE is_active = 1"
        params = []

        if source_site:
            query += " AND source_site = ?"
            params.append(source_site)

        if week_key:
            query += " AND week_key = ?"
            params.append(week_key)

        query += " ORDER BY scraped_at DESC"

        if limit:
            query += f" LIMIT {int(limit)}"

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_jobs_for_analysis(self, week_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get jobs optimized for skill analysis."""
        if not week_key:
            # Default to current week
            week_key = datetime.now().strftime("%Y-W%W")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT title, company, location, salary_range, date_posted,
                          job_url, source_site, raw_html
                   FROM jobs
                   WHERE week_key = ? AND is_active = 1
                   ORDER BY scraped_at DESC""",
                (week_key,)
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics for monitoring."""
        with self._get_connection() as conn:
            stats = {}

            # Total jobs
            cursor = conn.execute("SELECT COUNT(*) as count FROM jobs")
            stats["total_jobs"] = cursor.fetchone()["count"]

            # Jobs this week
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM jobs WHERE week_key = strftime('%Y-W%W', 'now')"
            )
            stats["jobs_this_week"] = cursor.fetchone()["count"]

            # Unique companies
            cursor = conn.execute(
                "SELECT COUNT(DISTINCT company) as count FROM jobs WHERE company != ''"
            )
            stats["unique_companies"] = cursor.fetchone()["count"]

            # Unique locations
            cursor = conn.execute(
                "SELECT COUNT(DISTINCT location) as count FROM jobs WHERE location != ''"
            )
            stats["unique_locations"] = cursor.fetchone()["count"]

            # Source breakdown
            cursor = conn.execute(
                """SELECT source_site, COUNT(*) as count
                   FROM jobs GROUP BY source_site ORDER BY count DESC"""
            )
            stats["by_source"] = {row["source_site"]: row["count"] for row in cursor.fetchall()}

            # Recent scrape runs
            cursor = conn.execute(
                """SELECT * FROM scrape_runs
                   ORDER BY started_at DESC LIMIT 10"""
            )
            stats["recent_runs"] = [dict(row) for row in cursor.fetchall()]

            return stats

    def get_location_distribution(self, week_key: Optional[str] = None) -> Dict[str, int]:
        """Get job count by location."""
        if not week_key:
            week_key = datetime.now().strftime("%Y-W%W")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT location, COUNT(*) as count
                   FROM jobs
                   WHERE week_key = ? AND location != ''
                   GROUP BY location
                   ORDER BY count DESC""",
                (week_key,)
            )
            return {row["location"]: row["count"] for row in cursor.fetchall()}

    def get_company_distribution(self, week_key: Optional[str] = None, limit: int = 20) -> Dict[str, int]:
        """Get job count by company."""
        if not week_key:
            week_key = datetime.now().strftime("%Y-W%W")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT company, COUNT(*) as count
                   FROM jobs
                   WHERE week_key = ? AND company != ''
                   GROUP BY company
                   ORDER BY count DESC
                   LIMIT ?""",
                (week_key, limit)
            )
            return {row["company"]: row["count"] for row in cursor.fetchall()}

    def cleanup_old_runs(self, days: int = 30) -> int:
        """Clean up scrape run logs older than specified days."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """DELETE FROM scrape_runs
                   WHERE started_at < datetime('now', '-{} days')""".format(days)
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.info(f"Cleaned up {deleted} old scrape run records")
            return deleted

    def vacuum(self) -> None:
        """Optimize database file size."""
        with self._get_connection() as conn:
            conn.execute("VACUUM")
            logger.info("Database vacuumed")


# Singleton instance for application-wide use
_storage_instance: Optional[StorageAgent] = None


def get_storage(db_path: str = "data/jobs.db") -> StorageAgent:
    """Get or create the singleton storage instance."""
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = StorageAgent(db_path)
    return _storage_instance


def reset_storage() -> None:
    """Reset the singleton (useful for testing)."""
    global _storage_instance
    _storage_instance = None
