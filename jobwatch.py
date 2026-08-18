#!/usr/bin/env python3
"""
jobwatch.py - watcher for GTA new-grad / entry-level engineering roles

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

# --- Freshness (alert mode) ---
# Each alert run only notifies about jobs whose verified original posting time
# falls within this window. The dedup DB prevents repeats across overlapping runs.
# Only roles whose original posting time is within the last day are eligible.
ALERT_WINDOW_MINUTES = 24 * 60
REQUIRE_VERIFIED_DETAILS = True
DETAIL_MIN_DESCRIPTION_CHARS = 200
DETAIL_PAGE_DELAY_SECONDS = 0.15

# LinkedIn quality controls. Explicit reposts are always excluded; extremely
# high applicant counts are treated as stale/low-value recruiting inventory.
LINKEDIN_MAX_APPLICANTS = 999

# Target annual base-pay band in CAD. Every stated endpoint must stay inside it.
TARGET_SALARY_MIN_CAD = 40_000
TARGET_SALARY_MAX_CAD = 65_000

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

# Whitelist: Greater Toronto Area and nearby commuter cities only. Do not add
# broad markers such as "Canada", "Ontario", or ", ON" here: those would let
# jobs elsewhere in the province/country through.
LOCATION_INCLUDE = [
    "greater toronto area", "gta", "toronto", "downtown toronto",
    "north york", "scarborough", "etobicoke", "east york", "york, on",
    "mississauga", "brampton", "caledon", "bolton",
    "markham", "richmond hill", "vaughan", "newmarket", "aurora",
    "whitchurch-stouffville", "stouffville", "king city", "georgina",
    "oakville", "burlington", "milton", "halton hills", "georgetown, on",
    "pickering", "ajax", "whitby", "oshawa", "clarington", "bowmanville",
]

# Remote roles are useful when they explicitly accept Canadian applicants.
# A bare "Remote" is too ambiguous and remains excluded.
REMOTE_CANADA_RE = re.compile(
    r"\b(remote\s*[-,/()]?\s*(canada|canadian|ontario)|"
    r"(canada|canadian|ontario)\s*[-,/()]?\s*remote|"
    r"remote\s+(within|across|in)\s+canada)\b",
    re.I,
)

# London, Ontario is outside the normal GTA radius. Keep it only for major,
# well-established employers where the opportunity can justify the distance.
LONDON_ON_RE = re.compile(
    r"\blondon\s*,?\s*(on|ontario|canada)\b|\blondon,\s*ontario,\s*canada\b",
    re.I,
)
LONDON_LARGE_COMPANIES = {
    "3m", "accenture", "amazon", "amd", "apple", "bell", "bmo", "cibc",
    "cisco", "deloitte", "ey", "ford", "general dynamics", "google", "ibm",
    "kpmg", "mastercard", "microsoft", "nvidia", "oracle", "paypal", "pwc",
    "rbc", "rogers", "salesforce", "scotiabank", "shopify", "td", "td bank",
    "telus", "thomson reuters", "toyota", "wealthsimple", "workday",
}

BLOCKED_COMPANIES = {"jobright.ai", "jobright ai", "jobright"}
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
    ("Simplify-NewGrad",
     "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"),
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
    # Toronto startups / growth companies with official Ashby job boards.
    "cerebras", "magical", "zip", "relayfi", "viggle", "marble.ai",
    "mycroft", "Maxima", "terminal",
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
# Keep LinkedIn enabled, alongside the expanded direct company/ATS sources.
ENABLE_LINKEDIN = True
# Keep these broad. LinkedIn search works better with short keyword groups;
# detailed term/duration/location rules are enforced by the filters below.
LINKEDIN_QUERIES = [
    ("new grad engineering", "Greater Toronto Area, Canada"),
    ("new grad software", "Greater Toronto Area, Canada"),
    ("new grad developer", "Greater Toronto Area, Canada"),
    ("new graduate technology", "Greater Toronto Area, Canada"),
    ("junior software", "Greater Toronto Area, Canada"),
    ("junior developer", "Greater Toronto Area, Canada"),
    ("entry level engineering", "Greater Toronto Area, Canada"),
    ("entry level software", "Greater Toronto Area, Canada"),
    ("entry level developer", "Greater Toronto Area, Canada"),
    ("entry level technology", "Greater Toronto Area, Canada"),
    ("technology analyst new grad", "Greater Toronto Area, Canada"),
]

# --- Indeed search keywords / location ---
# Keep these broad. Indeed often returns better results with simple keyword
# combinations, then the script filters the details.
INDEED_QUERIES = [
    (kw, "Toronto, ON") for kw in (
        "new grad engineering", "new grad software", "new grad developer",
        "new graduate technology", "junior software", "junior developer",
        "entry level engineering", "entry level software",
        "entry level developer", "entry level technology",
        "technology analyst new grad",
    )
]

# --- Notification method: pick one ---
NOTIFY = "discord"   # "discord" | "telegram" | "email" | "print"
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

# --- Resume skill matching ---
# Local use: point this at a PDF, DOCX, TXT, or JSON skill profile.
# GitHub Actions: set JOBWATCH_RESUME_SKILLS to a comma-separated skill list.
RESUME_PATH = os.environ.get("JOBWATCH_RESUME_PATH", "")
RESUME_SKILLS_ENV = os.environ.get("JOBWATCH_RESUME_SKILLS", "")

SKILL_PATTERNS = {
    "Python": r"\bpython\b", "Java": r"\bjava\b(?!script)",
    "JavaScript": r"\b(?:javascript|js)\b", "TypeScript": r"\b(?:typescript|ts)\b",
    "C": r"(?<![+#])\bc\b(?![+#])", "C++": r"(?<!\w)c\+\+(?!\w)",
    "C#": r"(?<!\w)c#(?!\w)", "Go": r"\b(?:golang|go)\b", "Rust": r"\brust\b",
    "SQL": r"\bsql\b", "HTML/CSS": r"\b(?:html|css|sass|scss)\b",
    "React": r"\breact(?:\.js|js)?\b", "Angular": r"\bangular\b",
    "Vue": r"\bvue(?:\.js|js)?\b", "Node.js": r"\bnode(?:\.js|js)?\b",
    "Django": r"\bdjango\b", "Flask": r"\bflask\b", "FastAPI": r"\bfastapi\b",
    "Spring": r"\bspring(?:\s+boot)?\b", ".NET": r"(?<!\w)(?:\.net|dotnet)\b",
    "AWS": r"\b(?:aws|amazon web services)\b", "Azure": r"\bazure\b",
    "GCP": r"\b(?:gcp|google cloud)\b", "Docker": r"\bdocker\b",
    "Kubernetes": r"\b(?:kubernetes|k8s)\b", "Terraform": r"\bterraform\b",
    "Git": r"\bgit(?:hub|lab)?\b", "Linux": r"\blinux\b",
    "PostgreSQL": r"\b(?:postgresql|postgres)\b", "MySQL": r"\bmysql\b",
    "MongoDB": r"\bmongodb\b", "Redis": r"\bredis\b", "Spark": r"\bspark\b",
    "Kafka": r"\bkafka\b", "Airflow": r"\bairflow\b",
    "Machine Learning": r"\b(?:machine learning|ml)\b",
    "PyTorch": r"\bpytorch\b", "TensorFlow": r"\btensorflow\b",
    "pandas": r"\bpandas\b", "scikit-learn": r"\bscikit(?:-learn)?\b",
    "REST APIs": r"\b(?:restful|rest api|rest APIs)\b", "GraphQL": r"\bgraphql\b",
    "CI/CD": r"\b(?:ci/cd|continuous integration|continuous delivery)\b",
    "Selenium": r"\bselenium\b", "Cypress": r"\bcypress\b",
    "Playwright": r"\bplaywright\b",
    "Verilog": r"\b(?:verilog|systemverilog)\b",
    "Express": r"\bexpress(?:\.js|js)?\b",
    "React Native": r"\breact\s+native\b",
    "Dart": r"\bdart\b", "Flutter": r"\bflutter\b",
    "Arduino": r"\barduino\b", "Figma": r"\bfigma\b",
    "SolidWorks": r"\bsolidworks\b",
    "Full-stack Development": r"\bfull[\s-]*stack\s+(?:development|engineering)\b",
    "Web Development": r"\bweb\s+(?:application\s+)?development\b",
    "Computer Networks": r"\bcomputer\s+networks?\b",
    "Network Security": r"\b(?:network|cyber)\s*security\b",
    "Operating Systems": r"\boperating\s+systems?\b",
    "Computer Architecture": r"\bcomputer\s+architecture\b",
    "Data Analytics": r"\bdata\s+analytics\b",
    "Cloud Computing": r"\bcloud\s+computing\b",
    "Hardware-Software Integration": r"\bhardware[\s-]+software\s+integration\b",
}


def extract_skills(text):
    """Return canonical skills found in resume or job text."""
    value = text or ""
    return {
        skill for skill, pattern in SKILL_PATTERNS.items()
        if re.search(pattern, value, re.I)
    }


def read_resume_text(path):
    """Extract text locally; the resume is never uploaded by this script."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data = data.get("skills", [])
        return " ".join(str(item) for item in data)
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(path).paragraphs)
    raise ValueError("resume must be PDF, DOCX, TXT, MD, or JSON")


