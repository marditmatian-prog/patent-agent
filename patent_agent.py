"""
Patent Monitor Agent
====================
Monitors competitors across GitHub, Google Patents, USPTO, EPO, and arXiv.
Analyzes findings with Claude and stores results in SQLite.

Usage:
    python patent_agent.py                  # run once
    python patent_agent.py --schedule 24    # run every 24 hours
    python patent_agent.py --report         # print latest report
"""

import os
import json
import time
import sqlite3
import logging
import argparse
import httpx
import requests
import arxiv
import schedule
import anthropic

from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG  — edit or move to .env
# ─────────────────────────────────────────────
CONFIG = {
    "github_token":    os.getenv("GITHUB_TOKEN", ""),
    "serpapi_key":     os.getenv("SERPAPI_KEY", ""),
    "epo_key":         os.getenv("EPO_KEY", ""),
    "epo_secret":      os.getenv("EPO_SECRET", ""),
    "anthropic_key":   os.getenv("ANTHROPIC_API_KEY", ""),

    # ← Customize these
    "competitors":     os.getenv("COMPETITORS", "advent-technologies,ballard-power-systems,toyota,plug-power,johnson-matthey").split(","),
    "keywords":        os.getenv("KEYWORDS", "PEM fuel cell,proton exchange membrane,HT-PEM,LT-PEM,polybenzimidazole,MEA membrane electrode,phosphoric acid doping,Nafion membrane,GDL gas diffusion,bipolar plate,platinum catalyst,fuel cell degradation").split(","),
    "your_technology": os.getenv("YOUR_TECHNOLOGY", "Low-temperature and high-temperature PEM fuel cell stack and MEA development, including PBI-based HT-PEM membranes and Nafion-based LT-PEM systems"),
    "db_path":         "reports/patent_monitor.db",
}


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                title       TEXT,
                url         TEXT,
                assignee    TEXT,
                published   TEXT,
                abstract    TEXT,
                raw_json    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_date  TEXT,
                high_risk   TEXT,
                watch_list  TEXT,
                opportunities TEXT,
                summary     TEXT,
                total_findings INTEGER,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_source ON findings(source);
            CREATE INDEX IF NOT EXISTS idx_created ON findings(created_at);
        """)
        self.conn.commit()

    def insert_finding(self, source: str, item: dict):
        self.conn.execute("""
            INSERT OR IGNORE INTO findings (source, title, url, assignee, published, abstract, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            source,
            item.get("title", ""),
            item.get("url", item.get("pdf_url", "")),
            item.get("assignee", item.get("org", "")),
            str(item.get("published", item.get("patent_date", ""))),
            item.get("abstract", item.get("summary", "")),
            json.dumps(item)
        ))
        self.conn.commit()

    def insert_report(self, report: dict, total: int):
        self.conn.execute("""
            INSERT INTO reports (cycle_date, high_risk, watch_list, opportunities, summary, total_findings)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            json.dumps(report.get("high_risk", [])),
            json.dumps(report.get("watch_list", [])),
            json.dumps(report.get("opportunities", [])),
            report.get("summary", ""),
            total
        ))
        self.conn.commit()

    def recent_findings(self, days: int = 1) -> list:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "SELECT source, title, url, assignee, abstract FROM findings WHERE created_at > ?", (since,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def latest_report(self) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM reports ORDER BY created_at DESC LIMIT 1"
        )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


# ─────────────────────────────────────────────
# GITHUB WATCHER
# ─────────────────────────────────────────────
class GitHubWatcher:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"
        } if token else {"Accept": "application/vnd.github+json"}
        self.base = "https://api.github.com"

    def search_prior_art(self, keywords: list[str], since_days: int = 7) -> list:
        date_from = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        query = " ".join(keywords) + f" pushed:>{date_from}"
        try:
            r = requests.get(f"{self.base}/search/repositories",
                headers=self.headers,
                params={"q": query, "sort": "updated", "per_page": 20},
                timeout=10)
            items = r.json().get("items", [])
            return [{
                "title": i["full_name"],
                "url": i["html_url"],
                "abstract": i.get("description", ""),
                "published": i.get("pushed_at", ""),
                "assignee": i.get("owner", {}).get("login", ""),
                "stars": i.get("stargazers_count", 0)
            } for i in items]
        except Exception as e:
            log.warning(f"GitHub search failed: {e}")
            return []

    def watch_competitor_org(self, org: str) -> list:
        try:
            repos = requests.get(f"{self.base}/orgs/{org}/repos",
                headers=self.headers,
                params={"sort": "pushed", "per_page": 10},
                timeout=10).json()
            if not isinstance(repos, list):
                return []
            results = []
            for repo in repos[:5]:
                r = requests.get(f"{self.base}/repos/{org}/{repo['name']}/releases/latest",
                    headers=self.headers, timeout=10)
                if r.status_code == 200:
                    rel = r.json()
                    results.append({
                        "title": f"{org}/{repo['name']} — {rel.get('tag_name','')}",
                        "url": rel.get("html_url", ""),
                        "abstract": rel.get("body", "")[:500],
                        "published": rel.get("published_at", ""),
                        "assignee": org
                    })
            return results
        except Exception as e:
            log.warning(f"GitHub org watch failed for {org}: {e}")
            return []


# ─────────────────────────────────────────────
# GOOGLE PATENTS (via SerpAPI)
# ─────────────────────────────────────────────
class GooglePatentsWatcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str = "", assignee: str = None, after_date: str = None) -> list:
        if not self.api_key:
            log.warning("SerpAPI key not set — skipping Google Patents")
            return []
        try:
            params = {"engine": "google_patents", "api_key": self.api_key, "num": 20}
            if query:    params["q"] = query
            if assignee: params["assignee"] = assignee
            if after_date: params["after"] = f"publication:{after_date}"

            r = requests.get("https://serpapi.com/search", params=params, timeout=15)
            results = r.json().get("organic_results", [])
            return [{
                "title": p.get("title", ""),
                "url": p.get("patent_link", ""),
                "abstract": p.get("snippet", ""),
                "published": p.get("filing_date", ""),
                "assignee": p.get("assignee", assignee or "")
            } for p in results]
        except Exception as e:
            log.warning(f"Google Patents failed: {e}")
            return []

    def monitor_competitor(self, company: str) -> list:
        year = datetime.now().year
        return self.search(assignee=company, after_date=f"{year}0101")


# ─────────────────────────────────────────────
# USPTO (PatentsView API — free, no key needed)
# ─────────────────────────────────────────────
class USPTOWatcher:
    BASE = "https://api.patentsview.org/patents/query"

    def search_by_assignee(self, company: str, after_date: str = None) -> list:
        if not after_date:
            after_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        try:
            payload = {
                "q": {"_and": [
                    {"_contains": {"assignee_organization": company}},
                    {"_gte": {"patent_date": after_date}}
                ]},
                "f": ["patent_id", "patent_title", "patent_abstract",
                      "patent_date", "assignee_organization"],
                "o": {"per_page": 25}
            }
            r = httpx.post(self.BASE, json=payload, timeout=15)
            patents = r.json().get("patents") or []
            return [{
                "title": p.get("patent_title", ""),
                "url": f"https://patents.google.com/patent/US{p.get('patent_id','')}",
                "abstract": p.get("patent_abstract", "")[:600],
                "published": p.get("patent_date", ""),
                "assignee": company
            } for p in patents]
        except Exception as e:
            log.warning(f"USPTO search failed for {company}: {e}")
            return []

    def search_by_keywords(self, keywords: list[str]) -> list:
        query_str = " ".join(keywords)
        try:
            payload = {
                "q": {"_text_any": {"patent_abstract": query_str}},
                "f": ["patent_id", "patent_title", "patent_abstract",
                      "patent_date", "assignee_organization"],
                "o": {"per_page": 20}
            }
            r = httpx.post(self.BASE, json=payload, timeout=15)
            patents = r.json().get("patents") or []
            return [{
                "title": p.get("patent_title", ""),
                "url": f"https://patents.google.com/patent/US{p.get('patent_id','')}",
                "abstract": p.get("patent_abstract", "")[:600],
                "published": p.get("patent_date", ""),
                "assignee": (p.get("assignees") or [{}])[0].get("assignee_organization", "")
            } for p in patents]
        except Exception as e:
            log.warning(f"USPTO keyword search failed: {e}")
            return []


# ─────────────────────────────────────────────
# EPO (OPS API)
# ─────────────────────────────────────────────
class EPOWatcher:
    BASE = "https://ops.epo.org/3.2/rest-services"

    def __init__(self, key: str, secret: str):
        self.token = None
        if key and secret:
            self.token = self._authenticate(key, secret)

    def _authenticate(self, key: str, secret: str) -> str | None:
        try:
            r = httpx.post("https://ops.epo.org/3.2/auth/accesstoken",
                data={"grant_type": "client_credentials"},
                auth=(key, secret), timeout=10)
            return r.json().get("access_token")
        except Exception as e:
            log.warning(f"EPO auth failed: {e}")
            return None

    def search_applicant(self, company: str) -> list:
        if not self.token:
            log.warning("EPO token not available — skipping EPO")
            return []
        try:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json"
            }
            r = httpx.get(f"{self.BASE}/published-data/search/biblio",
                params={"q": f"pa={company}", "Range": "1-10"},
                headers=headers, timeout=15)
            data = r.json()
            entries = (data.get("ops:world-patent-data", {})
                          .get("ops:biblio-search", {})
                          .get("ops:search-result", {})
                          .get("ops:publication-reference", []))
            if isinstance(entries, dict):
                entries = [entries]
            results = []
            for e in entries:
                doc_id = e.get("document-id", {})
                num = doc_id.get("doc-number", {}).get("$", "")
                results.append({
                    "title": f"EP Patent — {num}",
                    "url": f"https://worldwide.espacenet.com/patent/search/family/{num}/",
                    "abstract": "",
                    "published": doc_id.get("date", {}).get("$", ""),
                    "assignee": company
                })
            return results
        except Exception as e:
            log.warning(f"EPO search failed: {e}")
            return []


# ─────────────────────────────────────────────
# ARXIV WATCHER
# ─────────────────────────────────────────────
class ArxivWatcher:
    def search_recent(self, keywords: list[str], max_results: int = 15) -> list:
        query = " AND ".join(f'"{kw}"' for kw in keywords)
        try:
            search = arxiv.Search(
                query=query,
                max_results=max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending
            )
            return [{
                "title": r.title,
                "url": r.pdf_url,
                "abstract": r.summary[:600],
                "published": str(r.published),
                "assignee": ", ".join(a.name for a in r.authors[:3])
            } for r in search.results()]
        except Exception as e:
            log.warning(f"arXiv search failed: {e}")
            return []


# ─────────────────────────────────────────────
# LLM ANALYZER
# ─────────────────────────────────────────────
class PatentAnalyzer:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None

    def analyze(self, findings: list[dict], your_tech: str) -> dict:
        if not self.client:
            log.warning("Anthropic key not set — skipping AI analysis")
            return {
                "high_risk": [], "watch_list": [], "opportunities": [],
                "summary": "AI analysis skipped (no API key configured)."
            }

        # Trim findings for prompt
        trimmed = [{
            "source": f.get("source", ""),
            "title": f.get("title", ""),
            "assignee": f.get("assignee", ""),
            "abstract": (f.get("abstract", "") or "")[:300]
        } for f in findings[:60]]

        prompt = f"""You are a senior patent intelligence analyst.

Your company's technology: {your_tech}

Below are {len(trimmed)} recent findings from GitHub, USPTO, EPO, Google Patents, and arXiv.
Analyze them and respond ONLY with valid JSON (no markdown, no preamble) in this exact shape:

{{
  "high_risk": [
    {{"title": "...", "reason": "...", "source": "..."}}
  ],
  "watch_list": [
    {{"title": "...", "reason": "...", "source": "..."}}
  ],
  "opportunities": [
    {{"area": "...", "rationale": "..."}}
  ],
  "summary": "2-3 sentence executive summary"
}}

Findings:
{json.dumps(trimmed, indent=2)}
"""
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except Exception as e:
            log.error(f"Analysis failed: {e}")
            return {
                "high_risk": [], "watch_list": [], "opportunities": [],
                "summary": f"Analysis error: {e}"
            }


# ─────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────
class PatentMonitorAgent:
    def __init__(self, config: dict):
        self.config   = config
        self.db       = Database(config["db_path"])
        self.github   = GitHubWatcher(config["github_token"])
        self.gpatents = GooglePatentsWatcher(config["serpapi_key"])
        self.uspto    = USPTOWatcher()
        self.epo      = EPOWatcher(config["epo_key"], config["epo_secret"])
        self.arxiv    = ArxivWatcher()
        self.analyzer = PatentAnalyzer(config["anthropic_key"])

    def run_cycle(self):
        log.info("═" * 50)
        log.info("Starting patent monitoring cycle")
        log.info("═" * 50)

        all_findings = []

        # ── GitHub ──────────────────────────────────
        log.info("🔍 GitHub: scanning competitor orgs...")
        for org in self.config["competitors"]:
            items = self.github.watch_competitor_org(org)
            for item in items:
                item["source"] = "github"
                self.db.insert_finding("github", item)
            all_findings += items
            log.info(f"   {org}: {len(items)} releases found")

        log.info("🔍 GitHub: searching prior art keywords...")
        items = self.github.search_prior_art(self.config["keywords"])
        for item in items:
            item["source"] = "github"
            self.db.insert_finding("github", item)
        all_findings += items
        log.info(f"   {len(items)} repos found")

        # ── Google Patents ───────────────────────────
        log.info("🔍 Google Patents: scanning competitors...")
        for org in self.config["competitors"]:
            items = self.gpatents.monitor_competitor(org)
            for item in items:
                item["source"] = "google_patents"
                self.db.insert_finding("google_patents", item)
            all_findings += items
            log.info(f"   {org}: {len(items)} patents found")

        # ── USPTO ────────────────────────────────────
        log.info("🔍 USPTO: scanning competitors...")
        for org in self.config["competitors"]:
            items = self.uspto.search_by_assignee(org)
            for item in items:
                item["source"] = "uspto"
                self.db.insert_finding("uspto", item)
            all_findings += items
            log.info(f"   {org}: {len(items)} patents found")

        log.info("🔍 USPTO: keyword search...")
        items = self.uspto.search_by_keywords(self.config["keywords"])
        for item in items:
            item["source"] = "uspto"
            self.db.insert_finding("uspto", item)
        all_findings += items
        log.info(f"   {len(items)} patents found")

        # ── EPO ──────────────────────────────────────
        log.info("🔍 EPO: scanning competitors...")
        for org in self.config["competitors"]:
            items = self.epo.search_applicant(org)
            for item in items:
                item["source"] = "epo"
                self.db.insert_finding("epo", item)
            all_findings += items
            log.info(f"   {org}: {len(items)} patents found")

        # ── arXiv ────────────────────────────────────
        log.info("🔍 arXiv: scanning recent papers...")
        items = self.arxiv.search_recent(self.config["keywords"])
        for item in items:
            item["source"] = "arxiv"
            self.db.insert_finding("arxiv", item)
        all_findings += items
        log.info(f"   {len(items)} papers found")

        # ── Analyze ──────────────────────────────────
        log.info(f"🤖 Analyzing {len(all_findings)} findings with Claude...")
        report = self.analyzer.analyze(all_findings, self.config["your_technology"])
        self.db.insert_report(report, len(all_findings))

        # ── Save JSON report ─────────────────────────
        report_path = f"reports/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_path, "w") as f:
            json.dump({
                "generated_at": datetime.now().isoformat(),
                "total_findings": len(all_findings),
                "analysis": report,
                "findings": all_findings
            }, f, indent=2, default=str)

        log.info(f"✅ Done. {len(all_findings)} findings. Report saved: {report_path}")
        log.info(f"   Summary: {report.get('summary','')}")
        log.info(f"   High risk: {len(report.get('high_risk',[]))} | Watch: {len(report.get('watch_list',[]))} | Opportunities: {len(report.get('opportunities',[]))}")
        return report

    def print_latest_report(self):
        r = self.db.latest_report()
        if not r:
            print("No reports found. Run the agent first.")
            return
        print(f"\n{'═'*60}")
        print(f"LATEST PATENT INTELLIGENCE REPORT")
        print(f"Generated: {r['created_at']}")
        print(f"Total findings: {r['total_findings']}")
        print(f"{'═'*60}")
        print(f"\n📋 SUMMARY\n{r['summary']}")
        print(f"\n🚨 HIGH RISK ({len(json.loads(r['high_risk']))})")
        for item in json.loads(r['high_risk']):
            print(f"  • {item.get('title','')} — {item.get('reason','')}")
        print(f"\n👁  WATCH LIST ({len(json.loads(r['watch_list']))})")
        for item in json.loads(r['watch_list']):
            print(f"  • {item.get('title','')} — {item.get('reason','')}")
        print(f"\n💡 OPPORTUNITIES ({len(json.loads(r['opportunities']))})")
        for item in json.loads(r['opportunities']):
            print(f"  • {item.get('area','')} — {item.get('rationale','')}")
        print(f"{'═'*60}\n")

    def start_scheduler(self, hours: int = 24):
        log.info(f"Scheduler started — running every {hours}h")
        self.run_cycle()
        schedule.every(hours).hours.do(self.run_cycle)
        while True:
            schedule.run_pending()
            time.sleep(60)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Patent Monitor Agent")
    parser.add_argument("--schedule", type=int, metavar="HOURS",
                        help="Run on schedule every N hours")
    parser.add_argument("--report", action="store_true",
                        help="Print latest report and exit")
    args = parser.parse_args()

    agent = PatentMonitorAgent(CONFIG)

    if args.report:
        agent.print_latest_report()
    elif args.schedule:
        agent.start_scheduler(args.schedule)
    else:
        agent.run_cycle()
