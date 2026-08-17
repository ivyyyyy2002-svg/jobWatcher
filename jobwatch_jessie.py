#!/usr/bin/env python3
"""Job watcher configured for Jessie's biotech and medical job search.

This file reuses the collectors and notification code in jobwatch.py while
keeping Jessie's filters, searches, dedup database, and company list separate.
"""

import hashlib
import os
import re

import jobwatch as watcher


# Target roles. Common spelling variants are included deliberately.
watcher.ROLE_RE = re.compile(
    r"\b(bio[\s-]*tech(?:nology)?|life\s*sciences?|research\s+assistant|"
    r"medical(?:\s+data\s+analyst)?|pharm(?:a|aceutical|aceuticals)?(?:\s+research)?|"
    r"bio[\s-]*informatics?|clinical|biomedical(?:\s+engineering?)?|"
    r"quality\s+assurance|qa|technologist|molecular|nano[\s-]*technologist|"
    r"biochemical(?:\s+engineering?)?)\b",
    re.I,
)

# Requested employment levels/types. Senior roles are still rejected below.
watcher.EARLY_RE = re.compile(
    r"\b(full[\s-]*time|permanent|new\s*grad(?:uate)?|graduate|junior|"
    r"contract|entry[\s-]*level|early\s*career|early[\s-]*talent)\b",
    re.I,
)
watcher.INTERN_RE = re.compile(r"(?!)")
watcher.NEW_GRAD_RE = re.compile(
    r"\b(new\s*grad(?:uate)?|graduate|junior|entry[\s-]*level|"
    r"early\s*career|early[\s-]*talent)\b",
    re.I,
)
watcher.TERM_RE = watcher.EARLY_RE
watcher.OTHER_TERM_RE = re.compile(r"(?!)")

# Only the five requested Greater Toronto Area locations are accepted.
watcher.LOCATION_MODE = "whitelist"
watcher.LOCATION_INCLUDE = [
    "toronto", "north york", "mississauga", "scarborough", "etobicoke",
]
watcher.LOCATION_EXCLUDE = []
watcher.KEEP_UNKNOWN_LOCATION = False

# The existing community feeds focus on software internships, so they add noise
# for this search. LinkedIn and Indeed searches below cover the requested roles.
watcher.COMMUNITY_REPOS = []
watcher.BAMBOOHR_COMPANIES = []
watcher.GREENHOUSE_COMPANIES = []
watcher.LEVER_COMPANIES = []
watcher.ASHBY_COMPANIES = []
watcher.WORKDAY_COMPANIES = []
watcher.GENERIC_CAREER_SITES = []

SEARCH_LOCATION = "Greater Toronto Area, Canada"
SEARCH_TERMS = [
    "biotech", "research assistant", "medical", "pharmaceutical research",
    "medical data analyst", "bioinformatics", "clinical",
    "biomedical engineering", "quality assurance", "technologist",
    "molecular", "nano technologist", "biochemical engineering",
]
watcher.LINKEDIN_QUERIES = [
    (term, SEARCH_LOCATION) for term in SEARCH_TERMS
]
watcher.INDEED_QUERIES = list(watcher.LINKEDIN_QUERIES)

# Kept as explicit configuration/documentation and used for company-name
# canonicalization during cross-site deduplication.
PREFERRED_COMPANIES = [
    "Pfizer Canada", "Sanofi Canada", "Johnson & Johnson",
    "Novartis Pharmaceuticals Canada", "Merck Canada", "Apotex",
    "Pharmascience", "Bausch Health", "Antibe Therapeutics",
    "SickKids Research Institute", "UHN", "Krembil Research Institute",
    "Allan Slaight Medical Innovation Labs", "Sunnybrook Research Institute",
    "Women's College Hospital Research Institute", "Unity Health Research",
    "Sinai Health",
]

FRENCH_OR_BILINGUAL_RE = re.compile(r"\b(french|bilingual|fran[cç]ais)\b", re.I)
LIFTING_RE = re.compile(
    r"\b(?:lift|carry|weigh(?:ing)?)[^.]{0,50}(?:23\s*kg|50\s*(?:lb|lbs|pounds?))\b",
    re.I,
)
DRIVER_RE = re.compile(
    r"\b(driver'?s?\s+licen[cs]e|valid\s+licen[cs]e|class\s+g[12]?|g[12]?\s+licen[cs]e)\b",
    re.I,
)
CERTIFICATE_RE = re.compile(
    r"\b(?:require[sd]?|must\s+(?:have|hold)|mandatory)[^.]{0,60}"
    r"(?:certificate|certification)|\b(?:certificate|certification)\s+required\b",
    re.I,
)
STATUS_RE = re.compile(
    r"\b(permanent\s+resident(?:cy)?|canadian\s+citizen(?:ship)?|"
    r"citizens?\s+only|pr\s+status)\b",
    re.I,
)
PHD_ONLY_RE = re.compile(
    r"\b(?:ph\.?\s*d\.?|doctorate|doctoral)[^.]{0,50}"
    r"(?:required|only|must|minimum)|\b(?:requires?|must\s+have)[^.]{0,50}"
    r"(?:ph\.?\s*d\.?|doctorate)\b",
    re.I,
)


def match_reject_reason(title, description=""):
    """Apply Jessie's role, job-type, seniority, and hard exclusions."""
    blob = f"{title or ''} {description or ''}"
    if not watcher.ROLE_RE.search(blob):
        return "role"
    if not watcher.EARLY_RE.search(blob):
        return "job type"
    if watcher.SENIORITY_EXCLUDE_RE.search(title or ""):
        return "seniority"
    exclusions = (
        (FRENCH_OR_BILINGUAL_RE, "French/bilingual"),
        (LIFTING_RE, "lifting requirement"),
        (DRIVER_RE, "driver's licence"),
        (CERTIFICATE_RE, "certificate"),
        (STATUS_RE, "residency/citizenship"),
        (PHD_ONLY_RE, "PhD required"),
    )
    for pattern, reason in exclusions:
        if pattern.search(blob):
            return reason
    return None


def match_note(title, description=""):
    return ""


COMPANY_ALIASES = {
    "johnson johnson": "johnson and johnson",
    "jnj": "johnson and johnson",
    "university health network": "uhn",
    "sickkids": "sickkids research institute",
    "hospital for sick children": "sickkids research institute",
    "womens college hospital": "womens college hospital research institute",
    "merck canada inc": "merck canada",
}


def normalize_dedup_text(value):
    text = (value or "").lower().replace("&", " and ")
    text = re.sub(r"\b(?:inc|incorporated|ltd|limited|corp|corporation|company|co)\b", " ", text)
    text = re.sub(r"\b(?:full[\s-]*time|permanent|contract|toronto|ontario|canada)\b", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def make_uid(company, title, url=""):
    """Deduplicate the same company/title even across sites or posting dates."""
    normalized_company = normalize_dedup_text(company)
    normalized_company = COMPANY_ALIASES.get(normalized_company, normalized_company)
    normalized_title = normalize_dedup_text(title)
    key = f"{normalized_company}||{normalized_title}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# Install the friend-specific behavior into the shared collectors.
watcher.match_reject_reason = match_reject_reason
watcher.match_note = match_note
watcher.make_uid = make_uid
watcher.DB_PATH = os.environ.get(
    "JESSIE_JOBWATCH_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "jessie_seen_jobs.db"),
)


if __name__ == "__main__":
    watcher.main()
