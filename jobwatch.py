#!/usr/bin/env python3
"""
jobwatch.py - watcher for 2026 Fall intern / new-grad / entry-level roles

监控来源:
  - Greenhouse (public JSON API, most reliable)
  - Lever     (public JSON API, most reliable)
  - Workday   (per-company subdomain, configure individually)
  - LinkedIn  (guest endpoint, may get rate-limited)
  - Indeed    (RSS, may break)

Flow: pull all sources -> keyword filter -> diff against last run -> only push NEW jobs.
Run every 30 min via cron / scheduled task.

Deps: pip install requests beautifulsoup4 lxml
"""

import json
import os
import re
import sys
import time
import hashlib
import sqlite3
import smtplib
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ============================================================
# 1. Config - edit here
# ============================================================

# --- Keyword filter ---
# Role keywords: word-boundary match to avoid false hits like "Internal"/"International".
# Covers software-first roles plus adjacent technical / engineering roles.
ROLE_RE = re.compile(
    r"\b(software|developer|engineer(ing)?|programmer|full[\s-]*stack|backend|"
    r"front[\s-]*end|frontend|platform|cloud|devops|sre|data|machine\s*learning|"
    r"ml|ai|security|qa|quality\s*assurance|test|systems?|technical|technology|"
    r"it|analyst)\b|实习",
    re.I,
)
# Term signal words (Fall 2026). Hitting any one counts as the target term.
TERM_RE = re.compile(r"\b(2026|fall|autumn|september|sept|sep|new\s*grad)\b", re.I)
# Explicitly belongs to another term -> drop it.
OTHER_TERM_RE = re.compile(r"\b(summer|spring|winter)\s*20(25|27)\b|\b2025\b|\b2027\b", re.I)
EXCLUDE = [
    "phd only",
    "canadian citizenship required",
    "must be a canadian citizen",
    "must be canadian citizen",
    "canadian citizens only",
    "requires canadian citizenship",
    "french required",
    "must speak french",
    "fluent in french",
    "bilingual french",
]

# Filter mode:
#   "strict" = must be a target role AND mention 2026/fall
#   "loose"  = target role and not tagged as another term (best early in the cycle)
FILTER_MODE = "loose"

# --- Freshness (alert mode) ---
# Each alert run only notifies about jobs whose minute-precise posting time
# falls within this window. The dedup DB still prevents repeats if a job appears
# in overlapping runs.
ALERT_WINDOW_MINUTES = 120

# --- Daily digest ---
# A separate "digest" run (meant for ~midnight) summarizes everything posted
# during the day, regardless of whether it was already alerted. It does NOT
# touch the dedup DB, so it never interferes with alert mode.
DIGEST_LOOKBACK_HOURS = 24
JOBWATCH_TIMEZONE = ZoneInfo("America/Toronto")

# --- Location filter ---
# Two modes:
#   "blacklist" = drop jobs whose location matches LOCATION_EXCLUDE
#   "whitelist" = keep ONLY jobs whose location matches LOCATION_INCLUDE
# Whitelist is more reliable for "Canada + China + remote, no US" because you
# can't enumerate every US city, but you CAN enumerate the places you want.
LOCATION_MODE = "whitelist"

# Whitelist: keep a job only if its location contains any of these.
# Canada only (no remote, no China) per your request.
LOCATION_INCLUDE = [
    "canada", "ontario", "quebec", "british columbia", "alberta",
    "manitoba", "saskatchewan", "nova scotia", "new brunswick",
    "toronto", "vancouver", "montreal", "ottawa", "waterloo", "kitchener",
    "calgary", "edmonton", "mississauga", "hamilton", "halifax", "winnipeg",
    "victoria", "kingston", "oshawa", "oakville", "burnaby", "markham",
    "richmond hill", "brampton", "guelph", "windsor", "regina", "saskatoon",
    ", on", ", bc", ", qc", ", ab", ", mb", ", sk", ", ns", ", nb", ", nl",
]
# Blacklist (only used when LOCATION_MODE == "blacklist").
LOCATION_EXCLUDE = [
    "united states", "usa", "u.s.", "u.s.a", ", us",
    "california", "new york", "san francisco", "seattle", "austin",
    "boston", "chicago", "atlanta", "denver", "los angeles", "texas",
    "washington", "remote - us", "us-remote", "us remote",
]
# If a location string is empty/unknown: under whitelist we DROP it (could be US
# with a blank field). Set True only if you'd rather keep unknowns.
KEEP_UNKNOWN_LOCATION = False

# --- Community repos (Simplify / Vansh listings.json) ---
# These aggregate tens of thousands of postings scraped from company career
# pages. We read their raw JSON directly = their coverage UNION your own ATS.
# Set to [] to disable. Each entry: (label, raw_json_url)
COMMUNITY_REPOS = [
    ("Simplify-Intern",
     "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("Simplify-NewGrad",
     "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"),
    ("Vansh-Intern",
     "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json"),
]
# Only keep community postings newer than this many days (avoid back-flooding
# with thousands of old entries on first run). Set to 0 for no age limit.
COMMUNITY_MAX_AGE_DAYS = 14

# --- Public company career pages ---
# BambooHR entries use (company_name, company_subdomain).
BAMBOOHR_COMPANIES = [
    ("Signal 1", "signal1"),
    ("Ludia", "ludia"),
    ("Carbon60", "carbon60"),
]

# Generic entries use (company_name, public_careers_url).
GENERIC_CAREER_SITES = [
    # ("Example Company", "https://example.com/careers"),
]

# Limit detail-page requests from each configured careers page.
CAREER_DETAIL_PAGE_LIMIT = 50

