#!/usr/bin/env python3
"""
NAIL Digest — NAIL Digest: AI-Driven Nursing Informatics
=============================================
Implements the NAIL Digest Prompt Engineering Spec v1.0
Born at AINurse-26, Ottawa, July 2026 · NAIL Collaborative

Usage:
    python3 ni_biweekly.py                           # Run with defaults (PubMed, 14 days, all topics)
    python3 ni_biweekly.py --days 14                 # Set lookback window in days
    python3 ni_biweekly.py --max 50                  # Max papers per source (default: 50)
    python3 ni_biweekly.py --issue 2                 # Set issue number (default: 1)
    python3 ni_biweekly.py --dry-run                 # Fetch papers but skip API calls (for testing)
    python3 ni_biweekly.py --config config.json      # Load audience-voted config (sources, style, topics)
    python3 ni_biweekly.py --output html             # Output HTML only (default: both HTML + JSON)
    python3 ni_biweekly.py --output json             # Output JSON only
    python3 ni_biweekly.py --classify-only          # Cheap preview: fetch + classify only, no summaries, see topic breakdown
    python3 ni_biweekly.py --clean                   # Reset index.html to blank (run before going live)

Config file (config.json) controls:
    sources        — which databases to search: "pubmed", "arxiv", "medrxiv", "cinahl"
    summary_style  — how papers are summarised: "1_sentence", "2-3_sentences", "structured", "ai_decides"
    topics         — which of the 8 topic buckets to include (audience vote at AINurse-26)

Filtering: papers are classified BEFORE summarising. Any paper that doesn't
match one of the chosen topics with HIGH confidence is dropped entirely —
it does not appear in the issue as "Other / Unclassified" and does not
incur the cost of a summary call. This keeps issue size and API cost tied
directly to the community's chosen scope.

Requirements:
    python3 -m pip install anthropic requests
    export ANTHROPIC_API_KEY="sk-ant-..."
    export EBSCO_CUST_ID="s5240361"                   # customer ID from EBSCO setup email
    export EBSCO_GROUP_ID="main"                       # default; set only to override
    export EBSCO_PROFILE_ID="cineit"                   # default; set only to override
    export EBSCO_PROFILE_PWD="your-eit-profile-password"   # only needed if "cinahl" is in sources
    export EBSCO_DB="cul"                               # default (per EBSCO EIT profile); set only to override

The script writes (run from inside the ni-biweekly/ folder):
    ni-biweekly-YYYY-MM-DD.html   — issue page (deploy to ni-biweekly/ in nail-website repo)
    ni-biweekly-YYYY-MM-DD.json   — structured data archive
    index.html                    — hub page, auto-regenerated from all JSON files each run
"""

import os
import json
import time
import argparse
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import HTTPError

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install anthropic")
    exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION (edit these or set as environment variables)
# ─────────────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-5"

# Community-agreed topic buckets (AINurse-26 vote, July 10 2026)
TOPIC_BUCKETS = [
    "Clinical Decision Support",
    "NLP & Generative AI",
    "AI Ethics & Governance",
    "EHR & Workflows",
    "Workforce & Education",
    "Patient-Facing AI",
    "AI Methods & Evaluation",
    "Other / Unclassified",
]

# Banned superlatives (from spec)
BANNED_WORDS = [
    "groundbreaking", "revolutionary", "pioneering",
    "first ever", "first-ever", "unprecedented", "cutting-edge",
    "game-changing", "transformative", "landmark", "seminal",
    "paradigm shift", "breakthrough",
]

# PubMed E-utilities base URL
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Community-agreed search query (AINurse-26 vote)
PUBMED_SEARCH_TERMS = """(
    "nursing informatics"[MeSH Terms] OR
    "nursing informatics"[Title/Abstract] OR
    ("artificial intelligence"[Title/Abstract] AND "nursing"[Title/Abstract]) OR
    ("machine learning"[Title/Abstract] AND "nursing"[Title/Abstract]) OR
    ("natural language processing"[Title/Abstract] AND "nursing"[Title/Abstract]) OR
    ("large language model"[Title/Abstract] AND "nursing"[Title/Abstract]) OR
    ("clinical decision support"[Title/Abstract] AND "nursing"[Title/Abstract]) OR
    ("electronic health record"[Title/Abstract] AND "nursing"[Title/Abstract] AND
        ("artificial intelligence"[Title/Abstract] OR "machine learning"[Title/Abstract] OR
         "algorithm"[Title/Abstract] OR "predictive"[Title/Abstract]))
)
AND "humans"[MeSH Terms]
AND "journal article"[pt]
AND english[la]"""


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT (from spec)
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are NI Weekly Summarizer, an AI assistant that helps the nursing informatics \
community stay current with research literature. Your role is strictly limited to summarising PubMed \
abstracts. You are not an expert, an advisor, or an authority. You do not draw clinical conclusions \
or make recommendations.

You are part of a trustworthy AI pipeline designed with the following commitments:
- Faithfulness: you only report what the abstract states
- Transparency: you flag uncertainty rather than hide it
- Restraint: you do not add information not in the abstract
- Humility: you acknowledge what the study cannot tell us

These commitments were established by the nursing informatics community at AINurse-26 \
(Ottawa, July 2026) and are not negotiable."""

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT CONFIG — overridden by --config JSON file
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "sources":        ["pubmed"],
    "summary_style":  "2-3_sentences",   # "1_sentence" | "2-3_sentences" | "structured" | "ai_decides"
    "topics":         list(TOPIC_BUCKETS),
}

# Summary instruction block for each style
SUMMARY_INSTRUCTIONS = {
    "1_sentence": (
        "Write exactly ONE sentence (maximum 30 words) stating the single most important finding. "
        "Maximum 30 words.",
        30
    ),
    "2-3_sentences": (
        "Write a 2–3 sentence summary following this structure:\n"
        "1. What was studied (population, setting, or problem)\n"
        "2. What was found (key result or contribution)\n"
        "3. Why it matters for nursing practice or nursing informatics research\n"
        "Maximum 75 words.",
        75
    ),
    "structured": (
        "Write a structured summary using exactly these three labelled fields (one sentence each):\n"
        "Methods: <how the study was done>\n"
        "Finding: <the key result>\n"
        "Relevance to practice: <why it matters for nursing>\n"
        "Maximum 75 words total.",
        75
    ),
    "ai_decides": (
        "Choose the most appropriate format for this paper type "
        "(brief narrative, structured fields, or single finding) and summarise accordingly. "
        "Maximum 75 words.",
        75
    ),
}


def build_summarise_prompt(paper: dict, style: str) -> str:
    """Return the summarisation prompt adapted for the chosen style."""
    instructions, _ = SUMMARY_INSTRUCTIONS.get(style, SUMMARY_INSTRUCTIONS["2-3_sentences"])
    return f"""You will summarise a nursing informatics research abstract for a bi-weekly community digest.

ABSTRACT:
---
{paper['abstract']}
---

METADATA:
- Title: {paper['title']}
- Authors: {paper['authors']}
- Journal: {paper['journal']}
- Publication date: {paper['pub_date']}
- PMID: {paper.get('pmid', 'N/A')}
- DOI: {paper.get('doi', '')}
- Source: {paper.get('source', 'PubMed')}

INSTRUCTIONS:
{instructions}

HARD RULES — you must follow all of these without exception:
- Write only from information in the abstract above. Do not use your training knowledge to add, infer, or complete missing information.
- Do not speculate about implications not stated in the abstract.
- Do not use the words "groundbreaking", "revolutionary", "novel", "first ever", "unprecedented", or any superlative unless those exact words appear in the abstract.
- Write in plain language at approximately an 8th-grade reading level.
- Do not use passive voice where active voice is possible.

FLAGGING RULES — if any of the following apply, add the flag in the JSON:
- The abstract does not state the study population → flag: "population_unclear"
- The abstract does not state the setting or context → flag: "setting_unclear"
- The abstract describes work in progress with no results yet → flag: "in_progress"
- The paper is an unreviewed preprint, OR is itself a pre-registered protocol for a future systematic review (a plan for a review, not a completed one), OR is a letter/correspondence piece → flag: "not_peer_reviewed". Do NOT apply this flag to a completed narrative, literature, or systematic review that has been published in a peer-reviewed journal — those went through the same peer review as original research and are not preprints or protocols.
- The abstract is fewer than 50 words or appears incomplete → flag: "abstract_incomplete"

OUTPUT FORMAT — return only valid JSON, nothing else:
{{
  "summary": "<your summary here>",
  "flags": []
}}"""


def build_classify_prompt(paper: dict, active_topics: list[str]) -> str:
    """Return the classification prompt using only the audience-agreed topics."""
    all_defs = {
        "Clinical Decision Support":  "AI tools that help clinicians make decisions at the point of care",
        "NLP & Generative AI":        "natural language processing, LLMs, text mining, documentation AI",
        "AI Ethics & Governance":     "bias, fairness, accountability, privacy, consent, governance frameworks",
        "EHR & Workflows":            "electronic health records, workflow integration, documentation burden, interoperability",
        "Workforce & Education":      "nursing AI literacy, training, education, competency, workforce development",
        "Patient-Facing AI":          "chatbots, apps, and tools that patients interact with directly",
        "AI Methods & Evaluation":    "new algorithms, benchmarks, evaluation frameworks, datasets",
        "Other / Unclassified":       "does not clearly fit any of the above",
    }
    buckets = [t for t in active_topics if t != "Other / Unclassified"] + ["Other / Unclassified"]
    bucket_lines = "\n".join(
        f"{i+1}. {t} — {all_defs.get(t, t)}"
        for i, t in enumerate(buckets)
    )
    return f"""Classify the following nursing informatics paper into exactly one of the topic buckets listed below.
Use only the title and abstract. If the paper fits multiple buckets, choose the most prominent.

TITLE: {paper['title']}
ABSTRACT: {paper['abstract'][:1000]}

TOPIC BUCKETS:
{bucket_lines}

