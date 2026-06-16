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
from datetime import datetime, timezone
from urllib.parse import quote_plus

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
ALERT_WINDOW_MINUTES = 60

# --- Daily digest ---
# A separate "digest" run (meant for ~midnight) summarizes everything posted
# during the day, regardless of whether it was already alerted. It does NOT
# touch the dedup DB, so it never interferes with alert mode.
DIGEST_LOOKBACK_HOURS = 24

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
LINKEDIN_QUERIES = [
    ("software engineer intern fall 2026", "Canada"),
    ("software intern fall 2026", "Canada"),
    ("developer intern fall 2026", "Canada"),
    ("software developer intern fall 2026", "Canada"),
    ("software engineering co-op fall 2026", "Canada"),
    ("developer co-op fall 2026", "Canada"),
    ("4 month software intern September 2026", "Canada"),
    ("data analyst intern fall 2026", "Canada"),
    ("data intern fall 2026", "Canada"),
    ("qa test intern fall 2026", "Canada"),
    ("quality assurance intern fall 2026", "Canada"),
    ("cloud devops intern fall 2026", "Canada"),
    ("IT intern fall 2026", "Canada"),
    ("technology intern fall 2026", "Canada"),
    ("technical analyst intern fall 2026", "Canada"),
    ("new grad software engineer 2026", "Canada"),
    ("junior software developer 2026", "Canada"),
    ("junior software engineer Canada 2026", "Canada"),
    ("entry level software developer 2026", "Canada"),
    ("entry level technology analyst 2026", "Canada"),
    ("technology analyst new grad 2026", "Canada"),
]

# --- Indeed search keywords / location ---
INDEED_QUERIES = [
    ("software engineer intern fall 2026", "Canada"),
    ("software intern fall 2026", "Canada"),
    ("developer intern fall 2026", "Canada"),
    ("software developer intern fall 2026", "Canada"),
    ("computer engineering intern fall 2026", "Canada"),
    ("software engineering co-op fall 2026", "Canada"),
    ("developer co-op fall 2026", "Canada"),
    ("4 month software intern September 2026", "Canada"),
    ("data analyst intern fall 2026", "Canada"),
    ("data intern fall 2026", "Canada"),
    ("qa test intern fall 2026", "Canada"),
    ("quality assurance intern fall 2026", "Canada"),
    ("cloud devops intern fall 2026", "Canada"),
    ("cybersecurity intern fall 2026", "Canada"),
    ("IT intern fall 2026", "Canada"),
    ("technology intern fall 2026", "Canada"),
    ("technical analyst intern fall 2026", "Canada"),
    ("software engineer new grad 2026", "Canada"),
    ("software developer new grad 2026", "Canada"),
    ("junior software developer 2026", "Canada"),
    ("junior software engineer Canada 2026", "Canada"),
    ("entry level software engineer 2026", "Canada"),
    ("entry level technology analyst 2026", "Canada"),
    ("technology analyst new grad 2026", "Canada"),
]