# --- Greenhouse: board token (the slug in the careers-page URL) ---
# e.g. https://boards.greenhouse.io/stripe -> "stripe"
# Some startup slugs may move or disappear; failures are logged and skipped.
GREENHOUSE_COMPANIES = [
    "stripe", "databricks", "airbnb", "robinhood", "coinbase", "instacart",
    "samsara", "figma", "brex", "gusto", "flexport", "affirm", "reddit",
    "pinterest", "dropbox", "asana", "twitch", "cloudflare", "datadog",
    "elastic", "gitlab", "okta", "twilio", "sofi", "chime", "faire", "vercel",
    "anthropic", "airtable", "attentive", "webflow", "calendly", "duolingo",
    "discord", "roblox", "nuro", "wayve", "verkada", "waymo", "lyft",
    "sigmacomputing", "mixpanel", "amplitude", "coursera", "khanacademy",
    "nubank", "adyen", "monzo", "n26", "gocardless", "betterment", "marqeta",
    "toast", "block", "project44", "phonepe", "groww", "postman",
    "1password", "wealthsimple", "koho", "stackadapt", "benchscience",
    "benchsci", "ecobee", "loopio", "vidyard", "applyboard", "clearco",
    "klue", "wave", "freshbooks", "ramp", "rippling", "plaid", "notion",
    "retool", "zapier", "segment", "hashicorp", "mongodb", "canva",
    "miro", "loom", "linear", "mercury", "cashapp", "doordash",
    "super", "coda", "intercom", "pleo", "bolt",
    "checkout", "checkoutcom", "supabase", "huggingface",
    "scaleai", "cohere", "wandb", "weightsandbiases",
]

# --- Lever: same idea, fill in the company slug ---
# e.g. https://jobs.lever.co/netflix -> "netflix"
# Some slugs may move or disappear; failures are logged and skipped.
LEVER_COMPANIES = [
    "palantir", "spotify", "mistral", "shieldai", "matchgroup",
    "outreach", "highspot", "people-ai", "tala", "wealthfront",
    "alloy", "velo3d", "whoop", "15five", "angellist",
    "wealthsimple", "shopify", "ecobee", "clearco", "borrowell",
    "ada", "humi", "miovision", "geotab", "mappedin", "koho",
    "stackadapt", "loopio", "vidyard", "applyboard", "league",
    "pointclickcare", "automattic", "zapier", "gitlab", "mongodb",
    "cockroachlabs", "grafana", "sentry", "launchdarkly", "posthog",
    "sourcegraph", "mattermost", "webflow", "rippling", "ramp",
    "mercury", "brex", "plaid", "notion", "airtable", "retool",
]

# --- Ashby: common with startups ---
# e.g. https://jobs.ashbyhq.com/cohere -> "cohere"
ASHBY_COMPANIES = [
    "cohere", "openai", "anthropic", "perplexity", "cursor", "linear",
    "mercury", "ramp", "retool", "notion", "airtable", "vercel",
    "supabase", "huggingface", "weightsandbiases", "wandb", "modal",
    "runway", "pika", "elevenlabs", "mistral", "poolside", "replicate",
    "browserbase", "turso", "neon", "railway", "render", "tailscale",
    "incidentio", "posthog", "sentry", "sourcegraph", "grafana",
    "deepmind", "scaleai", "adept", "harvey", "gretel", "modal-labs",
]

# --- Workday: 每家独立, 格式 (公司名, 子域host, tenant, 站点路径) ---
# Careers URL looks like https://<host>/wday/cxs/<tenant>/<site>/jobs
# e.g. NVIDIA -> ("NVIDIA","nvidia.wd5.myworkdayjobs.com","nvidia","NVIDIAExternalCareerSite")
WORKDAY_COMPANIES = [
    ("NVIDIA",     "nvidia.wd5.myworkdayjobs.com",      "nvidia",     "NVIDIAExternalCareerSite"),
    ("Salesforce", "salesforce.wd12.myworkdayjobs.com", "salesforce", "External_Career_Site"),
    ("Adobe",      "adobe.wd5.myworkdayjobs.com",        "adobe",      "external_experienced"),
    ("HP",         "hp.wd5.myworkdayjobs.com",           "hp",         "ExternalCareerSite"),
    ("PayPal",     "paypal.wd1.myworkdayjobs.com",       "paypal",     "jobs"),
    ("Autodesk",   "autodesk.wd1.myworkdayjobs.com",     "autodesk",   "Ext"),
    ("Sony",       "sonyglobal.wd1.myworkdayjobs.com",   "sonyglobal", "SonyGlobalCareers"),
    ("Mastercard", "mastercard.wd1.myworkdayjobs.com",   "mastercard", "CorporateCareers"),
    ("TD Bank",    "td.wd3.myworkdayjobs.com",           "td",         "TD_Bank_Careers"),
    ("Workday",    "workday.wd5.myworkdayjobs.com",       "workday",    "Workday"),
]

# --- LinkedIn search keywords / location ---
# Keep these broad. LinkedIn search works better with short keyword groups;
# detailed term/duration/location rules are enforced by the filters below.
LINKEDIN_QUERIES = [
    ("software intern", "Canada"),
    ("developer intern", "Canada"),
    ("engineering intern", "Canada"),
    ("engineering student", "Canada"),
    ("software co-op", "Canada"),
    ("developer co-op", "Canada"),
    ("engineering co-op", "Canada"),
    ("computer science intern", "Canada"),
    ("computer engineering intern", "Canada"),
    ("data intern", "Canada"),
    ("data analyst intern", "Canada"),
    ("qa intern", "Canada"),
    ("quality assurance intern", "Canada"),
    ("test engineering intern", "Canada"),
    ("cloud intern", "Canada"),
    ("devops intern", "Canada"),
    ("security intern", "Canada"),
    ("IT intern", "Canada"),
    ("technology intern", "Canada"),
    ("technical analyst intern", "Canada"),
    ("new grad engineering", "Canada"),
    ("new grad software", "Canada"),
    ("new grad developer", "Canada"),
    ("new graduate technology", "Canada"),
    ("junior software", "Canada"),
    ("junior developer", "Canada"),
    ("entry level software", "Canada"),
    ("entry level developer", "Canada"),
    ("entry level technology", "Canada"),
    ("technology analyst new grad", "Canada"),
]

