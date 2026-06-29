"""
Scraper Agent - Page fetching and HTML/JSON parsing for JobScrape Pro.

Responsibilities:
- Fetch job listing pages with retry logic and rate limiting
- Parse HTML/JSON to extract structured job data
- Handle pagination (URL parameter-based and cursor-based)
- Extract: title, company, location, salary, date_posted, job_url
- Graceful handling of network failures and malformed HTML
"""

import re
import json
import time
import random
import logging
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Default headers to appear as a real browser
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_BASE = 2  # seconds
RETRY_DELAY_MAX = 10  # seconds
REQUEST_TIMEOUT = 30  # seconds
RATE_LIMIT_DELAY = (1, 3)  # Random delay range between requests


@dataclass
class ScrapedJob:
    """Intermediate representation of a scraped job before storage."""
    title: str = ""
    company: str = ""
    location: str = ""
    salary_range: str = ""
    date_posted: str = ""
    job_url: str = ""
    raw_html: str = ""
    source_site: str = ""
    _validation_errors: List[str] = field(default_factory=list, repr=False)

    def is_valid(self) -> bool:
        """Check if the scraped job has minimum required fields."""
        self._validation_errors = []
        if not self.title or len(self.title.strip()) < 2:
            self._validation_errors.append("Missing or invalid title")
        if not self.job_url:
            self._validation_errors.append("Missing job URL")
        elif not self.job_url.startswith(("http://", "https://")):
            self._validation_errors.append(f"Invalid URL format: {self.job_url}")
        return len(self._validation_errors) == 0

    def to_storage_record(self):
        """Convert to the format expected by StorageAgent."""
        from src.storage import JobRecord
        return JobRecord(
            title=self.title.strip(),
            company=self.company.strip(),
            location=self.location.strip(),
            salary_range=self.salary_range.strip(),
            date_posted=self.date_posted.strip(),
            job_url=self.job_url.strip(),
            source_site=self.source_site,
            raw_html=self.raw_html,
            scraped_at=datetime.now().isoformat(),
        )