def load_resume_skills():
    """Load a private skill profile from env or a local resume/profile file."""
    if RESUME_SKILLS_ENV.strip():
        raw = {item.strip() for item in RESUME_SKILLS_ENV.split(",") if item.strip()}
        # Preserve known canonical names while still accepting custom skills.
        known = extract_skills(" ".join(raw))
        return known | {item for item in raw if item in SKILL_PATTERNS}

    candidates = [RESUME_PATH] if RESUME_PATH else []
    base = os.path.dirname(os.path.abspath(__file__))
    candidates.extend(os.path.join(base, name) for name in (
        "skills_profile.json", "resume_skills.json", "resume.pdf",
        "resume.docx", "resume.txt"
    ))
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            skills = extract_skills(read_resume_text(path))
            print(f"[resume] loaded {len(skills)} skills from {os.path.basename(path)}")
            return skills
        except Exception as e:
            print(f"[resume] could not read {path}: {e}")
    print("[resume] no profile configured; match scores disabled")
    return set()


def add_skill_match(job, resume_skills):
    """Annotate one job with requirement coverage based on recognized skills."""
    if not resume_skills:
        return
    job_text = " ".join((
        str(job.get("title") or ""), str(job.get("description") or "")
    ))
    job_skills = extract_skills(job_text)
    if not job_skills:
        job["match_score"] = None
        job["match_note"] = "no comparable technical skills found in JD"
        return
    matched = sorted(job_skills & resume_skills)
    missing = sorted(job_skills - resume_skills)
    job["match_score"] = round(100 * len(matched) / len(job_skills))
    job["matched_skills"] = matched
    job["missing_skills"] = missing

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