# --- Indeed search keywords / location ---
# Keep these broad. Indeed often returns better results with simple keyword
# combinations, then the script filters the details.
INDEED_QUERIES = [
    ("software intern", "Canada"),
    ("developer intern", "Canada"),
    ("engineering intern", "Canada"),
    ("engineering student", "Canada"),
    ("software co-op", "Canada"),
    ("developer co-op", "Canada"),
    ("engineering co-op", "Canada"),
    ("computer science intern", "Canada"),
    ("computer engineering intern", "Canada"),
    ("data intern", "Canada"),
    ("data analyst intern", "Canada"),
    ("qa intern", "Canada"),
    ("quality assurance intern", "Canada"),
    ("test engineering intern", "Canada"),
    ("cloud intern", "Canada"),
    ("devops intern", "Canada"),
    ("cybersecurity intern", "Canada"),
    ("security intern", "Canada"),
    ("IT intern", "Canada"),
    ("technology intern", "Canada"),
    ("technical analyst intern", "Canada"),
    ("new grad engineering", "Canada"),
    ("new grad software", "Canada"),
    ("new grad developer", "Canada"),
    ("new graduate technology", "Canada"),
    ("junior software", "Canada"),
    ("junior developer", "Canada"),
    ("entry level software", "Canada"),
    ("entry level developer", "Canada"),
    ("entry level technology", "Canada"),
    ("technology analyst new grad", "Canada"),
]

# --- Notification method: pick one ---
NOTIFY = "discord"   # "discord" | "telegram" | "email" | "print"
NOTIFY_WHEN_NO_NEW_JOBS = False

# Regular 30-minute checks stay silent when there are no new matching jobs.
# Set to True only if you want a heartbeat/status message after every check.
NOTIFY_WHEN_NO_NEW_JOBS = False

# Discord: paste your channel webhook URL (Server Settings -> Integrations ->
# Webhooks -> New Webhook -> Copy Webhook URL). Stored as an env var/secret.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

EMAIL_FROM = os.environ.get("MAIL_FROM", "")
EMAIL_PASS = os.environ.get("MAIL_PASS", "")   # app password, not your login password
EMAIL_TO   = os.environ.get("MAIL_TO", "")
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587

DB_PATH = os.environ.get(
    "JOBWATCH_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_jobs.db"),
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
TIMEOUT = 20

# ============================================================
# 2. Database (dedup) - track jobs already pushed
# ============================================================

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con

def get_meta(con, key, default=""):
    cur = con.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def set_meta(con, key, value):
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()

def is_new(con, uid):
    cur = con.execute("SELECT 1 FROM seen WHERE id=?", (uid,))
    return cur.fetchone() is None

def mark_seen(con, uid):
    con.execute("INSERT OR IGNORE INTO seen(id, ts) VALUES(?,?)",
                (uid, datetime.now(timezone.utc).isoformat()))
    con.commit()

def make_uid(*parts):
    return hashlib.sha256("||".join(str(p) for p in parts).encode()).hexdigest()[:16]

def parse_iso(s):
    """ISO-8601 string -> Unix seconds, or None."""
    if not s:
        return None
    try:
        from datetime import datetime as _dt
        s = s.replace("Z", "+00:00")
        dt = _dt.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JOBWATCH_TIMEZONE)
        return int(dt.timestamp())
    except Exception:
        return None

def parse_rss_date(s):
    """RSS date string -> Unix seconds, or None."""
    if not s:
        return None
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        return None

def humanize_age(ts):
    """Unix seconds -> ('2026-06-16 14:05', '23m ago'). Returns ('','') if None."""
    if not ts:
        return ("", "")
    try:
        dt = datetime.fromtimestamp(ts, JOBWATCH_TIMEZONE)
        stamp = dt.strftime("%Y-%m-%d %H:%M")
        secs = max(0, int(time.time() - ts))
        if secs < 3600:
            ago = f"{secs // 60}m ago"
        elif secs < 86400:
            ago = f"{secs // 3600}h ago"
        else:
            ago = f"{secs // 86400}d ago"
        return (stamp, ago)
    except Exception:
        return ("", "")

# ============================================================
# 3. Keyword filter
# ============================================================

# Early-career signal: at least one of these must be present, otherwise a plain
# "Software Engineer" (senior) would slip through.
EARLY_RE = re.compile(
    r"\b(intern|internship|co-?op|new\s*grad|graduate|entry[\s-]*level|"
    r"early\s*career|early[\s-]*talent|student|university|junior)\b|实习",
    re.I,
)
INTERN_RE = re.compile(r"\b(intern|internship|co-?op|student)\b|实习", re.I)
NEW_GRAD_RE = re.compile(
    r"\b(new\s*grad|graduate|entry[\s-]*level|early\s*career|"
    r"early[\s-]*talent|junior)\b",
    re.I,
)
FALL_TERM_RE = re.compile(
    r"\b(fall|autumn|sept(?:ember)?|sep(?:tember)?|"
    r"sep\.?\s*(?:-|to|through|–|—)\s*dec\.?|"
    r"sept\.?\s*(?:-|to|through|–|—)\s*dec\.?|"
    r"september\s*(?:-|to|through|–|—)\s*december|"
    r"4\s*[- ]?\s*months?|four\s*months?)\b",
    re.I,
)
LONG_INTERNSHIP_RE = re.compile(
    r"\b(6|8|12|16)\s*[- ]?\s*(?:-|to|–|—)?\s*months?\b|"
    r"\b(six|eight|twelve|sixteen)\s*months?\b|"
    r"\b(year[\s-]*long|one\s*year|1\s*year)\b",
    re.I,
)
SENIORITY_EXCLUDE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head\s+of|"
    r"architect|distinguished|executive|vp|vice\s+president)\b",
    re.I,
)
PHD_RE = re.compile(r"\b(ph\.?\s*d\.?|doctorate|doctoral)\b", re.I)
CITIZENSHIP_RE = re.compile(
    r"\b(canadian\s+citizenship|required\s+canadian\s+citizenship|"
    r"canadian\s+citizens?\s+only|must\s+be\s+(?:a\s+)?canadian\s+citizen|"
    r"requires?\s+canadian\s+citizenship)\b",
    re.I,
)
FRENCH_REQUIRED_RE = re.compile(
    r"\b(french\s+(?:is\s+)?required|required\s+french|must\s+speak\s+french|"
    r"fluent\s+in\s+french|bilingual\s+.*french|french\s+and\s+english\s+required|"
    r"fran[cç]ais\s+(?:obligatoire|requis))\b",
    re.I,
)
UNRELATED_MAJOR_RE = re.compile(
    r"\b(must\s+be\s+(?:currently\s+)?(?:enrolled|pursuing)|"
    r"requires?\s+(?:a\s+)?(?:degree|major)|degree\s+in|major\s+in)"
    r"[^.]{0,120}\b("
    r"accounting|finance|marketing|human\s+resources|hr|law|legal|"
    r"nursing|pharmacy|medicine|medical|biology|biochemistry|chemistry|"
    r"architecture|urban\s+planning|education|psychology|social\s+work|"
    r"mechanical\s+engineering|civil\s+engineering|chemical\s+engineering|"
    r"industrial\s+engineering|aerospace\s+engineering|environmental\s+engineering"
    r")\b",
    re.I,
)

