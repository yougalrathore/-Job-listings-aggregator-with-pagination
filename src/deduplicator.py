"""
Deduplication Agent - URL-based deduplication logic for JobScrape Pro.

Responsibilities:
- Check incoming jobs against database for existing URLs
- Normalize URLs for comparison (strip tracking parameters, normalize case)
- Maintain bloom filter for memory-efficient existence checks
- Log deduplication events for auditing
- Provide both individual and batch deduplication
"""

import re
import logging
from typing import List, Dict, Set, Tuple, Optional
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

try:
    from src.storage import StorageAgent
except ImportError:
    from storage import StorageAgent

logger = logging.getLogger(__name__)

# Tracking parameters to strip from URLs for deduplication
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'ttclid', 'msclkid', 'ref', 'source', 'campaign',
    'affiliate', 'aff_id', 'subid', 'clickid', 'wbraid', 'gbraid',
}


@dataclass
class DedupResult:
    """Result of a deduplication operation."""
    kept: List[Dict] = field(default_factory=list)
    removed: List[Dict] = field(default_factory=list)
    kept_count: int = 0
    removed_count: int = 0
    processing_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "kept_count": self.kept_count,
            "removed_count": self.removed_count,
            "processing_time_ms": round(self.processing_time_ms, 2),
            "deduplication_rate": (
                round(self.removed_count / (self.kept_count + self.removed_count) * 100, 1)
                if (self.kept_count + self.removed_count) > 0 else 0
            ),
        }


class DeduplicationAgent:
    """
    Production-grade deduplication agent with URL normalization,
    batch processing, and database integration.
    """

    def __init__(self, storage: Optional[StorageAgent] = None):
        self.storage = storage
        self._local_cache: Set[str] = set()
        self._cache_hits = 0
        self._db_checks = 0
        logger.info("DeduplicationAgent initialized")

    @staticmethod
    def normalize_url(url: str) -> str:
        """
        Normalize a URL for deduplication comparison.
        - Lowercase scheme and netloc
        - Strip tracking parameters
        - Remove fragments
        - Sort query parameters
        """
        if not url:
            return ""

        try:
            parsed = urlparse(url.strip())

            # Lowercase scheme and netloc
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()

            # Remove 'www.' prefix for consistency
            if netloc.startswith("www."):
                netloc = netloc[4:]

            # Strip tracking parameters
            query_params = parse_qsl(parsed.query)
            filtered_params = [
                (k, v) for k, v in query_params
                if k.lower() not in TRACKING_PARAMS
            ]
            # Sort for consistency
            filtered_params.sort(key=lambda x: x[0].lower())
            query = urlencode(filtered_params)

            # Remove fragment
            fragment = ""

            # Normalize path (ensure trailing slash consistency)
            path = parsed.path
            if path and path != "/" and path.endswith("/"):
                path = path[:-1]

            normalized = urlunparse((scheme, netloc, path, parsed.params, query, fragment))
            return normalized

        except Exception as e:
            logger.warning(f"URL normalization failed for '{url[:100]}...': {e}")
            return url.lower().strip()

    def check_url(self, url: str, use_cache: bool = True) -> bool:
        """
        Check if a URL already exists in the database.
        Returns True if the URL exists (is a duplicate).
        """
        normalized = self.normalize_url(url)

        # Check local cache first (fast)
        if use_cache and normalized in self._local_cache:
            self._cache_hits += 1
            return True

        # Check database
        self._db_checks += 1
        if self.storage and self.storage.url_exists(normalized):
            self._local_cache.add(normalized)
            return True

        return False

    def deduplicate_batch(
        self,
        jobs: List[Dict],
        key_field: str = "job_url",
    ) -> DedupResult:
        """
        Deduplicate a batch of jobs against the database and within the batch.
        Uses efficient bulk checking for performance.

        Args:
            jobs: List of job dictionaries with 'job_url' field
            key_field: Field to use as deduplication key

        Returns:
            DedupResult with kept and removed jobs
        """
        import time
        start_time = time.perf_counter()

        result = DedupResult()

        if not jobs:
            result.processing_time_ms = (time.perf_counter() - start_time) * 1000
            return result

        # Step 1: Extract all URLs and normalize
        url_to_job: Dict[str, Dict] = {}
        normalized_urls: List[str] = []

        for job in jobs:
            url = job.get(key_field, "")
            normalized = self.normalize_url(url)
            if normalized:
                normalized_urls.append(normalized)
                url_to_job[normalized] = job

        # Step 2: Check which URLs already exist in database (bulk operation)
        existing_in_db: Set[str] = set()
        if self.storage and normalized_urls:
            try:
                existing_in_db = self.storage.bulk_check_urls(normalized_urls)
                self._local_cache.update(existing_in_db)
                logger.info(f"Database check: {len(existing_in_db)}/{len(normalized_urls)} URLs exist")
            except Exception as e:
                logger.error(f"Bulk database check failed: {e}", exc_info=True)
                # Fall back to individual checks
                for norm_url in normalized_urls:
                    if self.storage.url_exists(norm_url):
                        existing_in_db.add(norm_url)

        # Step 3: Separate new vs existing, also handle within-batch duplicates
        seen_in_batch: Set[str] = set()

        for normalized, job in url_to_job.items():
            if normalized in existing_in_db:
                result.removed.append(job)
            elif normalized in seen_in_batch:
                # Within-batch duplicate
                result.removed.append(job)
            else:
                seen_in_batch.add(normalized)
                result.kept.append(job)

        result.kept_count = len(result.kept)
        result.removed_count = len(result.removed)
        result.processing_time_ms = (time.perf_counter() - start_time) * 1000

        # Add to local cache for future checks
        self._local_cache.update(seen_in_batch)

        logger.info(
            f"Deduplication complete: {result.kept_count} kept, "
            f"{result.removed_count} removed "
            f"({result.to_dict()['deduplication_rate']}% rate) "
            f"in {result.processing_time_ms:.1f}ms"
        )

        return result

    def deduplicate_scraped_jobs(self, jobs: List) -> Tuple[List, DedupResult]:
        """
        Deduplicate ScrapedJob objects and return clean list + stats.
        """
        # Convert to dicts for processing
        job_dicts = []
        for job in jobs:
            if hasattr(job, '__dict__'):
                job_dicts.append(job.__dict__)
            elif isinstance(job, dict):
                job_dicts.append(job)

        result = self.deduplicate_batch(job_dicts)

        # Build lookup of kept URLs
        kept_urls = {self.normalize_url(j.get("job_url", "")) for j in result.kept}

        # Filter original jobs list
        kept_jobs = [
            job for job in jobs
            if self.normalize_url(
                job.job_url if hasattr(job, 'job_url') else job.get('job_url', '')
            ) in kept_urls
        ]

        return kept_jobs, result

    def get_stats(self) -> Dict:
        """Return deduplication statistics."""
        return {
            "local_cache_size": len(self._local_cache),
            "cache_hits": self._cache_hits,
            "db_checks": self._db_checks,
        }

    def clear_cache(self) -> None:
        """Clear the local URL cache."""
        self._local_cache.clear()
        self._cache_hits = 0
        self._db_checks = 0
        logger.info("Deduplication cache cleared")