Return only valid JSON, nothing else:
{{ "topic": "<exact bucket name>", "confidence": "high" | "medium" | "low" }}"""
# ─────────────────────────────────────────────────────────────────────────────

def build_date_range(days_back: int) -> tuple[str, str]:
    end = datetime.today()
    start = end - timedelta(days=days_back)
    return start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")


def search_pubmed(days_back: int = 7, max_results: int = 50) -> list[str]:
    """Return a list of PMIDs matching the community search query."""
    start_date, end_date = build_date_range(days_back)
    query = PUBMED_SEARCH_TERMS.replace("\n", " ").strip()
    query += f' AND ("{start_date}"[dp]:"{end_date}"[dp])'

    params = urlencode({
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "pub+date",
    })
    url = f"{PUBMED_BASE}/esearch.fcgi?{params}"

    try:
        with urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read())
        pmids = data["esearchresult"]["idlist"]
        print(f"  PubMed returned {len(pmids)} PMIDs for {start_date} – {end_date}")
        return pmids
    except Exception as e:
        print(f"  ERROR searching PubMed: {e}")
        return []


def fetch_abstracts(pmids: list[str]) -> list[dict]:
    """Fetch full abstract data for a list of PMIDs."""
    if not pmids:
        return []

    params = urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    })
    url = f"{PUBMED_BASE}/efetch.fcgi?{params}"

    try:
        with urlopen(url, timeout=30) as resp:
            xml_data = resp.read()
    except Exception as e:
        print(f"  ERROR fetching abstracts: {e}")
        return []

    papers = []
    root = ET.fromstring(xml_data)

    for article in root.findall(".//PubmedArticle"):
        try:
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text if pmid_el is not None else "unknown"

            # Title
            title_el = article.find(".//ArticleTitle")
            title = "".join(title_el.itertext()) if title_el is not None else ""

            # Abstract text (may have multiple AbstractText elements)
            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join("".join(p.itertext()) for p in abstract_parts).strip()

            # Authors
            authors = []
            for author in article.findall(".//Author"):
                last = author.findtext("LastName", "")
                initials = author.findtext("Initials", "")
                if last:
                    authors.append(f"{last} {initials}".strip())
            author_str = ", ".join(authors[:6])
            if len(authors) > 6:
                author_str += " et al."

            # Journal
            journal = article.findtext(".//Journal/Title", "") or \
                      article.findtext(".//MedlineJournalInfo/MedlineTA", "")

            # Publication date
            pub_year  = article.findtext(".//PubDate/Year", "")
            pub_month = article.findtext(".//PubDate/Month", "")
            pub_date  = f"{pub_month} {pub_year}".strip() if pub_year else "2026"

            # DOI — look only in PubmedData/ArticleIdList, NOT .// which picks up reference DOIs
            doi = ""
            for id_el in article.findall("PubmedData/ArticleIdList/ArticleId"):
                if id_el.attrib.get("IdType") == "doi":
                    doi = id_el.text or ""
                    break

            papers.append({
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": author_str,
                "journal": journal,
                "pub_date": pub_date,
                "doi": doi,
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            })
        except Exception as e:
            print(f"  WARNING: could not parse article: {e}")
            continue

    print(f"  Parsed {len(papers)} abstracts")
    return papers


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1b — FETCH FROM ADDITIONAL SOURCES (arXiv, medRxiv, CINAHL)
# ─────────────────────────────────────────────────────────────────────────────

ARXIV_NI_QUERY = (
    '(ti:%22nursing%22+AND+(ti:%22artificial+intelligence%22+OR+ti:%22machine+learning%22'
    '+OR+ti:%22natural+language+processing%22+OR+ti:%22large+language+model%22))'
    '+OR+ti:%22nursing+informatics%22'
)

def fetch_arxiv(days_back: int = 14, max_results: int = 20) -> list[dict]:
    """Fetch nursing informatics preprints from arXiv (cs.AI, cs.CL, eess.IV categories)."""
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y%m%d")
    url = (
        f"https://export.arxiv.org/api/query?search_query={ARXIV_NI_QUERY}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    )
    try:
        import requests as req

        r = None
        for attempt in range(3):
            r = req.get(url, headers={"User-Agent": "NI-Biweekly/1.0 (ainurse@nailcollab.org)"}, timeout=60)
            if r.status_code != 429:
                break
            # arXiv is rate-limiting us. Respect Retry-After if given, else
            # back off with increasing delay — arXiv's own courtesy guideline
            # is roughly one request per 3 seconds.
            wait = int(r.headers.get("Retry-After", 0)) or (5 * (attempt + 1))
            print(f"  arXiv rate-limited (429) — waiting {wait}s before retry {attempt+1}/3...")
            time.sleep(wait)

        if not r.ok:
            print(f"  WARNING: arXiv HTTP {r.status_code} — {r.text[:300]}")
            return []
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            print(f"  WARNING: arXiv returned non-XML response: {e}")
            print(f"  Response status: {r.status_code} · Content-Type: {r.headers.get('Content-Type')}")
            print(f"  First 300 chars of response: {r.text[:300]!r}")
            return []
        papers = []
        for entry in root.findall("atom:entry", ns):
            pub = entry.findtext("atom:published", "", ns)[:10].replace("-", "")
            if pub < cutoff:
                continue
            arxiv_id = entry.findtext("atom:id", "", ns).split("/")[-1]
            title   = "".join(entry.find("atom:title", ns).itertext()).strip().replace("\n", " ")
            summary = "".join(entry.find("atom:summary", ns).itertext()).strip().replace("\n", " ")
            authors = ", ".join(
                "".join(a.find("atom:name", ns).itertext())
                for a in entry.findall("atom:author", ns)[:6]
            )
            papers.append({
                "pmid":        f"arxiv:{arxiv_id}",
                "title":       title,
                "abstract":    summary,
                "authors":     authors,
                "journal":     "arXiv (preprint)",
                "pub_date":    pub[:4],
                "doi":         entry.findtext("atom:id", "", ns),
                "pubmed_url":  f"https://arxiv.org/abs/{arxiv_id}",
                "source":      "arXiv",
            })
        print(f"  arXiv returned {len(papers)} papers")
        return papers
    except Exception as e:
        print(f"  WARNING: arXiv fetch failed: {type(e).__name__}: {e}")
        return []


MEDRXIV_NI_TERMS = ["nursing", "nurse", "nursing informatics"]

def fetch_medrxiv(days_back: int = 14, max_results: int = 20) -> list[dict]:
    """Fetch nursing informatics preprints from medRxiv."""
    from datetime import datetime, timedelta, timezone
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url   = f"https://api.biorxiv.org/details/medrxiv/{start}/{end}/0/json"
    try:
        import requests as req
        r = req.get(url, headers={"User-Agent": "NI-Biweekly/1.0 (ainurse@nailcollab.org)"}, timeout=30)
        data = r.json()
        papers = []
        for item in data.get("collection", [])[:max_results]:
            text = (item.get("title", "") + " " + item.get("abstract", "")).lower()
            if not any(t in text for t in MEDRXIV_NI_TERMS):
                continue
            doi = item.get("doi", "")
            papers.append({
                "pmid":       f"medrxiv:{doi}",
                "title":      item.get("title", ""),
                "abstract":   item.get("abstract", ""),
                "authors":    item.get("authors", ""),
                "journal":    "medRxiv (preprint)",
                "pub_date":   item.get("date", "")[:4],
                "doi":        doi,
                "pubmed_url": f"https://doi.org/{doi}" if doi else "",
                "source":     "medRxiv",
            })
        print(f"  medRxiv returned {len(papers)} papers (after nursing filter)")
        return papers
    except Exception as e:
        print(f"  WARNING: medRxiv fetch failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CINAHL via EBSCOhost Integration Toolkit (EIT) — REST/GET web service
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: This account uses the older EIT web service, NOT the EDS REST API.
# EIT is stateless — no auth token, no session. Every request carries
# prof + pwd directly. Confirmed via EBSCO support, June 2026.

EIT_SEARCH_URL = "http://eit.ebscohost.com/Services/SearchService.asmx/Search"

CINAHL_QUERY = (
    '(nursing OR "nursing informatics") AND '
    '("artificial intelligence" OR "machine learning" OR "natural language processing" OR '
    '"large language model" OR "clinical decision support" OR "generative AI" OR '
    'algorithm OR predictive)'
)


def fetch_cinahl(days_back: int = 14, max_results: int = 20) -> list[dict]:
    """Fetch nursing informatics articles from CINAHL via EBSCOhost EIT web service.

    EIT is a stateless GET-based API — every request carries the profile
    and password directly, no auth token or session needed.

    Output is capped at 25 papers WITH abstracts (records lacking an abstract
    are skipped and do not count toward the cap). This cap is fixed and does
    not scale with the global --max flag, since CINAHL Ultimate mixes brief
    items with research articles and needs a larger raw pool to filter from.

    Required environment variables:
        EBSCO_CUST_ID     — customer ID (default: s5240361)
        EBSCO_GROUP_ID    — group ID (default: main)
        EBSCO_PROFILE_ID  — EIT profile ID (default: cineit)
        EBSCO_PROFILE_PWD — EIT profile password
        EBSCO_DB          — database code to search (default: cul, per profile setup)
        EBSCO_FORMAT      — record format: brief|detailed|full (default: detailed — brief has NO abstract)
    """
    import xml.etree.ElementTree as ET
    import requests as req

    profile_id  = os.environ.get("EBSCO_PROFILE_ID",  "cineit")
    cust_id     = os.environ.get("EBSCO_CUST_ID",     "s5240361")
    group_id    = os.environ.get("EBSCO_GROUP_ID",    "main")
    profile_pwd = os.environ.get("EBSCO_PROFILE_PWD", "ebs3648")
    db          = os.environ.get("EBSCO_DB",           "cul")  # per EBSCO EIT profile setup
    fmt         = os.environ.get("EBSCO_FORMAT",       "detailed")  # brief|detailed|full — brief has NO abstract

    # EIT requires the compound format: <user>.<group>.<profile>
    full_profile = f"{cust_id}.{group_id}.{profile_id}"

    if not profile_pwd:
        print("  CINAHL: skipped — EBSCO_PROFILE_PWD not set. Export this before running:")
        print("          export EBSCO_CUST_ID='s5240361'")
        print("          export EBSCO_GROUP_ID='main'")
        print("          export EBSCO_PROFILE_ID='cineit'")
        print("          export EBSCO_PROFILE_PWD='your-profile-password'")
        return []

    try:
        # CINAHL output is capped at a fixed 25 papers WITH abstracts, regardless
        # of the global --max setting. Since ~40-60% of raw records lack an
        # abstract, request a generous raw pool from EBSCO to have enough to
        # filter down from.
        CINAHL_MAX_PAPERS = 25
        numrec = 200  # EBSCO EIT's documented max — maximize the raw candidate pool
                       # so the 25-paper cap is filled from the best matches, not
                       # whatever happens to be scanned first

        # Build the date range using EBSCO's DT field code (yyyymmdd-yyyymmdd, no
        # parens around the date term itself, combined with AND).
        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days_back)
        date_range = f"{start_dt.strftime('%Y%m%d')}-{end_dt.strftime('%Y%m%d')}"
        scoped_query = f"({CINAHL_QUERY}) AND DT {date_range}"

        print(f"  CINAHL: searching db={db}, date range={date_range}")

        resp = req.get(EIT_SEARCH_URL, params={
            "prof":   full_profile,
            "pwd":    profile_pwd,
            "query":  scoped_query,
            "db":     db,
            "format": fmt,
            "numrec": numrec,
            "sort":   "relevance",
        }, headers={"User-Agent": "NAIL-Digest/1.0 (ainurse@nailcollab.org)"}, timeout=30)

        if not resp.ok:
            print(f"  CINAHL: request failed {resp.status_code} — {resp.text[:300]}")
            return []

        # Check for an API-level error embedded in the XML
        if "APIErrorMessage" in resp.text or "ErrorNumber" in resp.text:
            print(f"  CINAHL: EBSCO error — {resp.text[:300]}")
            return []

        root = ET.fromstring(resp.content)

        # EIT search responses wrap each hit in a <rec> element with nested
        # <header> (identifiers) and a flat or grouped set of data fields.
        # Tags vary slightly by account/db config, so we search broadly
        # by local tag name (ignoring namespaces) for resilience.
        def local(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        def find_text(elem, *candidates) -> str:
            """Search elem's descendants for the first matching local tag name,
            gathering ALL nested text (EBSCO often wraps text in child tags
            like <ab><p>...</p></ab>, so direct .text is often empty)."""
            for child in elem.iter():
                if local(child.tag) in candidates:
                    text = "".join(child.itertext()).strip()
                    if text:
                        return text
            return ""

        records = [el for el in root.iter() if local(el.tag) in ("rec", "Rec", "Record")]
        print(f"  CINAHL: db={db} · found {len(records)} raw <rec> elements in response")
        if len(records) == 0:
            print(f"  CINAHL raw response (first 500 chars): {resp.text[:500]}")
        elif os.environ.get("CINAHL_DEBUG"):
            # Dump the full first record so we can see actual field names
            print("  CINAHL DEBUG — first raw <rec> element:")
            print(ET.tostring(records[0], encoding="unicode")[:3000])

        papers = []
        scanned = 0
        for rec in records:
            if len(papers) >= CINAHL_MAX_PAPERS:
                break  # stop once we have enough papers WITH abstracts
            scanned += 1

            an       = find_text(rec, "an", "AN")
            title    = find_text(rec, "atl", "title", "Title", "tig")
            abstract = find_text(rec, "ab", "Abstract", "abstract")
            authors  = find_text(rec, "aug", "au", "Author", "authors")
            journal  = find_text(rec, "jtl", "Source", "TitleSource", "journal") or "CINAHL"
            pub_date = find_text(rec, "dt", "PubDate", "date", "PubYear")
            doi      = find_text(rec, "doi", "DOI")
            plink    = find_text(rec, "plink", "pdfLink")

            if not abstract:
                continue  # skip records with no abstract — doesn't count toward the cap

            # DOI, when present, is the best link — works for any public
            # reader, no login required. But CINAHL Ultimate's nursing-journal
            # records essentially never carry a DOI in this API's metadata
            # (confirmed empirically: 0 of 12 sampled records had one).
            # EBSCO's own persistent link (plink) is NOT a usable fallback —
            # it requires the READER's own institutional EBSCO session and
            # always fails with an authentication error for public readers of
            # the digest (confirmed in production — every plink hit this).
            # Fall back to a Google Scholar search built from the title
            # instead — no login needed, reliably surfaces the paper.
            if doi:
                link = f"https://doi.org/{doi}"
            else:
                link = "https://scholar.google.com/scholar?q=" + quote(title)

            papers.append({
                "pmid":       f"cinahl:{an or len(papers)}",
                "title":      title,
                "abstract":   abstract,
                "authors":    authors,
                "journal":    journal,
                "pub_date":   pub_date[:4] if pub_date else "",
                "doi":        doi,
                "pubmed_url": link,
                "source":     "CINAHL",
            })

        print(f"  CINAHL returned {len(papers)} papers with abstracts "
              f"(capped at {CINAHL_MAX_PAPERS}, scanned {scanned} of {len(records)} raw records)")
        if scanned > 0:
            yield_pct = round(len(papers) / scanned * 100)
            print(f"  CINAHL: abstract yield {yield_pct}% among scanned records — "
                  f"CINAHL Ultimate includes many brief items without abstracts, this is normal")
        if len(records) > 0 and len(papers) == 0:
            all_tags = sorted(set(local(el.tag) for el in records[0].iter()))
            print(f"  CINAHL: 0 papers had a recognized abstract field. Tags seen in first record: {all_tags}")
            print(f"  Re-run with CINAHL_DEBUG=1 to see the full first record's XML.")
        return papers

    except ET.ParseError as e:
        print(f"  CINAHL: could not parse XML response — {e}")
        print(f"  Raw response (first 500 chars): {resp.text[:500]}")
        return []
    except Exception as e:
        print(f"  CINAHL: search failed — {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — GENERATE SUMMARIES (from spec prompt)
# ─────────────────────────────────────────────────────────────────────────────

SUMMARISE_PROMPT = """You will summarise a nursing informatics research abstract for a weekly community digest.

ABSTRACT:
---
{abstract}
---

METADATA:
- Title: {title}
- Authors: {authors}
- Journal: {journal}
- Publication date: {pub_date}
- PMID: {pmid}
- DOI: {doi}

INSTRUCTIONS:
Write a 2–3 sentence summary following this structure:
1. What was studied (population, setting, or problem)
2. What was found (key result or contribution)
3. Why it matters for nursing practice or nursing informatics research

HARD RULES — you must follow all of these without exception:
- Write only from information in the abstract above. Do not use your training knowledge to add, infer, or complete missing information.
- Do not speculate about implications not stated in the abstract.
- Do not use the words "groundbreaking", "revolutionary", "novel", "first ever", "unprecedented", or any superlative unless those exact words appear in the abstract.
- Write in plain language at approximately an 8th-grade reading level.
- Do not use passive voice where active voice is possible.
- Maximum 75 words.

FLAGGING RULES — if any of the following apply, add the flag in the JSON:
- The abstract does not state the study population → flag: "population_unclear"
- The abstract does not state the setting or context → flag: "setting_unclear"
- The abstract describes work in progress with no results yet → flag: "in_progress"
- The paper is an unreviewed preprint, OR is itself a pre-registered protocol for a future systematic review (a plan for a review, not a completed one), OR is a letter/correspondence piece → flag: "not_peer_reviewed". Do NOT apply this flag to a completed narrative, literature, or systematic review that has been published in a peer-reviewed journal — those went through the same peer review as original research and are not preprints or protocols.
- The abstract is fewer than 50 words or appears incomplete → flag: "abstract_incomplete"

OUTPUT FORMAT — return only valid JSON, nothing else:
{{
  "summary": "<your 2-3 sentence summary here>",
  "flags": []
}}"""

CLASSIFY_PROMPT = """Classify the following nursing informatics paper into exactly one of the topic buckets listed below.
Use only the title and abstract. If the paper fits multiple buckets, choose the most prominent.

TITLE: {title}
ABSTRACT: {abstract}

TOPIC BUCKETS:
1. Clinical Decision Support — AI tools that help clinicians make decisions at the point of care
2. NLP & Generative AI — natural language processing, LLMs, text mining, documentation AI
3. AI Ethics & Governance — bias, fairness, accountability, privacy, consent, governance frameworks
4. EHR & Workflows — electronic health records, workflow integration, documentation burden, interoperability
5. Workforce & Education — nursing AI literacy, training, education, competency, workforce development
6. Patient-Facing AI — chatbots, apps, and tools that patients interact with directly
7. AI Methods & Evaluation — new algorithms, benchmarks, evaluation frameworks, datasets
8. Other / Unclassified — does not clearly fit any of the above