def match_reject_reason(title, description=""):
    t = title or ""
    blob = t + " " + (description or "")
    if not ROLE_RE.search(blob):
        return "role"
    if not EARLY_RE.search(blob):
        return "level"
    if SENIORITY_EXCLUDE_RE.search(blob):
        return "seniority"
    if OTHER_TERM_RE.search(blob):         # tagged as another term -> drop
        return "term"
    if PHD_RE.search(blob):
        return "PhD"
    if CITIZENSHIP_RE.search(blob):
        return "citizenship"
    if FRENCH_REQUIRED_RE.search(blob):
        return "French"
    if any(x in blob.lower() for x in EXCLUDE):
        return "hard requirement"
    if UNRELATED_MAJOR_RE.search(blob):
        return "major"
    if INTERN_RE.search(blob):
        if LONG_INTERNSHIP_RE.search(blob):
            return "duration"
    if FILTER_MODE == "strict":
        return None if TERM_RE.search(blob) else "term"
    return None                            # loose: keep early-career role

def match_note(title, description=""):
    t = title or ""
    blob = t + " " + (description or "")
    if INTERN_RE.search(blob) and not FALL_TERM_RE.search(blob):
        return "term/duration not explicit; please verify"
    if NEW_GRAD_RE.search(blob) and not TERM_RE.search(blob):
        return "start term not explicit; please verify"
    return ""

def matches(title, description=""):
    return match_reject_reason(title, description) is None

def reject_reason(title, description, location):
    reason = match_reject_reason(title, description)
    if reason:
        return reason
    if not location_ok(location):
        return "location (not Canada)"
    return None


def location_ok(loc):
    """Decide whether to keep a job based on its location string."""
    if not loc or not loc.strip():
        return KEEP_UNKNOWN_LOCATION
    low = loc.lower()
    if LOCATION_MODE == "whitelist":
        # Guard: a "remote" string that also names a US place is still US.
        us_markers = ["united states", "usa", "u.s", ", us", "- us", "-us",
                      "remote us", "us remote", "us-remote", "remote-us",
                      "(us)", "(usa)", "u.s.",
                      "california", "new york", "san francisco", "seattle",
                      "austin", "boston", "chicago", "atlanta", "denver",
                      "los angeles", "texas", ", ca", ", wa", ", ny", ", tx",
                      ", ma", ", il", ", co", ", ga", ", fl", ", or", ", nj"]
        has_include = any(x in low for x in LOCATION_INCLUDE)
        has_us = any(x in low for x in us_markers)
        if not has_include:
            return False
        # If it matched only via "remote" but also carries a US marker, drop it.
        if has_us and not any(
            x in low for x in LOCATION_INCLUDE if x != "remote"
        ):
            return False
        return True
    # blacklist mode
    return not any(x in low for x in LOCATION_EXCLUDE)

# ============================================================
# 4. Fetchers - each returns [{title, company, location, url}]
# ============================================================

def iter_jobposting_schema(value):
    """Yield JobPosting dictionaries from common JSON-LD containers."""
    if isinstance(value, list):
        for item in value:
            yield from iter_jobposting_schema(item)
        return
    if not isinstance(value, dict):
        return

    schema_type = value.get("@type")
    schema_types = schema_type if isinstance(schema_type, list) else [schema_type]
    normalized_types = {
        str(item).rstrip("/").rsplit("/", 1)[-1] for item in schema_types if item
    }
    if "JobPosting" in normalized_types:
        yield value

    for key in ("@graph", "mainEntity", "item", "itemListElement"):
        if key in value:
            yield from iter_jobposting_schema(value[key])

def schema_value_name(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return ""

def schema_job_location(posting):
    locations = posting.get("jobLocation") or []
    if not isinstance(locations, list):
        locations = [locations]

    labels = []
    for location in locations:
        if isinstance(location, str):
            labels.append(location.strip())
            continue
        if not isinstance(location, dict):
            continue
        address = location.get("address") or {}
        if isinstance(address, str):
            labels.append(address.strip())
            continue
        if not isinstance(address, dict):
            continue
        country = schema_value_name(address.get("addressCountry"))
        if country.upper() == "CA":
            country = "Canada"
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            country,
        ]
        label = ", ".join(str(part).strip() for part in parts if part)
        if label:
            labels.append(label)

    if labels:
        return " / ".join(dict.fromkeys(labels))

    requirements = posting.get("applicantLocationRequirements") or []
    if not isinstance(requirements, list):
        requirements = [requirements]
    allowed = [schema_value_name(item) for item in requirements]
    allowed = ["Canada" if name.upper() == "CA" else name for name in allowed]
    allowed = [name for name in allowed if name]
    if allowed and posting.get("jobLocationType") == "TELECOMMUTE":
        return f"Remote - {', '.join(allowed)}"
    return ""