# New-grad/entry-level signal: internships, co-ops and student roles are
# intentionally not included.
EARLY_RE = re.compile(
    r"\b(new\s*grad|new\s*graduate|graduate|entry[\s-]*level|"
    r"early\s*career|early[\s-]*talent|junior|associate)\b",
    re.I,
)
# A role may omit "junior" but explicitly welcome candidates with no experience
# or up to one year.
EARLY_EXPERIENCE_RE = re.compile(
    r"\b0\s*(?:-|–|—|to)\s*1\s*years?\b|"
    r"\b0\s*years?\s+(of\s+)?(professional\s+)?experience\b|"
    r"\b(no\s+(professional\s+)?experience|required experience:\s*none)\b",
    re.I,
)

# Hard blocker, regardless of title: never send a role whose stated minimum
# experience is one year or more.
REQUIRED_EXPERIENCE_RE = re.compile(
    r"\b(?:at\s+least|minimum(?:\s+of)?|min\.?|requires?|must\s+have|"
    r"you(?:'ll|\s+will)?\s+(?:need|have)|with)\s+"
    r"(?:a\s+minimum\s+of\s+)?(?:1|[2-9]\d*)\+?\s*years?"
    r"(?:\s+of)?\s+(?:relevant\s+|professional\s+|industry\s+)?experience\b|"
    r"\b(?:1|[2-9]\d*)\+\s*years?\s+(?:of\s+)?"
    r"(?:relevant\s+|professional\s+|industry\s+)?experience\b|"
    r"\b(?:1|[2-9]\d*)\s*(?:-|–|—|to)\s*\d+\s*years?\s+(?:of\s+)?"
    r"(?:relevant\s+|professional\s+|industry\s+)?experience\b|"
    r"\b(?:1|[2-9]\d*)\s*years?\s+of\s+"
    r"(?:relevant\s+|professional\s+|industry\s+)?experience\b|"
    r"\bexperience\s*(?:required)?\s*:\s*(?:1|[2-9]\d*)\+?\s*years?\b",
    re.I,
)
ANY_ONE_PLUS_EXPERIENCE_RE = re.compile(
    r"(?<![\d-])\b(?:1|[2-9]\d*)\+?\s*years?\b"
    r"[^.;\n]{0,80}\bexperience\b|"
    r"(?<![\d-])\b(?:1|[2-9]\d*)\s*(?:-|–|—|to)\s*\d+\s*years?\b"
    r"[^.;\n]{0,80}\bexperience\b",
    re.I,
)
INTERN_RE = re.compile(r"\b(intern|internship|co-?op|student)\b|实习", re.I)
NEW_GRAD_RE = re.compile(
    r"\b(new\s*grad|graduate|entry[\s-]*level|early\s*career|"
    r"early[\s-]*talent|junior)\b",
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
UNRELATED_ENGINEERING_TITLE_RE = re.compile(
    r"\b(mechanical|civil|chemical|industrial|aerospace|environmental|"
    r"nuclear|structural|geotechnical|mining)\s+engineer(?:ing)?\b",
    re.I,
)


def requires_one_plus_years(text):
    """Detect stated job requirements while allowing explicit 0-N ranges."""
    value = text or ""
    value = re.sub(
        r"\b0\s*(?:-|–|—|to)\s*\d+\s*years?\b[^.;\n]{0,80}\bexperience\b",
        "",
        value,
        flags=re.I,
    )
    matches = list(REQUIRED_EXPERIENCE_RE.finditer(value))
    matches.extend(ANY_ONE_PLUS_EXPERIENCE_RE.finditer(value))
    for match in sorted(matches, key=lambda item: item.start()):
        context = value[max(0, match.start() - 50):match.end() + 20].lower()
        if re.search(r"\b(?:we|our\s+(?:company|team)|the\s+company)\s+ha(?:s|ve)\b", context):
            continue
        return True
    return False

def match_reject_reason(title, description=""):
    t = title or ""
    blob = t + " " + (description or "")
    if not ROLE_RE.search(blob):
        return "role"
    if UNRELATED_ENGINEERING_TITLE_RE.search(t):
        return "unrelated engineering field"
    # Titles are authoritative. Also catch descriptions that explicitly define
    # the opening as an internship/co-op, without rejecting a full-time role
    # merely because its boilerplate mentions an internship program.
    explicit_intern_desc = re.search(
        r"\b(this|the)\s+(position|role|opportunity)\s+is\s+(an?\s+)?"
        r"(internship|co-?op)\b",
        description or "",
        re.I,
    )
    if INTERN_RE.search(t) or explicit_intern_desc:
        return "intern/co-op"
    if requires_one_plus_years(blob):
        return "experience (1+ years)"
    if not (EARLY_RE.search(blob) or EARLY_EXPERIENCE_RE.search(blob)):
        return "level"
    if SENIORITY_EXCLUDE_RE.search(t):
        return "seniority"
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
    return None

def match_note(title, description=""):
    t = title or ""
    blob = t + " " + (description or "")
    return ""

def matches(title, description=""):
    return match_reject_reason(title, description) is None

def reject_reason(title, description, location):
    reason = match_reject_reason(title, description)
    if reason:
        return reason
    if not location_ok(location):
        return "location (outside GTA)"
    return None


def normalized_company(company):
    """Normalize a company label for block/allow-list decisions."""
    return re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()


def company_blocked(company):
    low = normalized_company(company or "")
    blocked = {normalized_company(name) for name in BLOCKED_COMPANIES}
    return any(low == name or low.startswith(name + " ") for name in blocked)


def london_large_company(company):
    low = normalized_company(company or "")
    return any(low == name or low.startswith(name + " ")
               for name in LONDON_LARGE_COMPANIES)


def location_ok(loc, company=""):
    """Decide whether to keep a job based on its location string."""
    if not loc or not loc.strip():
        return KEEP_UNKNOWN_LOCATION
    low = loc.lower()
    if LOCATION_MODE == "whitelist":
        # Guard: a "remote" string that also names a US place is still US.
        us_markers = ["united states", "usa", "u.s", ", us", "- us", "-us",
                      "remote us", "us remote", "us-remote", "remote-us",
                      "(us)", "(usa)", "/ us", "us /", "u.s.",
                      "california", "new york", "san francisco", "seattle",
                      "austin", "boston", "chicago", "atlanta", "denver",
                      "los angeles", "texas", ", wa", ", ny", ", tx",
                      ", ma", ", il", ", co", ", ga", ", fl", ", or", ", nj"]
        has_include = any(x in low for x in LOCATION_INCLUDE)
        has_us = any(x in low for x in us_markers)
        if has_us:
            return False
        if REMOTE_CANADA_RE.search(loc):
            return True
        if LONDON_ON_RE.search(loc) and london_large_company(company):
            return True
        if not has_include:
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


def parse_salary_amount(value):
    """Normalize a salary number such as 65k or 65,000."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower().replace(",", "")
    match = re.fullmatch(r"\$?\s*(\d+(?:\.\d+)?)\s*(k)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    if match.group(2):
        amount *= 1000
    return amount


def annualize_salary(low, high, unit="YEAR"):
    if low is None and high is None:
        return None
    unit = str(unit or "YEAR").upper()
    multiplier = 2080 if "HOUR" in unit else 1
    low = round(low * multiplier) if low is not None else None
    high = round(high * multiplier) if high is not None else None
    return (low, high)


def salary_from_schema(posting):
    base = posting.get("baseSalary") or posting.get("estimatedSalary") or {}
    if not isinstance(base, dict):
        return None
    value = base.get("value", base)
    if isinstance(value, (int, float, str)):
        amount = parse_salary_amount(value)
        return annualize_salary(amount, amount, base.get("unitText"))
    if not isinstance(value, dict):
        return None
    low = parse_salary_amount(value.get("minValue") or value.get("value"))
    high = parse_salary_amount(value.get("maxValue") or value.get("value"))
    return annualize_salary(low, high, value.get("unitText") or base.get("unitText"))


def salary_from_text(text):
    """Extract the first credible CAD/$ salary band from job-description text."""
    value = (text or "").replace("\u00a0", " ")
    amount = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s*[kK]?)"
    currency = r"(?:CA\$|CAD\s*\$?|\$)"
    range_re = re.compile(
        rf"{currency}\s*({amount})\s*(?:-|–|—|to)\s*"
        rf"(?:{currency}\s*)?({amount})([^.;\n]{{0,40}})",
        re.I,
    )
    for match in range_re.finditer(value):
        low = parse_salary_amount(match.group(1))
        high = parse_salary_amount(match.group(2))
        if low is None or high is None:
            continue
        unit = "HOUR" if re.search(r"\b(?:hour|hourly|hr)\b", match.group(3), re.I) else "YEAR"
        result = annualize_salary(min(low, high), max(low, high), unit)
        if result and 20_000 <= (result[1] or 0) <= 500_000:
            return result

    single_re = re.compile(
        rf"(?:salary|compensation|base\s+pay|starting\s+(?:salary|at)|"
        rf"pay\s+range)[^.;\n]{{0,35}}{currency}\s*({amount})([^.;\n]{{0,30}})",
        re.I,
    )
    match = single_re.search(value)
    if match:
        number = parse_salary_amount(match.group(1))
        unit = "HOUR" if re.search(r"\b(?:hour|hourly|hr)\b", match.group(2), re.I) else "YEAR"
        result = annualize_salary(number, number, unit)
        if result and 20_000 <= (result[0] or 0) <= 500_000:
            return result
    return None


def salary_reject_reason(salary):
    if not salary:
        return None
    low, high = salary
    if (low is not None and low > TARGET_SALARY_MAX_CAD) or (
        high is not None and high > TARGET_SALARY_MAX_CAD
    ):
        return "salary above target"
    if (low is not None and low < TARGET_SALARY_MIN_CAD) or (
        high is not None and high < TARGET_SALARY_MIN_CAD
    ):
        return "salary below target"
    return None


def format_salary(salary):
    if not salary:
        return ""
    low, high = salary
    if low is not None and high is not None and low != high:
        return f"CAD ${low:,.0f}-${high:,.0f}"
    amount = low if low is not None else high
    return f"CAD ${amount:,.0f}" if amount is not None else ""


def iter_nested_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_nested_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_nested_dicts(item)


def parse_relative_age(text):
    match = re.search(r"\b(\d+)\s+(minute|hour|day)s?\s+ago\b", text or "", re.I)
    if not match:
        return None
    count = int(match.group(1))
    seconds = {"minute": 60, "hour": 3600, "day": 86400}[match.group(2).lower()]
    return int(time.time() - count * seconds)


def extract_detail_page(html, page_url=""):
    """Extract full JD and quality metadata from a public job-detail page."""
    soup = BeautifulSoup(html or "", "html.parser")
    result = {
        "description": "", "posted_ts": None, "salary": None,
        "is_repost": False, "applicant_count": None, "detail_url": page_url,
        "location": "",
    }
    parsed_scripts = []
    for script in soup.select("script"):
        raw = script.string or script.get_text() or ""
        if not raw.lstrip().startswith(("{", "[")):
            continue
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        parsed_scripts.append(data)
        for posting in iter_jobposting_schema(data):
            raw_desc = str(posting.get("description") or "")
            desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)
            if len(desc) > len(result["description"]):
                result["description"] = desc
            result["posted_ts"] = result["posted_ts"] or parse_iso(posting.get("datePosted"))
            result["salary"] = result["salary"] or salary_from_schema(posting)
            result["location"] = result["location"] or schema_job_location(posting)

    # Workday, Simplify and several JS job boards embed posting data in app
    # state rather than standards-based JobPosting JSON-LD.
    for data in parsed_scripts:
        for node in iter_nested_dicts(data):
            raw_desc = node.get("jobDescription") or node.get("descriptionHtml")
            if not raw_desc and isinstance(node.get("description"), str):
                raw_desc = node.get("description")
            if raw_desc:
                desc = BeautifulSoup(str(raw_desc), "html.parser").get_text(" ", strip=True)
                if len(desc) > len(result["description"]):
                    result["description"] = desc
            if not result["posted_ts"]:
                for key in ("datePosted", "publishedAt", "firstPublished", "createdAt"):
                    result["posted_ts"] = parse_iso(node.get(key))
                    if result["posted_ts"]:
                        break

    if len(result["description"]) < DETAIL_MIN_DESCRIPTION_CHARS:
        selectors = (
            ".show-more-less-html__markup", ".description__text",
            "[data-automation-id='jobPostingDescription']", ".job-description",
            ".jobDescription", "#job-description", "article",
        )
        candidates = []
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text:
                    candidates.append(text)
        if candidates:
            result["description"] = max(candidates, key=len)

    page_text = soup.get_text(" ", strip=True)
    if not result["posted_ts"]:
        time_node = soup.select_one("time[datetime]")
        if time_node:
            result["posted_ts"] = parse_iso(time_node.get("datetime"))
    if not result["posted_ts"]:
        result["posted_ts"] = parse_relative_age(page_text)
    result["is_repost"] = bool(re.search(r"\b(?:reposted|re-posted)\b", page_text, re.I))
    applicant = re.search(
        r"\b(?:over\s+)?([\d,]+)\+?\s+(?:people\s+clicked\s+apply|applicants?)\b",
        page_text,
        re.I,
    )
    if applicant:
        result["applicant_count"] = int(applicant.group(1).replace(",", ""))
    result["salary"] = result["salary"] or salary_from_text(result["description"])
    return result


DETAIL_REQUIRED_SOURCES = {"linkedin", "indeed", "community", "workday"}
_DETAIL_CACHE = {}


def enrich_job_details(job):
    """Ensure every potentially eligible job has a verified complete JD."""
    source = job.get("source", "")
    existing = str(job.get("description") or "").strip()
    if "<" in existing and ">" in existing:
        existing = BeautifulSoup(existing, "html.parser").get_text(" ", strip=True)
    must_fetch = source in DETAIL_REQUIRED_SOURCES or len(existing) < DETAIL_MIN_DESCRIPTION_CHARS
    details = {
        "description": existing,
        "posted_ts": job.get("posted_ts"),
        "salary": salary_from_text(existing),
        "is_repost": False,
        "applicant_count": None,
        "detail_url": job.get("url", ""),
        "location": job.get("location", ""),
    }
    if must_fetch:
        url = job.get("url", "")
        if not url:
            return False, "details unavailable"
        if url in _DETAIL_CACHE:
            fetched = dict(_DETAIL_CACHE[url])
        else:
            try:
                response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                response.raise_for_status()
                fetched = extract_detail_page(response.text, response.url)
                _DETAIL_CACHE[url] = dict(fetched)
                time.sleep(DETAIL_PAGE_DELAY_SECONDS)
            except Exception as e:
                print(f"[detail:{source}] {url}: {e}")
                return False, "details unavailable"
        # A full detail page takes precedence over feed snippets/metadata.
        if fetched.get("description"):
            details["description"] = fetched["description"]
        details["posted_ts"] = fetched.get("posted_ts") or details["posted_ts"]
        details["salary"] = fetched.get("salary") or details["salary"]
        details["is_repost"] = fetched.get("is_repost", False)
        details["applicant_count"] = fetched.get("applicant_count")
        details["detail_url"] = fetched.get("detail_url") or url
        details["location"] = fetched.get("location") or details["location"]

    if len(details["description"]) < DETAIL_MIN_DESCRIPTION_CHARS:
        return False, "full JD unavailable"
    job.update(details)
    job["detail_verified"] = True
    if details["is_repost"]:
        return False, "reposted"
    if (
        source == "linkedin"
        and details["applicant_count"] is not None
        and details["applicant_count"] > LINKEDIN_MAX_APPLICANTS
    ):
        return False, "too many LinkedIn applicants"
    reason = salary_reject_reason(details["salary"])
    if reason:
        return False, reason
    if not details["posted_ts"]:
        return False, "first posted time unavailable"
    return True, None

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
                "description": description,
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
                    "description": description,
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
                            "description": desc,
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
                            "description": desc,
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
                    "description": desc,
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
        for search in ("new grad", "entry level", "junior"):
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
                    "description": desc,
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
                    "description": desc,
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
    if "match_score" in j:
        score = j["match_score"]
        bits.append(f"resume match {score}%" if score is not None else "resume match N/A")
    salary_label = format_salary(j.get("salary"))
    if salary_label:
        bits.append(salary_label)
    line2 = " · ".join(bits)
    lines = [line1]
    if line2:
        lines.append(line2)
    if j.get("matched_skills"):
        lines.append("Matched: " + ", ".join(j["matched_skills"][:6]))
    if j.get("missing_skills"):
        lines.append("Check: " + ", ".join(j["missing_skills"][:4]))
    if j.get("match_note"):
        lines.append("Match note: " + j["match_note"])
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
    resume_skills = load_resume_skills()
    fetchers = [
        fetch_bamboohr, fetch_generic_careers, fetch_greenhouse, fetch_lever,
        fetch_ashby, fetch_workday, fetch_community, fetch_indeed,
    ]
    if ENABLE_LINKEDIN:
        fetchers.append(fetch_linkedin)
    for fn in fetchers:
        try:
            jobs = fn()
            source = fn.__name__.removeprefix("fetch_")
            for job in jobs:
                job.setdefault("source", source)
                company = job.get("company", "")
                if company_blocked(company):
                    job["reject_reason"] = "blocked company"
                    continue
                initial_location = str(job.get("location") or "").strip()
                if initial_location and not location_ok(initial_location, company):
                    job["reject_reason"] = "location (outside GTA)"
                    continue
                verified, detail_reason = enrich_job_details(job)
                if REQUIRE_VERIFIED_DETAILS and not verified:
                    job["reject_reason"] = detail_reason
                    continue
                if not location_ok(job.get("location", ""), company):
                    job["reject_reason"] = "location (outside GTA)"
                    continue
                # Re-run hard filters against the complete JD. Jessie overrides
                # this function with her domain-specific policy.
                job["reject_reason"] = match_reject_reason(
                    job.get("title", ""), job.get("description", "")
                )
                if not job.get("reject_reason"):
                    add_skill_match(job, resume_skills)
            all_jobs.extend(jobs)
            print(f"[source:{source}] fetched {len(jobs)} candidates")
        except Exception as e:
            print(f"[{fn.__name__}] {e}")
        time.sleep(1)  # be polite
    return all_jobs

def run_alert():
    """Notify verified jobs originally posted within the configured window."""
    con = db_init()
    all_jobs = collect_all_jobs()
    now = time.time()
    cutoff = now - ALERT_WINDOW_MINUTES * 60

    new_jobs = []
    source_stats = {}
    stats = {
        "fetched": len(all_jobs),
        "no_time": 0,
        "outside_window": 0,
        "in_window": 0,
        "duplicate": 0,
        "filtered": 0,
        "examples": [],
    }

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
        if not ts:
            stats["no_time"] += 1
            increment_source(j, "no_time")
            add_example(stats, "first posted time unavailable", j)
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

    print(f"Fetched {len(all_jobs)} jobs, {stats['in_window']} eligible, "
          f"{stats['duplicate']} duplicates, {len(new_jobs)} new to notify")
    for source, counters in source_stats.items():
        print(
            f"[source:{source}] fetched {counters.get('fetched', 0)}, "
            f"filtered {counters.get('filtered', 0)}, "
            f"no time {counters.get('no_time', 0)}, "
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