Return only valid JSON, nothing else:
{{ "topic": "<exact bucket name>", "confidence": "high" | "medium" | "low" }}"""


def check_banned_words(text: str) -> list[str]:
    found = []
    lower = text.lower()
    for word in BANNED_WORDS:
        if word in lower:
            found.append(word)
    return found


def classify_paper(client: anthropic.Anthropic, paper: dict, dry_run: bool = False,
                   active_topics: list = None) -> tuple[str, str]:
    """Classify a paper's topic. Cheap call — run this BEFORE summarising so
    papers that don't fit the community's chosen scope can be dropped without
    paying for a full summary generation.

    Returns (topic, confidence). topic is "Other / Unclassified" with
    confidence "low" if classification fails or doesn't match active_topics.
    """
    if active_topics is None:
        active_topics = list(TOPIC_BUCKETS)
    if dry_run or not paper["abstract"]:
        return "Other / Unclassified", "low"

    classify_prompt = build_classify_prompt(paper, active_topics)

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=80,   # padded for Sonnet 5's new tokenizer (~30% more tokens/text)
                thinking={"type": "disabled"},  # simple structured task — no reasoning needed;
                                                 # also avoids ThinkingBlock appearing at content[0]
                messages=[{"role": "user", "content": classify_prompt}],
            )
            text_block = next((b for b in resp.content if getattr(b, "type", None) == "text"), None)
            if text_block is None:
                raise ValueError(f"No text block in response (got: {[getattr(b,'type','?') for b in resp.content]})")
            raw = text_block.text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            candidate  = parsed.get("topic", "")
            confidence = parsed.get("confidence", "low")
            if candidate in active_topics and confidence == "high":
                return candidate, confidence
            return "Other / Unclassified", "low"
        except Exception as e:
            is_transient = "overloaded" in str(e).lower() or "529" in str(e) or "rate_limit" in str(e).lower()
            print(f"  CLASSIFY ERROR for '{paper.get('title','')[:50]}' (attempt {attempt+1}): {type(e).__name__}: {e}")
            if attempt == 0 and is_transient:
                time.sleep(2)  # brief backoff before retrying a transient error
                continue
            return "Other / Unclassified", "low"
        finally:
            time.sleep(0.2)


def generate_summary(client: anthropic.Anthropic, paper: dict, dry_run: bool = False,
                     style: str = "2-3_sentences") -> tuple[str, list]:
    """Generate the paper summary. Only call this for papers that already
    passed classify_paper's topic/confidence filter — this is the more
    expensive of the two calls.

    Returns (summary, flags).
    """
    if dry_run or not paper["abstract"]:
        return "[DRY RUN — no API call made]", []

    summary_prompt = build_summarise_prompt(paper, style)
    _, max_words = SUMMARY_INSTRUCTIONS.get(style, SUMMARY_INSTRUCTIONS["2-3_sentences"])
    summary = ""
    flags = []

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=400,  # padded for Sonnet 5's new tokenizer (~30% more tokens/text)
                thinking={"type": "disabled"},  # simple structured task — no reasoning needed;
                                                 # also avoids ThinkingBlock appearing at content[0]
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            text_block = next((b for b in resp.content if getattr(b, "type", None) == "text"), None)
            if text_block is None:
                raise ValueError(f"No text block in response (got: {[getattr(b,'type','?') for b in resp.content]})")
            raw = text_block.text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            summary = parsed.get("summary", "").strip()
            flags = parsed.get("flags", [])

            word_count = len(summary.split())
            if word_count < 5 or word_count > max_words + 10:
                summary_prompt += f"\n\nIMPORTANT: Your previous response was {word_count} words. Please keep it under {max_words} words."
                if attempt == 0:
                    continue

            banned = check_banned_words(summary)
            if banned:
                summary_prompt += f"\n\nIMPORTANT: Your previous response contained banned words: {banned}. Remove them."
                if attempt == 0:
                    continue

            break
        except Exception as e:
            print(f"  SUMMARY ERROR for '{paper.get('title','')[:50]}' (attempt {attempt+1}): {type(e).__name__}: {e}")
            if attempt == 1:
                summary = "[ERROR: could not generate summary]"
                flags = ["generation_error"]
            time.sleep(1)

    time.sleep(0.3)
    return summary, flags


def summarise_paper(client: anthropic.Anthropic, paper: dict, dry_run: bool = False,
                    style: str = "2-3_sentences", active_topics: list = None) -> dict:
    """Classify + summarise a paper in one call. Kept for backward compatibility —
    prefer classify_paper() + generate_summary() separately in main() so papers
    outside the community's chosen topics can be filtered out BEFORE paying for
    the more expensive summary call.
    """
    topic, topic_confidence = classify_paper(client, paper, dry_run=dry_run, active_topics=active_topics)
    summary, flags = generate_summary(client, paper, dry_run=dry_run, style=style)
    return {
        **paper,
        "summary": summary,
        "flags": flags,
        "topic": topic,
        "topic_confidence": topic_confidence,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2c — ISSUE SYNTHESIS ("This Issue at a Glance")
# ─────────────────────────────────────────────────────────────────────────────
# One extra model call per issue. The model writes the thematic narrative from
# the per-paper summaries ONLY; every number shown in the glance box (paper
# counts, deltas, theme counts) is computed deterministically in Python and is
# never produced by the model.

def compute_deltas(papers: list[dict], prev_issue: dict | None) -> dict | None:
    """Compare this issue's topic mix against the previous issue's JSON data.
    Topic movement is measured in percentage points of issue share, so a
    49-paper issue and a 10-paper issue compare fairly. Returns None when
    there is no previous issue."""
    if not prev_issue or not prev_issue.get("papers"):
        return None

    def topic_counts(plist):
        counts: dict[str, int] = {}
        for p in plist:
            t = p.get("topic", "Other / Unclassified")
            counts[t] = counts.get(t, 0) + 1
        return counts

    cur_counts  = topic_counts(papers)
    prev_counts = topic_counts(prev_issue["papers"])
    cur_total, prev_total = len(papers), len(prev_issue["papers"])
    cur_flagged  = sum(1 for p in papers if p.get("flags"))
    prev_flagged = sum(1 for p in prev_issue["papers"] if p.get("flags"))

    def leading(counts):
        best = max(counts.values(), default=0)
        for t in TOPIC_BUCKETS:  # bucket order breaks ties deterministically
            if counts.get(t, 0) == best and best > 0:
                return t
        return None

    share_changes = {}
    for t in TOPIC_BUCKETS:
        if t == "Other / Unclassified":
            continue
        cur_share  = cur_counts.get(t, 0)  / cur_total  * 100 if cur_total  else 0
        prev_share = prev_counts.get(t, 0) / prev_total * 100 if prev_total else 0
        share_changes[t] = round(cur_share - prev_share)

    rising  = max(share_changes, key=lambda t: share_changes[t]) if share_changes else None
    falling = min(share_changes, key=lambda t: share_changes[t]) if share_changes else None

    return {
        "prev_issue":        prev_issue.get("issue"),
        "papers":            cur_total,
        "papers_delta":      cur_total - prev_total,
        "leading_topic":     leading(cur_counts),
        "prev_leading_topic": leading(prev_counts),
        "rising_topic":      rising  if rising  and share_changes[rising]  > 0 else None,
        "rising_pp":         share_changes.get(rising, 0)  if rising  else 0,
        "falling_topic":     falling if falling and share_changes[falling] < 0 else None,
        "falling_pp":        share_changes.get(falling, 0) if falling else 0,
        "flagged":           cur_flagged,
        "flagged_delta":     cur_flagged - prev_flagged,
    }


def build_synthesis_prompt(papers: list[dict], issue_num: int, deltas: dict | None,
                           prev_issue: dict | None) -> str:
    """Prompt for the issue-level 'At a Glance' synthesis. Follows the same
    AINurse-26 commitments as the per-paper prompts: the model may draw ONLY
    on the per-paper summaries already generated under those rules."""
    paper_lines = []
    for p in papers:
        flags = f" · flags: {', '.join(p['flags'])}" if p.get("flags") else ""
        paper_lines.append(f"- [{p['pmid']}] ({p['topic']}) {p['title']}\n  Summary: {p['summary']}{flags}")
    paper_block = "\n".join(paper_lines)

    counts: dict[str, int] = {}
    for p in papers:
        counts[p["topic"]] = counts.get(p["topic"], 0) + 1
    count_block = "\n".join(f"- {t}: {n}" for t, n in sorted(counts.items(), key=lambda x: -x[1]))

    if prev_issue and deltas:
        prev_counts: dict[str, int] = {}
        for p in prev_issue.get("papers", []):
            t = p.get("topic", "Other / Unclassified")
            prev_counts[t] = prev_counts.get(t, 0) + 1
        prev_count_block = "\n".join(f"- {t}: {n}" for t, n in sorted(prev_counts.items(), key=lambda x: -x[1]))
        prev_synth = ""
        if prev_issue.get("synthesis", {}) and prev_issue["synthesis"].get("overview"):
            prev_synth = "\nPREVIOUS ISSUE'S SYNTHESIS (for continuity of narrative):\n" + \
                         "\n".join(prev_issue["synthesis"]["overview"])
        prev_block = f"""
