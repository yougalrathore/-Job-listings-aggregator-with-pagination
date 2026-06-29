"""
Analyzer Agent - Skill extraction and trend analysis for JobScrape Pro.

Responsibilities:
- Extract skills from job titles and descriptions using NLP patterns
- Rank skills by frequency across job postings
- Identify trends over time (week-over-week comparison)
- Generate job count by location and company
- Produce portfolio-grade analysis reports in Markdown and JSON
"""

import re
import json
import logging
from typing import List, Dict, Counter as CounterType, Optional, Any, Tuple
from collections import Counter
from datetime import datetime, timedelta

try:
    from src.storage import StorageAgent
except ImportError:
    from storage import StorageAgent

logger = logging.getLogger(__name__)

# Comprehensive tech skills taxonomy
SKILL_CATEGORIES = {
    "programming_languages": [
        "python", "javascript", "typescript", "java", "c++", "c#", "go", "golang",
        "rust", "ruby", "php", "swift", "kotlin", "scala", "perl", "r",
        "matlab", "dart", "lua", "haskell", "clojure", "erlang", "elixir",
        "objective-c", "shell", "bash", "powershell", "sql", "vba", "groovy",
        "julia", "coffeescript", "elm", "f#", "ocaml", "solidity", "vba",
    ],
    "frontend": [
        "react", "vue", "vue.js", "angular", "svelte", "next.js", "nuxt",
        "html", "css", "sass", "scss", "less", "bootstrap", "tailwind",
        "webpack", "vite", "rollup", "parcel", "gulp", "grunt",
        "jquery", "backbone", "ember", "knockout", "htmx", "alpine.js",
        "material-ui", "chakra-ui", "ant-design", "storybook",
    ],
    "backend": [
        "node.js", "nodejs", "express", "django", "flask", "fastapi",
        "spring", "spring boot", "laravel", "symfony", "rails", "ruby on rails",
        "asp.net", "asp.net core", "nest.js", "nestjs", "koa", "hapi",
        "gin", "echo", "fiber", "rocket", "actix", "phoenix",
        "graphql", "rest api", "restful", "soap", "grpc", "websocket",
        "microservices", "serverless", "lambda", "api gateway",
    ],
    "databases": [
        "postgresql", "postgres", "mysql", "mariadb", "sqlite",
        "mongodb", "redis", "elasticsearch", "cassandra", "dynamodb",
        "couchdb", "neo4j", "influxdb", "timescaledb", "cockroachdb",
        "firebase", "supabase", "planetscale", "prisma", "sqlalchemy",
        "mongoose", "hibernate", "typeorm", "sequelize", "orm",
    ],
    "cloud_devops": [
        "aws", "amazon web services", "azure", "gcp", "google cloud",
        "docker", "kubernetes", "k8s", "terraform", "ansible", "chef", "puppet",
        "jenkins", "gitlab ci", "github actions", "circleci", "travis ci",
        "prometheus", "grafana", "datadog", "new relic", "pagerduty",
        "nginx", "apache", "cdn", "cloudflare", "vercel", "netlify",
        "heroku", "digitalocean", "linode", "aws lambda", "ec2", "s3",
        "cloudformation", "pulumi", "vagrant", "istio", "helm",
    ],
    "ai_ml": [
        "machine learning", "deep learning", "tensorflow", "pytorch",
        "keras", "scikit-learn", "sklearn", "pandas", "numpy",
        "opencv", "hugging face", "transformers", "llm", "openai",
        "langchain", "vector database", "embedding", "nltk", "spacy",
        "keras", "xgboost", "lightgbm", "catboost", "jupyter",
        "data science", "data engineering", "etl", "data pipeline",
        "tableau", "power bi", "looker", "dbt", "snowflake",
        "airflow", "prefect", "dagster", "spark", "apache spark",
        "hadoop", "kafka", "airbyte", "fivetran", "databricks",
    ],
    "mobile": [
        "react native", "flutter", "ios", "android", "swift", "kotlin",
        "xamarin", "ionic", "cordova", "phonegap", "capacitor",
        "expo", "realm", "core data", "jetpack compose",
    ],
    "security": [
        "cybersecurity", "penetration testing", "owasp", "oauth",
        "sso", "ldap", "ssl", "tls", "encryption", "hashing",
        "jwt", "auth0", "okta", "firewall", "vpn", "soc2",
        "gdpr", "hipaa", "compliance", "vulnerability",
    ],
    "soft_skills": [
        "agile", "scrum", "kanban", "jira", "confluence",
        "leadership", "mentoring", "communication", "collaboration",
        "problem solving", "critical thinking", "project management",
        "stakeholder management", "cross-functional", "remote work",
    ],
    "other_tools": [
        "git", "github", "gitlab", "bitbucket", "jira", "confluence",
        "slack", "notion", "figma", "sketch", "adobe xd",
        "postman", "insomnia", "swagger", "openapi",
        "linux", "unix", "ubuntu", "centos", "debian",
        "wordpress", "shopify", "webflow", "framer",
        "salesforce", "hubspot", "marketo", "zendesk",
        "stripe", "paypal", "twilio", "sendgrid",
    ],
}

