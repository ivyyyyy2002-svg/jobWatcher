#!/usr/bin/env python3
"""Job watcher configured for Jessie's biotech and medical job search.

This file reuses the collectors and notification code in jobwatch.py while
keeping Jessie's filters, searches, dedup database, and company list separate.
"""

import hashlib
import os
import re
from datetime import datetime, timezone

import jobwatch as watcher


# Broad life-sciences role taxonomy. Matching is title-first below so words such
# as "medical" in an unrelated employer description do not create noisy hits.
watcher.ROLE_RE = re.compile(
    r"\b(bio[\s-]*tech(?:nology)?|life[\s-]*sciences?|research(?:\s+assistant|"
    r"\s+associate|\s+coordinator|\s+technician)?|laboratory|lab\s+(?:assistant|"
    r"technician|technologist|analyst)|medical(?:\s+data|\s+device|\s+laboratory)?|"
    r"pharm(?:a|aceutical|aceuticals)?|clinical(?:\s+research|\s+trial|\s+data|"
    r"\s+operations)?|bio[\s-]*informatics?|biostatistics?|health\s+data|"
    r"computational\s+biology|genomics?|proteomics?|biomedical|biochemical|"
    r"bio[\s-]*process|bio[\s-]*manufacturing|quality\s+(?:assurance|control|"
    r"systems?)|\bqa\b|\bqc\b|compliance|validation|regulatory\s+affairs?|"
    r"document\s+control|pharmaco[\s-]*vigilance|drug\s+safety|medical\s+affairs|"
    r"technologist|molecular|nano[\s-]*(?:technology|technologist)|microbiology|"
    r"immunology|cell\s+(?:biology|culture|therapy)|tissue\s+culture|assay|"
    r"analytical\s+(?:chemistry|development)|formulation|upstream|downstream|"
    r"purification|fermentation|aseptic|sterility|gmp|glp|gcp|cmc|vaccine|"
    r"diagnostics?|specimen|sample\s+management|clinical\s+study)\b",
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

# The existing community feeds focus on software internships, so they add noise.
# This edition combines official employer pages with LinkedIn and Indeed.
watcher.COMMUNITY_REPOS = []
watcher.BAMBOOHR_COMPANIES = []
watcher.GREENHOUSE_COMPANIES = []
watcher.LEVER_COMPANIES = ["deepgenomics"]
watcher.ASHBY_COMPANIES = []
watcher.WORKDAY_COMPANIES = []
watcher.CAREER_DETAIL_PAGE_LIMIT = 15
watcher.GENERIC_CAREER_SITES = [
    ("Pfizer Canada", "https://www.pfizer.com/about/careers"),
    ("Sanofi Canada", "https://jobs.sanofi.com/en/location/toronto-ontario-canada-jobs/507-18104/6251999-6093943-6167865/4"),
    ("Johnson & Johnson", "https://www.careers.jnj.com/en/jobs/"),
    ("Novartis Canada", "https://www.novartis.com/ca-en/careers"),
    ("Merck Canada", "https://jobs.merck.com/us/en/"),
    ("Apotex", "https://www.apotex.com/global/about-us/careers"),
    ("SickKids", "https://www.sickkids.ca/en/careers-volunteer/careers/"),
    ("University Health Network", "https://www.uhn.ca/corporate/careers"),
    ("Women's College Hospital", "https://www.womenscollegehospital.ca/careers/"),
    ("Sinai Health", "https://www.sinaihealth.ca/careers-at-sinai-health/research-jobs"),
    ("CAMH", "https://www.camh.ca/en/driving-change/about-camh/careers"),
    ("Ontario Institute for Cancer Research", "https://oicr.on.ca/careers/"),
    ("University of Toronto", "https://jobs.utoronto.ca/"),
    ("Roche Canada", "https://careers.roche.com/global/en/canada"),
    ("AstraZeneca Canada", "https://careers.astrazeneca.com/canada"),
    ("GSK Canada", "https://www.gsk.com/en-gb/careers/"),
    ("Thermo Fisher Scientific", "https://jobs.thermofisher.com/global/en"),
    ("Eurofins Canada", "https://careers.eurofins.com/"),
    ("SGS Canada", "https://www.sgs.com/en-ca/our-company/careers-at-sgs"),
    ("Deep Genomics", "https://www.deepgenomics.com/careers/"),
    ("BenchSci", "https://www.benchsci.com/careers"),
]

SEARCH_LOCATION = "Greater Toronto Area, Canada"
SEARCH_TERMS = [
    "biotech associate", "life science assistant", "research assistant",
    "research coordinator", "research technician", "laboratory technician",
    "lab technologist", "medical laboratory", "pharmaceutical research",
    "clinical research coordinator", "clinical trial assistant",
    "clinical data coordinator", "medical data analyst", "bioinformatics",
    "biostatistics", "computational biology", "genomics analyst",
    "biomedical engineering", "medical device", "bioprocess technician",
    "biomanufacturing", "quality assurance pharma", "quality control lab",
    "GMP compliance", "validation specialist", "regulatory affairs associate",
    "pharmacovigilance", "drug safety associate", "molecular biology",
    "microbiology technician", "cell culture", "analytical chemistry",
    "formulation scientist", "upstream downstream technologist",
    "sample management", "specimen processing",
]
# Kept as explicit configuration/documentation and used for company-name
# canonicalization during cross-site deduplication.
PREFERRED_COMPANIES = [
    "Pfizer Canada", "Sanofi Canada", "Johnson & Johnson",
    "Novartis Pharmaceuticals Canada", "Merck Canada", "Apotex",
    "Pharmascience", "Bausch Health", "Antibe Therapeutics",
    "SickKids Research Institute", "UHN", "Krembil Research Institute",
    "Allan Slaight Medical Innovation Labs", "Sunnybrook Research Institute",
    "Women's College Hospital Research Institute", "Unity Health Research",
    "Sinai Health", "CAMH", "Ontario Institute for Cancer Research",
    "University of Toronto", "Roche Canada", "AstraZeneca Canada",
    "GSK Canada", "Bayer Canada", "Thermo Fisher Scientific",
    "Eurofins Canada", "SGS Canada", "LifeLabs", "Dynacare",
    "Deep Genomics", "BenchSci", "BlueRock Therapeutics",
    "POINT Biopharma", "Fusion Pharmaceuticals", "Kite Pharma",
]

# Targeted employer searches complement official pages that render their job
# boards entirely in JavaScript and therefore expose little HTML to a scraper.
COMPANY_SEARCH_GROUPS = [
    "Pfizer OR Sanofi OR Novartis OR Merck OR Apotex",
    '"Johnson & Johnson" OR Janssen OR Pharmascience OR Bausch',
    "SickKids OR UHN OR Krembil OR Sunnybrook",
    '"Women\'s College Hospital" OR "Unity Health" OR "Sinai Health" OR CAMH',
    '"Deep Genomics" OR BenchSci OR OICR OR "BlueRock Therapeutics"',
    'Roche OR AstraZeneca OR GSK OR Bayer OR "Thermo Fisher"',
    'LifeLabs OR Dynacare OR Eurofins OR SGS',
]
ALL_SEARCH_QUERIES = [
    (term, SEARCH_LOCATION) for term in SEARCH_TERMS
] + [
    (f"({companies}) (research OR clinical OR laboratory OR quality)", SEARCH_LOCATION)
    for companies in COMPANY_SEARCH_GROUPS
]

# GitHub-hosted IPs are quickly rate-limited when dozens of searches run at
# once. Rotate five queries per half-hour; the complete set is covered over
# several runs without hammering LinkedIn/Indeed on every invocation.
QUERY_BATCH_SIZE = 5
half_hour_slot = int(datetime.now(timezone.utc).timestamp() // 1800)
batch_start = (half_hour_slot * QUERY_BATCH_SIZE) % len(ALL_SEARCH_QUERIES)
watcher.LINKEDIN_QUERIES = [
    ALL_SEARCH_QUERIES[(batch_start + offset) % len(ALL_SEARCH_QUERIES)]
    for offset in range(QUERY_BATCH_SIZE)
]
watcher.INDEED_QUERIES = list(watcher.LINKEDIN_QUERIES)
print(
    f"[search-rotation] running {QUERY_BATCH_SIZE}/{len(ALL_SEARCH_QUERIES)} "
    f"queries this half-hour (batch starts at {batch_start})"
)

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
ENTRY_TITLE_RE = re.compile(
    r"\b(assistant|associate|coordinator|technician|technologist|analyst|"
    r"specialist|scientist\s*(?:i|1)?|engineer\s*(?:i|1)?|operator|officer|"
    r"new\s*grad(?:uate)?|junior|entry[\s-]*level)\b",
    re.I,
)
SCIENCE_CONTEXT_RE = re.compile(
    r"\b(research|laboratory|clinical|patient|healthcare|hospital|pharma|"
    r"biotech|biology|chemistry|medical|diagnostic|specimen|sample|gmp|gcp|glp)\b",
    re.I,
)
UNWANTED_TYPE_RE = re.compile(
    r"\b(part[\s-]*time|casual|on[\s-]*call|volunteer|unpaid|post[\s-]*doc|"
    r"postdoctoral|fellowship|faculty|professor|internship|intern|co[\s-]*op|"
    r"summer\s+student)\b",
    re.I,
)
UNRELATED_TITLE_RE = re.compile(
    r"\b(nurse|nursing|physician|surgeon|dentist|veterinarian|social\s+worker|"
    r"sales|account\s+manager|marketing|business\s+development|receptionist|"
    r"administrative\s+assistant|personal\s+support\s+worker|pharmacist)\b",
    re.I,
)


def match_reject_reason(title, description=""):
    """Apply broad title-first life-science matching and hard exclusions."""
    title = title or ""
    description = description or ""
    blob = f"{title} {description}"
    title_matches = watcher.ROLE_RE.search(title)
    generic_science_role = (
        ENTRY_TITLE_RE.search(title)
        and watcher.ROLE_RE.search(description)
        and SCIENCE_CONTEXT_RE.search(description)
    )
    if not title_matches and not generic_science_role:
        return "role"
    if UNRELATED_TITLE_RE.search(title):
        return "unrelated occupation"
    if watcher.SENIORITY_EXCLUDE_RE.search(title):
        return "seniority"
    if UNWANTED_TYPE_RE.search(blob):
        return "job type"
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
    blob = f"{title or ''} {description or ''}"
    if not watcher.EARLY_RE.search(blob):
        return "employment type/experience level not explicit; please verify"
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
# Never fall back to the owner's Discord channel. Jessie must have her own
# repository secret named JESSIE_DISCORD_WEBHOOK.
watcher.DISCORD_WEBHOOK = os.environ.get("JESSIE_DISCORD_WEBHOOK", "")
watcher.DB_PATH = os.environ.get(
    "JESSIE_JOBWATCH_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "jessie_seen_jobs.db"),
)


if __name__ == "__main__":
    watcher.main()
