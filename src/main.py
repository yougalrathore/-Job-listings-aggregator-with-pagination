#!/usr/bin/env python3
"""
JobScrape Pro - Main Orchestrator

Agent swarm orchestrator that coordinates scraping, deduplication,
storage, and analysis operations. Provides CLI interface for
on-demand execution and scheduler integration.

Usage:
    python src/main.py scrape              # Run full scraping pipeline
    python src/main.py analyze             # Run analysis on existing data
    python src/main.py full                # Full pipeline: scrape + analyze
    python src/main.py stats               # Show database statistics
    python src/main.py --config config/custom.yaml scrape
"""

import os
import sys
import argparse
import logging
import logging.handlers
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

# Ensure project root is in path for imports
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.storage import StorageAgent, get_storage, reset_storage
from src.scraper import ScraperAgent
from src.deduplicator import DeduplicationAgent
from src.analyzer import AnalysisAgent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = "data/jobs.db"
DEFAULT_REPORTS_DIR = "reports"
DEFAULT_LOGS_DIR = "logs"

# Target job boards configuration
DEFAULT_TARGETS = [
    {
        "name": "RemoteOK",
        "base_url": "https://remoteok.com/remote-dev-jobs",
        "parser_type": "remoteok",
        "max_pages": 5,
        "page_strategy": "query_param",
        "page_param": "page",
        "enabled": True,
    },
    {
        "name": "We Work Remotely",
        "base_url": "https://weworkremotely.com/remote-jobs/search?term=developer",
        "parser_type": "weworkremotely",
        "max_pages": 5,
        "page_strategy": "query_param",
        "page_param": "page",
        "enabled": True,
    },
]

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

def setup_logging(
    log_dir: str = DEFAULT_LOGS_DIR,
    log_level: str = "INFO",
    log_to_file: bool = True
) -> logging.Logger:
    """Configure production-grade logging with rotation."""
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    # Create logger
    logger = logging.getLogger("jobscrape")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers = []  # Clear existing handlers

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler with rotation
    if log_to_file:
        log_file = log_dir_path / f"jobscrape_{datetime.now():%Y%m%d}.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10_000_000,  # 10MB
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Also log errors separately
        error_file = log_dir_path / f"jobscrape_errors_{datetime.now():%Y%m%d}.log"
        error_handler = logging.handlers.RotatingFileHandler(
            error_file,
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8"
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)

    return logger


# ---------------------------------------------------------------------------
# Agent Swarm Orchestrator
# ---------------------------------------------------------------------------

