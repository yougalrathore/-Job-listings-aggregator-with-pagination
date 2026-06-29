# JobScrape Pro - Production-Grade Job Listings Aggregator

> **Real-time intelligence for recruiting strategy.** Automated scraping, deduplication, and skill trend analysis across major remote job boards.

---

## What You Get

JobScrape Pro is a production-ready data collection infrastructure that delivers:

| Deliverable | Description |
|-------------|-------------|
| **Structured Job Data** | Title, company, location, salary range, date posted, and direct job URLs — stored in indexed SQLite |
| **Zero Duplicates** | URL-normalized deduplication ensures clean datasets on every run |
| **Weekly Skills Reports** | Top 10 in-demand skills ranked by frequency with category breakdowns |
| **Location & Company Intel** | Distribution analysis showing where hiring is concentrated |
| **Audit Trail** | Full logging of every scrape run, deduplication event, and error |

---

## System Architecture

JobScrape Pro uses a **multi-agent swarm pattern** where each agent has a clear responsibility:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  ScraperAgent   │────▶│ DeduplicationAgent│────▶│  StorageAgent   │
│  (Fetch + Parse)│     │  (URL-based dedup) │     │  (SQLite + Index)│
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                                                          ▼
┌─────────────────┐     ┌──────────────────────────────────────────┐
│  Report Output  │◀────│            AnalysisAgent                  │
│  (.md + .json)  │     │  (Skill extraction, ranking, trends)     │
└─────────────────┘     └──────────────────────────────────────────┘
```

**Agents:**

| Agent | File | Responsibility |
|-------|------|----------------|
| `ScraperAgent` | `src/scraper.py` | HTTP fetching with retry logic, HTML/JSON parsing, pagination handling |
| `DeduplicationAgent` | `src/deduplicator.py` | URL normalization, bulk existence checking, dedup rate tracking |
| `StorageAgent` | `src/storage.py` | SQLite schema management, indexed CRUD, transaction safety |
| `AnalysisAgent` | `src/analyzer.py` | Skill taxonomy matching, frequency ranking, report generation |
| `Orchestrator` | `src/main.py` | CLI interface, swarm coordination, logging, error recovery |

---

## Quick Start

### Prerequisites

- Python 3.10+
- pip

### Installation

```bash
# Clone or copy the project
cd job_scraper

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
```

### Run Your First Scrape

```bash
# Scrape all enabled job boards
python src/main.py scrape

# Check database statistics
python src/main.py stats

# Generate analysis report
python src/main.py analyze

# Full pipeline: scrape + analyze
python src/main.py full
```

### Sample Output

```
============================================================
JOBSCRAPE PRO - DATABASE STATISTICS
============================================================
  Total jobs:        100
  Jobs this week:    100
  Unique companies:  87
  Unique locations:  42

  By source:
    remoteok              100

  Recent scrape runs:
    ✓ RemoteOK              jobs:100 pages:1 [completed]
============================================================
```

---

## Data Schema

### Jobs Table (Primary)

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment primary key |
| `title` | TEXT | Job title |
| `company` | TEXT | Hiring company name |
| `location` | TEXT | Job location (remote/onsite/hybrid) |
| `salary_range` | TEXT | Salary if disclosed |
| `date_posted` | TEXT | ISO-8601 date (YYYY-MM-DD) |
| `job_url` | TEXT UNIQUE | Direct link to posting (deduplication key) |
| `source_site` | TEXT | Origin job board |
| `raw_html` | TEXT | Raw source data for analysis |
| `scraped_at` | TIMESTAMP | When record was captured |
| `is_active` | INTEGER | Soft-delete flag (1=active) |
| `week_key` | TEXT (generated) | `YYYY-WWW` for weekly grouping |

### Indexes

- `idx_jobs_url` — URL deduplication lookups
- `idx_jobs_company` — Company filtering
- `idx_jobs_location` — Location aggregation
- `idx_jobs_scraped_at` — Time-based queries
- `idx_jobs_week_key` — Weekly report generation
- `idx_jobs_source` — Per-source statistics

---

## Supported Job Boards

| Board | Status | Parser | Notes |
|-------|--------|--------|-------|
| **RemoteOK** | ✅ Active | Built-in JSON API | 100 jobs/page via API |
| **We Work Remotely** | ⚠️ Disabled | Built-in HTML | Requires proxy rotation |
| **Your Custom Board** | 🔧 Config | Generic CSS | See configuration guide below |

### Adding a New Job Board

Edit `config.yaml` and add a new target:

```yaml
targets:
  - name: "YourJobBoard"
    base_url: "https://example.com/jobs?page={page}"
    parser_type: "generic"
    enabled: true
    max_pages: 5
    page_strategy: "path"  # Uses {page} placeholder
    selectors:
      container: ".job-card"      # CSS for each job element
      title: ".job-title"        # Job title element
      company: ".company-name"   # Company element
      location: ".job-location"  # Location element
      salary: ".salary-text"     # Salary element
      date: ".posted-date"       # Date element
      link: "a.job-link"         # URL element