class ScraperAgent:
    """
    Production-grade scraper with retry logic, rate limiting,
    and support for multiple job board formats including JSON APIs.
    """

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        max_retries: int = MAX_RETRIES,
        rate_limit_delay: tuple = RATE_LIMIT_DELAY,
        request_timeout: int = REQUEST_TIMEOUT,
    ):
        self.headers = headers or DEFAULT_HEADERS.copy()
        self.max_retries = max_retries
        self.rate_limit_delay = rate_limit_delay
        self.request_timeout = request_timeout
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self._pages_scraped = 0
        self._errors_encountered = 0

    def _fetch_page(self, url: str, is_json: bool = False) -> Optional[Any]:
        """
        Fetch a single page with retry logic and exponential backoff.
        Returns HTML content, JSON data, or None on failure.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"Fetching {url} (attempt {attempt}/{self.max_retries})")

                # Random delay to be respectful to servers
                delay = random.uniform(*self.rate_limit_delay)
                if attempt > 1:
                    # Exponential backoff on retry
                    delay = min(RETRY_DELAY_BASE * (2 ** (attempt - 1)), RETRY_DELAY_MAX)
                time.sleep(delay)

                response = self.session.get(
                    url,
                    timeout=self.request_timeout,
                    allow_redirects=True
                )
                response.raise_for_status()

                self._pages_scraped += 1
                content_preview = len(response.text)
                logger.info(f"Successfully fetched {url} ({content_preview} chars)")

                if is_json:
                    return response.json()
                return response.text

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout fetching {url} (attempt {attempt})")
                self._errors_encountered += 1

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response else "unknown"
                logger.warning(f"HTTP {status} for {url} (attempt {attempt})")
                self._errors_encountered += 1
                if status in (403, 404, 410):
                    # Don't retry on client errors
                    break

            except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                logger.warning(f"Request/JSON error for {url}: {e} (attempt {attempt})")
                self._errors_encountered += 1

        logger.error(f"Failed to fetch {url} after {self.max_retries} attempts")
        return None

    def _parse_relative_date(self, text: str) -> str:
        """Convert relative date strings to ISO format dates."""
        text = text.lower().strip()
        now = datetime.now()

        # Patterns like "2 days ago", "1 week ago", "today", etc.
        patterns = [
            (r'(\d+)\s*day[s]?\s*ago', lambda m: now - timedelta(days=int(m.group(1)))),
            (r'(\d+)\s*week[s]?\s*ago', lambda m: now - timedelta(weeks=int(m.group(1)))),
            (r'(\d+)\s*month[s]?\s*ago', lambda m: now - timedelta(days=int(m.group(1)) * 30)),
            (r'(\d+)\s*hour[s]?\s*ago', lambda m: now - timedelta(hours=int(m.group(1)))),
            (r'today', lambda m: now),
            (r'yesterday', lambda m: now - timedelta(days=1)),
        ]

        for pattern, converter in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    result = converter(match)
                    return result.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass

        # Try to parse as an actual date
        date_patterns = [
            (r'(\d{4})-(\d{2})-(\d{2})', "%Y-%m-%d"),
            (r'(\d{2})/(\d{2})/(\d{4})', "%m/%d/%Y"),
        ]

        for pattern, fmt in date_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return datetime.strptime(match.group(0), fmt).strftime("%Y-%m-%d")
                except ValueError:
                    pass

        return text  # Return original if we can't parse

    # ------------------------------------------------------------------
    # Site-specific parsers
    # ------------------------------------------------------------------

    def _parse_remoteok_api(self, data: List[Dict], base_url: str) -> List[ScrapedJob]:
        """
        Parse RemoteOK JSON API response.
        API docs: https://remoteok.com/api
        """
        jobs = []

        # First element is metadata, skip it
        for entry in data[1:] if len(data) > 1 else data:
            try:
                job = ScrapedJob(source_site="remoteok")

                # Position/title
                job.title = entry.get("position", "").strip()

                # Company
                job.company = entry.get("company", "").strip()

                # Location - RemoteOK jobs are remote but may have location hints
                location = entry.get("location", "")
                if not location:
                    # Try to infer from tags
                    tags = entry.get("tags", [])
                    location_tags = [t for t in tags if any(
                        keyword in t.lower() for keyword in
                        ["remote", "usa", "europe", "asia", "americas", "worldwide",
                         "uk", "canada", "germany", "france", "australia"]
                    )]
                    location = ", ".join(location_tags) if location_tags else "Remote"
                job.location = location

                # Salary - look in description for salary patterns
                description = entry.get("description", "")
                salary_match = re.search(
                    r'[$€£]\s*[\d,]+\s*[–-]\s*[$€£]?\s*[\d,]+|\$\d+[kK][\s–-]+\$?\d+[kK]|[\d,]+\s*(USD|EUR|GBP|per year|per month|/yr|/month)',
                    description
                )
                if salary_match:
                    job.salary_range = salary_match.group(0)

                # Date posted
                date_str = entry.get("date", "")
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        job.date_posted = dt.strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        job.date_posted = date_str[:10]

                # Job URL - construct from slug
                slug = entry.get("slug", "")
                if slug:
                    job.job_url = f"https://remoteok.com/remote-jobs/{slug}"
                elif entry.get("id"):
                    job.job_url = f"https://remoteok.com/remote-jobs/{entry['id']}"

                # Store raw data
                job.raw_html = json.dumps(entry)[:5000]

                # Add tags as additional context in raw_html for skill extraction
                tags = entry.get("tags", [])
                if tags:
                    job.raw_html += f"\n<!-- TAGS: {', '.join(tags)} -->"

                if job.is_valid():
                    jobs.append(job)
                else:
                    logger.debug(f"Invalid RemoteOK job: {job._validation_errors}")

            except Exception as e:
                logger.warning(f"Error parsing RemoteOK API entry: {e}")
                self._errors_encountered += 1

        return jobs

    def _parse_remoteok_html(self, html: str, base_url: str) -> List[ScrapedJob]:
        """Parse RemoteOK HTML page (fallback when API unavailable)."""
        soup = BeautifulSoup(html, "lxml")
        jobs = []

        # Try table rows first
        job_rows = soup.find_all("tr", class_="job")
        for row in job_rows:
            try:
                if "placeholder" in row.get("class", []):
                    continue

                job = ScrapedJob(source_site="remoteok")

                title_elem = row.select_one("td.position a h2, td.position h2")
                if title_elem:
                    job.title = title_elem.get_text(strip=True)

                company_elem = row.select_one("td.company a h3, td.company h3, .company")
                if company_elem:
                    job.company = company_elem.get_text(strip=True)

                loc_elem = row.select_one("td.location, .location")
                if loc_elem:
                    job.location = loc_elem.get_text(strip=True)

                salary_elem = row.select_one(".salary, td.salary")
                if salary_elem:
                    job.salary_range = salary_elem.get_text(strip=True)

                time_elem = row.select_one("td.time a, td.time, .time")
                if time_elem:
                    job.date_posted = self._parse_relative_date(
                        time_elem.get_text(strip=True)
                    )

                link_elem = row.select_one("td.source a, td.position a")
                if link_elem and link_elem.get("href"):
                    href = link_elem["href"]
                    job.job_url = urljoin(base_url, href)

                job.raw_html = str(row)

                if job.is_valid():
                    jobs.append(job)

            except Exception as e:
                logger.warning(f"Error parsing RemoteOK HTML row: {e}")
                self._errors_encountered += 1

        return jobs

    def _parse_weworkremotely(self, html: str, base_url: str) -> List[ScrapedJob]:
        """Parse We Work Remotely job listings from HTML."""
        soup = BeautifulSoup(html, "lxml")
        jobs = []

        job_listings = soup.select("section.jobs article li, .jobs-list li, .job")
        if not job_listings:
            job_listings = soup.select("li.featured, li:not(.view-all)")

        for listing in job_listings:
            try:
                job = ScrapedJob(source_site="weworkremotely")

                title_elem = listing.select_one("span.title, .job-title, h4 a, a span.title")
                if title_elem:
                    job.title = title_elem.get_text(strip=True)

                company_elem = listing.select_one("span.company, .company, .company-name")
                if company_elem:
                    job.company = company_elem.get_text(strip=True)

                loc_elem = listing.select_one("span.region, .region, .location")
                if loc_elem:
                    job.location = loc_elem.get_text(strip=True)

                time_elem = listing.select_one("span.date, time, .time-ago")
                if time_elem:
                    datetime_val = time_elem.get("datetime", "")
                    if datetime_val:
                        job.date_posted = datetime_val[:10]
                    else:
                        job.date_posted = self._parse_relative_date(
                            time_elem.get_text(strip=True)
                        )

                link_elem = listing.select_one("a[href*='/remote-jobs/'], a[href*='/listings/']")
                if not link_elem:
                    link_elem = listing.find("a")
                if link_elem and link_elem.get("href"):
                    href = link_elem["href"]
                    job.job_url = urljoin(base_url, href)

                salary_elem = listing.select_one(".salary")
                if salary_elem:
                    job.salary_range = salary_elem.get_text(strip=True)

                job.raw_html = str(listing)

                if job.is_valid():
                    jobs.append(job)
                else:
                    logger.debug(f"Invalid WWR job: {job._validation_errors}")

            except Exception as e:
                logger.warning(f"Error parsing WWR job listing: {e}")
                self._errors_encountered += 1

        return jobs

    def _parse_generic(self, html: str, base_url: str, selectors: Dict[str, str]) -> List[ScrapedJob]:
        """Generic parser using CSS selectors from configuration."""
        soup = BeautifulSoup(html, "lxml")
        jobs = []

        container_selector = selectors.get("container", ".job, .job-listing, [data-job]")
        job_elems = soup.select(container_selector)

        for elem in job_elems:
            try:
                job = ScrapedJob(source_site=selectors.get("source_name", "generic"))

                def extract_text(selector: str) -> str:
                    el = elem.select_one(selector)
                    return el.get_text(strip=True) if el else ""

                job.title = extract_text(selectors.get("title", ".title, h2, h3, h4"))
                job.company = extract_text(selectors.get("company", ".company, .employer"))
                job.location = extract_text(selectors.get("location", ".location, .region"))
                job.salary_range = extract_text(selectors.get("salary", ".salary"))
                job.date_posted = self._parse_relative_date(
                    extract_text(selectors.get("date", ".date, .posted, time"))
                )

                link_selector = selectors.get("link", "a")
                link_elem = elem.select_one(link_selector)
                if link_elem and link_elem.get("href"):
                    job.job_url = urljoin(base_url, link_elem["href"])

                job.raw_html = str(elem)

                if job.is_valid():
                    jobs.append(job)

            except Exception as e:
                logger.warning(f"Error in generic parser: {e}")
                self._errors_encountered += 1

        return jobs

    # ------------------------------------------------------------------
    # Public scraping interface
    # ------------------------------------------------------------------

    def scrape_page(self, url: str, parser_type: str = "auto", selectors: Optional[Dict] = None) -> List[ScrapedJob]:
        """
        Scrape a single page and return list of job records.
        Automatically detects JSON APIs vs HTML parsing.
        """
        # Auto-detect parser from URL
        if parser_type == "auto":
            domain = urlparse(url).netloc.lower()
            if "remoteok" in domain:
                parser_type = "remoteok"
            elif "weworkremotely" in domain:
                parser_type = "weworkremotely"
            else:
                parser_type = "generic"

        # Use JSON API for RemoteOK (more reliable)
        if parser_type == "remoteok":
            # Try API first, fallback to HTML
            api_url = "https://remoteok.com/api"
            data = self._fetch_page(api_url, is_json=True)
            if data and isinstance(data, list) and len(data) > 0:
                return self._parse_remoteok_api(data, url)

            # Fallback to HTML
            html = self._fetch_page(url)
            if html:
                return self._parse_remoteok_html(html, url)
            return []

        elif parser_type == "weworkremotely":
            html = self._fetch_page(url)
            if html:
                return self._parse_weworkremotely(html, url)
            return []

        elif parser_type == "generic":
            html = self._fetch_page(url)
            if html:
                return self._parse_generic(html, url, selectors or {})
            return []

        else:
            logger.error(f"Unknown parser type: {parser_type}")
            return []

    def scrape_paginated(
        self,
        base_url: str,
        parser_type: str = "auto",
        selectors: Optional[Dict] = None,
        max_pages: int = 10,
        page_param: str = "page",
        page_starts_at: int = 1,
        page_strategy: str = "query_param",
        cursor_selector: Optional[str] = None,
    ) -> List[ScrapedJob]:
        """
        Scrape multiple pages with pagination support.
        For API-based sources, this may only scrape once (single API call).
        """
        all_jobs: List[ScrapedJob] = []
        seen_urls = set()
        current_page = page_starts_at
        consecutive_empty = 0
        MAX_CONSECUTIVE_EMPTY = 2

        # For RemoteOK API, we get all jobs in one call - no pagination needed
        if parser_type == "remoteok" or (parser_type == "auto" and "remoteok" in base_url.lower()):
            logger.info(f"Using RemoteOK API (single call for all jobs)")
            jobs = self.scrape_page(base_url, parser_type, selectors)
            return jobs

        logger.info(
            f"Starting paginated scrape of {base_url} "
            f"(max {max_pages} pages, strategy: {page_strategy})"
        )

        for page_num in range(max_pages):
            # Build page URL based on strategy
            if page_strategy == "query_param":
                separator = "&" if "?" in base_url else "?"
                page_url = f"{base_url}{separator}{page_param}={current_page}"
            elif page_strategy == "path":
                page_url = base_url.format(page=current_page)
            else:
                page_url = base_url

            logger.info(f"Scraping page {current_page}: {page_url}")
            jobs = self.scrape_page(page_url, parser_type, selectors)

            if not jobs:
                consecutive_empty += 1
                logger.warning(
                    f"No jobs found on page {current_page} "
                    f"({consecutive_empty}/{MAX_CONSECUTIVE_EMPTY} consecutive empty)"
                )
                if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                    logger.info("Stopping pagination: consecutive empty pages")
                    break
            else:
                consecutive_empty = 0
                new_jobs = [j for j in jobs if j.job_url not in seen_urls]
                seen_urls.update(j.job_url for j in new_jobs)
                all_jobs.extend(new_jobs)
                logger.info(
                    f"Page {current_page}: {len(jobs)} jobs found, "
                    f"{len(new_jobs)} new unique"
                )

            current_page += 1

        logger.info(
            f"Paginated scrape complete: {len(all_jobs)} unique jobs "
            f"from {self._pages_scraped} pages"
        )
        return all_jobs

    def get_stats(self) -> Dict[str, int]:
        """Return scraper statistics."""
        return {
            "pages_scraped": self._pages_scraped,
            "errors_encountered": self._errors_encountered,
        }

    def reset_stats(self) -> None:
        """Reset scraper statistics."""
        self._pages_scraped = 0
        self._errors_encountered = 0