class JobScrapeOrchestrator:
    """
    Orchestrates the multi-agent swarm for job scraping operations.
    Coordinates: ScraperAgent, DeduplicationAgent, StorageAgent, AnalysisAgent
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        reports_dir: str = DEFAULT_REPORTS_DIR,
        targets: Optional[list] = None,
    ):
        self.db_path = db_path
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.targets = targets or DEFAULT_TARGETS

        # Initialize agents
        self.storage = get_storage(db_path)
        self.scraper = ScraperAgent()
        self.dedup = DeduplicationAgent(storage=self.storage)
        self.analyzer = AnalysisAgent(storage=self.storage)

        self.logger = logging.getLogger("jobscrape.orchestrator")
        self.logger.info("JobScrapeOrchestrator initialized")

    def run_scrape(self, target_filter: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute the scraping pipeline for all enabled targets.

        Args:
            target_filter: Optional name filter to scrape only matching targets

        Returns:
            Dict with scrape statistics
        """
        start_time = time.time()
        total_stats = {
            "targets_processed": 0,
            "total_pages": 0,
            "total_jobs_found": 0,
            "total_inserted": 0,
            "total_deduplicated": 0,
            "errors": 0,
        }

        self.logger.info("=" * 60)
        self.logger.info("STARTING SCRAPE PIPELINE")
        self.logger.info("=" * 60)

        for target in self.targets:
            if not target.get("enabled", True):
                continue

            if target_filter and target_filter.lower() not in target["name"].lower():
                continue

            self.logger.info(f"--- Processing target: {target['name']} ---")

            # Start scrape run tracking
            run_id = self.storage.start_scrape_run(target["name"])
            target_errors = 0

            try:
                # Scrape pages
                jobs = self.scraper.scrape_paginated(
                    base_url=target["base_url"],
                    parser_type=target.get("parser_type", "auto"),
                    max_pages=target.get("max_pages", 5),
                    page_strategy=target.get("page_strategy", "query_param"),
                    page_param=target.get("page_param", "page"),
                )

                scraper_stats = self.scraper.get_stats()
                target_errors += scraper_stats["errors_encountered"]

                self.logger.info(f"Scraped {len(jobs)} raw jobs from {target['name']}")

                # Deduplicate
                clean_jobs, dedup_result = self.dedup.deduplicate_scraped_jobs(jobs)
                self.logger.info(
                    f"Deduplication: {dedup_result.kept_count} kept, "
                    f"{dedup_result.removed_count} removed"
                )

                # Convert to storage records and insert
                storage_records = [j.to_storage_record() for j in clean_jobs]
                inserted, skipped = self.storage.insert_jobs_bulk(storage_records)

                self.logger.info(f"Inserted {inserted} new jobs into database")

                # Update stats
                total_stats["targets_processed"] += 1
                total_stats["total_pages"] += scraper_stats["pages_scraped"]
                total_stats["total_jobs_found"] += len(jobs)
                total_stats["total_inserted"] += inserted
                total_stats["total_deduplicated"] += dedup_result.removed_count
                total_stats["errors"] += target_errors

                # Complete scrape run tracking
                self.storage.complete_scrape_run(
                    run_id=run_id,
                    pages_scraped=scraper_stats["pages_scraped"],
                    jobs_found=len(jobs),
                    jobs_inserted=inserted,
                    jobs_deduplicated=dedup_result.removed_count,
                    errors_encountered=target_errors,
                    status="completed",
                )

            except Exception as e:
                self.logger.error(f"Failed to process {target['name']}: {e}", exc_info=True)
                total_stats["errors"] += 1
                self.storage.complete_scrape_run(
                    run_id=run_id,
                    status="failed",
                    error_message=str(e),
                )

            # Reset scraper stats between targets
            self.scraper.reset_stats()

        elapsed = time.time() - start_time
        total_stats["elapsed_seconds"] = round(elapsed, 2)

        self.logger.info("=" * 60)
        self.logger.info("SCRAPE PIPELINE COMPLETE")
        self.logger.info(f"  Targets: {total_stats['targets_processed']}")
        self.logger.info(f"  Pages: {total_stats['total_pages']}")
        self.logger.info(f"  Jobs found: {total_stats['total_jobs_found']}")
        self.logger.info(f"  Jobs inserted: {total_stats['total_inserted']}")
        self.logger.info(f"  Jobs deduplicated: {total_stats['total_deduplicated']}")
        self.logger.info(f"  Errors: {total_stats['errors']}")
        self.logger.info(f"  Elapsed: {elapsed:.1f}s")
        self.logger.info("=" * 60)

        return total_stats

    def run_analysis(self, week_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Run analysis on stored job data and generate reports.

        Args:
            week_key: Optional week key filter (e.g., '2024-W25')

        Returns:
            Dict with analysis results and report paths
        """
        self.logger.info("=" * 60)
        self.logger.info("STARTING ANALYSIS PIPELINE")
        self.logger.info("=" * 60)

        # Run analysis
        analysis = self.analyzer.analyze_from_storage(week_key)

        # Generate timestamp for filenames
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        week_label = week_key or datetime.now().strftime("%Y-W%W")

        # Generate Markdown report
        md_path = self.reports_dir / f"skills_report_{week_label}_{timestamp}.md"
        self.analyzer.generate_markdown_report(analysis, str(md_path))

        # Generate JSON report
        json_path = self.reports_dir / f"skills_report_{week_label}_{timestamp}.json"
        self.analyzer.generate_json_report(analysis, str(json_path))

        # Also save as latest
        latest_md = self.reports_dir / "LATEST_SKILLS_REPORT.md"
        self.analyzer.generate_markdown_report(analysis, str(latest_md))

        self.logger.info("=" * 60)
        self.logger.info("ANALYSIS PIPELINE COMPLETE")
        self.logger.info(f"  Jobs analyzed: {analysis['total_jobs_analyzed']}")
        self.logger.info(f"  Unique skills: {analysis['unique_skills_found']}")
        self.logger.info(f"  Top skill: {analysis['top_10_skills'][0]['skill'] if analysis['top_10_skills'] else 'N/A'}")
        self.logger.info(f"  Reports saved:")
        self.logger.info(f"    - {md_path}")
        self.logger.info(f"    - {json_path}")
        self.logger.info(f"    - {latest_md}")
        self.logger.info("=" * 60)

        return {
            "analysis": analysis,
            "report_paths": {
                "markdown": str(md_path),
                "json": str(json_path),
                "latest": str(latest_md),
            }
        }

    def show_stats(self) -> Dict[str, Any]:
        """Display current database statistics."""
        stats = self.storage.get_stats()

        print("\n" + "=" * 60)
        print("JOBSCRAPE PRO - DATABASE STATISTICS")
        print("=" * 60)
        print(f"  Total jobs:        {stats['total_jobs']:,}")
        print(f"  Jobs this week:    {stats['jobs_this_week']:,}")
        print(f"  Unique companies:  {stats['unique_companies']:,}")
        print(f"  Unique locations:  {stats['unique_locations']:,}")
        print(f"\n  By source:")
        for source, count in stats['by_source'].items():
            print(f"    {source:20s} {count:,}")

        if stats['recent_runs']:
            print(f"\n  Recent scrape runs:")
            for run in stats['recent_runs'][:5]:
                status_icon = "✓" if run['status'] == 'completed' else "✗"
                print(f"    {status_icon} {run['source_site']:20s} "
                      f"jobs:{run['jobs_inserted'] or 0} "
                      f"pages:{run['pages_scraped'] or 0} "
                      f"[{run['status']}]")

        print("=" * 60 + "\n")

        return stats

    def run_full_pipeline(self, target_filter: Optional[str] = None) -> Dict[str, Any]:
        """Execute complete pipeline: scrape + analyze."""
        scrape_results = self.run_scrape(target_filter)
        analysis_results = self.run_analysis()

        return {
            "scrape": scrape_results,
            "analysis": analysis_results,
        }


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser for CLI."""
    parser = argparse.ArgumentParser(
        prog="jobscrape",
        description="JobScrape Pro - Production-grade job listings aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s scrape                    # Scrape all enabled targets
  %(prog)s scrape --target remoteok  # Scrape only RemoteOK
  %(prog)s analyze                   # Run analysis on stored data
  %(prog)s full                      # Full pipeline: scrape + analyze
  %(prog)s stats                     # Show database statistics
  %(prog)s --db ./myjobs.db full     # Use custom database
        """
    )

    parser.add_argument(
        "command",
        choices=["scrape", "analyze", "full", "stats"],
        help="Command to execute"
    )

    parser.add_argument(
        "--db", "--database",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})"
    )

    parser.add_argument(
        "--reports",
        default=DEFAULT_REPORTS_DIR,
        help=f"Directory for reports (default: {DEFAULT_REPORTS_DIR})"
    )

    parser.add_argument(
        "--target",
        help="Filter to specific target by name"
    )

    parser.add_argument(
        "--week",
        help="Week key for analysis (e.g., 2024-W25)"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level"
    )

    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Disable file logging (console only)"
    )

    return parser


def main():
    """Main entry point for the CLI."""
    parser = create_parser()
    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(
        log_level=args.log_level,
        log_to_file=not args.no_log_file
    )

    logger.info(f"JobScrape Pro v1.0.0 - Command: {args.command}")

    # Initialize orchestrator
    orchestrator = JobScrapeOrchestrator(
        db_path=args.db,
        reports_dir=args.reports,
    )

    # Execute command
    try:
        if args.command == "scrape":
            results = orchestrator.run_scrape(target_filter=args.target)
            print(f"\nScrape complete: {results['total_inserted']} jobs inserted")

        elif args.command == "analyze":
            results = orchestrator.run_analysis(week_key=args.week)
            analysis = results["analysis"]
            print(f"\nAnalysis complete:")
            print(f"  Jobs analyzed: {analysis['total_jobs_analyzed']}")
            print(f"  Unique skills: {analysis['unique_skills_found']}")
            if analysis['top_10_skills']:
                print(f"\n  Top 3 skills:")
                for s in analysis['top_10_skills'][:3]:
                    print(f"    {s['rank']}. {s['skill'].title()} ({s['count']} mentions)")
            print(f"\n  Reports saved to: {results['report_paths']['latest']}")

        elif args.command == "full":
            results = orchestrator.run_full_pipeline(target_filter=args.target)
            scrape = results["scrape"]
            analysis = results["analysis"]["analysis"]
            print(f"\nPipeline complete:")
            print(f"  Scraped: {scrape['total_inserted']} jobs from {scrape['targets_processed']} targets")
            print(f"  Analysis: {analysis['unique_skills_found']} unique skills found")

        elif args.command == "stats":
            orchestrator.show_stats()

    except KeyboardInterrupt:
        logger.info("Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("JobScrape Pro completed successfully")


if __name__ == "__main__":
    main()