```

Then run:
```bash
python src/main.py scrape --target YourJobBoard
```

---

## Scheduling (Production)

### Cron (Linux/Mac)

```bash
# Daily scrape at 6:00 AM
0 6 * * * cd /path/to/job_scraper && python src/main.py scrape >> logs/cron.log 2>&1

# Weekly analysis report every Monday at 7:00 AM
0 7 * * 1 cd /path/to/job_scraper && python src/main.py analyze >> logs/cron.log 2>&1

# Monthly database maintenance (first of month at 3 AM)
0 3 1 * * cd /path/to/job_scraper && sqlite3 data/jobs.db "VACUUM;"
```

### Windows Task Scheduler

Create a `.bat` file:
```batch
@echo off
cd C:\path\to\job_scraper
C:\path\to\python.exe src\main.py full
```

Schedule to run daily via Task Scheduler.

---

## Idempotency Guarantee

Running the scraper multiple times will **never** create duplicates:

```
# First run
Scrape complete: 100 jobs inserted

# Second run (same data)
Scrape complete: 0 jobs inserted  (100 deduplicated)

# Third run (partial new data)
Scrape complete: 12 jobs inserted  (88 deduplicated)
```

The deduplication engine normalizes URLs (strips tracking parameters, lowercases, removes fragments) and uses a UNIQUE constraint on the database for absolute integrity.

---

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JOBSCRAPE_DB` | `data/jobs.db` | SQLite database path |
| `JOBSCRAPE_LOG_LEVEL` | `INFO` | Logging verbosity |
| `JOBSCRAPE_REPORTS_DIR` | `reports` | Report output directory |

### CLI Options

```bash
python src/main.py [command] [options]

Commands:
  scrape     Fetch and store job listings
  analyze    Generate skills report from stored data
  full       Scrape + analyze in sequence
  stats      Display database statistics

Options:
  --db PATH           Custom database path
  --reports DIR       Custom reports directory
  --target NAME       Scrape only matching target
  --week KEY          Analyze specific week (YYYY-WNN)
  --log-level LEVEL   DEBUG, INFO, WARNING, ERROR
  --no-log-file       Console output only
```

---

## Pricing Model

JobScrape Pro is designed as a **client-deliverable product**. Use this guidance for client engagements:

| Tier | Price | What's Included | Best For |
|------|-------|-----------------|----------|
| **Starter** | $300/project | 1 job board, 1 week of data, basic skills report | Small agencies testing the value |
| **Professional** | $500/project | 2-3 job boards, daily scraping, weekly reports, 1 month | Recruiting teams wanting ongoing intelligence |
| **Enterprise** | $800/project | Unlimited boards, custom parsers, real-time API, 3 months | Staffing firms and large HR departments |

### Add-Ons