PREVIOUS ISSUE (#{prev_issue.get('issue')}, {prev_issue.get('date_range', {}).get('start', '')} – {prev_issue.get('date_range', {}).get('end', '')}):
- Total papers: {len(prev_issue.get('papers', []))}
- Topic counts:
{prev_count_block}
{prev_synth}
COMPUTED CHANGES vs previous issue (already verified in code — use these numbers, do not derive your own):
- Papers: {deltas['papers']} this issue ({deltas['papers_delta']:+d})
- Leading topic now: {deltas['leading_topic']} (previously: {deltas['prev_leading_topic']})
- Rising share: {deltas['rising_topic'] or 'none'} · Falling share: {deltas['falling_topic'] or 'none'}

PARAGRAPH 2 INSTRUCTIONS: describe what changed relative to the previous issue —
the shift in topic balance, threads that continued, and notable absences —
using ONLY the computed changes above and the paper summaries."""
    else:
        prev_block = """
This is the first issue, so there is no previous issue to compare against.

PARAGRAPH 2 INSTRUCTIONS: instead of changes over time, describe the spread of
the issue — the balance between study types (e.g. reviews vs. original studies),
settings, and any clusters of related work."""

    return f"""You will write the "At a Glance" synthesis that opens Issue #{issue_num} of the NAIL Digest.
Readers use it to understand, in under a minute, what this issue's literature is about and what is shifting.

THIS ISSUE'S PAPERS — each already summarised under the digest's faithfulness rules:
{paper_block}

TOPIC COUNTS THIS ISSUE (computed in code):
{count_block}
{prev_block}

INSTRUCTIONS:
Write exactly TWO paragraphs, then name the recurring themes.

Paragraph 1 — the 2–4 dominant threads of THIS issue. Ground every claim in the
papers above, referring to them by what they studied (not by author or ID).
Paragraph 2 — follow the PARAGRAPH 2 INSTRUCTIONS above.

Each paragraph: maximum 110 words. You may bold up to three SHORT key phrases
(2–5 words each) per paragraph by wrapping them as <strong>phrase</strong> —
every <strong> MUST have a matching closing </strong>. Use no other HTML.

Then list 2–4 named themes. A theme is a thread that cuts across papers (it may
span topic buckets). Assign each theme the IDs of the papers that belong to it,
copied exactly from the [brackets] above. A paper may appear in at most one theme;
not every paper needs a theme.

HARD RULES — you must follow all of these without exception:
- Draw only on the summaries and computed numbers above. No outside knowledge.
- Do not speculate about where the field is heading or what the trends imply.
- No recommendations, no clinical conclusions.
- Do not use the words "groundbreaking", "revolutionary", "novel", "first ever", "unprecedented", or any superlative unless they appear in a summary above.
- Plain language, approximately 8th-grade reading level. Active voice.
- Any count you mention must match the computed numbers provided.

OUTPUT FORMAT — return only valid JSON, nothing else:
{{
  "overview": ["<paragraph 1>", "<paragraph 2>"],
  "themes": [{{"name": "<short theme name>", "paper_ids": ["<id>", "<id>"]}}]
}}"""


def generate_synthesis(client: "anthropic.Anthropic", papers: list[dict], issue_num: int,
                       deltas: dict | None, prev_issue: dict | None,
                       dry_run: bool = False) -> dict | None:
    """Generate the issue-level synthesis. Returns None on dry run or failure —
    the issue then simply publishes without the glance box."""
    if dry_run or not papers:
        return None

    prompt = build_synthesis_prompt(papers, issue_num, deltas, prev_issue)
    valid_ids = {p["pmid"] for p in papers}

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text_block = next((b for b in resp.content if getattr(b, "type", None) == "text"), None)
            if text_block is None:
                raise ValueError("No text block in response")
            raw = text_block.text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)

            overview = []
            for para in parsed.get("overview", [])[:2]:
                para = str(para).strip()
                # allow <strong> only; strip any other tag the model may emit
                para = re.sub(r"</?(?!strong\b)[^>]+>", "", para)
                # unbalanced bolding would leak across the rest of the page —
                # if open/close counts differ, drop the bolding entirely
                if para.count("<strong>") != para.count("</strong>"):
                    para = para.replace("<strong>", "").replace("</strong>", "")
                if para:
                    overview.append(para)
            if not overview:
                raise ValueError("Empty overview")

            themes = []
            for th in parsed.get("themes", [])[:4]:
                name = re.sub(r"<[^>]+>", "", str(th.get("name", ""))).strip()
                ids = [i for i in th.get("paper_ids", []) if i in valid_ids]
                if name and ids:
                    themes.append({"name": name, "paper_ids": ids})

            joined = " ".join(overview)
            banned = check_banned_words(joined)
            too_long = any(len(p.split()) > 125 for p in overview)
            if (banned or too_long) and attempt == 0:
                if banned:
                    prompt += f"\n\nIMPORTANT: Your previous response contained banned words: {banned}. Remove them."
                if too_long:
                    prompt += "\n\nIMPORTANT: Your previous paragraphs exceeded 110 words. Shorten them."
                continue

            return {
                "overview":     overview,
                "themes":       themes,
                "deltas":       deltas,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "model":        MODEL,
            }
        except Exception as e:
            print(f"  SYNTHESIS ERROR (attempt {attempt+1}): {type(e).__name__}: {e}")
            time.sleep(1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — RENDER HTML OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

FLAG_LABELS = {
    "population_unclear": "Population unclear",
    "setting_unclear":    "Setting unclear",
    "in_progress":        "Work in progress",
    "not_peer_reviewed":  "Not peer-reviewed",
    "abstract_incomplete":"Abstract incomplete",
    "generation_error":   "Summary error",
}

TOPIC_COLORS = {
    "Clinical Decision Support": ("#1A6B8A", "#EAF4F8"),
    "NLP & Generative AI":       ("#1A6B8A", "#EAF4F8"),
    "AI Ethics & Governance":    ("#9B6B00", "#FEF4E0"),
    "EHR & Workflows":           ("#2D6B4A", "#E6F4EE"),
    "Workforce & Education":     ("#6B3A8A", "#F2EAF8"),
    "Patient-Facing AI":         ("#1A6B8A", "#EAF4F8"),
    "AI Methods & Evaluation":   ("#4A5D6B", "#EEF2F5"),
    "Other / Unclassified":      ("#6B7A8A", "#F0F3F5"),
}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG OVERLAY — renders human-readable config as an in-page modal
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_LABELS = {
    "pubmed":  ("ti-database-search", "PubMed",  "Peer-reviewed biomedical literature",  "#1A6CB0", "#BFDBF7", "#FFFFFF"),
    "arxiv":   ("ti-file-text",       "arXiv",   "Computer science & AI preprints",       "#B83B1A", "#F5C8BC", "#FEF6F4"),
    "medrxiv": ("ti-stethoscope",     "medRxiv", "Health sciences preprints",             "#1A8A6E", "#B8E8DA", "#F2FCF9"),
    "cinahl":  ("ti-heart-rate-monitor","CINAHL","Nursing & allied health literature (max 25/issue)",    "#7C5CC4", "#D8CEEF", "#F7F4FD"),
}

STYLE_LABELS = {
    "1_sentence":   ("One sentence",      "A single sentence stating the key finding (max 30 words)."),
    "2-3_sentences":("2–3 sentences",     "What was studied · What was found · Why it matters for nursing practice."),
    "structured":   ("Structured fields", "Three labelled fields: Methods · Finding · Relevance to practice."),
    "ai_decides":   ("AI-selected",       "Claude chooses the most appropriate format for each paper type."),
}

def build_config_overlay(cfg: dict) -> str:
    """Return the full modal HTML for the digest configuration overlay."""
    sources       = cfg.get("sources", ["pubmed"])
    style_key     = cfg.get("summary_style", "2-3_sentences")
    topics        = cfg.get("topics", list(TOPIC_BUCKETS))
    all_topics    = [t for t in TOPIC_BUCKETS if t != "Other / Unclassified"]
    excluded      = [t for t in all_topics if t not in topics]

    # Sources pills
    src_pills = ""
    for s in sources:
        icon, label, desc, color, border, bg = SOURCE_LABELS.get(s, ("ti-database", s, "", "#1A6CB0", "#BFDBF7", "#FFFFFF"))
        src_pills += f'''
        <div class="cfg-source-row">
          <div class="cfg-source-pill" style="color:{color};background:{bg};border-color:{border};"><i class="ti {icon}"></i> {label}</div>
          <span class="cfg-source-desc">{desc}</span>
        </div>'''

    # Summary style
    style_name, style_desc = STYLE_LABELS.get(style_key, (style_key, ""))

    # Topics
    TOPIC_COLORS = {
        "Clinical Decision Support": "#1E8A6E",
        "NLP & Generative AI":       "#7C5CC4",
        "AI Ethics & Governance":    "#C76B33",
        "EHR & Workflows":           "#1A6CB0",
        "Workforce & Education":     "#B08A1F",
        "Patient-Facing AI":         "#1E8A6E",
        "AI Methods & Evaluation":   "#5E6B76",
    }
    topic_pills = ""
    for t in topics:
        if t == "Other / Unclassified":
            continue
        c = TOPIC_COLORS.get(t, "#5E6B76")
        topic_pills += f'<span class="cfg-topic-pill" style="border-color:{c};color:{c};">{t}</span>'

    excl_html = ""
    if excluded:
        excl_pills = "".join(f'<span class="cfg-excl-pill">{t}</span>' for t in excluded)
        excl_html = f'<div class="cfg-excl-row"><span class="cfg-excl-label">Excluded:</span>{excl_pills}</div>'

    return f"""
<!-- CONFIG OVERLAY -->
<div id="cfg-overlay" role="dialog" aria-modal="true" aria-label="Digest configuration" style="display:none;">
  <div id="cfg-backdrop"></div>
  <div id="cfg-modal">
    <button id="cfg-close" aria-label="Close"><i class="ti ti-x"></i></button>
    <div id="cfg-modal-head">
      <div id="cfg-modal-icon"><i class="ti ti-adjustments-horizontal"></i></div>
      <div>
        <h2>Digest Configuration</h2>
        <p>Settings community-agreed at <strong>AINurse-26</strong>, Ottawa, July 10 2026.</p>
      </div>
    </div>
    <div class="cfg-section">
      <div class="cfg-section-label"><i class="ti ti-database-search"></i> Sources</div>
      <div class="cfg-sources">{src_pills}</div>
    </div>
    <div class="cfg-section">
      <div class="cfg-section-label"><i class="ti ti-text-size"></i> Summary Style</div>
      <div class="cfg-style-name">{style_name}</div>
      <div class="cfg-style-desc">{style_desc}</div>
    </div>
    <div class="cfg-section">
      <div class="cfg-section-label"><i class="ti ti-tags"></i> Topics ({len(topics)} active)</div>
      <div class="cfg-topic-pills">{topic_pills}</div>
      {excl_html}
      <p class="cfg-filter-note">Papers are classified before summarising. Any paper that doesn't clearly match one of these topics is left out of the issue entirely — not summarised, not shown as "Other."</p>
      <p class="cfg-filter-note">Results are also restricted to English-language papers. Title and abstract are each checked independently; either being predominantly non-English excludes the paper from this issue, regardless of topic relevance.</p>
    </div>
    <div class="cfg-section">
      <div class="cfg-section-label"><i class="ti ti-flag"></i> Why a paper gets flagged</div>
      <div class="cfg-flag-list">
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Population unclear</span><span class="cfg-flag-desc">The abstract doesn't state who was studied.</span></div>
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Setting unclear</span><span class="cfg-flag-desc">The abstract doesn't state the setting or context of the study.</span></div>
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Work in progress</span><span class="cfg-flag-desc">The paper describes work that's still underway, with no results yet.</span></div>
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Not peer-reviewed</span><span class="cfg-flag-desc">The paper is an unreviewed preprint, a protocol for a future systematic review, or a letter/correspondence piece. Completed reviews published in a peer-reviewed journal are not included here.</span></div>
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Abstract incomplete</span><span class="cfg-flag-desc">The abstract is very short or appears cut off.</span></div>
        <div class="cfg-flag-row"><span class="cfg-flag-badge">Summary error</span><span class="cfg-flag-desc">A technical issue prevented a summary from being generated — not a comment on the paper itself.</span></div>
      </div>
      <p class="cfg-filter-note">A paper can carry more than one flag. Flagged papers still appear in the issue, grouped under their topic within the Flagged Papers section.</p>
    </div>
    <div class="cfg-footer">
      Votes are version-controlled in <code>config.json</code> alongside this digest.
      Propose changes via the NAIL Collaborative editorial board.
    </div>
  </div>
</div>
<style>
#cfg-overlay{{position:fixed;inset:0;z-index:9000;display:flex;align-items:center;justify-content:center;padding:20px;}}
#cfg-backdrop{{position:absolute;inset:0;background:rgba(17,28,38,.65);backdrop-filter:blur(4px);}}
#cfg-modal{{position:relative;background:#fff;border-radius:16px;padding:36px;max-width:560px;width:100%;box-shadow:0 24px 64px rgba(17,28,38,.28);max-height:90vh;overflow-y:auto;animation:cfg-in .22s cubic-bezier(.22,.8,.36,1);}}
@keyframes cfg-in{{from{{opacity:0;transform:translateY(12px);}}to{{opacity:1;transform:none;}}}}
#cfg-close{{position:absolute;top:16px;right:16px;background:var(--paper-dim,#F3F1EC);border:1px solid var(--line,#E4E0D8);border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:16px;color:var(--mut,#5E6B76);transition:background .15s;}}
#cfg-close:hover{{background:var(--line,#E4E0D8);}}
#cfg-modal-head{{display:flex;gap:14px;align-items:flex-start;margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid var(--line,#E4E0D8);}}
#cfg-modal-icon{{width:44px;height:44px;border-radius:10px;background:var(--amber-pale,#FDF5E0);border:1px solid #F0DFA0;display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--amber-strong,#B08A1F);flex-shrink:0;}}
#cfg-modal-head h2{{font-family:var(--serif,serif);font-size:20px;font-weight:500;color:var(--slate-deep,#111C26);margin-bottom:3px;}}
#cfg-modal-head p{{font-size:13px;color:var(--mut,#5E6B76);line-height:1.5;}}
.cfg-section{{margin-bottom:22px;}}
.cfg-section:last-of-type{{margin-bottom:0;}}
.cfg-section-label{{font-size:10.5px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:var(--mut,#5E6B76);margin-bottom:10px;display:flex;align-items:center;gap:6px;}}
.cfg-source-row{{display:flex;align-items:center;gap:10px;margin-bottom:6px;}}
.cfg-source-pill{{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:var(--slate-deep,#111C26);background:var(--paper-dim,#F3F1EC);border:1px solid var(--line,#E4E0D8);padding:5px 12px;border-radius:99px;}}
.cfg-source-desc{{font-size:12.5px;color:var(--mut,#5E6B76);}}
.cfg-style-name{{font-size:15px;font-weight:600;color:var(--slate-deep,#111C26);margin-bottom:4px;}}
.cfg-style-desc{{font-size:13px;color:var(--mut,#5E6B76);line-height:1.6;}}
.cfg-topic-pills{{display:flex;flex-wrap:wrap;gap:7px;}}
.cfg-topic-pill{{font-size:12px;font-weight:600;padding:4px 11px;border-radius:99px;border:1.5px solid;background:transparent;}}
.cfg-excl-row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px;}}
.cfg-excl-label{{font-size:11px;font-weight:700;color:var(--mut,#5E6B76);text-transform:uppercase;letter-spacing:1px;}}
.cfg-excl-pill{{font-size:12px;color:var(--mut,#5E6B76);background:var(--paper-dim,#F3F1EC);padding:3px 10px;border-radius:99px;text-decoration:line-through;opacity:.6;}}
.cfg-filter-note{{font-size:12px;color:var(--mut,#5E6B76);line-height:1.6;margin-top:10px;padding-top:10px;border-top:1px dashed var(--line,#E4E0D8);}}
.cfg-filter-note + .cfg-filter-note{{margin-top:6px;padding-top:0;border-top:none;}}
.cfg-flag-list{{display:flex;flex-direction:column;gap:8px;}}
.cfg-flag-row{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}}
.cfg-flag-badge{{flex-shrink:0;font-size:11px;font-weight:700;letter-spacing:.3px;color:#B91C1C;background:#FEE2E2;border:1px solid #FCA5A5;border-radius:99px;padding:2px 10px;white-space:nowrap;}}
.cfg-flag-desc{{font-size:12.5px;color:var(--mut,#5E6B76);line-height:1.5;}}
.cfg-footer{{margin-top:22px;padding-top:16px;border-top:1px solid var(--line,#E4E0D8);font-size:12.5px;color:var(--mut,#5E6B76);line-height:1.6;}}
.cfg-footer code{{font-size:11.5px;background:var(--paper-dim,#F3F1EC);padding:1px 6px;border-radius:4px;}}
</style>
<script>
(function(){{
  var overlay=document.getElementById('cfg-overlay');
  var backdrop=document.getElementById('cfg-backdrop');
  var closeBtn=document.getElementById('cfg-close');
  function openCfg(){{overlay.style.display='flex';document.body.style.overflow='hidden';}}
  function closeCfg(){{overlay.style.display='none';document.body.style.overflow='';}}
  document.querySelectorAll('.cfg-trigger').forEach(function(b){{b.addEventListener('click',openCfg);}});
  if(backdrop)backdrop.addEventListener('click',closeCfg);
  if(closeBtn)closeBtn.addEventListener('click',closeCfg);
  document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeCfg();}});
}})();
</script>"""


def build_glance_html(synthesis: dict | None, papers: list[dict]) -> str:
    """Render the 'This Issue at a Glance' box. Returns '' when the issue has
    no synthesis (older issues, dry runs, or a failed generation) so the page
    degrades gracefully."""
    if not synthesis or not synthesis.get("overview"):
        return ""
    from html import escape

    paras = "".join(f"<p>{p}</p>" for p in synthesis["overview"])

    title_by_id = {p["pmid"]: p["title"] for p in papers}
    chips = ""
    for th in synthesis.get("themes", []):
        titles = " • ".join(title_by_id.get(i, i) for i in th["paper_ids"])
        chips += (f'<span class="theme" title="{escape(titles, quote=True)}">'
                  f'{escape(th["name"])} <i>{len(th["paper_ids"])}</i></span>')
    themes_html = f'<div class="themes">{chips}</div>' if chips else ""

    d = synthesis.get("deltas")
    delta_html = ""
    if d:
        def arrow(delta: int) -> str:
            if delta > 0:
                return f'<span class="up">▲ {delta}</span>'
            if delta < 0:
                return f'<span class="down">▼ {abs(delta)}</span>'
            return '<span class="d-was">±0</span>'

        items = ""
        if d.get("prev_issue"):
            items += f'<div class="d-item"><small>Compared with</small><span>Issue #{d["prev_issue"]}</span></div>'
        items += f'<div class="d-item"><small>Papers</small><span>{d["papers"]} {arrow(d["papers_delta"])}</span></div>'
        lead = d.get("leading_topic")
        if lead:
            was = d.get("prev_leading_topic")
            suffix = f' <span class="d-was">(was {was})</span>' if was and was != lead else ' <span class="d-was">(unchanged)</span>'
            items += f'<div class="d-item"><small>Leading topic</small><span>{lead}{suffix}</span></div>'
        if d.get("rising_topic"):
            items += f'<div class="d-item"><small>Rising</small><span class="up">▲ {d["rising_topic"]}</span></div>'
        if d.get("falling_topic"):
            items += f'<div class="d-item"><small>Cooling</small><span class="down">▼ {d["falling_topic"]}</span></div>'
        items += f'<div class="d-item"><small>Flagged</small><span>{d["flagged"]} {arrow(d["flagged_delta"])}</span></div>'
        delta_html = f'<div class="delta-strip">{items}</div>'

    return f"""
    <div class="glance">
      <div class="glance-kick">This issue at a glance</div>
      {paras}
      {themes_html}
      {delta_html}
    </div>"""


def render_html(papers: list[dict], issue_num: int, start_date: str, end_date: str,
                config: dict = None, synthesis: dict = None,
                generated_at_str: str = None) -> str:
    """Render a single issue as Option B (Slate & Amber) HTML."""
    by_topic: dict[str, list] = {t: [] for t in TOPIC_BUCKETS}
    flagged = []
    for p in papers:
        if len(p.get("flags", [])) >= 1:
            flagged.append(p)
        else:
            by_topic[p["topic"]].append(p)

    # topic color map
    TOPIC_STYLE = {
        "Clinical Decision Support":  ("t-cds", "ti-heart-rate-monitor"),
        "NLP & Generative AI":        ("t-nlp", "ti-message-code"),
        "AI Ethics & Governance":     ("t-eth", "ti-scale"),
        "EHR & Workflows":            ("t-ehr", "ti-database"),
        "Workforce & Education":      ("t-edu", "ti-school"),
        "Patient-Facing AI":          ("t-cds", "ti-device-mobile"),
        "AI Methods & Evaluation":    ("t-oth", "ti-cpu"),
        "Other / Unclassified":       ("t-oth", "ti-dots"),
    }

    def paper_card(p: dict) -> str:
        tcls, _ = TOPIC_STYLE.get(p["topic"], ("t-oth", "ti-dots"))
        flags_html = "".join(
            f'<span class="flag"><i class="ti ti-flag"></i> {FLAG_LABELS.get(f, f)}</span>'
            for f in p.get("flags", [])
        )
        doi_chip = f'<a href="https://doi.org/{p["doi"]}" class="plink plink-doi" target="_blank"><i class="ti ti-external-link"></i> DOI</a>' if p.get("doi") else ""
        flagged_attr = ' data-flagged="true"' if p.get("flags") else ''
        source = p.get("source", "PubMed")
        if source == "arXiv":
            pmeta_html = f'<a href="{p["pubmed_url"]}" class="plink plink-arxiv" target="_blank"><i class="ti ti-external-link"></i> arXiv</a>'
        elif source == "medRxiv":
            pmeta_html = f'<a href="{p["pubmed_url"]}" class="plink plink-medrxiv" target="_blank"><i class="ti ti-external-link"></i> medRxiv</a>'
        elif source == "CINAHL":
            pmeta_html = f'<a href="{p["pubmed_url"]}" class="plink plink-cinahl" target="_blank"><i class="ti ti-external-link"></i> CINAHL</a>'
        else:
            pmeta_html = f'<span class="pmid">PMID {p["pmid"]}</span><a href="{p["pubmed_url"]}" class="plink plink-pubmed" target="_blank"><i class="ti ti-external-link"></i> PubMed</a>{doi_chip}'
        return f"""
    <div class="pli" data-topic="{p["topic"]}"{flagged_attr}>
      <div class="pli-body">
        <div class="pli-topic {tcls}">{p["topic"]}</div>
        {flags_html}
        <h3 style="margin-top:{'7px' if flags_html else '0'};"><a href="{p["pubmed_url"]}" target="_blank">{p["title"]}</a></h3>
        <div class="authors">{p["authors"]}</div>
        <div class="venue">{p["journal"]} · {p["pub_date"]}</div>
        <div class="pli-summary">{p["summary"]}</div>
        <div class="pmeta">
          {pmeta_html}
        </div>
      </div>
    </div>"""

    # topic_totals counts ALL papers per topic (flagged + unflagged) — computed
    # here (before section rendering) so both the main sections and the
    # flagged sub-sections can show counts consistent with the filter pills.
    topic_totals: dict[str, int] = {t: 0 for t in TOPIC_BUCKETS}
    for p in papers:
        topic_totals[p["topic"]] = topic_totals.get(p["topic"], 0) + 1

    topic_sections = ""
    for topic in TOPIC_BUCKETS:
        plist = by_topic[topic]
        if not plist:
            continue
        _, icon = TOPIC_STYLE.get(topic, ("t-oth", "ti-dots"))
        cards = "".join(paper_card(p) for p in plist)
        n_total = topic_totals[topic]
        topic_sections += f"""
    <div class="sec-h" data-topic="{topic}"><i class="ti {icon}" aria-hidden="true"></i>{topic}<span class="cnt">{n_total} paper{"s" if n_total!=1 else ""}</span></div>
    {cards}"""

    flagged_section = ""
    if flagged:
        flagged_by_topic: dict[str, list] = {t: [] for t in TOPIC_BUCKETS}
        for p in flagged:
            flagged_by_topic[p["topic"]].append(p)

        flagged_subsections = ""
        for topic in TOPIC_BUCKETS:
            plist = flagged_by_topic[topic]
            if not plist:
                continue
            _, icon = TOPIC_STYLE.get(topic, ("t-oth", "ti-dots"))
            cards = "".join(paper_card(p) for p in plist)
            n_flagged_here = len(plist)
            # data-topic carries the REAL topic name (not "flagged") so that
            # clicking a topic-specific filter pill can find and reveal this
            # sub-header even when every paper in that topic happens to be
            # flagged (i.e. no main section exists for it above).
            # The count shown here is the FLAGGED-ONLY count for this topic —
            # not the topic's grand total — since that's what's actually
            # listed under this specific sub-header. A red "Flagged" marker
            # distinguishes it from the neutral count pill used elsewhere.
            flagged_subsections += f"""
    <div class="sec-h sec-h-sub" data-topic="{topic}"><i class="ti {icon}" aria-hidden="true"></i>{topic}<span class="cnt cnt-flagged"><i class="ti ti-flag"></i> {n_flagged_here} Flagged</span></div>
    {cards}"""

        flagged_section = f"""
    <div class="sec-h" data-topic="flagged"><i class="ti ti-flag" aria-hidden="true"></i>Flagged Papers<span class="cnt">{len(flagged)}</span></div>
    {flagged_subsections}"""

    # filter pills — topic_totals already computed above (flagged + unflagged
    # per topic), reused here for consistency with section heading counts.
    filter_counts = [(t, topic_totals[t]) for t in TOPIC_BUCKETS if topic_totals[t]]
    total = len(papers)
    flag_count = sum(1 for p in papers if p.get("flags"))
    filter_pills = f'<a href="#" class="active" data-filter="all">All ({total})</a>'
    for t, n in filter_counts:
        filter_pills += f'<a href="#" data-filter="{t}">{t} ({n})</a>'
    if flag_count:
        filter_pills += f'<a href="#" data-filter="flagged" class="flag-pill"><i class="ti ti-flag" style="font-size:11px;"></i> Flagged ({flag_count})</a>'

    generated_at = generated_at_str or datetime.now(timezone.utc).strftime("%b %d, %Y")
    date_slug = datetime.now().strftime("%Y-%m-%d")
    issue_filename = f"ni-biweekly-{date_slug}.html"

    # Config overlay — use passed config or fall back to defaults
    _cfg = config if config else DEFAULT_CONFIG
    config_overlay_html = build_config_overlay(_cfg)

    # "At a Glance" synthesis box — empty string when no synthesis exists
    glance_html = build_glance_html(synthesis, papers)

    # sidebar topic bars — use topic_totals (flagged + unflagged) for accuracy
    max_n = max((topic_totals[t] for t in TOPIC_BUCKETS if topic_totals[t]), default=1)
    topic_bars = ""
    for t in TOPIC_BUCKETS:
        n = topic_totals[t]
        if not n:
            continue
        pct = round(n / max_n * 100)
        short = t.replace("& Generative AI","& Gen AI").replace("Workforce & Education","Workforce & Edu.").replace("AI Ethics & Governance","Ethics & Gov.").replace("Clinical Decision Support","Clinical Decision")
        topic_bars += f'<div class="tbr"><span class="tbr-name">{short}</span><div class="tbr-track"><div class="tbr-fill" style="width:{pct}%"></div></div><span class="tbr-n">{n}</span></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NAIL Digest Issue #{issue_num} — {start_date} | NAIL Collaborative</title>
<meta name="description" content="NAIL Digest: AI-Driven Nursing Informatics Issue #{issue_num} — {total} papers from PubMed, summarized by Claude Sonnet, overseen by the NAIL Collaborative.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 36 36'%3E%3Crect width='36' height='36' rx='8' fill='%231D2B3A'/%3E%3Cpath d='M11 26.5V9.5' stroke='%23F0DFA0' stroke-width='2.6' stroke-linecap='round'/%3E%3Cpath d='M25 26.5V9.5' stroke='%23F0DFA0' stroke-width='2.6' stroke-linecap='round'/%3E%3Cpath d='M11 9.5l5.5 9 2-5 2 7.5 4.5 5.5' stroke='%23E8C46A' stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round' fill='none'/%3E%3C/svg%3E">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css">
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400;1,9..144,500&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap');
:root{{--ink:#1D2B3A;--slate:#1D2B3A;--slate-deep:#111C26;--amber:#E8C46A;--amber-strong:#B08A1F;--amber-pale:#FDF5E0;--paper:#FAFAF7;--paper-dim:#F3F1EC;--card:#FFFFFF;--line:#E4E0D8;--line-soft:#EDEAE2;--mut:#5E6B76;--sky:#1A6CB0;--serif:'Fraunces',Georgia,serif;--sans:'Instrument Sans',-apple-system,sans-serif;--shadow-s:0 1px 2px rgba(17,28,38,.05),0 2px 8px rgba(17,28,38,.04);--shadow-m:0 2px 6px rgba(17,28,38,.07),0 12px 28px rgba(17,28,38,.1);--r-s:8px;--r-m:12px;--r-l:18px;}}
*{{box-sizing:border-box;margin:0;padding:0;}}html{{scroll-behavior:smooth;}}body{{font-family:var(--sans);background:var(--paper);color:var(--ink);min-height:100vh;font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased;}}::selection{{background:var(--amber-pale);color:var(--slate-deep);}}
.nav{{position:sticky;top:0;z-index:200;background:rgba(250,250,247,.9);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);}}
.nav-in{{max-width:1140px;margin:0 auto;padding:0 28px;height:64px;display:flex;align-items:center;justify-content:space-between;gap:16px;}}
.logo{{display:flex;align-items:center;gap:11px;text-decoration:none;color:var(--ink);}}
.logo svg{{width:36px;height:36px;flex-shrink:0;border-radius:8px;box-shadow:var(--shadow-s);}}
.logo-t b{{display:block;font-family:var(--serif);font-weight:500;font-size:17px;line-height:1.15;color:var(--slate-deep);}}
.logo-t span{{display:block;font-size:10px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:var(--mut);margin-top:1px;}}
.nav-right{{display:flex;align-items:center;gap:8px;}}
.nav-btn{{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:600;color:var(--mut);text-decoration:none;padding:6px 14px;border-radius:99px;border:1px solid var(--line);transition:color .18s,border-color .18s;}}
.nav-btn:hover{{color:var(--slate-deep);border-color:var(--slate);}}
.hero{{background:var(--slate);position:relative;overflow:hidden;}}
.hero::before{{content:'';position:absolute;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='52'%3E%3Cpath d='M26 22v8M22 26h8' stroke='%23F0DFA0' stroke-opacity='.06' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E");}}
.hero::after{{content:'';position:absolute;width:600px;height:600px;right:-180px;top:-300px;border-radius:50%;background:radial-gradient(circle,rgba(232,196,106,.14),transparent 65%);}}
.hero-in{{position:relative;z-index:2;max-width:1140px;margin:0 auto;padding:64px 28px 0;}}
.kicker{{display:inline-flex;align-items:center;gap:10px;color:var(--amber);font-size:11.5px;font-weight:700;letter-spacing:2.2px;text-transform:uppercase;margin-bottom:18px;animation:rise .55s .05s cubic-bezier(.22,.8,.36,1) both;}}
.kicker::before{{content:'';width:24px;height:2px;background:var(--amber);border-radius:2px;}}
.hero h1{{font-family:var(--serif);font-weight:500;color:#fff;font-size:clamp(28px,4.5vw,50px);line-height:1.1;letter-spacing:-.4px;max-width:680px;margin-bottom:16px;animation:rise .55s .1s cubic-bezier(.22,.8,.36,1) both;}}
.hero h1 em{{font-style:italic;color:var(--amber);}}
.hero-sub{{color:rgba(220,208,186,.7);font-size:16px;line-height:1.7;max-width:680px;margin-bottom:28px;animation:rise .55s .18s cubic-bezier(.22,.8,.36,1) both;}}
.btn{{display:inline-flex;align-items:center;gap:7px;font-size:14px;font-weight:600;padding:11px 22px;border-radius:99px;cursor:pointer;border:none;transition:transform .2s;text-decoration:none;font-family:var(--sans);}}
.btn:hover{{transform:translateY(-2px);}}
.btn-amber{{background:var(--amber);color:var(--slate-deep);box-shadow:0 6px 18px rgba(232,196,106,.3);}}
.btn-ghost{{background:transparent;color:rgba(220,208,186,.85);border:1px solid rgba(240,223,160,.25);}}
.cta-row{{display:flex;gap:10px;flex-wrap:wrap;animation:rise .55s .26s cubic-bezier(.22,.8,.36,1) both;margin-bottom:44px;}}
@keyframes rise{{from{{opacity:0;transform:translateY(14px);}}to{{opacity:1;transform:none;}}}}
.hero-meta{{position:relative;z-index:2;border-top:1px solid rgba(240,223,160,.1);}}
.hero-meta-in{{max-width:1140px;margin:0 auto;padding:18px 28px 24px;display:flex;gap:48px;flex-wrap:wrap;animation:rise .55s .34s cubic-bezier(.22,.8,.36,1) both;}}
.hm-item{{display:flex;flex-direction:column;gap:2px;align-items:center;text-align:center;}}
.hm-label{{font-size:10px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;color:rgba(240,223,160,.38);}}
.hm-value{{font-size:13.5px;color:rgba(220,208,186,.8);font-weight:500;}}
.page-body{{max-width:1140px;margin:0 auto;padding:48px 28px 80px;display:grid;grid-template-columns:1fr 284px;gap:48px;align-items:start;}}
@media(max-width:860px){{.page-body{{grid-template-columns:1fr;}}}}
.provenance{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:0 var(--r-m) var(--r-m) 0;padding:16px 20px;margin-bottom:28px;display:flex;gap:12px;align-items:flex-start;box-shadow:var(--shadow-s);}}
.provenance i{{font-size:17px;color:var(--amber-strong);flex-shrink:0;margin-top:1px;}}
.provenance p{{font-size:13.5px;color:#44525D;line-height:1.65;}}
.provenance p strong{{color:var(--slate-deep);}}
.issue-hdr{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px;padding-bottom:18px;border-bottom:1px solid var(--line);}}
.issue-hdr h2{{font-family:var(--serif);font-weight:500;font-size:24px;color:var(--slate-deep);letter-spacing:-.3px;}}
.issue-hdr h2 span{{font-size:14px;font-weight:400;font-family:var(--sans);color:var(--mut);display:block;margin-top:2px;}}
.issue-count b{{font-family:var(--serif);font-size:34px;font-weight:500;color:var(--amber-strong);display:block;line-height:1;text-align:right;}}
.issue-count small{{font-size:10.5px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut);}}
.filters{{display:inline-flex;gap:3px;margin-bottom:24px;flex-wrap:wrap;background:var(--paper-dim);border:1px solid var(--line);border-radius:99px;padding:4px;}}
.filters a{{background:transparent;border:none;border-radius:99px;font-size:13px;font-weight:600;color:var(--mut);cursor:pointer;padding:6px 14px;transition:background .18s,color .18s;text-decoration:none;display:inline-block;}}
.filters a.flag-pill{{color:#B91C1C;}}
.filters a.active-flagged{{background:#B91C1C;color:#fff;}}
.filters a:hover{{color:var(--slate-deep);}}
.filters a.active{{background:var(--slate-deep);color:#fff;}}
.sec-h{{display:flex;align-items:center;gap:12px;font-family:var(--serif);font-weight:500;font-size:19px;color:var(--slate-deep);letter-spacing:-.2px;margin:34px 0 14px;}}
.sec-h-sub{{font-size:15px;margin:22px 0 10px;padding-left:14px;color:var(--mut);}}
.sec-h-sub i{{font-size:14px;}}
.sec-h:first-of-type{{margin-top:0;}}
.sec-h i{{font-size:17px;color:var(--amber-strong);}}
.sec-h::after{{content:'';flex:1;height:1px;background:var(--line);}}
.sec-h .cnt{{font-size:10.5px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut);font-family:var(--sans);background:var(--paper-dim);border:1px solid var(--line);border-radius:99px;padding:3px 10px;}}
.sec-h .cnt-flagged{{display:inline-flex;align-items:center;gap:4px;color:#B91C1C;background:#FEE2E2;border-color:#FCA5A5;}}
.sec-h .cnt-flagged i{{font-size:10px;}}
.pli{{padding:20px 0;border-bottom:1px solid var(--line);}}
.pli:first-of-type{{border-top:1px solid var(--line);}}
.pli-body{{flex:1;min-width:0;}}
.pli-topic{{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;margin-bottom:8px;}}
.pli-topic::before{{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0;}}
.t-cds{{color:#1E8A6E;}}.t-nlp{{color:#7C5CC4;}}.t-eth{{color:#C76B33;}}.t-ehr{{color:#1A6CB0;}}.t-edu{{color:#B08A1F;}}.t-oth{{color:#5E6B76;}}
.pli h3{{font-family:var(--serif);font-size:18px;font-weight:500;line-height:1.45;margin-bottom:5px;color:var(--slate-deep);letter-spacing:-.1px;}}
.pli h3 a{{color:inherit;text-decoration:none;border-bottom:1px solid var(--line);transition:border-color .18s,color .18s;}}
.pli h3 a:hover{{color:var(--sky);border-color:var(--sky);}}
.glance{{background:linear-gradient(135deg,#FFFDF6,#FFFFFF 55%);border:1px solid #EFE3BC;border-left:3px solid var(--amber);border-radius:0 var(--r-m) var(--r-m) 0;padding:22px 26px;box-shadow:var(--shadow-s);margin-bottom:28px;}}
.glance-kick{{display:flex;align-items:center;gap:8px;font-size:10.5px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:var(--amber-strong);margin-bottom:12px;}}
.glance-kick::after{{content:'';flex:1;height:1px;background:#F0DFA0;}}
.glance p{{font-size:14.5px;color:#37444F;line-height:1.75;margin-bottom:12px;}}
.glance p strong{{color:var(--slate-deep);font-weight:600;}}
.glance .themes{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px;}}
.glance .theme{{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;color:var(--slate-deep);background:var(--card);border:1px solid var(--line);border-radius:99px;padding:5px 13px;cursor:default;}}
.glance .theme i{{font-style:normal;font-size:11px;font-weight:700;color:var(--amber-strong);background:var(--amber-pale);border-radius:99px;padding:1px 7px;}}
.glance .delta-strip{{display:flex;gap:26px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px dashed #EFE3BC;}}
.glance .d-item small{{display:block;font-size:9.5px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:var(--mut);}}
.glance .d-item span{{font-size:13.5px;font-weight:600;color:var(--slate-deep);}}
.glance .d-was{{color:var(--mut);font-weight:400;font-size:12px;}}
.glance .up{{color:#1E8A6E;}}.glance .down{{color:#B91C1C;}}
.pli .authors{{font-size:13px;color:var(--mut);font-style:italic;margin-bottom:2px;}}
.pli .venue{{font-size:13px;color:var(--mut);margin-bottom:10px;}}
.pli-summary{{font-size:14px;color:#44525D;line-height:1.75;margin-bottom:12px;padding:14px 16px;background:var(--paper-dim);border-radius:var(--r-s);border:1px solid var(--line-soft);}}
.pmeta{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}}
.plink{{font-size:12px;font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:5px;background:var(--card);border:1px solid var(--line);padding:5px 12px;border-radius:99px;transition:border-color .18s,box-shadow .18s;}}
.plink:hover{{box-shadow:var(--shadow-s);}}
.plink-pubmed{{color:#1A6CB0;border-color:#BFDBF7;}}.plink-pubmed:hover{{border-color:#1A6CB0;}}
.plink-arxiv{{color:#B83B1A;border-color:#F5C8BC;background:#FEF6F4;}}.plink-arxiv:hover{{border-color:#B83B1A;}}
.plink-medrxiv{{color:#1A8A6E;border-color:#B8E8DA;background:#F2FCF9;}}.plink-medrxiv:hover{{border-color:#1A8A6E;}}
.plink-cinahl{{color:#7C5CC4;border-color:#D8CEEF;background:#F7F4FD;}}.plink-cinahl:hover{{border-color:#7C5CC4;}}
.plink-doi{{color:#9A6200;border-color:#F0D49A;background:#FEF8EC;}}.plink-doi:hover{{border-color:#9A6200;}}
.pmid{{font-size:12px;color:var(--mut);background:var(--paper-dim);border:1px solid var(--line);padding:5px 12px;border-radius:99px;}}
.flag{{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#B91C1C;background:#FEE2E2;border:1px solid #FCA5A5;padding:3px 9px;border-radius:99px;margin-bottom:8px;}}
.sc{{background:var(--card);border:1px solid var(--line);border-radius:var(--r-m);padding:20px 22px;margin-bottom:16px;box-shadow:var(--shadow-s);}}
.sc-label{{font-size:10.5px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:var(--mut);margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line);}}
.sr{{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--line-soft);}}
.sr:last-child{{border-bottom:none;}}
.sr-lbl{{font-size:13.5px;color:var(--mut);}}.sr-val{{font-size:14px;font-weight:600;color:var(--slate-deep);}}
.tbr{{display:flex;align-items:center;gap:9px;padding:4px 0;}}
.tbr-name{{font-size:12.5px;color:var(--mut);min-width:110px;}}
.tbr-track{{flex:1;height:4px;background:var(--paper-dim);border-radius:2px;overflow:hidden;}}
.tbr-fill{{height:100%;background:var(--amber);border-radius:2px;}}
.tbr-n{{font-size:12px;color:var(--mut);min-width:14px;text-align:right;}}
.arch-row{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line-soft);}}
.arch-row:last-child{{border-bottom:none;}}
.arch-lnk{{font-size:13.5px;font-weight:600;color:var(--sky);text-decoration:none;display:inline-flex;align-items:center;gap:5px;transition:gap .18s;}}
.arch-lnk:hover{{gap:9px;}}
.arch-meta{{font-size:12px;color:var(--mut);}}
.about-txt{{font-size:13.5px;color:#44525D;line-height:1.7;margin-bottom:12px;}}
.gh-btn{{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:var(--slate-deep);text-decoration:none;background:var(--paper-dim);border:1px solid var(--line);border-radius:99px;padding:7px 16px;transition:border-color .18s,box-shadow .18s;}}
.gh-btn:hover{{border-color:var(--slate);box-shadow:var(--shadow-s);}}
.dark-sc{{background:var(--slate-deep);border:1px solid rgba(240,223,160,.12);border-radius:var(--r-m);padding:20px 22px;position:relative;overflow:hidden;}}
.dark-sc::before{{content:'';position:absolute;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='52'%3E%3Cpath d='M26 22v8M22 26h8' stroke='%23F0DFA0' stroke-opacity='.05' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E");}}
.dark-sc .sc-label{{position:relative;color:rgba(240,223,160,.38);border-color:rgba(240,223,160,.12);}}
.dark-sc p{{position:relative;font-size:13.5px;color:rgba(220,208,186,.65);line-height:1.7;margin-bottom:10px;}}
.dark-sc a{{position:relative;color:var(--amber);font-weight:600;text-decoration:none;font-size:13.5px;display:inline-flex;align-items:center;gap:5px;transition:gap .18s;}}
.dark-sc a:hover{{gap:9px;}}
footer{{background:var(--slate-deep);padding:24px;text-align:center;}}
footer p{{font-size:13px;color:rgba(220,208,186,.38);}}
footer a{{color:rgba(220,208,186,.6);text-decoration:none;font-weight:600;}}
footer a:hover{{color:var(--amber);}}
</style>
<!-- ANALYTICS: privacy-friendly, no cookies. Dashboard: https://nailcollab.goatcounter.com -->
<script data-goatcounter="https://nailcollab.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
</head>
<body>
<nav class="nav">
  <div class="nav-in">
    <a class="logo" href="/ni-biweekly/">
      <svg viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg"><rect width="36" height="36" rx="8" fill="#1D2B3A"/><path d="M11 26.5V9.5" stroke="#F0DFA0" stroke-width="2.6" stroke-linecap="round"/><path d="M25 26.5V9.5" stroke="#F0DFA0" stroke-width="2.6" stroke-linecap="round"/><path d="M11 9.5l5.5 9 2-5 2 7.5 4.5 5.5" stroke="#E8C46A" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" fill="none"/></svg>
      <div class="logo-t"><b>NI Weekly</b><span>NAIL Collaborative</span></div>
    </a>
    <div class="nav-right">
      <a class="nav-btn" href="/ni-biweekly/"><i class="ti ti-layout-list"></i> All Issues</a>
      <a class="nav-btn" href="https://www.nailcollab.org/"><i class="ti ti-arrow-left"></i> nailcollab.org</a>
    </div>
  </div>
</nav>
<header class="hero">
  <div class="hero-in">
    <div class="kicker">NAIL Digest · Issue #{issue_num}</div>
    <h1>NAIL Digest: <em>AI-Driven Nursing Informatics</em></h1>
    <p class="hero-sub">Week of {start_date} – {end_date} · Retrieved from PubMed · Summarized by Claude Sonnet</p>
    <div class="cta-row">
      <a href="/ni-biweekly/" class="btn btn-amber"><i class="ti ti-layout-list"></i> All Issues</a>
      <a href="#how-it-works" class="btn btn-ghost"><i class="ti ti-info-circle"></i> How it works</a>
    </div>
  </div>
  <div class="hero-meta">
    <div class="hero-meta-in">
      <div class="hm-item"><span class="hm-label">Issue</span><span class="hm-value">#{issue_num}</span></div>
      <div class="hm-item"><span class="hm-label">Week</span><span class="hm-value">{start_date} – {end_date}</span></div>
      <div class="hm-item"><span class="hm-label">Papers</span><span class="hm-value">{total}</span></div>
      <div class="hm-item"><span class="hm-label">Flagged</span><span class="hm-value" style="color:#F0A0A0;">{flag_count}</span></div>
      <div class="hm-item"><span class="hm-label">Generated</span><span class="hm-value">{generated_at}</span></div>
    </div>
  </div>
</header>
<div class="page-body">
  <main>
    <div class="issue-hdr">
      <h2>Issue #{issue_num} <span>Week of {start_date} – {end_date}</span></h2>
      <div class="issue-count"><b>{total}</b><small>Papers</small></div>
    </div>
    {glance_html}
    <div class="provenance" id="how-it-works">
      <i class="ti ti-shield-check" aria-hidden="true"></i>
      <p><strong>How this digest is made:</strong> Papers retrieved bi-weekly from the community-configured sources (PubMed, arXiv, medRxiv, and/or CINAHL — see Digest Settings for this issue's exact sources) using search terms agreed at AINurse-26. Results are restricted to English-language papers; both title and abstract are checked independently, and either being predominantly non-English excludes the paper. Papers are then classified against the community's chosen topics — anything that doesn't clearly match is left out of the issue rather than shown as "Other." Summaries are generated by Claude Sonnet, constrained to report only what the abstract states, with no speculation or inference. The "At a Glance" section above is written under the same constraints — the model synthesizes themes only from the summaries included in this issue, and every number in it (counts, changes since the previous issue) is computed directly from the archive, not by the model. Flagged items indicate incomplete abstracts or unverified peer-review status.</p>
    </div>
    <div class="filters">{filter_pills}</div>
    {topic_sections}
    {flagged_section}
  </main>
  <aside>
    <div class="sc">
      <div class="sc-label">This Issue</div>
      <div class="sr"><span class="sr-lbl">Issue</span><span class="sr-val">#{issue_num}</span></div>
      <div class="sr"><span class="sr-lbl">Papers</span><span class="sr-val">{total}</span></div>
      <div class="sr"><span class="sr-lbl">Flagged</span><span class="sr-val">{flag_count}</span></div>
      <div class="sr"><span class="sr-lbl">Generated</span><span class="sr-val">{generated_at}</span></div>
    </div>
    <div class="sc">
      <div class="sc-label">By Topic</div>
      {topic_bars}
    </div>
    <div class="sc" id="about">
      <div class="sc-label">About NI Weekly</div>
      <p class="about-txt">Born at <strong>AINurse-26</strong> (Ottawa, July 2026) — participants voted on sources, topics, and summary constraints. Runs every other Monday via GitHub Actions.</p>
      <button class="gh-btn cfg-trigger" style="cursor:pointer;border:none;"><i class="ti ti-adjustments-horizontal"></i> Digest settings</button>
    </div>
    <div class="dark-sc">
      <div class="sc-label">Suggest a Topic</div>
      <p>Help shape NAIL Digest — propose search terms, flag missed papers, or suggest governance changes.</p>
      <a href="mailto:ainurse@nailcollab.org">ainurse@nailcollab.org <i class="ti ti-arrow-right" style="font-size:12px;"></i></a>
    </div>
  </aside>
</div>
<footer><p>NAIL Digest · <a href="https://www.nailcollab.org/">NAIL Collaborative</a> · Summaries by Claude Sonnet · Born at AINurse-26, Ottawa 2026</p></footer>
{config_overlay_html}
<script>
(function(){{
  document.querySelectorAll('.filters a').forEach(function(btn){{
    btn.addEventListener('click',function(e){{
      e.preventDefault();
      document.querySelectorAll('.filters a').forEach(function(b){{b.classList.remove('active');b.classList.remove('active-flagged');}});
      var filter=this.dataset.filter||'all';
      this.classList.add(filter==='flagged'?'active-flagged':'active');
      document.querySelectorAll('.pli').forEach(function(p){{
        if(filter==='all'){{p.style.display='';}}
        else if(filter==='flagged'){{p.style.display=p.dataset.flagged==='true'?'':'none';}}
        else{{p.style.display=p.dataset.topic===filter?'':'none';}}
      }});
      document.querySelectorAll('.sec-h').forEach(function(hdr){{
        if(filter==='all'){{hdr.style.display='';return;}}
        if(filter==='flagged'){{
          var isSub=hdr.classList.contains('sec-h-sub');
          var sib=hdr.nextElementSibling,visible=false;
          while(sib){{
            if(sib.classList.contains('sec-h')){{
              if(isSub)break;                          // sub-header stops at the very next header
              if(!sib.classList.contains('sec-h-sub'))break; // top-level header skips over sub-headers, stops at next TOP-LEVEL header
            }}
            if(sib.classList.contains('pli')&&sib.style.display!=='none')visible=true;
            sib=sib.nextElementSibling;
          }}
          hdr.style.display=visible?'':'none';
        }}else{{hdr.style.display=hdr.dataset.topic===filter?'':'none';}}
      }});
    }});
  }});
}})();
</script>
</body>
</html>"""


def render_index(all_issues: list[dict]) -> str:
    """Render the index page listing all issues. all_issues sorted newest-first.
    Each dict: issue_num, filename, start_date, end_date, generated_at, paper_count,
                flag_count, topic_counts (dict topic->int)"""
    current = all_issues[0] if all_issues else None
    back = all_issues[1:] if len(all_issues) > 1 else []
    total_papers = sum(i["paper_count"] for i in all_issues)
    num_issues = len(all_issues)

    TOPIC_STYLE = {
        "Clinical Decision Support": "t-cds",
        "NLP & Generative AI":       "t-nlp",
        "AI Ethics & Governance":    "t-eth",
        "EHR & Workflows":           "t-ehr",
        "Workforce & Education":     "t-edu",
    }

    def topic_pill(t, n):
        short = t.replace(" & Generative AI"," & Gen AI").replace("AI Ethics & Governance","AI Ethics").replace("Workforce & Education","Workforce").replace("Clinical Decision Support","Clin. Decision").replace("EHR & Workflows","EHR")
        return f'<span class="cc-topic">{short} ({n})</span>'

    def back_card(iss):
        tc_html = ""
        if iss.get("topic_counts"):
            for t, n in sorted(iss["topic_counts"].items(), key=lambda x: -x[1])[:4]:
                cls = TOPIC_STYLE.get(t, "t-oth")
                short = t.split("&")[0].strip()[:18]
                tc_html += f'<span class="bc-dot {cls}">{short}</span>'
        flag_n = iss.get("flag_count", 0)
        flagged_badge = (
            f'<span class="bc-flagged"><i class="ti ti-flag" style="font-size:10px;"></i> {flag_n} flagged</span>'
            if flag_n else ""
        )
        return f"""
      <a href="{iss["filename"]}" class="back-card">
        <div class="bc-head">
          <span class="bc-issue">Issue #{iss["issue_num"]}</span>
          <div style="text-align:right;"><b style="font-family:var(--serif);font-size:20px;font-weight:500;color:var(--slate-deep);line-height:1;">{iss["paper_count"]}</b><br><span style="font-size:10.5px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--mut);">papers</span></div>
        </div>
        <div class="bc-date">{iss["start_date"]}</div>
        <div class="bc-week">through {iss["end_date"]}</div>
        <div class="bc-topics">{tc_html}</div>
        {flagged_badge}
        <div class="bc-arrow">Read issue <i class="ti ti-arrow-right" style="font-size:12px;"></i></div>
      </a>"""

    current_pills = ""
    cc_papers = cc_flags = cc_issue = 0
    cc_fn = cc_start = cc_end = cc_gen = ""

    if current:
        cc_papers = current["paper_count"]
        cc_flags  = current["flag_count"]
        cc_fn     = current["filename"]
        cc_issue  = current["issue_num"]
        cc_start  = current["start_date"]
        cc_end    = current["end_date"]
        cc_gen    = current["generated_at"]
        if current.get("topic_counts"):
            for t, n in sorted(current["topic_counts"].items(), key=lambda x: -x[1])[:5]:
                current_pills += topic_pill(t, n)

    back_cards_html = "".join(back_card(i) for i in back) if back else \
        '<div class="empty-state"><i class="ti ti-books" aria-hidden="true"></i><p>Back issues will appear here as more digests are published.<br>New issues every other Monday.</p></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NAIL Digest — All Issues | NAIL Collaborative</title>
<meta name="description" content="Browse all issues of NAIL Digest: AI-Driven Nursing Informatics — a community-curated AI-assisted digest from the NAIL Collaborative.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 36 36'%3E%3Crect width='36' height='36' rx='8' fill='%231D2B3A'/%3E%3Cpath d='M11 26.5V9.5' stroke='%23F0DFA0' stroke-width='2.6' stroke-linecap='round'/%3E%3Cpath d='M25 26.5V9.5' stroke='%23F0DFA0' stroke-width='2.6' stroke-linecap='round'/%3E%3Cpath d='M11 9.5l5.5 9 2-5 2 7.5 4.5 5.5' stroke='%23E8C46A' stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round' fill='none'/%3E%3C/svg%3E">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css">
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400;1,9..144,500&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap');
:root{{--ink:#1D2B3A;--slate:#1D2B3A;--slate-deep:#111C26;--amber:#E8C46A;--amber-strong:#B08A1F;--amber-pale:#FDF5E0;--paper:#FAFAF7;--paper-dim:#F3F1EC;--card:#FFFFFF;--line:#E4E0D8;--line-soft:#EDEAE2;--mut:#5E6B76;--sky:#1A6CB0;--serif:'Fraunces',Georgia,serif;--sans:'Instrument Sans',-apple-system,sans-serif;--shadow-s:0 1px 2px rgba(17,28,38,.05),0 2px 8px rgba(17,28,38,.04);--shadow-m:0 2px 6px rgba(17,28,38,.07),0 12px 28px rgba(17,28,38,.1);--r-s:8px;--r-m:12px;--r-l:18px;}}
*{{box-sizing:border-box;margin:0;padding:0;}}html{{scroll-behavior:smooth;}}body{{font-family:var(--sans);background:var(--paper);color:var(--ink);min-height:100vh;font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased;}}::selection{{background:var(--amber-pale);color:var(--slate-deep);}}
.nav{{position:sticky;top:0;z-index:200;background:rgba(250,250,247,.9);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);}}
.nav-in{{max-width:1140px;margin:0 auto;padding:0 28px;height:64px;display:flex;align-items:center;justify-content:space-between;gap:16px;}}
.logo{{display:flex;align-items:center;gap:11px;text-decoration:none;color:var(--ink);}}
.logo svg{{width:36px;height:36px;flex-shrink:0;border-radius:8px;box-shadow:var(--shadow-s);}}
.logo-t b{{display:block;font-family:var(--serif);font-weight:500;font-size:17px;line-height:1.15;color:var(--slate-deep);}}
.logo-t span{{display:block;font-size:10px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:var(--mut);margin-top:1px;}}
.nav-back{{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:600;color:var(--mut);text-decoration:none;padding:6px 14px;border-radius:99px;border:1px solid var(--line);transition:color .18s,border-color .18s;}}
.nav-back:hover{{color:var(--slate-deep);border-color:var(--slate);}}
.hero{{background:var(--slate);position:relative;overflow:hidden;}}
.hero::before{{content:'';position:absolute;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='52'%3E%3Cpath d='M26 22v8M22 26h8' stroke='%23F0DFA0' stroke-opacity='.06' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E");}}
.hero::after{{content:'';position:absolute;width:600px;height:600px;right:-180px;top:-300px;border-radius:50%;background:radial-gradient(circle,rgba(232,196,106,.14),transparent 65%);}}
.hero-in{{position:relative;z-index:2;max-width:1140px;margin:0 auto;padding:64px 28px 48px;}}
.kicker{{display:inline-flex;align-items:center;gap:10px;color:var(--amber);font-size:11.5px;font-weight:700;letter-spacing:2.2px;text-transform:uppercase;margin-bottom:18px;}}
.kicker::before{{content:'';width:24px;height:2px;background:var(--amber);border-radius:2px;}}
.hero h1{{font-family:var(--serif);font-weight:500;color:#fff;font-size:clamp(26px,4vw,46px);line-height:1.1;letter-spacing:-.4px;margin-bottom:12px;}}
.hero h1 em{{font-style:italic;color:var(--amber);}}
.hero-sub{{color:rgba(220,208,186,.65);font-size:15.5px;line-height:1.7;max-width:680px;}}
.hero-stats{{position:relative;z-index:2;border-top:1px solid rgba(240,223,160,.1);}}
.hero-stats-in{{max-width:1140px;margin:0 auto;padding:16px 28px 22px;display:flex;gap:48px;flex-wrap:wrap;}}
.hs{{display:flex;flex-direction:column;gap:2px;align-items:center;text-align:center;}}
.hs-label{{font-size:10px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;color:rgba(240,223,160,.35);}}
.hs-value{{font-family:var(--serif);font-size:22px;font-weight:500;color:#fff;line-height:1;}}
.wrap{{max-width:1140px;margin:0 auto;padding:52px 28px 80px;}}
.current-label{{display:inline-flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;color:var(--amber-strong);background:var(--amber-pale);border:1px solid #F0DFA0;padding:5px 14px;border-radius:99px;margin-bottom:18px;}}
.current-label::before{{content:'';width:7px;height:7px;border-radius:50%;background:var(--amber-strong);animation:blink 1.8s ease-in-out infinite;}}
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:.25;}}}}
.current-card{{background:var(--slate-deep);border:1px solid rgba(240,223,160,.15);border-radius:var(--r-l);padding:32px 36px;margin-bottom:52px;position:relative;overflow:hidden;display:grid;grid-template-columns:1fr auto;gap:32px;align-items:center;}}
.current-card::before{{content:'';position:absolute;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='52'%3E%3Cpath d='M26 22v8M22 26h8' stroke='%23F0DFA0' stroke-opacity='.05' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E");}}
.current-card::after{{content:'';position:absolute;width:400px;height:400px;right:-150px;top:-200px;border-radius:50%;background:radial-gradient(circle,rgba(232,196,106,.12),transparent 65%);}}
.cc-body{{position:relative;z-index:1;}}
.cc-issue-num{{font-size:11px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;color:rgba(240,223,160,.45);margin-bottom:8px;}}
.cc-title{{font-family:var(--serif);font-size:clamp(18px,2.2vw,26px);font-weight:500;color:#fff;line-height:1.2;margin-bottom:8px;letter-spacing:-.2px;}}
.cc-date{{font-size:14px;color:rgba(220,208,186,.6);margin-bottom:18px;}}
.cc-topics{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px;}}
.cc-topic{{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:4px 11px;border-radius:99px;border:1px solid rgba(255,255,255,.12);color:rgba(220,208,186,.7);}}
.cc-cta{{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:700;padding:12px 24px;border-radius:99px;background:var(--amber);color:var(--slate-deep);text-decoration:none;transition:transform .2s,box-shadow .2s;box-shadow:0 4px 16px rgba(232,196,106,.3);}}
.cc-cta:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(232,196,106,.4);}}
.cc-stats{{position:relative;z-index:1;display:flex;flex-direction:column;gap:14px;align-items:flex-end;}}
.cc-stat{{text-align:right;}}
.cc-stat b{{font-family:var(--serif);font-size:36px;font-weight:500;color:var(--amber);display:block;line-height:1;}}
.cc-stat span{{font-size:10.5px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:rgba(240,223,160,.38);}}
.sec-hdr{{display:flex;align-items:center;gap:12px;margin-bottom:20px;}}
.sec-hdr h2{{font-family:var(--serif);font-weight:500;font-size:22px;color:var(--slate-deep);letter-spacing:-.2px;}}
.sec-hdr::after{{content:'';flex:1;height:1px;background:var(--line);}}
.back-issues-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;}}
.back-card{{background:var(--card);border:1px solid var(--line);border-radius:var(--r-m);padding:22px 24px;text-decoration:none;display:flex;flex-direction:column;gap:10px;box-shadow:var(--shadow-s);transition:transform .22s,box-shadow .22s,border-color .22s;position:relative;overflow:hidden;}}
.back-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--amber),transparent);opacity:0;transition:opacity .22s;}}
.back-card:hover{{transform:translateY(-3px);box-shadow:var(--shadow-m);border-color:#D4CFC5;}}
.back-card:hover::before{{opacity:1;}}
.bc-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;}}
.bc-issue{{font-size:11px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:var(--amber-strong);}}
.bc-date{{font-family:var(--serif);font-size:17px;font-weight:500;color:var(--slate-deep);line-height:1.3;}}
.bc-week{{font-size:13px;color:var(--mut);}}
.bc-topics{{display:flex;gap:6px;flex-wrap:wrap;}}
.bc-flagged{{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;letter-spacing:.4px;color:#B91C1C;background:#FEE2E2;border:1px solid #FCA5A5;border-radius:99px;padding:2px 9px;width:fit-content;}}
.bc-dot{{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;color:var(--mut);}}
.bc-dot::before{{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0;}}
.t-cds{{color:#1E8A6E;}}.t-nlp{{color:#7C5CC4;}}.t-eth{{color:#C76B33;}}.t-ehr{{color:#1A6CB0;}}.t-edu{{color:#B08A1F;}}.t-oth{{color:#5E6B76;}}
.bc-arrow{{font-size:13px;font-weight:600;color:var(--sky);display:flex;align-items:center;gap:5px;transition:gap .18s;margin-top:auto;}}
.back-card:hover .bc-arrow{{gap:9px;}}
.empty-state{{text-align:center;padding:48px 24px;border:1.5px dashed var(--line);border-radius:var(--r-m);}}
.empty-state i{{font-size:32px;color:var(--line);display:block;margin-bottom:12px;}}
.empty-state p{{font-size:14px;color:var(--mut);}}
.about-strip{{background:var(--slate-deep);border-radius:var(--r-l);padding:36px 40px;margin-top:52px;display:grid;grid-template-columns:1fr auto;gap:32px;align-items:center;position:relative;overflow:hidden;}}
.about-strip::before{{content:'';position:absolute;inset:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='52'%3E%3Cpath d='M26 22v8M22 26h8' stroke='%23F0DFA0' stroke-opacity='.05' stroke-width='1.3' stroke-linecap='round'/%3E%3C/svg%3E");}}
.about-strip h3{{position:relative;font-family:var(--serif);font-size:22px;font-weight:500;color:#fff;margin-bottom:6px;letter-spacing:-.2px;}}
.about-strip p{{position:relative;font-size:14px;color:rgba(220,208,186,.62);line-height:1.7;}}
.about-strip-btns{{position:relative;display:flex;gap:10px;flex-wrap:wrap;}}
.btn-amber{{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;font-weight:600;padding:10px 20px;border-radius:99px;background:var(--amber);color:var(--slate-deep);text-decoration:none;transition:transform .2s;border:none;font-family:var(--sans);}}
.btn-amber:hover{{transform:translateY(-2px);}}
.btn-ghost-sm{{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;font-weight:600;padding:10px 20px;border-radius:99px;background:transparent;color:rgba(220,208,186,.8);border:1px solid rgba(240,223,160,.2);text-decoration:none;transition:border-color .2s;font-family:var(--sans);}}
.btn-ghost-sm:hover{{border-color:rgba(240,223,160,.55);}}
@media(max-width:700px){{.current-card{{grid-template-columns:1fr;}}.cc-stats{{flex-direction:row;align-items:flex-start;}}.cc-stat{{text-align:left;}}.about-strip{{grid-template-columns:1fr;gap:20px;padding:28px 24px;}}}}
footer{{background:var(--slate-deep);padding:24px;text-align:center;}}
footer p{{font-size:13px;color:rgba(220,208,186,.38);}}
footer a{{color:rgba(220,208,186,.6);text-decoration:none;font-weight:600;}}
footer a:hover{{color:var(--amber);}}
</style>
<!-- ANALYTICS: privacy-friendly, no cookies. Dashboard: https://nailcollab.goatcounter.com -->
<script data-goatcounter="https://nailcollab.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
</head>
<body>
<nav class="nav">
  <div class="nav-in">
    <a class="logo" href="/ni-biweekly/">
      <svg viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg"><rect width="36" height="36" rx="8" fill="#1D2B3A"/><path d="M11 26.5V9.5" stroke="#F0DFA0" stroke-width="2.6" stroke-linecap="round"/><path d="M25 26.5V9.5" stroke="#F0DFA0" stroke-width="2.6" stroke-linecap="round"/><path d="M11 9.5l5.5 9 2-5 2 7.5 4.5 5.5" stroke="#E8C46A" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" fill="none"/></svg>
      <div class="logo-t"><b>NAIL Digest</b><span>NAIL Collaborative</span></div>
    </a>
    <a class="nav-back" href="https://www.nailcollab.org/"><i class="ti ti-arrow-left"></i> nailcollab.org</a>
  </div>
</nav>
<header class="hero">
  <div class="hero-in">
    <div class="kicker">NAIL Collaborative · Community Digest</div>
    <h1>NAIL Digest: <em>AI-Driven Nursing Informatics</em></h1>
    <p class="hero-sub">A community-curated AI-assisted digest of nursing informatics research — retrieved bi-weekly from PubMed, arXiv, medRxiv, and CINAHL, summarized by Claude Sonnet, overseen by the NAIL editorial board.</p>
  </div>
  <div class="hero-stats">
    <div class="hero-stats-in">
      <div class="hs"><span class="hs-label">Issues published</span><span class="hs-value">{num_issues}</span></div>
      <div class="hs"><span class="hs-label">Papers curated</span><span class="hs-value">{total_papers}</span></div>
      <div class="hs"><span class="hs-label" style="font-family:var(--sans);">Cadence</span><span class="hs-value" style="font-size:16px;font-family:var(--sans);">Bi-weekly · Mondays</span></div>
    </div>
  </div>
</header>
<div class="wrap">
  <div class="current-label"><i class="ti ti-circle-check" aria-hidden="true"></i> Current Issue</div>
  <div class="current-card">
    <div class="cc-body">
      <div class="cc-issue-num">Issue #{cc_issue}</div>
      <div class="cc-title">Week of {cc_start} – {cc_end}</div>
      <div class="cc-date">Published {cc_gen} · Generated by Claude Sonnet</div>
      <div class="cc-topics">{current_pills}</div>
      <a href="{cc_fn}" class="cc-cta">Read Issue #{cc_issue} <i class="ti ti-arrow-right"></i></a>
    </div>
    <div class="cc-stats">
      <div class="cc-stat"><b>{cc_papers}</b><span>Papers</span></div>
      <div class="cc-stat"><b>{cc_flags}</b><span>Flagged</span></div>
    </div>
  </div>
  <div class="sec-hdr"><h2>Back Issues</h2></div>
  <div class="back-issues-grid">{back_cards_html}</div>
  <div class="about-strip">
    <div>
      <h3>Built live at AINurse-26</h3>
      <p>NAIL Digest was created in front of workshop participants who voted on the sources, topics, and summary constraints — making trustworthy AI design a community act. The workflow is open source and runs every other Monday.</p>
    </div>
    <div class="about-strip-btns">
      <a href="mailto:ainurse@nailcollab.org" class="btn-amber"><i class="ti ti-mail"></i> Suggest a topic</a>
    </div>
  </div>
</div>
<footer><p>NAIL Digest · <a href="https://www.nailcollab.org/">NAIL Collaborative</a> · Summaries by Claude Sonnet · Born at AINurse-26, Ottawa 2026</p></footer>
</body>
</html>"""


def regenerate_index() -> None:
    """Rebuild index.html from every ni-biweekly-*.json in the current directory.
    Also writes latest.json — a compact summary of the newest issue that the
    nailcollab.org landing page fetches client-side to render its digest card."""
    import glob
    all_json = sorted(glob.glob("ni-biweekly-*.json"), reverse=True)
    all_issues = []
    newest_data = None
    for jf in all_json:
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            if newest_data is None:
                newest_data = data
            topic_counts = {}
            sources_used = set()
            for p in data.get("papers", []):
                t = p.get("topic", "Other / Unclassified")
                topic_counts[t] = topic_counts.get(t, 0) + 1
                sources_used.add(p.get("source", "PubMed"))
            all_issues.append({
                "issue_num":    data.get("issue", 1),
                "filename":     jf.replace(".json", ".html"),
                "start_date":   data.get("date_range", {}).get("start", ""),
                "end_date":     data.get("date_range", {}).get("end", ""),
                "generated_at": data.get("generated_at", "")[:10],
                "paper_count":  data.get("paper_count", 0),
                "flag_count":   sum(1 for p in data.get("papers", []) if p.get("flags")),
                "topic_counts": topic_counts,
                "sources_used": sorted(sources_used),
            })
        except Exception as e:
            print(f"  WARNING: could not read {jf}: {e}")
    index_html = render_index(all_issues)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"  INDEX → index.html ({len(all_issues)} issue(s))")

    if all_issues and newest_data is not None:
        cur = all_issues[0]
        synth = newest_data.get("synthesis") or {}
        teaser = ""
        if synth.get("overview"):
            teaser = re.sub(r"<[^>]+>", "", synth["overview"][0])
        latest = {
            "issue":        cur["issue_num"],
            "filename":     cur["filename"],
            "date_range":   newest_data.get("date_range", {}),
            "generated_at": newest_data.get("generated_at", ""),
            "paper_count":  cur["paper_count"],
            "flag_count":   cur["flag_count"],
            "topic_counts": cur["topic_counts"],
            "teaser":       teaser,
            "themes":       [{"name": t.get("name", ""), "count": len(t.get("paper_ids", []))}
                             for t in synth.get("themes", [])],
        }
        with open("latest.json", "w", encoding="utf-8") as f:
            json.dump(latest, f, indent=2, ensure_ascii=False)
        print("  LATEST → latest.json (landing-page digest card)")


def is_english_enough(text: str, threshold: float = 0.15) -> bool:
    """Lightweight, dependency-free English-language check.

    Flags text as non-English if more than `threshold` of its alphabetic-ish
    characters fall outside the basic Latin script (catches CJK, Cyrillic,
    Arabic, Hangul, Devanagari, etc.). This is a safety net applied uniformly
    across all sources — PubMed and CINAHL both have their own language
    limiters applied at query time, but this catches anything that slips
    through (e.g. an English title with a non-English abstract, or a source
    without a reliable language filter).
    """
    if not text:
        return True  # nothing to judge, don't false-positive drop it
    non_latin = 0
    counted = 0
    for ch in text:
        if ch.isalpha():
            counted += 1
            # Basic Latin + Latin-1 Supplement + Latin Extended-A/B cover
            # English and most European-language accented characters.
            cp = ord(ch)
            if not (0x0041 <= cp <= 0x024F or cp < 0x0080):
                non_latin += 1
    if counted == 0:
        return True
    return (non_latin / counted) <= threshold


def main():
    parser = argparse.ArgumentParser(description="NAIL Digest — Nursing Informatics Digest")
    parser.add_argument("--days",     type=int, default=14,     help="Lookback window in days (default: 14)")
    parser.add_argument("--max",      type=int, default=50,     help="Max papers per source (default: 50)")
    parser.add_argument("--issue",    type=int, default=1,      help="Issue number (default: 1)")
    parser.add_argument("--dry-run",  action="store_true",      help="Skip API calls, use placeholder summaries")
    parser.add_argument("--output",   choices=["html","json","both"], default="both")
    parser.add_argument("--clean",    action="store_true",      help="Reset index.html to blank (run before going live)")
    parser.add_argument("--config",   type=str, default=None,   help="Path to audience config JSON (e.g. config.json)")
    parser.add_argument("--classify-only", action="store_true", help="Fetch + classify topics only (cheap), skip summaries. Prints a topic/source breakdown and exits.")
    parser.add_argument("--resynthesize",  action="store_true", help="Backfill: generate the At-a-Glance synthesis for existing issues that lack one, re-render their HTML, and rebuild the index. No new papers fetched.")
    parser.add_argument("--force-resynth", action="store_true", help="With --resynthesize: regenerate the synthesis even for issues that already have one.")
    args = parser.parse_args()

    # ── Load config (audience votes override defaults) ─────────────────────
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        try:
            with open(args.config) as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
            print(f"  Config loaded from {args.config}")
            print(f"  Sources:       {cfg['sources']}")
            print(f"  Summary style: {cfg['summary_style']}")
            print(f"  Topics:        {len(cfg['topics'])} selected")
        except Exception as e:
            print(f"  WARNING: could not load config ({e}). Using defaults.")

    active_topics = cfg["topics"] if cfg["topics"] else list(TOPIC_BUCKETS)
    summary_style = cfg["summary_style"]
    sources       = cfg["sources"]

    # ── --clean: reset index to empty state ───────────────────────────────
    if args.clean:
        index_html = render_index([])
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(index_html)
        print("✓ index.html reset to blank (0 issues). Ready to go live.")
        return

    if not args.dry_run and not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        return

    # ── --resynthesize: backfill At-a-Glance synthesis for existing issues ──
    if args.resynthesize:
        import glob
        files = sorted(glob.glob("ni-biweekly-*.json"))  # chronological
        if not files:
            print("  No issue JSON files found in the current directory. Run from ni-biweekly/.")
            return
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if not args.dry_run else None
        prev_data = None
        for jf in files:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            issue_n = data.get("issue", 1)
            if data.get("synthesis") and not args.force_resynth:
                print(f"  Issue #{issue_n} ({jf}): synthesis already present — skipping generation.")
            else:
                print(f"  Issue #{issue_n} ({jf}): generating synthesis...")
                deltas = compute_deltas(data.get("papers", []), prev_data)
                synthesis = generate_synthesis(client, data.get("papers", []), issue_n,
                                               deltas, prev_data, dry_run=args.dry_run)
                if synthesis:
                    data["synthesis"] = synthesis
                    with open(jf, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    print(f"    ✓ synthesis written to {jf}")
                else:
                    print(f"    synthesis not generated (dry run or error) — leaving {jf} unchanged")
            # Re-render HTML either way so template changes propagate to old issues
            gen_iso = data.get("generated_at", "")[:10]
            try:
                gen_str = datetime.fromisoformat(gen_iso).strftime("%b %d, %Y")
            except ValueError:
                gen_str = gen_iso
            html_out = render_html(
                data.get("papers", []), issue_n,
                data.get("date_range", {}).get("start", ""),
                data.get("date_range", {}).get("end", ""),
                config=data.get("config"),
                synthesis=data.get("synthesis"),
                generated_at_str=gen_str,
            )
            html_path = jf.replace(".json", ".html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_out)
            print(f"    HTML → {html_path}")
            prev_data = data
        regenerate_index()
        print("\n✓ Resynthesize complete.\n")
        return

    print(f"\n{'='*60}")
    print(f"  NAIL Digest · Issue #{args.issue}")
    print(f"  Lookback: {args.days} days · Max papers: {args.max} per source")
    print(f"  Sources: {', '.join(sources)}")
    print(f"  Summary style: {summary_style}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    # Dates
    end_dt    = datetime.today()
    start_dt  = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%b %d, %Y")
    end_str   = end_dt.strftime("%b %d, %Y")

    # ── Step 1: Fetch from all configured sources ──────────────────────────
    all_papers = []
    seen_titles = set()
    non_english_skipped = 0

    def _add_if_english_and_new(p: dict) -> None:
        nonlocal non_english_skipped
        if p["title"] in seen_titles:
            return
        # Check title and abstract INDEPENDENTLY, not pooled — pooling lets a
        # non-English title slip through when paired with a long English
        # structured abstract (common for internationally-indexed nursing
        # journals), since the abstract's volume dilutes the title's signal.
        title_ok    = is_english_enough(p["title"])
        abstract_ok = is_english_enough(p.get("abstract", ""))
        if not (title_ok and abstract_ok):
            non_english_skipped += 1
            return
        all_papers.append(p)
        seen_titles.add(p["title"])

    if "pubmed" in sources:
        print("► Step 1a: Searching PubMed...")
        pmids = search_pubmed(args.days, args.max)
        if pmids:
            print("► Step 1b: Fetching PubMed abstracts...")
            pub_papers = fetch_abstracts(pmids)
            for p in pub_papers:
                _add_if_english_and_new(p)

    if "arxiv" in sources:
        print("► Step 1c: Searching arXiv...")
        for p in fetch_arxiv(args.days, args.max):
            _add_if_english_and_new(p)

    if "medrxiv" in sources:
        print("► Step 1d: Searching medRxiv...")
        for p in fetch_medrxiv(args.days, args.max):
            _add_if_english_and_new(p)

    if "cinahl" in sources:
        print("► Step 1e: Searching CINAHL...")
        for p in fetch_cinahl(args.days, args.max):
            _add_if_english_and_new(p)

    if non_english_skipped:
        print(f"  Language filter: skipped {non_english_skipped} non-English paper(s)")

    if not all_papers:
        print("  No papers found across all sources. Exiting.")
        return

    print(f"\n  Total papers across all sources: {len(all_papers)}")

    # ── --classify-only: cheap classification pass, no summaries, no output files ──
    if args.classify_only:
        if args.dry_run:
            print("  NOTE: --dry-run + --classify-only together skip API calls entirely;")
            print("        all papers will show as 'Other / Unclassified' (dropped). Remove --dry-run")
            print("        to get real topic classification (still much cheaper than full summaries).")

        print(f"► Classifying {len(all_papers)} papers (topic only, no summaries — cheap)...")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if not args.dry_run else None

        topic_counts  = {t: 0 for t in active_topics}
        source_counts_kept    = {}
        source_counts_dropped = {}
        kept_n = dropped_n = 0

        for i, paper in enumerate(all_papers, 1):
            src = paper.get("source", "PubMed")
            topic, confidence = classify_paper(client, paper, dry_run=args.dry_run, active_topics=active_topics)
            keep = topic != "Other / Unclassified" and confidence == "high"
            status = "kept" if keep else "dropped"
            print(f"  [{i}/{len(all_papers)}] [{src}] {status:<8} {topic:<30} {paper['title'][:50]}...")

            if keep:
                kept_n += 1
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
                source_counts_kept[src] = source_counts_kept.get(src, 0) + 1
            else:
                dropped_n += 1
                source_counts_dropped[src] = source_counts_dropped.get(src, 0) + 1

        print(f"\n{'='*60}")
        print(f"  CLASSIFICATION PREVIEW — {len(all_papers)} papers fetched")
        print(f"{'='*60}")
        print(f"\n  {kept_n} would be KEPT · {dropped_n} would be DROPPED "
              f"(outside chosen topics or low-confidence match)")
        print(f"\n  Kept, by source:")
        for src, n in sorted(source_counts_kept.items(), key=lambda x: -x[1]):
            print(f"    {src:<12} {n}")
        print(f"\n  Kept, by topic:")
        for topic, n in sorted(topic_counts.items(), key=lambda x: -x[1]):
            if n > 0:
                print(f"    {n:>3}  {topic}")
        if dropped_n:
            print(f"\n  Dropped, by source:")
            for src, n in sorted(source_counts_dropped.items(), key=lambda x: -x[1]):
                print(f"    {src:<12} {n}")
        print(f"\n  No files written. Re-run without --classify-only to generate the full issue.\n")
        return

    # ── Step 2: Classify, filter, then summarise only survivors ─────────────
    print(f"► Step 2a: Classifying {len(all_papers)} papers (cheap pass)...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if not args.dry_run else None

    classified = []
    for i, paper in enumerate(all_papers, 1):
        src = paper.get("source", "PubMed")
        topic, confidence = classify_paper(client, paper, dry_run=args.dry_run, active_topics=active_topics)
        keep = topic != "Other / Unclassified" and confidence == "high"
        status = "kept" if keep else "dropped"
        print(f"  [{i}/{len(all_papers)}] [{src}] {status:<8} {topic:<30} {paper['title'][:50]}...")
        classified.append((paper, topic, confidence, keep))

    kept_papers = [(p, t, c) for p, t, c, k in classified if k]
    dropped_count = len(classified) - len(kept_papers)
    print(f"\n  Classification filter: {len(kept_papers)} kept, {dropped_count} dropped "
          f"(outside chosen topics or low-confidence match)")

    if not kept_papers:
        print("\n  No papers survived the topic/confidence filter. "
              "No issue generated — consider widening --days, adding sources, "
              "or reviewing the community's chosen topics in config.json.")
        return

    print(f"\n► Step 2b: Generating summaries for {len(kept_papers)} papers ({summary_style} style)...")
    processed = []
    for i, (paper, topic, confidence) in enumerate(kept_papers, 1):
        src = paper.get("source", "PubMed")
        print(f"  [{i}/{len(kept_papers)}] [{src}] {paper['title'][:60]}...")
        summary, flags = generate_summary(client, paper, dry_run=args.dry_run, style=summary_style)
        processed.append({
            **paper,
            "summary": summary,
            "flags": flags,
            "topic": topic,
            "topic_confidence": confidence,
        })

    # ── Step 2c: Issue synthesis ("This Issue at a Glance") ────────────────
    date_slug = datetime.today().strftime("%Y-%m-%d")
    print(f"\n► Step 2c: Generating issue synthesis (At a Glance)...")
    import glob
    prev_issue_data = None
    prev_files = [f for f in sorted(glob.glob("ni-biweekly-*.json"))
                  if f != f"ni-biweekly-{date_slug}.json"]  # exclude same-day rerun
    if prev_files:
        try:
            with open(prev_files[-1], encoding="utf-8") as f:
                prev_issue_data = json.load(f)
            print(f"  Comparing against Issue #{prev_issue_data.get('issue')} ({prev_files[-1]})")
        except Exception as e:
            print(f"  WARNING: could not read previous issue {prev_files[-1]}: {e}")
    deltas = compute_deltas(processed, prev_issue_data)
    synthesis = generate_synthesis(client, processed, args.issue, deltas,
                                   prev_issue_data, dry_run=args.dry_run)
    print("  Synthesis " + ("generated." if synthesis else
          "skipped (dry run or generation failed) — issue will publish without the glance box."))

    # ── Step 3: Output ─────────────────────────────────────────────────────
    print(f"\n► Step 3: Writing output...")

    if args.output in ("json", "both"):
        json_path = f"ni-biweekly-{date_slug}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "issue":        args.issue,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "date_range":   {"start": start_str, "end": end_str},
                "paper_count":  len(processed),
                "config":       cfg,
                "synthesis":    synthesis,
                "papers":       processed,
            }, f, indent=2, ensure_ascii=False)
        print(f"  JSON → {json_path}")

    if args.output in ("html", "both"):
        html_path = f"ni-biweekly-{date_slug}.html"
        html = render_html(processed, args.issue, start_str, end_str, config=cfg,
                           synthesis=synthesis)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML → {html_path}")

        # Regenerate index.html from all JSON files in the current directory
        regenerate_index()

    flag_count = sum(1 for p in processed if p.get("flags"))
    print(f"\n✓ Done. {len(processed)} papers · {flag_count} flagged.\n")


if __name__ == "__main__":
    main()