# Flatten all skills for matching
ALL_SKILLS = []
for category, skills in SKILL_CATEGORIES.items():
    for skill in skills:
        ALL_SKILLS.append((skill, category))

# Sort by length (longest first) to match multi-word skills before single-word
ALL_SKILLS.sort(key=lambda x: len(x[0]), reverse=True)

# Common false positives to exclude
FALSE_POSITIVES = {
    "it", "to", "go", "in", "as", "be", "by", "or", "an", "no", "so",
    "if", "up", "on", "at", "do", "he", "we", "us", "my", "me",
    "pm", "am", "hr", "ui", "ux", "ai", "jr", "sr", "ii", "iv",
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l",
    "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
}


class AnalysisAgent:
    """
    Production-grade analysis agent for extracting skills,
    computing rankings, and generating reports.
    """

    def __init__(self, storage: Optional[StorageAgent] = None):
        self.storage = storage
        logger.info("AnalysisAgent initialized")

    def extract_skills(self, text: str) -> List[Tuple[str, str]]:
        """
        Extract skills from job title/description text.
        Returns list of (skill_name, category) tuples.
        """
        if not text:
            return []

        text_lower = text.lower()
        found_skills = []
        matched_positions = set()

        for skill, category in ALL_SKILLS:
            # Use word boundary matching
            pattern = r'\b' + re.escape(skill) + r'\b'
            for match in re.finditer(pattern, text_lower):
                start = match.start()
                # Avoid overlapping matches
                if not any(start >= pos[0] and start < pos[1] for pos in matched_positions):
                    matched_positions.add((start, start + len(skill)))
                    found_skills.append((skill, category))

        return found_skills

    def analyze_jobs(self, jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyze a list of jobs and extract comprehensive statistics.
        """
        logger.info(f"Analyzing {len(jobs)} jobs")

        if not jobs:
            return self._empty_analysis()

        # Extract all skills
        all_found_skills: CounterType[str] = Counter()
        skills_by_category: Dict[str, CounterType[str]] = {
            cat: Counter() for cat in SKILL_CATEGORIES.keys()
        }
        job_skill_count = 0

        for job in jobs:
            # Combine title, company, and raw_html (contains tags/descriptions)
            text = f"{job.get('title', '')} {job.get('company', '')} {job.get('raw_html', '')}"
            skills = self.extract_skills(text)
            # Deduplicate: count each skill only once per job
            unique_skills = list(dict.fromkeys([s[0] for s in skills]))  # preserve order, remove dupes
            skill_categories = {s[0]: s[1] for s in skills}
            job_skill_count += len(unique_skills)
            for skill_name in unique_skills:
                category = skill_categories.get(skill_name, "unknown")
                all_found_skills[skill_name] += 1
                skills_by_category[category][skill_name] += 1

        # Top 10 skills overall
        top_10 = all_found_skills.most_common(10)

        # Skills by category
        category_stats = {}
        for cat, counter in skills_by_category.items():
            if counter:
                top_in_cat = counter.most_common(5)
                category_stats[cat] = {
                    "total_mentions": sum(counter.values()),
                    "unique_skills": len(counter),
                    "top_skills": [
                        {"skill": skill, "count": count, "percentage": round(count / len(jobs) * 100, 1)}
                        for skill, count in top_in_cat
                    ]
                }

        # Build result
        analysis = {
            "generated_at": datetime.now().isoformat(),
            "period": self._get_period_info(jobs),
            "total_jobs_analyzed": len(jobs),
            "total_skill_mentions": job_skill_count,
            "unique_skills_found": len(all_found_skills),
            "top_10_skills": [
                {
                    "rank": i + 1,
                    "skill": skill,
                    "count": count,
                    "percentage_of_jobs": round(count / len(jobs) * 100, 1),
                    "category": next((cat for s, cat in ALL_SKILLS if s == skill), "unknown"),
                }
                for i, (skill, count) in enumerate(top_10)
            ],
            "skills_by_category": category_stats,
            "avg_skills_per_job": round(job_skill_count / len(jobs), 2) if jobs else 0,
        }

        return analysis

    def analyze_from_storage(self, week_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Load jobs from storage and perform analysis.
        """
        if not self.storage:
            raise ValueError("Storage agent required for database analysis")

        jobs = self.storage.get_jobs_for_analysis(week_key)
        logger.info(f"Loaded {len(jobs)} jobs from storage for analysis")

        base_analysis = self.analyze_jobs(jobs)

        # Add location and company distributions
        base_analysis["location_distribution"] = self.storage.get_location_distribution(week_key)
        base_analysis["company_distribution"] = self.storage.get_company_distribution(week_key)

        return base_analysis

    def generate_markdown_report(self, analysis: Dict[str, Any], output_path: str) -> str:
        """
        Generate a professional Markdown report from analysis results.
        """
        lines = []

        # Header
        lines.append("# JobScrape Pro - Weekly Skills Analysis Report")
        lines.append("")
        lines.append(f"**Generated:** {analysis['generated_at']}")
        lines.append(f"**Period:** {analysis['period'].get('week_key', 'N/A')}")
        lines.append(f"**Total Jobs Analyzed:** {analysis['total_jobs_analyzed']:,}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Executive Summary
        lines.append("## Executive Summary")
        lines.append("")
        lines.append(
            f"This report analyzes **{analysis['total_jobs_analyzed']:,} job postings** "
            f"and identifies **{analysis['unique_skills_found']} unique skills** across "
            f"{len(SKILL_CATEGORIES)} technology categories. "
            f"On average, each job posting mentions **{analysis['avg_skills_per_job']} skills** "
            f"in its title or description."
        )
        lines.append("")
        lines.append("---")
        lines.append("")

        # Top 10 Skills
        lines.append("## Top 10 Most In-Demand Skills")
        lines.append("")
        lines.append("| Rank | Skill | Mentions | % of Jobs | Category |")
        lines.append("|------|-------|----------|-----------|----------|")

        for skill_info in analysis['top_10_skills']:
            lines.append(
                f"| {skill_info['rank']} | **{skill_info['skill'].title()}** | "
                f"{skill_info['count']:,} | {skill_info['percentage_of_jobs']}% | "
                f"{skill_info['category'].replace('_', ' ').title()} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("")

        # Category Breakdown
        if analysis.get('skills_by_category'):
            lines.append("## Skills by Category")
            lines.append("")

            for cat_name, cat_data in analysis['skills_by_category'].items():
                display_name = cat_name.replace('_', ' ').title()
                lines.append(f"### {display_name}")
                lines.append("")
                lines.append(f"- **Total Mentions:** {cat_data['total_mentions']:,}")
                lines.append(f"- **Unique Skills:** {cat_data['unique_skills']}")
                lines.append("")
                lines.append("| Skill | Mentions | % of Jobs |")
                lines.append("|-------|----------|-----------|")
                for s in cat_data['top_skills']:
                    lines.append(f"| {s['skill'].title()} | {s['count']:,} | {s['percentage']}% |")
                lines.append("")

            lines.append("---")
            lines.append("")

        # Location Distribution
        if analysis.get('location_distribution'):
            lines.append("## Job Count by Location")
            lines.append("")
            lines.append("| Location | Job Count |")
            lines.append("|----------|-----------|")
            sorted_locs = sorted(
                analysis['location_distribution'].items(),
                key=lambda x: x[1],
                reverse=True
            )[:15]  # Top 15
            for loc, count in sorted_locs:
                lines.append(f"| {loc} | {count:,} |")
            lines.append("")
            lines.append("---")
            lines.append("")

        # Company Distribution
        if analysis.get('company_distribution'):
            lines.append("## Top Hiring Companies")
            lines.append("")
            lines.append("| Company | Job Postings |")
            lines.append("|---------|-------------|")
            sorted_cos = sorted(
                analysis['company_distribution'].items(),
                key=lambda x: x[1],
                reverse=True
            )[:15]
            for company, count in sorted_cos:
                lines.append(f"| {company} | {count:,} |")
            lines.append("")
            lines.append("---")
            lines.append("")

        # Methodology
        lines.append("## Methodology")
        lines.append("")
        lines.append(
            "Skills are extracted from job titles using a comprehensive taxonomy of "
            "technology terms across 10 categories: Programming Languages, Frontend, "
            "Backend, Databases, Cloud & DevOps, AI/ML, Mobile, Security, Soft Skills, "
            "and Other Tools. The matching uses case-insensitive whole-word matching to "
            "avoid false positives."
        )
        lines.append("")
        lines.append(
            "**Data Freshness:** Jobs are scraped daily and deduplicated by URL. "
            "This report reflects the current week's job market activity."
        )
        lines.append("")

        # Footer
        lines.append("---")
        lines.append("")
        lines.append("*Report generated by JobScrape Pro v1.0.0*")
        lines.append("*For inquiries: contact@jobscrape.pro*")

        report = "\n".join(lines)

        # Write to file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)

        logger.info(f"Markdown report written to {output_path}")
        return report

    def generate_json_report(self, analysis: Dict[str, Any], output_path: str) -> str:
        """Generate JSON report from analysis results."""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"JSON report written to {output_path}")
        return json.dumps(analysis, indent=2, ensure_ascii=False, default=str)

    def _get_period_info(self, jobs: List[Dict]) -> Dict[str, str]:
        """Extract period information from job data."""
        if not jobs:
            now = datetime.now()
            return {
                "week_key": now.strftime("%Y-W%W"),
                "start_date": (now - timedelta(days=7)).strftime("%Y-%m-%d"),
                "end_date": now.strftime("%Y-%m-%d"),
            }

        dates = []
        for job in jobs:
            dp = job.get('date_posted', '')
            if dp:
                try:
                    dates.append(datetime.strptime(dp[:10], "%Y-%m-%d"))
                except ValueError:
                    pass

        if dates:
            min_date = min(dates)
            max_date = max(dates)
            return {
                "week_key": max_date.strftime("%Y-W%W"),
                "start_date": min_date.strftime("%Y-%m-%d"),
                "end_date": max_date.strftime("%Y-%m-%d"),
            }

        now = datetime.now()
        return {
            "week_key": now.strftime("%Y-W%W"),
            "start_date": (now - timedelta(days=7)).strftime("%Y-%m-%d"),
            "end_date": now.strftime("%Y-%m-%d"),
        }

    def _empty_analysis(self) -> Dict[str, Any]:
        """Return empty analysis structure."""
        return {
            "generated_at": datetime.now().isoformat(),
            "period": {},
            "total_jobs_analyzed": 0,
            "total_skill_mentions": 0,
            "unique_skills_found": 0,
            "top_10_skills": [],
            "skills_by_category": {},
            "avg_skills_per_job": 0,
            "location_distribution": {},
            "company_distribution": {},
        }