| Service | Price | Description |
|---------|-------|-------------|
| Custom board integration | +$150/source | Build parser for non-standard job board |
| Historical data backfill | +$200 | Scrape 3-6 months of historical postings |
| BI dashboard (Tableau/Looker) | +$400 | Visual dashboard connected to SQLite |
| Hosted API endpoint | +$300/month | REST API with authentication |

### Monthly Recurring (Ongoing Engagements)

| Plan | Monthly Price | Data Freshness | Reports |
|------|---------------|----------------|---------|
| **Monitor** | $150/mo | Weekly scrape | Monthly summary |
| **Growth** | $350/mo | Daily scrape | Weekly skills report |
| **Scale** | $600/mo | Daily + real-time alerts | Weekly + on-demand |

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| HTTP | `requests` with retry/backoff |
| Parsing | `BeautifulSoup4` (HTML), `json` (APIs) |
| Database | SQLite with WAL mode, 7 indexes |
| Analysis | Custom taxonomy (200+ skills, 10 categories) |
| Reports | Markdown + JSON output |
| Logging | Rotating file + console handlers |

---

## File Structure

```
job_scraper/
├── src/
│   ├── __init__.py           # Package init
│   ├── main.py               # Orchestrator + CLI
│   ├── scraper.py            # ScraperAgent
│   ├── deduplicator.py       # DeduplicationAgent
│   ├── storage.py            # StorageAgent (SQLite)
│   └── analyzer.py           # AnalysisAgent
├── config.yaml               # Job board configuration
├── requirements.txt          # Python dependencies
├── data/
│   └── jobs.db               # SQLite database (created on first run)
├── reports/
│   ├── LATEST_SKILLS_REPORT.md
│   └── skills_report_*.json  # Timestamped JSON exports
├── logs/
│   ├── jobscrape_*.log       # Application logs
│   └── jobscrape_errors_*.log # Error-only logs
└── README.md                 # This file
```

---

## Error Handling & Reliability

| Scenario | Handling |
|----------|----------|
| Network timeout | 3 retries with exponential backoff (2s, 4s, 8s) |
| HTTP 403/404 | Logged and skipped (no retry for client errors) |
| Malformed HTML | Graceful parsing, invalid rows skipped |
| Database lock | WAL mode + connection pooling |
| Empty pages | Stop after 2 consecutive empty pages |
| Duplicate URLs | Silently deduplicated, event logged |

---

## Sample Analysis Report

Reports are generated as both **Markdown** (human-readable) and **JSON** (machine-consumable).

### Top 10 Skills (Example)

| Rank | Skill | Mentions | % of Jobs | Category |
|------|-------|----------|-----------|----------|
| 1 | **Communication** | 32 | 32.0% | Soft Skills |
| 2 | **Golang** | 15 | 15.0% | Programming Languages |
| 3 | **Leadership** | 15 | 15.0% | Soft Skills |
| 4 | **Compliance** | 14 | 14.0% | Security |
| 5 | **Python** | 13 | 13.0% | Programming Languages |
| 6 | **Collaboration** | 12 | 12.0% | Soft Skills |
| 7 | **SQL** | 10 | 10.0% | Programming Languages |
| 8 | **PHP** | 9 | 9.0% | Programming Languages |
| 9 | **Data Science** | 7 | 7.0% | AI/ML |
| 10 | **Linux** | 7 | 7.0% | Other Tools |

Reports include category breakdowns, location distribution, top hiring companies, and methodology notes.

---

## License & Usage

This system is built for **commercial client delivery**. The codebase is designed to be:

- **Extensible**: Add new job boards in ~15 minutes
- **Maintainable**: Clear agent separation, comprehensive logging
- **Reliable**: Idempotent operations, graceful degradation
- **Professional**: Portfolio-grade reports suitable for C-suite presentation

---

## Support

For configuration help, custom integrations, or enterprise deployments:

- **Documentation**: This README + inline code comments
- **Configuration**: `config.yaml` for job boards, CLI flags for runtime behavior
- **Logs**: Check `logs/jobscrape_errors_*.log` for troubleshooting

---

*JobScrape Pro v1.0.0 — Built for production, ready for clients.*