def parse_jobposting_page(page_url, html, fallback_company):
    """Convert JSON-LD JobPosting objects on one public page to job records."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (TypeError, json.JSONDecodeError):
            continue
        for posting in iter_jobposting_schema(data):
            title = str(posting.get("title") or posting.get("name") or "").strip()
            if not title:
                continue
            organization = posting.get("hiringOrganization") or {}
            company = schema_value_name(organization) or fallback_company
            location = schema_job_location(posting)
            description_html = str(posting.get("description") or "")
            description = BeautifulSoup(description_html, "html.parser").get_text(
                " ", strip=True
            )
            job_url = urljoin(page_url, str(posting.get("url") or page_url))
            posted_ts = parse_iso(posting.get("datePosted"))
            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "url": job_url,
                "posted_ts": posted_ts,
                "first_seen_fallback": posted_ts is None,
                "reject_reason": reject_reason(title, description, location),
                "note": match_note(title, description),
            })
    return jobs

def discover_job_detail_links(page_url, html, bamboohr_only=False):
    """Find likely public job-detail links on a configured careers page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(page_url, href).split("#", 1)[0]
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https") or full_url == page_url:
            continue
        page_host = urlparse(page_url).netloc.lower()
        target_host = parsed.netloc.lower()
        trusted_ats_hosts = (
            "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
            "smartrecruiters.com", "applytojob.com", "successfactors.com",
            "taleo.net", "phenompeople.com", "icims.com",
        )
        same_site = (
            target_host == page_host
            or target_host.endswith("." + page_host)
            or page_host.endswith("." + target_host)
        )
        trusted_ats = any(
            target_host == host or target_host.endswith("." + host)
            for host in trusted_ats_hosts
        )
        if not same_site and not trusted_ats:
            continue
        if bamboohr_only:
            is_bamboohr_host = (
                parsed.netloc == "bamboohr.com"
                or parsed.netloc.endswith(".bamboohr.com")
            )
            is_job_link = (
                is_bamboohr_host
                and "/careers/" in parsed.path.rstrip("/")
            )
        else:
            signal = f"{parsed.path} {anchor.get_text(' ', strip=True)}".lower()
            is_job_link = bool(re.search(
                r"\b(job|jobs|career|careers|opening|openings|position|positions|"
                r"vacancy|vacancies|opportunity|opportunities)\b",
                signal,
            ))
        if is_job_link and full_url not in seen:
            seen.add(full_url)
            links.append(full_url)
    return links[:CAREER_DETAIL_PAGE_LIMIT]

def fetch_public_career_site(company, page_url, bamboohr_only=False):
    """Fetch one public careers page and parse linked JSON-LD job pages."""
    response = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    jobs = parse_jobposting_page(response.url, response.text, company)
    detail_links = discover_job_detail_links(
        response.url, response.text, bamboohr_only=bamboohr_only
    )
    for detail_url in detail_links:
        try:
            detail = requests.get(detail_url, headers=HEADERS, timeout=TIMEOUT)
            detail.raise_for_status()
            jobs.extend(parse_jobposting_page(detail.url, detail.text, company))
        except Exception as e:
            print(f"[career-page:{company}] {detail_url}: {e}")

    unique = {}
    for job in jobs:
        key = (job.get("company"), job.get("title"), job.get("url"))
        unique[key] = job
    return list(unique.values())

def bamboohr_location(opening):
    location = opening.get("location") or opening.get("atsLocation") or {}
    if not isinstance(location, dict):
        return str(location)
    parts = [
        location.get("city"),
        location.get("state") or location.get("province"),
        location.get("addressCountry") or location.get("country"),
    ]
    return ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))

def fetch_bamboohr():
    out = []
    for company, subdomain in BAMBOOHR_COMPANIES:
        base_url = f"https://{subdomain}.bamboohr.com/careers"
        try:
            response = requests.get(
                f"{base_url}/list", headers=HEADERS, timeout=TIMEOUT
            )
            response.raise_for_status()
            openings = response.json().get("result", [])
            for summary in openings[:CAREER_DETAIL_PAGE_LIMIT]:
                job_id = summary.get("id")
                if not job_id:
                    continue
                opening = summary
                try:
                    detail = requests.get(
                        f"{base_url}/{job_id}/detail",
                        headers=HEADERS,
                        timeout=TIMEOUT,
                    )
                    detail.raise_for_status()
                    opening = (
                        detail.json().get("result", {}).get("jobOpening")
                        or summary
                    )
                except Exception as e:
                    print(f"[bamboohr:{company}:{job_id}] {e}")

                if opening.get("jobOpeningStatus", "Open") != "Open":
                    continue
                title = str(
                    opening.get("jobOpeningName")
                    or summary.get("jobOpeningName")
                    or ""
                ).strip()
                description_html = str(opening.get("description") or "")
                description = BeautifulSoup(
                    description_html, "html.parser"
                ).get_text(" ", strip=True)
                location = bamboohr_location(opening) or bamboohr_location(summary)
                job_url = opening.get("jobOpeningShareUrl") or f"{base_url}/{job_id}"
                out.append({
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": job_url,
                    "posted_ts": None,
                    "first_seen_fallback": True,
                    "reject_reason": reject_reason(title, description, location),
                    "note": match_note(title, description),
                })
                time.sleep(0.1)
        except Exception as e:
            print(f"[bamboohr:{company}] {e}")
    return out

def fetch_generic_careers():
    out = []
    for company, url in GENERIC_CAREER_SITES:
        try:
            out.extend(fetch_public_career_site(company, url))
        except Exception as e:
            print(f"[generic-careers:{company}] {e}")
    return out

def fetch_greenhouse():
    out = []
    for slug in GREENHOUSE_COMPANIES:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                title = j.get("title", "")
                loc = (j.get("location") or {}).get("name", "")
                desc = j.get("content", "")
                out.append({"title": title, "company": slug,
                            "location": loc, "url": j.get("absolute_url", ""),
                            "posted_ts": parse_iso(j.get("first_published")
                                                    or j.get("updated_at")),
                            "reject_reason": reject_reason(title, desc, loc),
                            "note": match_note(title, desc)})
        except Exception as e:
            print(f"[greenhouse:{slug}] {e}")
    return out

def fetch_lever():
    out = []
    for slug in LEVER_COMPANIES:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for j in r.json():
                title = j.get("text", "")
                loc = (j.get("categories") or {}).get("location", "")
                desc = j.get("descriptionPlain", "")
                cts = j.get("createdAt")
                pts = int(cts / 1000) if isinstance(cts, (int, float)) else None
                out.append({"title": title, "company": slug,
                            "location": loc, "url": j.get("hostedUrl", ""),
                            "posted_ts": pts,
                            "reject_reason": reject_reason(title, desc, loc),
                            "note": match_note(title, desc)})
        except Exception as e:
            print(f"[lever:{slug}] {e}")
    return out