# --- Notification method: pick one ---
NOTIFY = "discord"   # "discord" | "telegram" | "email" | "print"

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
    con.commit()
    return con

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
        return int(_dt.fromisoformat(s).timestamp())
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
        dt = datetime.fromtimestamp(ts)
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
    now = time.time()
    if "today" in t:
        return int(now)
    if "yesterday" in t:
        return int(now - 86400)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return int(now - int(m.group(1)) * 86400)
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return int(now - int(m.group(1)) * 30 * 86400)
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
                                    "reject_reason": reject_reason(title, search, loc),
                                    "note": match_note(title, search)})
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
                    "reject_reason": reject_reason(title, kw, job_loc),
                    "note": match_note(title, kw),
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
                match_text = f"{kw} {desc}"
                out.append({
                    "title": title,
                    "company": company,
                    "location": job_loc,
                    "url": link,
                    "posted_ts": parse_rss_date(pub),
                    "reject_reason": reject_reason(title, match_text, job_loc),
                    "note": match_note(title, match_text),
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
    Discord caps each message at 2000 chars, so we batch blocks under that.
    Jobs are separated with a light divider for readability."""
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
            r = requests.post(DISCORD_WEBHOOK, json={"content": SEP.join(b)},
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
    msg["Subject"] = f"New job alert {datetime.now():%m-%d %H:%M}"
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
    stamp, ago = humanize_age(j.get("posted_ts"))
    title = j.get("title", "")
    company = j.get("company", "")
    line1 = f"**{title}**"
    bits = []
    if company:
        bits.append(company)
    if j.get("location"):
        bits.append(j["location"])
    if ago:
        bits.append(ago)
    line2 = " · ".join(bits)
    line3 = j.get("url", "")
    lines = ["---", line1]
    if line2:
        lines.append(line2)
    if j.get("note"):
        lines.append(f"Note: {j['note']}")
    if line3:
        lines.append(line3)
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
    now = datetime.now().strftime("%b %d %H:%M")
    noun = "posting" if count == 1 else "postings"
    lines = [f"**Jobwatch: {count} new {noun}** · {now}"]
    if stats:
        summary = [
            f"window {alert_window_label()}",
            f"candidates {stats['fetched']}",
            f"usable {stats['in_window']}",
        ]
        if stats.get("duplicate"):
            summary.append(f"duplicate {stats['duplicate']}")
        if stats.get("filtered"):
            summary.append(f"filtered {stats['filtered']}")
        lines.append(" · ".join(summary))
        if count:
            lines.append("")
    else:
        lines.append(f"window {alert_window_label()}")
        if count:
            lines.append("")
    if stats:
        if stats.get("examples"):
            lines.append("Filtered examples:")
            for reason, label in stats["examples"]:
                lines.append(f"- {reason}: {label}")
    return "\n".join(lines)

def send(jobs, header=None):
    if header is None:
        header = alert_header(len(jobs))
    if not jobs:
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
    for fn in (fetch_greenhouse, fetch_lever, fetch_ashby, fetch_workday,
               fetch_community, fetch_linkedin, fetch_indeed):
        try:
            all_jobs.extend(fn())
        except Exception as e:
            print(f"[{fn.__name__}] {e}")
        time.sleep(1)  # be polite
    return all_jobs

def run_alert():
    """Incremental mode: notify ONLY about jobs whose posting time falls within
    the last ALERT_WINDOW_MINUTES. The dedup DB is a backstop against repeats.

    Jobs without a minute-precise posting time are skipped in alert mode because
    they cannot be safely proven to belong to the current window."""
    con = db_init()
    all_jobs = collect_all_jobs()
    now = time.time()
    cutoff = now - ALERT_WINDOW_MINUTES * 60

    def is_date_only(ts):
        # midnight local time -> the source only gave us a date
        dt = datetime.fromtimestamp(ts)
        return dt.hour == 0 and dt.minute == 0 and dt.second == 0

    new_jobs = []
    stats = {
        "fetched": len(all_jobs),
        "no_time": 0,
        "date_only": 0,
        "outside_window": 0,
        "in_window": 0,
        "duplicate": 0,
        "filtered": 0,
        "examples": [],
    }
    for j in all_jobs:
        reason = j.get("reject_reason")
        if reason:
            stats["filtered"] += 1
            if reason not in ("role", "level"):
                add_example(stats, reason, j)
            continue
        ts = j.get("posted_ts")
        if not ts:
            stats["no_time"] += 1
            add_example(stats, "missing exact time", j)
            continue
        if is_date_only(ts):
            stats["date_only"] += 1
            add_example(stats, "date only", j)
            continue
        if ts < cutoff:
            stats["outside_window"] += 1
            add_example(stats, "outside window", j)
            continue
        stats["in_window"] += 1
        # Backstop: skip anything we've already notified about.
        uid = make_uid(j["company"], j["title"], j["url"])
        if not is_new(con, uid):
            stats["duplicate"] += 1
            continue
        new_jobs.append(j)
        mark_seen(con, uid)

    print(f"Fetched {len(all_jobs)} jobs, {stats['in_window']} eligible, "
          f"{stats['duplicate']} duplicates, {len(new_jobs)} new to notify")
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
    header = (f"📊 **Daily digest** · {datetime.now():%b %d}\n"
              f"{n} posting{'s' if n != 1 else ''} in the last "
              f"{DIGEST_LOOKBACK_HOURS}h")
    print(f"Digest: {n} jobs in last {DIGEST_LOOKBACK_HOURS}h")
    if n:
        send(todays, header=header)
    elif NOTIFY == "discord" and DISCORD_WEBHOOK:
        # still send a heartbeat so you know it ran
        notify_discord([], header + "\n(nothing new today)")

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