def fetch_ashby():
    out = []
    for slug in ASHBY_COMPANIES:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"[ashby:{slug}] status {r.status_code}")
                continue
            for j in r.json().get("jobs", []):
                title = j.get("title", "")
                locs = j.get("location") or j.get("locations") or []
                if isinstance(locs, list):
                    loc = ", ".join(
                        x.get("name", "") if isinstance(x, dict) else str(x)
                        for x in locs
                    )
                elif isinstance(locs, dict):
                    loc = locs.get("name", "")
                else:
                    loc = str(locs)
                desc = j.get("descriptionPlain") or j.get("descriptionHtml") or ""
                posted = (
                    j.get("publishedAt")
                    or j.get("createdAt")
                    or j.get("updatedAt")
                )
                out.append({
                    "title": title,
                    "company": slug,
                    "location": loc,
                    "url": j.get("jobUrl") or j.get("applyUrl") or "",
                    "posted_ts": parse_iso(posted),
                    "reject_reason": reject_reason(title, desc, loc),
                    "note": match_note(title, desc),
                })
        except Exception as e:
            print(f"[ashby:{slug}] {e}")
    return out

def parse_workday_posted(text):
    """'Posted 3 Days Ago' / 'Posted Today' -> approx Unix seconds."""
    if not text:
        return None
    t = text.lower()
    today = datetime.now(JOBWATCH_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if "today" in t:
        return int(today.timestamp())
    if "yesterday" in t:
        return int((today - timedelta(days=1)).timestamp())
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return int((today - timedelta(days=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        days = int(m.group(1)) * 30
        return int((today - timedelta(days=days)).timestamp())
    return None

def fetch_workday():
    out = []
    for name, host, tenant, site in WORKDAY_COMPANIES:
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        for search in ("intern", "new grad", "co-op"):
            try:
                offset = 0
                while True:
                    payload = {"appliedFacets": {}, "limit": 20, "offset": offset,
                               "searchText": search}
                    r = requests.post(url, json=payload, headers=HEADERS, timeout=TIMEOUT)
                    r.raise_for_status()
                    data = r.json()
                    postings = data.get("jobPostings", [])
                    if not postings:
                        break
                    for j in postings:
                        title = j.get("title", "")
                        loc = j.get("locationsText", "")
                        path = j.get("externalPath", "")
                        full = f"https://{host}{('/' + site) if site else ''}{path}"
                        out.append({"title": title, "company": name,
                                    "location": loc, "url": full,
                                    "posted_ts": parse_workday_posted(j.get("postedOn")),
                                    "reject_reason": reject_reason(title, "", loc),
                                    "note": match_note(title, "")})
                    offset += 20
                    if offset >= data.get("total", 0) or offset > 100:
                        break
            except Exception as e:
                print(f"[workday:{name}:{search}] {e}")
    return out

def fetch_linkedin():
    out = []
    base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    for kw, loc in LINKEDIN_QUERIES:
        try:
            params = {"keywords": kw, "location": loc, "f_TPR": "r86400", "start": 0}
            r = requests.get(base, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"[linkedin] status {r.status_code} (possibly rate-limited)")
                if r.status_code == 429:
                    print("[linkedin] rate limited; skipping remaining queries")
                    break
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("li"):
                a = card.select_one("a.base-card__full-link") or card.select_one("a")
                title_el = card.select_one("h3")
                comp_el = card.select_one("h4")
                loc_el = card.select_one(".job-search-card__location")
                if not (a and title_el):
                    continue
                title = title_el.get_text(strip=True)
                job_loc = loc_el.get_text(strip=True) if loc_el else loc
                t_el = card.select_one("time")
                pts = parse_iso(t_el.get("datetime")) if t_el else None
                out.append({
                    "title": title,
                    "company": comp_el.get_text(strip=True) if comp_el else "",
                    "location": job_loc,
                    "url": a.get("href", "").split("?")[0],
                    "posted_ts": pts,
                    "reject_reason": reject_reason(title, "", job_loc),
                    "note": match_note(title, ""),
                })
        except Exception as e:
            print(f"[linkedin] {e}")
    return out

def fetch_indeed():
    out = []
    for kw, loc in INDEED_QUERIES:
        try:
            url = (
                "https://ca.indeed.com/rss"
                f"?q={quote_plus(kw)}&l={quote_plus(loc)}&fromage=1"
            )
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"[indeed] status {r.status_code}")
                if r.status_code == 403:
                    print("[indeed] access denied; skipping remaining queries")
                    break
                continue
            soup = BeautifulSoup(r.text, "xml")
            for item in soup.select("item"):
                raw_title = item.title.get_text(strip=True) if item.title else ""
                desc = item.description.get_text(" ", strip=True) if item.description else ""
                link = item.link.get_text(strip=True) if item.link else ""
                pub = item.pubDate.get_text(strip=True) if item.pubDate else ""
                parts = [p.strip() for p in raw_title.split(" - ") if p.strip()]
                title = parts[0] if parts else raw_title
                company = parts[1] if len(parts) > 1 else "Indeed"
                job_loc = parts[2] if len(parts) > 2 else loc
                out.append({
                    "title": title,
                    "company": company,
                    "location": job_loc,
                    "url": link,
                    "posted_ts": parse_rss_date(pub),
                    "reject_reason": reject_reason(title, desc, job_loc),
                    "note": match_note(title, desc),
                })
        except Exception as e:
            print(f"[indeed] {e}")
    return out

def fetch_community():
    """Read Simplify / Vansh listings.json directly. This is the big multiplier:
    their scrapers cover hundreds of companies, and we union it with our own."""
    out = []
    cutoff = 0
    if COMMUNITY_MAX_AGE_DAYS > 0:
        cutoff = time.time() - COMMUNITY_MAX_AGE_DAYS * 86400
    for label, url in COMMUNITY_REPOS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            if r.status_code != 200:
                print(f"[community:{label}] status {r.status_code}")
                continue
            data = json.loads(r.text)
            for j in data:
                if not j.get("active", True) or not j.get("is_visible", True):
                    continue
                dp = j.get("date_posted") or j.get("date_updated") or 0
                try:
                    dp = int(dp)
                except (TypeError, ValueError):
                    dp = 0
                if cutoff and dp and dp < cutoff:
                    continue
                title = j.get("title", "")
                locs = j.get("locations") or []
                loc = ", ".join(locs) if isinstance(locs, list) else str(locs)
                desc = json.dumps(j, ensure_ascii=False)
                out.append({
                    "title": title,
                    "company": j.get("company_name", label),
                    "location": loc,
                    "url": j.get("url", ""),
                    "posted_ts": dp or None,
                    "reject_reason": reject_reason(title, desc, loc),
                    "note": match_note(title, desc),
                })
        except Exception as e:
            print(f"[community:{label}] {e}")
    return out

# ============================================================
# 5. Notifications
# ============================================================

def notify_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                 "disable_web_page_preview": True}, timeout=TIMEOUT)
    except Exception as e:
        print(f"[telegram] {e}")

def notify_discord(blocks, header):
    """Send to a Discord webhook. blocks = list of per-job text chunks.
    Discord caps each message at 2000 chars, so we batch blocks under that."""
    if not DISCORD_WEBHOOK:
        print("[discord] DISCORD_WEBHOOK not set")
        return
    LIMIT = 1900  # leave headroom under Discord's 2000-char cap
    SEP = "\n\n"
    batch, size = [header], len(header)
    def flush(b):
        if not b:
            return
        try:
            payload = {"content": SEP.join(b), "flags": 4}
            r = requests.post(DISCORD_WEBHOOK, json=payload,
                              timeout=TIMEOUT)
            if r.status_code not in (200, 204):
                print(f"[discord] status {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[discord] {e}")
        time.sleep(0.7)  # stay under webhook rate limit
    for blk in blocks:
        if size + len(blk) + len(SEP) > LIMIT:
            flush(batch)
            batch, size = [], 0
        batch.append(blk)
        size += len(blk) + len(SEP)
    flush(batch)

def notify_email(text):
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = f"New job alert {datetime.now(JOBWATCH_TIMEZONE):%m-%d %H:%M}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_PASS)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        s.quit()
    except Exception as e:
        print(f"[email] {e}")

def format_block(j):
    """One job -> a compact, readable block (Discord markdown)."""
    _, ago = humanize_age(j.get("posted_ts"))
    title = j.get("title", "")
    company = j.get("company", "")
    url = j.get("url", "")
    line1 = f"**[{title}]({url})**" if url else f"**{title}**"
    bits = []
    if company:
        bits.append(company)
    if j.get("location"):
        bits.append(j["location"])
    if j.get("time_label"):
        bits.append(j["time_label"])
    elif ago:
        bits.append(ago)
    line2 = " · ".join(bits)
    lines = [line1]
    if line2:
        lines.append(line2)
    return "\n".join(lines)

def compact_job_label(j):
    title = j.get("title") or "Untitled"
    company = j.get("company") or "Unknown"
    return f"{title} — {company}"

def alert_window_label():
    if ALERT_WINDOW_MINUTES % 60 == 0:
        hours = ALERT_WINDOW_MINUTES // 60
        noun = "hour" if hours == 1 else "hours"
        return f"{hours} {noun}"
    return f"{ALERT_WINDOW_MINUTES} minutes"

def add_example(stats, reason, job, limit=5):
    examples = stats.setdefault("examples", [])
    if len(examples) < limit:
        examples.append((reason, compact_job_label(job)))

def alert_header(count, stats=None):
    """Build the alert-mode Discord header."""
    now = datetime.now(JOBWATCH_TIMEZONE).strftime("%b %d %H:%M")
    if count == 0:
        summary = "No new postings"
    else:
        noun = "posting" if count == 1 else "postings"
        summary = f"{count} new {noun}"
    return f"**Jobwatch · {summary} · {now}**"

def digest_header(jobs):
    now = datetime.now(JOBWATCH_TIMEZONE)
    count = len(jobs)
    noun = "posting" if count == 1 else "postings"
    return (
        f"**Daily Jobwatch Digest** · {now:%b %d}\n"
        f"{count} matching {noun} from the last {DIGEST_LOOKBACK_HOURS} hours"
    )

def digest_blocks(jobs):
    jobs = sorted(
        jobs,
        key=lambda j: ((j.get("company") or "").lower(), -(j.get("posted_ts") or 0)),
    )
    return [format_block(j) for j in jobs]

def send(jobs, header=None):
    if header is None:
        header = alert_header(len(jobs))
    if not jobs:
        if not NOTIFY_WHEN_NO_NEW_JOBS:
            print("No new postings; notification skipped.")
            return
        if NOTIFY == "discord":
            notify_discord([], header)
        elif NOTIFY == "telegram":
            notify_telegram(header)
        elif NOTIFY == "email":
            notify_email(header)
        else:
            print(header)
        return
    # Newest first; unknown-time jobs go last.
    jobs = sorted(jobs, key=lambda j: j.get("posted_ts") or 0, reverse=True)
    blocks = [format_block(j) for j in jobs]

    if NOTIFY == "discord":
        notify_discord(blocks, header)
    elif NOTIFY == "telegram":
        notify_telegram(header + "\n\n" + "\n\n".join(blocks))
    elif NOTIFY == "email":
        notify_email(header + "\n\n" + "\n\n".join(blocks))
    else:
        print(header + "\n\n" + "\n\n".join(blocks))

# ============================================================
# 6. Main
# ============================================================

def collect_all_jobs():
    all_jobs = []
    for fn in (fetch_bamboohr, fetch_generic_careers, fetch_greenhouse,
               fetch_lever, fetch_ashby, fetch_workday, fetch_community,
               fetch_linkedin, fetch_indeed):
        try:
            jobs = fn()
            source = fn.__name__.removeprefix("fetch_")
            for job in jobs:
                job.setdefault("source", source)
            all_jobs.extend(jobs)
            print(f"[source:{source}] fetched {len(jobs)} candidates")
        except Exception as e:
            print(f"[{fn.__name__}] {e}")
        time.sleep(1)  # be polite
    return all_jobs

def run_alert():
    """Incremental mode: notify ONLY about jobs whose posting time falls within
    the last ALERT_WINDOW_MINUTES. The dedup DB is a backstop against repeats.

    Jobs without a minute-precise posting time are normally skipped. Configured
    public career pages may instead alert once when a posting is first seen."""
    con = db_init()
    all_jobs = collect_all_jobs()
    now = time.time()
    cutoff = now - ALERT_WINDOW_MINUTES * 60

    def is_date_only(ts):
        # midnight local time -> the source only gave us a date
        dt = datetime.fromtimestamp(ts, JOBWATCH_TIMEZONE)
        return dt.hour == 0 and dt.minute == 0 and dt.second == 0

    new_jobs = []
    source_stats = {}
    stats = {
        "fetched": len(all_jobs),
        "no_time": 0,
        "date_only": 0,
        "date_only_baselined": 0,
        "date_only_discovered": 0,
        "outside_window": 0,
        "in_window": 0,
        "duplicate": 0,
        "filtered": 0,
        "examples": [],
    }
    date_only_baseline_ready = get_meta(con, "date_only_baseline_v1") == "ready"

    def increment_source(job, key):
        source = job.get("source", "unknown")
        counters = source_stats.setdefault(source, {})
        counters[key] = counters.get(key, 0) + 1

    for j in all_jobs:
        increment_source(j, "fetched")
        reason = j.get("reject_reason")
        if reason:
            stats["filtered"] += 1
            increment_source(j, "filtered")
            if reason not in ("role", "level"):
                add_example(stats, reason, j)
            continue
        uid = make_uid(j["company"], j["title"], j["url"])
        ts = j.get("posted_ts")
        if not ts and j.get("first_seen_fallback"):
            stats["in_window"] += 1
            increment_source(j, "eligible")
            increment_source(j, "first_seen")
            if not is_new(con, uid):
                stats["duplicate"] += 1
                increment_source(j, "duplicate")
                continue
            j["posted_ts"] = int(now)
            j["time_label"] = "newly discovered"
            new_jobs.append(j)
            increment_source(j, "new")
            mark_seen(con, uid)
            continue
        if not ts:
            stats["no_time"] += 1
            increment_source(j, "no_time")
            add_example(stats, "missing exact time", j)
            continue
        if is_date_only(ts):
            stats["date_only"] += 1
            increment_source(j, "date_only")
            if not date_only_baseline_ready:
                # Establish a quiet baseline once so enabling first-seen alerts
                # does not flood Discord with every existing date-only posting.
                if is_new(con, uid):
                    mark_seen(con, uid)
                stats["date_only_baselined"] += 1
                increment_source(j, "date_only_baselined")
                continue
            if not is_new(con, uid):
                stats["duplicate"] += 1
                increment_source(j, "duplicate")
                continue
            stats["in_window"] += 1
            stats["date_only_discovered"] += 1
            increment_source(j, "eligible")
            increment_source(j, "date_only_discovered")
            j["posted_ts"] = int(now)
            j["time_label"] = "newly discovered (source provides date only)"
            new_jobs.append(j)
            increment_source(j, "new")
            mark_seen(con, uid)
            continue
        if ts < cutoff:
            stats["outside_window"] += 1
            increment_source(j, "outside_window")
            add_example(stats, "outside window", j)
            continue
        stats["in_window"] += 1
        increment_source(j, "eligible")
        # Backstop: skip anything we've already notified about.
        if not is_new(con, uid):
            stats["duplicate"] += 1
            increment_source(j, "duplicate")
            continue
        new_jobs.append(j)
        increment_source(j, "new")
        mark_seen(con, uid)

    if not date_only_baseline_ready:
        set_meta(con, "date_only_baseline_v1", "ready")
        print(
            f"Established date-only baseline with "
            f"{stats['date_only_baselined']} matching postings; future newly "
            "discovered date-only jobs will alert once."
        )

    print(f"Fetched {len(all_jobs)} jobs, {stats['in_window']} eligible, "
          f"{stats['duplicate']} duplicates, {len(new_jobs)} new to notify")
    for source, counters in source_stats.items():
        print(
            f"[source:{source}] fetched {counters.get('fetched', 0)}, "
            f"filtered {counters.get('filtered', 0)}, "
            f"no time {counters.get('no_time', 0)}, "
            f"date only {counters.get('date_only', 0)}, "
            f"date-only baseline {counters.get('date_only_baselined', 0)}, "
            f"date-only discovered {counters.get('date_only_discovered', 0)}, "
            f"first seen {counters.get('first_seen', 0)}, "
            f"outside window {counters.get('outside_window', 0)}, "
            f"eligible {counters.get('eligible', 0)}, "
            f"duplicate {counters.get('duplicate', 0)}, "
            f"new {counters.get('new', 0)}"
        )
    send(new_jobs, header=alert_header(len(new_jobs), stats=stats))

def run_digest():
    """Daily mode (~midnight): summarize everything posted in the last
    DIGEST_LOOKBACK_HOURS, regardless of prior alerts. Does NOT touch the DB."""
    all_jobs = collect_all_jobs()
    cutoff = time.time() - DIGEST_LOOKBACK_HOURS * 3600
    # dedup within this run by uid (same posting from two sources)
    seen, todays = set(), []
    for j in all_jobs:
        if j.get("reject_reason"):
            continue
        ts = j.get("posted_ts")
        if not ts or ts < cutoff:
            continue
        uid = make_uid(j["company"], j["title"], j["url"])
        if uid in seen:
            continue
        seen.add(uid)
        todays.append(j)
    n = len(todays)
    header = digest_header(todays)
    print(f"Digest: {n} jobs in last {DIGEST_LOOKBACK_HOURS}h")
    if n:
        blocks = digest_blocks(todays)
        if NOTIFY == "discord":
            notify_discord(blocks, header)
        elif NOTIFY == "telegram":
            notify_telegram(header + "\n\n" + "\n\n".join(blocks))
        elif NOTIFY == "email":
            notify_email(header + "\n\n" + "\n\n".join(blocks))
        else:
            print(header + "\n\n" + "\n\n".join(blocks))
    elif NOTIFY == "discord" and DISCORD_WEBHOOK:
        # still send a heartbeat so you know it ran
        notify_discord([], header + "\nNo matching postings today.")

def main():
    mode = "alert"
    if "--digest" in sys.argv or os.environ.get("JOBWATCH_MODE") == "digest":
        mode = "digest"
    if mode == "digest":
        run_digest()
    else:
        run_alert()

if __name__ == "__main__":
    main()
