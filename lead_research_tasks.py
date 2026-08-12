import os
import asyncio
import json
import re
from datetime import timedelta, datetime
from urllib.parse import urljoin, urlparse
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from tavily import TavilyClient
    from db_setup import SessionLocal, CompanyLead
    from company_names import clean_company_name, PAREN
    from cognitive_engine import (
        client as groq_client,
        GROQ_API_KEY,
        classify_lead_relevance_batch,
        _fuzzy_company_match,
    )
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sync_playwright = None

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LINKEDIN_DOMAINS = ["linkedin.com"]

_DIRECTORY_HOSTS = (
    "indiamart.com", "justdial", "sulekha", "tradeindia", "yellowpages",
    "exportersindia", "yelu", "indiabizclub", "zaubacorp.com", "tofler",
    "tracxn.com", "crediwatch", "registerkaro", "vakilsearch", "ebizprise",
    "emis.com", "crunchbase", "owler", "bizapedia", "opencorporates",
    "thecompanycheck", "quickcompany", "corporationwiki", "buzzfile",
    "moneycontrol", "bloomberg", "dnb.com", "kompass",
    "instafinancials.com", "indiafilings.com", "sensibook.com", "falcon ebiz",
    "financeninsurance.com", "charteredone", "ibphub.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "glassdoor.com", "indeed.com", "naukri.com", "timesjobs.com", "zoominfo",
)

def _company_tokens(company_name: str) -> set:
    cleaned = clean_company_name(PAREN.sub("", company_name or ""))
    return {w for w in re.findall(r"[a-z0-9]+", cleaned.lower()) if len(w) >= 4}


def _is_clean_host(url: str) -> bool:
    host = url.split("/")[2] if "//" in url else ""
    return not host or not any(d in host.lower() for d in _DIRECTORY_HOSTS)


def _website_plausible(url: str, company_name: str) -> bool:
    """A real company website, not a directory page or another firm whose URL
    merely mentions the company (e.g. rpgroupbd.com/industry/rp-biotech is an
    apparel group, NOT R.P. Biotech). The company token must be in the HOST."""
    if not _is_clean_host(url):
        return False
    host = url.split("/")[2] if "//" in url else url
    tokens = {t for t in _company_tokens(company_name) if len(t) >= 4}
    if tokens:
        return any(t in host.lower() for t in tokens)
    clean = re.sub(r"[^a-z0-9]", "", company_name.lower())
    host_clean = re.sub(r"[^a-z0-9]", "", host.lower())
    return len(clean) >= 5 and clean in host_clean


def _pick_best_website(items: list, company_name: str) -> str:
    for it in items:
        url = it.get("url", "")
        if _website_plausible(url, company_name):
            return url
    return ""


def _linkedin_slug(url: str) -> str:
    low = url.lower().rstrip("/")
    return low.split("linkedin.com/", 1)[-1] if "linkedin.com" in low else ""


def _pick_linkedin(items: list, company_name: str) -> str:
    tokens = {t for t in _company_tokens(company_name) if len(t) >= 5}
    # Prefer company page with a name-matching slug
    for it in items:
        url = it.get("url", "")
        slug = _linkedin_slug(url)
        if not slug or "linkedin.com/company/" not in url.lower():
            continue
        if tokens and any(t in slug for t in tokens):
            return url
    # Any company page
    for it in items:
        url = it.get("url", "")
        if "linkedin.com/company/" in url.lower():
            return url
    # Fuzzy match on title for a company page
    for it in items:
        url = it.get("url", "")
        if "linkedin.com/company/" in url.lower() and _fuzzy_company_match(company_name, it.get("title", "")):
            return url
    return ""


_NON_PERSON_NAME_WORDS = (
    "alert", "alerts", "batch", "batches", "samples", "nsq", "substandard",
    "recall", "recalls", "flagged", "flags", "declared", "declares", "cdsco",
    "advisory", "warning", "fda", "drug", "drugs", "report", "reports", "news",
    "article", "announced", "ban", "bans", "banned", "sale", "sales", "pvt",
    "ltd", "limited", "private", "pharma", "pharmaceutical", "pharmaceuticals",
    "manufacturer", "supplier", "exporter", "retailer", "biotech", "company",
    "profile", "product", "products", "services", "vaccine", "tablets",
    "injection", "manufacturing", "industry", "india",
    # address / location / company-registry keywords (appear in ALL CAPS)
    "road", "street", "lane", "avenue", "plot", "phase", "sector", "pincode",
    "district", "distt", "tehsil", "taluka", "village", "post", "via", "near",
    "opp", "opposite", "behind", "beside", "court", "civil", "police", "station",
    "industrial", "area", "focal", "point", "nagar", "colony", "mandi", "bazaar",
    "gate", "chowk", "gali", "mohalla", "pur", "puram", "pura", "bad", "bazaar",
    "associates", "enterprises", "corporation", "international", "industries",
    "solutions", "traders", "trading", "agency", "group", "co", "corp",
    "incorporated", "holdings", "ventures", "infra", "projects", "contracts",
    "auto", "motors", "engines", "works", "foundry", "textiles", " mills",
    "fashion", "retail", "stores", "mart", "supermarket", "hospital", "clinic",
    "diagnostic", "diagnostics", "pathlab", "pharmacy", "pharmacies",
    "dispensary", "nursing", "school", "college", "institute", "academy",
    "coaching", "tutorials", "bank", "finance", "financial", "insurance",
    "investment", "funds", "capital", "credit", "housing", "estate", "realtors",
    "construction", "builders", "developers", "architects", "consultants",
    "logistics", "transport", "courier", "travel", "tours",
    "hotel", "restaurant", "cafe", "bakery", "caterers", "state", "roadopp",
    "officesunam", "india", "gujarat", "punjab", "rajasthan", "haryana",
    "maharashtra", "karnataka", "chennai", "mumbai", "delhi", "hyderabad",
    "bangalore", "kolkata", "pune", "ahmedabad", "jaipur", "lucknow",
)


def _looks_like_person_name(name: str) -> bool:
    """Reject news headlines, company descriptions and other non-person titles
    that leak into decision-maker results."""
    if not name:
        return False
    low = name.lower()
    if len(name.split()) > 6:
        return False
    return not any(re.search(rf"\b{re.escape(w)}\b", low) for w in _NON_PERSON_NAME_WORDS)


_NAME_TITLE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
_NAME_ALLCAPS_RE = re.compile(r"\b([A-Z]{2,}(?:\s+[A-Z]{2,}){1,2})\b")

# Common English words that appear in page chrome, product descriptions, UI
# labels and location fragments — but are NOT parts of Indian person names.
# A candidate name made ENTIRELY of these is almost certainly not a person.
_COMMON_ENGLISH = {
    "a", "an", "the", "and", "or", "but", "nor", "for", "yet", "so",
    "as", "at", "by", "in", "of", "on", "to", "up", "via", "per", "vs",
    "is", "be", "am", "are", "do", "no", "not", "all", "any", "few",
    "more", "most", "other", "some", "such", "than", "that", "this",
    "what", "which", "who", "whom", "how", "when", "where", "why",
    "about", "above", "after", "again", "also", "back", "been", "before",
    "being", "below", "between", "both", "can", "come", "could", "day",
    "days", "did", "does", "done", "down", "each", "end", "even", "ever",
    "example", "eye", "far", "find", "first", "for", "from", "get", "got",
    "had", "has", "have", "hear", "here", "high", "him", "his", "hold",
    "home", "into", "just", "kind", "knew", "know", "last", "less", "let",
    "like", "list", "long", "look", "made", "make", "man", "many", "may",
    "men", "mind", "more", "most", "must", "name", "near", "need", "new",
    "next", "now", "off", "old", "one", "only", "open", "our", "out",
    "over", "own", "part", "per", "plan", "point", "power", "price",
    "put", "ran", "read", "rest", "road", "run", "said", "same", "saw",
    "say", "second", "see", "seem", "send", "sent", "set", "she", "ship",
    "short", "show", "side", "simple", "since", "six", "small", "soon",
    "stand", "start", "state", "still", "such", "sure", "take", "tell",
    "than", "that", "the", "them", "then", "there", "these", "they",
    "thing", "think", "this", "those", "three", "through", "time", "told",
    "too", "took", "top", "town", "true", "turn", "two", "under", "unit",
    "until", "upon", "use", "very", "voice", "wait", "walk", "want",
    "war", "was", "way", "week", "well", "went", "were", "west", "what",
    "when", "where", "which", "while", "who", "why", "will", "wind",
    "with", "word", "work", "world", "would", "year", "yes", "you",
    "your", "add", "added", "address", "ago", "agree", "amount", "area",
    "ask", "away", "based", "best", "better", "big", "black", "blue",
    "board", "book", "box", "bring", "brought", "build", "built", "bus",
    "buy", "called", "came", "care", "carry", "case", "cause", "cent",
    "central", "certain", "change", "charge", "check", "child", "city",
    "claim", "class", "clean", "clear", "close", "cold", "common",
    "company", "complete", "contain", "cost", "count", "country", "county",
    "cover", "cross", "current", "cut", "data", "date", "deal", "dear",
    "deep", "design", "did", "direct", "district", "does", "done", "door",
    "down", "draw", "drawn", "drive", "dry", "during", "early", "east",
    "easy", "effect", "eight", "else", "enough", "ever", "every", "face",
    "fact", "fall", "family", "far", "fast", "father", "feel", "field",
    "figure", "fill", "final", "fine", "fire", "first", "five", "floor",
    "follow", "food", "foot", "force", "form", "found", "four", "free",
    "front", "full", "gave", "general", "girl", "give", "given", "go",
    "going", "gold", "gone", "good", "got", "grand", "great", "green",
    "ground", "group", "grow", "half", "hand", "happen", "hard", "head",
    "hear", "heat", "held", "help", "here", "high", "hill", "hit", "hold",
    "hole", "home", "hope", "hot", "hour", "house", "huge", "human",
    "hundred", "idea", "inch", "include", "interest", "issue", "job",
    "join", "joy", "jump", "keep", "kept", "kind", "king", "knew", "know",
    "known", "lack", "lady", "laid", "lake", "land", "large", "last",
    "late", "later", "lay", "lead", "learn", "least", "leave", "left",
    "less", "letter", "level", "lie", "life", "lift", "light", "line",
    "list", "listen", "little", "live", "longer", "lose", "loss", "lost",
    "lot", "love", "low", "main", "major", "make", "manage", "manager",
    "many", "mark", "market", "mass", "master", "matter", "mean", "meet",
    "member", "men", "middle", "might", "mile", "million", "mind", "miss",
    "money", "month", "moon", "morning", "mother", "move", "much",
    "music", "near", "need", "news", "nice", "nine", "none", "north",
    "note", "nothing", "notice", "number", "offer", "office", "often",
    "oil", "once", "order", "others", "outside", "page", "paid", "pair",
    "paper", "part", "pass", "past", "pay", "people", "period", "person",
    "picture", "piece", "place", "plain", "plan", "plane", "plant",
    "play", "please", "point", "poor", "position", "possible", "pound",
    "press", "pretty", "private", "problem", "produce", "product", "public",
    "pull", "purpose", "push", "question", "quick", "quite", "race",
    "rain", "raise", "range", "rate", "reach", "ready", "real", "reason",
    "receive", "record", "red", "remain", "remember", "report", "result",
    "return", "rich", "ride", "right", "ring", "rise", "river", "road",
    "rock", "room", "round", "rule", "safe", "sale", "salt", "same",
    "save", "school", "science", "sea", "season", "seat", "section",
    "seek", "seem", "seen", "self", "sell", "sense", "sent", "serve",
    "service", "set", "seven", "several", "shall", "shape", "share",
    "sharp", "ship", "short", "shot", "should", "show", "shut", "sick",
    "side", "sign", "simple", "sing", "sister", "sit", "six", "size",
    "sleep", "slow", "small", "snow", "soil", "soldier", "someone",
    "son", "song", "sort", "sound", "south", "space", "speak", "special",
    "speed", "spend", "spot", "spread", "spring", "square", "stage",
    "stand", "star", "start", "station", "stay", "step", "stick", "stone",
    "stop", "store", "story", "straight", "strange", "stream", "street",
    "strong", "student", "study", "subject", "substance", "sudden",
    "suffix", "sugar", "suggest", "suit", "summer", "sun", "supply",
    "support", "sure", "surface", "surprise", "swim", "system", "table",
    "tail", "talk", "tall", "teach", "teacher", "ten", "term", "test",
    "thank", "thick", "thin", "think", "third", "those", "though",
    "thought", "thousand", "thus", "tie", "today", "together", "tone",
    "too", "tool", "top", "total", "touch", "toward", "trade", "train",
    "travel", "tree", "trouble", "true", "trust", "try", "turn", "twenty",
    "type", "upon", "value", "verb", "view", "village", "visit", "wait",
    "wall", "watch", "water", "wear", "weight", "west", "wheel", "white",
    "whole", "wide", "wife", "wild", "win", "window", "winter", "wire",
    "wish", "woman", "wonder", "wood", "work", "write", "wrong", "yard",
    "yellow", "yesterday", "young",
    # domain-specific page chrome / descriptors
    "about", "also", "bulk", "care", "clip", "cool", "curly", "details",
    "dosage", "dry", "extension", "focal", "form", "hair", "industrial",
    "inquiry", "length", "liquid", "listed", "maharashtra", "marketing",
    "natural", "ndonso", "ndonco", "packs", "party", "plot", "price",
    "remy", "review", "seller", "send", "stand", "tamil", "vials", "wavy",
    "working", "send", "inquiry",
}


def _extract_person_names(text: str) -> list:
    """Extract candidate person names from a block of text.

    Handles two common forms on the web:
    - Title Case: "Vikas Kumar", "Bhawna Joshi"
    - ALL CAPS director lists: "KUNWAR RAMPAL, SAHIL RAMPAL" (corporate registries)

    Returns a deduplicated list of name strings (Title Case).
    """
    if not text:
        return []
    seen = set()
    out = []
    for m in _NAME_TITLE_RE.finditer(text):
        name = m.group(1)
        key = name.lower()
        if key not in seen and _looks_like_person_name(name) and not _all_common(name):
            seen.add(key)
            out.append(name)
    for m in _NAME_ALLCAPS_RE.finditer(text):
        raw = m.group(1)
        name = raw.title()
        key = name.lower()
        if key not in seen and _looks_like_person_name(name) and not _all_common(name):
            seen.add(key)
            out.append(name)
    return out


def _all_common(name: str) -> bool:
    """True if every word in the name is a common English word (not a proper
    noun). Such candidates are page chrome, not people."""
    words = name.lower().split()
    return bool(words) and all(w in _COMMON_ENGLISH for w in words)


def _regex_extract_people(company_name: str, combined: list) -> list:
    """Reliable fallback that does NOT need the LLM.

    Handles three signals that appear consistently in the combined pool:
    1. LinkedIn profiles (url contains linkedin.com/in/) -> name from title
    2. "Directors are X, Y, Z" patterns in corporate-registry snippets
    3. Signatory tables ("| Name | Director | ...")
    """
    seen = set()
    out = []

    def _add(name, role, role_type, ln_url, confidence):
        name = re.sub(r"\s+", " ", name).strip()
        if not name or len(name) < 3 or not _looks_like_person_name(name):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({
            "name": name,
            "role": role,
            "role_type": role_type,
            "linkedin_url": ln_url,
            "email": "",
            "confidence": confidence,
        })

    for it in combined:
        url = it.get("url", "")
        title = it.get("title", "")
        snippet = it.get("snippet", "") or ""

        # Signal 1: LinkedIn personal profile. The title is "First Last - role"
        # or just "First Last". The URL is the source of truth. Only accept if
        # the result actually mentions the target company (title/snippet), so a
        # LinkedIn profile of someone at another '...biotech' firm cannot leak.
        if "linkedin.com/in/" in url.lower():
            # The profile title is the authoritative employer signal.  Search
            # snippets may append "similar people" from unrelated companies.
            if not _fuzzy_company_match(company_name, title):
                continue
            raw = title.split(" - ")[0].split("|")[0].split("·")[0].strip()
            name = re.sub(r"\s+", " ", raw).strip()
            if name and _looks_like_person_name(name):
                role = title.split(" - ")[1].strip() if " - " in title else ""
                _add(name, role, _role_type_from_text(role), url, "high")
            continue

        # Only consider corporate-registry pages for the text patterns, and only
        # if they actually reference the target company (a snippet about
        # "Professional Biotech" or a bank list must not leak directors in).
        hay = (snippet + " " + title).lower()
        if not any(k in hay for k in ("director", "signatory", "board", "key management")):
            continue
        if not _fuzzy_company_match(company_name, title + " " + snippet):
            continue

        # Signal 2: "Directors ... are X, Y, Z" style lists.
        m = re.search(r"directors?\s+(?:of\s+.+?\s+)?(?:are|is)\s+(.+?)(?:\.\s|\.\n|$)", snippet, re.IGNORECASE)
        if m:
            blob = m.group(1)
            for part in re.split(r",|\band\b", blob):
                candidate = part.strip()
                if 1 <= len(candidate.split()) <= 3 and _looks_like_person_name(candidate):
                    _add(candidate.title(), "Director", "managing_director", "", "high")

        # Signal 3: signatory tables with "| Name | Designation |" rows.
        for row in re.finditer(r"\|\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*\|\s*(director|signatory|md|ceo|founder)", snippet, re.IGNORECASE):
            _add(row.group(1), row.group(2).title(), "managing_director", "", "high")
    return out


def _role_type_from_text(text: str) -> str:
    """Map a job-title/role string to a coarse role_type."""
    hay = text.lower()
    if any(k in hay for k in ("qa head", "qa manager", "quality assurance head", "quality manager", "quality control manager")):
        return "qa_head"
    if any(k in hay for k in ("qa", "quality assurance", "quality control", "quality officer")):
        return "qa_manager"
    if "managing director" in hay:
        return "managing_director"
    if any(k in hay for k in ("founder", "ceo ", "chief executive", " co-founder")):
        return "founder_ceo"
    if any(k in hay for k in ("plant head", "production head", "production manager")):
        return "plant_head"
    return "other"


# ---------------------------------------------------------------------------
# Website scraping (Playwright) + corporate-registry (MCA) data
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?:\+91[\s\-]?|0)?[\s\-]?\(?\d{5}\)?[\s\-]?\d{5}\b"
    r"|\+91[\s\-]?\d{5}[\s\-]?\d{5}"
)
_EMAIL_RE2 = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_ADDRESS_HINTS = (
    "address", "located at", "location", "head office", "registered office",
    "works at", "unit", "plot no", "phase", "industrial area", "batala",
    "gurdaspur", "punjab", "street", "road", "near", "opposite", "chandigarh",
)
_FINANCIAL_HINTS = (
    "turnover", "revenue", "sales", "crore", "lakh", "million", "billion",
    "export", "exports", "annual", "fiscal", "profit", "net worth", "roi",
)


def _scrape_site_text(url: str, max_pages: int = 6, per_page_chars: int = 12000) -> list:
    """Web-scrape a company website: home + likely about/team/contact pages.

    Uses Playwright (headless Chromium) so JS-rendered sites work. Returns a
    list of {url, title, text} for the pages that actually loaded."""
    if not url or sync_playwright is None:
        return []
    parsed = urlparse(url if "//" in url else "https://" + url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not parsed.scheme:
        base = "https://" + (url if "//" in url else url).split("/")[0]
    if not parsed.netloc:
        base = "https://" + url.split("/")[0]
    candidates = [
        "", "/", "/about", "/about-us", "/aboutus", "/team", "/our-team",
        "/management", "/leadership", "/contact", "/contact-us", "/contactus",
    ]
    pages = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            loaded = 0
            for path in candidates:
                if loaded >= max_pages:
                    break
                page_url = urljoin(base + "/", path.lstrip("/"))
                if page_url.rstrip("/") == base.rstrip("/"):
                    page_url = base + "/"
                try:
                    page = context.new_page()
                    page.goto(page_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    text = (page.inner_text("body") or "").strip()
                    title = (page.title() or "").strip()
                    page.close()
                except Exception:
                    continue
                if len(text) < 80:
                    continue
                pages.append({
                    "url": page_url,
                    "title": title,
                    "text": text[:per_page_chars],
                })
                loaded += 1
            context.close()
            browser.close()
    except Exception as e:
        print(f"scrape_company_website error for {url}: {e}")
    return pages


def _website_people(text: str) -> list:
    """Pull likely employee names off a company site: 'Name - Job Title',
    'Name, Designation' or 'Mr. Name (Role)' on the same line."""
    out = []
    seen = set()
    role_kw = re.compile(
        r"director|founder|ceo|chairman|managing\s+director|md\b|manager|head|qa|quality|"
        r"vp\b|president|chief|officer|executive|owner|proprietor|partner|admin|hr\b|"
        r"pharmacist|chemist|lead\b",
        re.I,
    )
    # Words that mark a role/descriptor instead of a person's given name. A
    # candidate made up of these ("Managing Director", "Lakh Directors
    # Information") is not an employee name.
    role_words = {
        "director", "directors", "founder", "ceo", "chairman", "chairperson",
        "managing", "manager", "managers", "head", "officer", "executive",
        "owner", "proprietor", "partner", "president", "chief", "admin",
        "pharmacist", "chemist", "information", "profile", "lakh", "lakhs",
        "crore", "million", "billion", "thousand", "companies", "gstin",
        "msme", "gst", "legal", "summary", "detailed", "contact", "team",
    }
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > 200:
            continue
        # Role keyword must be present on the SAME line.
        if not role_kw.search(line):
            continue
        for m in _NAME_TITLE_RE.finditer(line):
            name = m.group(1)
            low = name.lower()
            if low in seen or _all_common(name) or not _looks_like_person_name(name):
                continue
            if re.search(r"\d", name):
                continue
            words = set(low.split())
            if words & role_words:
                continue
            # The name must sit next to the role keyword (within 60 chars).
            window = line[max(0, m.start() - 30):m.end() + 60]
            if not role_kw.search(window):
                continue
            seen.add(low)
            out.append(name)
    return out


@activity.defn
async def scrape_company_website_activity(payload: dict) -> dict:
    """Web-scrape the company's own website and store everything found:
    page texts, contact emails/phones, physical + registered address,
    financial mentions and team member names. Returns a structured dict that
    evaluate_and_save persists into the lead record's scraped_data column."""
    def _work() -> dict:
        company_name = payload.get("company_name", "")
        website = payload.get("website", "")
        pages = _scrape_site_text(website)
        if not pages:
            return {"company_name": company_name, "website": website, "pages": [],
                    "emails": [], "phones": [], "address": "", "financials": [],
                    "team_members": [], "raw_text": ""}
        all_text = "\n\n".join(p.get("text", "") for p in pages)
        emails = list(dict.fromkeys(_EMAIL_RE2.findall(all_text)))
        phones = list(dict.fromkeys(m for m in _PHONE_RE.findall(all_text)))
        address = ""
        for line in re.split(r"\n+", all_text):
            if any(h in line.lower() for h in _ADDRESS_HINTS) and re.search(r"\d{5,6}", line):
                address = line.strip()[:300]
                break
        financials = []
        for line in re.split(r"\n+", all_text):
            if any(h in line.lower() for h in _FINANCIAL_HINTS) and re.search(r"\d", line):
                financials.append(line.strip()[:200])
        financials = list(dict.fromkeys(financials))[:6]
        return {
            "company_name": company_name,
            "website": website,
            "pages": [{"url": p["url"], "title": p["title"]} for p in pages],
            "emails": emails[:5],
            "phones": phones[:5],
            "address": address,
            "financials": financials,
            "team_members": _website_people(all_text)[:10],
            "raw_text": all_text[:20000],
        }
    return await asyncio.to_thread(_work)


# Corporate-registry (MCA-derived) platform domains — these publish director /
# signatory tables straight from Ministry of Corporate Affairs filings.
_REGISTRY_DOMAINS = (
    "zaubacorp.com", "tofler.com", "tracxn.com", "indiafilings.com",
    "thecompanycheck.com", "falcon ebiz", "falconebiz", "instafinancials.com",
    "quickcompany.com", "vakilsearch.com", "opencompany", "financeninsurance.com",
    "corporation", "credit risk monitor", "mercantile", "diligence",
)

def _registry_queries(company_name: str) -> list:
    q = company_name.strip().replace('"', '')
    return [
        f'"{q}" directors zaubacorp',
        f'"{q}" directors tofler',
        f'"{q}" CIN OR "corporate identification number" directors',
        f'"{q}" "registered office" directors MCA',
        f'"{q}" signatories tracxn OR indiafilings OR thecompanycheck',
    ]


@activity.defn
async def search_corporate_registry_activity(company_name: str) -> dict:
    """Search corporate-registry platforms (MCA-derived data). These list
    director names, DINs and the registered address authoritatively."""
    def _work() -> dict:
        items = _search_and_classify(company_name, _registry_queries(company_name),
                                     "decision_maker", threshold=20, max_results=8)
        registry_items = [it for it in items
                          if any(d in (it.get("url", "").lower() or it.get("title", "").lower())
                                 for d in _REGISTRY_DOMAINS)]
        directors, cin = _extract_registry_directors(company_name, registry_items or items)
        return {
            "company_name": company_name,
            "registry_items": items[:8],
            "directors": directors,
            "cin": cin,
        }
    return await asyncio.to_thread(_work)


def _extract_registry_directors(company_name: str, items: list) -> tuple:
    """Deterministic director extraction from corporate-registry snippets.

    The MCA-derived platforms (ZaubaCorp, Tofler, Tracxn, IndiaFilings, etc.)
    print director names in two very stable formats:
      - "Directors of <COMPANY> are X, Y, Z"  (or 'Directors : X, Y, Z')
      - signatory tables '| Name | Director |'
    Plus a CIN 'U24232PB2001PTC024580' wherever it appears.

    Only items that demonstrably refer to the TARGET company are used, so a
    same-word relative ("Professional Biotech", "Sucantis Biotech") or a bank
    list of unrelated directors cannot leak in."""
    seen = set()
    out = []
    cin = ""

    def _item_is_target(it: dict) -> bool:
        url = (it.get("url") or "").lower()
        text = (it.get("title", "") + " " + (it.get("snippet") or "")).lower()
        m = re.search(r"U\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}", text + url)
        if m:
            return True
        # URL slug must contain the company's distinctive name tokens
        # (legal suffixes removed word-wise): 'r p biotech' -> 'rpbiotech'.
        cleaned = clean_company_name(PAREN.sub("", company_name or "")).lower()
        tokens = [w for w in re.findall(r"[a-z0-9]+", cleaned)
                  if w not in ("pvt", "ltd", "private", "limited", "company", "co", "corp", "inc")]
        marker = "".join(tokens)
        if len(marker) < 4:
            marker = re.sub(r"[^a-z0-9]", "", cleaned)
        slug = re.sub(r"[^a-z0-9]", "", url)
        return marker in slug

    for it in items:
        text = (it.get("title", "") + " " + (it.get("snippet", "") or "")).strip()
        if not text:
            continue
        m = re.search(r"\bU\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b", text)
        if m and not cin:
            cin = m.group(0)
        if not _item_is_target(it):
            continue
        # Format 1: "Directors of X are A, B, C" / "Directors : A, B, C"
        m = re.search(r"directors?\s*(?:of\s+.+?\s+)?(?:are|is|:)\s*(.+?)(?:\.\s|\.\n|$)", text, re.I)
        if m:
            for part in re.split(r",|\band\b", m.group(1)):
                c = part.strip().strip("|").strip()
                if 1 <= len(c.split()) <= 3 and _looks_like_person_name(c):
                    key = c.lower()
                    if key not in seen:
                        seen.add(key)
                        out.append({"name": c.title(), "role": "Director",
                                    "role_type": "managing_director", "source": "corporate_registry",
                                    "source_url": it.get("url", "")})
        # Format 2: signatory-table rows '| Ashwani Kumar | Director |'
        for row in re.finditer(r"\|\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*\|\s*(director|signatory|director / promoter)", text, re.I):
            c = row.group(1).title()
            key = c.lower()
            if key not in seen and _looks_like_person_name(c):
                seen.add(key)
                out.append({"name": c, "role": "Director",
                            "role_type": "managing_director", "source": "corporate_registry",
                            "source_url": it.get("url", "")})
    return out, cin


# ---------------------------------------------------------------------------
# Generic agentic search + relevance classify
# ---------------------------------------------------------------------------

def _search_and_classify(company_name: str, queries: list, category: str,
                         max_results: int = 8, threshold: int = 25) -> list:
    """Search Tavily for each query, classify relevance, return scored items.

    Uses deterministic heuristic as the primary classifier (consistent) and the
    LLM as a secondary signal. This avoids the non-determinism of relying on
    the LLM alone."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []
    tavily = TavilyClient(api_key=api_key)
    seen = set()
    collected = []
    for spec in queries:
        query = spec if isinstance(spec, str) else spec.get("query", "")
        if not query:
            continue
        kwargs = dict(query=query, search_depth="advanced",
                      max_results=max_results, include_raw_content=False)
        try:
            response = tavily.search(**kwargs)
        except Exception as e:
            print(f"Tavily search error for '{query}': {e}")
            continue
        for result in response.get("results", []):
            url = result.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            collected.append({
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("content", ""),
                "source": url.split("/")[2] if "//" in url else "",
            })
    if not collected:
        return []
    # Primary: deterministic heuristic (consistent)
    from cognitive_engine import _heuristic_lead_relevance, _fuzzy_company_match as _fuzzy
    heuristic_scores = [_heuristic_lead_relevance(company_name, r, category) for r in collected]
    # Secondary: LLM for category matching signal (best-effort)
    try:
        llm_scores = classify_lead_relevance_batch(company_name, collected, category)
    except Exception:
        llm_scores = [{}] * len(collected)
    out = []
    for item, hscore, lscore in zip(collected, heuristic_scores, llm_scores):
        h = hscore.get("relevance_score") or 0
        l = lscore.get("relevance_score") or 0
        # Combine: take the max of heuristic and LLM. A fuzzy company-name match
        # only raises the floor when the classifier also agrees the result belongs
        # to this category — otherwise directory profiles and news articles that
        # merely mention the company pass every category filter (hiring, people...).
        fuzzy = _fuzzy(company_name, item.get("title", "") + " " + item.get("snippet", ""))
        judge = hscore if lscore.get("heuristic") else lscore
        category_match = judge.get("category_match")
        if fuzzy and category_match:
            score = max(h, l, 50)
        else:
            score = max(h, l)
        item["relevance_score"] = score
        item["relevance_reason"] = lscore.get("reason", "") or hscore.get("reason", "")
        if score >= threshold:
            out.append(item)
    out.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Activity 1: Company profile (website, LinkedIn, status)
# ---------------------------------------------------------------------------

def _profile_queries(company_name: str) -> dict:
    q = company_name.strip().replace('"', '')
    return {
        "website": [
            f'"{q}" official website',
            f'"{q}" pharmaceutical company',
        ],
        "linkedin": [
            f'"{q}" linkedin company page',
        ],
        "status": [
            f'"{q}" pharmaceutical company active manufacturing',
            f'"{q}" company news 2025 2026',
        ],
    }


@activity.defn
async def search_company_profile_activity(company_name: str) -> dict:
    def _work() -> dict:
        queries = _profile_queries(company_name)
        website_items = _search_and_classify(company_name, queries["website"], "website", threshold=30)
        linkedin_items = _search_and_classify(company_name, queries["linkedin"], "linkedin", threshold=30)
        status_items = _search_and_classify(company_name, queries["status"], "status", threshold=25)
        return {
            "company_name": company_name,
            "website_candidates": website_items,
            "linkedin_candidates": linkedin_items,
            "status_candidates": status_items,
        }
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 2: Decision makers (QA head / MD / key people + email)
# ---------------------------------------------------------------------------

def _decision_maker_queries(company_name: str) -> list:
    q = company_name.strip().replace('"', '')
    return [
        f'"{q}" "QA head" OR "quality assurance head" OR "quality head" linkedin',
        f'"{q}" "QA manager" OR "quality manager" OR "quality control" linkedin',
        f'"{q}" "managing director" OR "founder" OR "CEO" linkedin',
        f'"{q}" "plant head" OR "production head" linkedin',
        f'"{q}" directors OR "key management" OR "board of directors"',
        f'"{q}" email contact',
        f'"{q}" team members quality',
    ]


@activity.defn
async def search_decision_makers_activity(company_name: str) -> dict:
    def _work() -> dict:
        items = _search_and_classify(company_name, _decision_maker_queries(company_name),
                                     "decision_maker", threshold=30, max_results=10)
        return {"company_name": company_name, "people_candidates": items}
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 2b: LLM extraction of people from raw search results
# ---------------------------------------------------------------------------

@activity.defn
async def extract_people_activity(payload: dict) -> dict:
    """Send raw search results to the LLM and return structured people.

    Looks across ALL candidate pools (people + linkedin + status) because
    decision-makers surface in many places: LinkedIn profiles appear in the
    linkedin pool, while director lists appear in corporate-registry pages
    that the status pool surfaces. Retries once if the LLM returns empty,
    since the same input may succeed on retry (transient API behavior)."""
    def _work() -> dict:
        company_name = payload["company_name"]
        combined = list(payload.get("combined_candidates", []))
        # Only keep results that plausibly reference the target company. This
        # stops the LLM from extracting people who work at a DIFFERENT firm
        # whose name merely shares a generic word ("Maple Biotech" for "R.P.
        # Biotech"), which is what polluted decision_makers before.
        combined = [
            r for r in combined
            if _fuzzy_company_match(company_name, r.get("title", "") + " " + (r.get("snippet") or ""))
        ]
        # Keep the prompt bounded: take the highest-scoring results across all
        # pools. LinkedIn profiles and corporate-registry pages both surface here.
        combined.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)
        combined = combined[:15]
        people = _llm_extract_people(company_name, combined)
        if isinstance(people, list) and people:
            print(f"LLM extracted {len(people)} people for {company_name} (from {len(combined)} results)")
            return {"company_name": company_name, "extracted_people": people}
        # Retry once on empty — transient API failures are common.
        print(f"LLM found no people for {company_name} on first try; retrying...")
        people = _llm_extract_people(company_name, combined)
        if isinstance(people, list) and people:
            print(f"LLM extracted {len(people)} people for {company_name} on retry")
        else:
            print(f"LLM found no people for {company_name} after retry; will use regex fallback")
        return {"company_name": company_name, "extracted_people": people}
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 3: Intent signals (hiring, news, QMS triggers)
# ---------------------------------------------------------------------------

def _intent_queries(company_name: str) -> dict:
    q = company_name.strip().replace('"', '')
    return {
        "hiring": [
            f'"{q}" hiring jobs careers 2025 2026',
            f'"{q}" "now hiring" OR "walk in" OR job opening',
            f'"{q}" QA quality hiring',
        ],
        "news": [
            f'"{q}" news expansion investment 2025 2026',
            f'"{q}" new facility OR plant OR manufacturing',
        ],
        "triggers": [
            f'"{q}" NSQ OR "not of standard quality" OR substandard',
            f'"{q}" warning letter OR FDA OR "regulatory action"',
            f'"{q}" recall OR CAPA OR deviation OR inspection',
            f'"{q}" "paper QMS" OR "manual records" OR documentation',
        ],
    }


@activity.defn
async def search_intent_signals_activity(company_name: str) -> dict:
    def _work() -> dict:
        queries = _intent_queries(company_name)
        return {
            "company_name": company_name,
            "hiring_candidates": _search_and_classify(company_name, queries["hiring"], "hiring", threshold=35),
            "news_candidates": _search_and_classify(company_name, queries["news"], "news", threshold=25),
            "trigger_candidates": _search_and_classify(company_name, queries["triggers"], "triggers", threshold=30),
        }
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 4: Evaluate + save (LLM structures the final lead record)
# ---------------------------------------------------------------------------

def _evaluate_lead(company_name: str, profile: dict, people: dict, signals: dict) -> dict:
    """Structure raw search results into the final lead record.

    Uses a deterministic heuristic for the structured fields (website, LinkedIn,
    decision makers, signals) and a short LLM call ONLY for the narrative summary.
    This avoids the unreliability of asking one giant prompt to do everything."""
    base = _heuristic_evaluate(company_name, profile, people, signals)
    summary = _llm_summary(company_name, profile, people, signals, base)
    if summary:
        base["activity_summary"] = summary
    return base


def _llm_summary(company_name: str, profile: dict, people: dict, signals: dict, base: dict) -> str:
    """Short LLM call for the narrative activity summary only."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return ""
    decision_makers = base.get("decision_makers", [])
    dm_names = ", ".join(d.get("name", d.get("role", "?")) for d in decision_makers[:3])
    trigger_count = len(base.get("trigger_events", []))
    hiring_count = len([s for s in base.get("intent_signals", []) if s.get("category") == "hiring"])
    prompt = f"""Company: {company_name}
Status: {base.get("company_status", "unknown")}
Key people: {dm_names or "none found"}
Active job postings: {hiring_count}
QMS trigger events (NSQ/recall/warning): {trigger_count}
Website: {base.get("website", "")}
Write 2 sentences: is this pharma company active right now, and why might they need QMS software? Plain text, no JSON."""
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You write concise sales-intelligence notes."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"Groq summary error: {e}")
        return ""


def _llm_extract_people(company_name: str, people_candidates: list) -> list:
    """Use the LLM to extract structured person records from raw search results.

    The deterministic regex heuristic is brittle on page chrome (e.g. \"Send
    Inquiry\", \"Working Days\"). The LLM reads title + snippet + url in context
    and returns only real people who actually work at the company. Returns [] if
    the LLM is unavailable so the caller can fall back to the regex heuristic."""
    if not GROQ_API_KEY.startswith("gsk_") or not people_candidates:
        return []
    items_text = ""
    for i, r in enumerate(people_candidates, 1):
        items_text += f"\n--- Result {i} ---\n"
        items_text += f"Title: {r.get('title', '')}\n"
        items_text += f"URL: {r.get('url', '')}\n"
        items_text += f"Snippet: {(r.get('snippet', '') or '')[:500]}\n"
    prompt = f"""You extract real people who work at a specific company from web search results.

Target company: {company_name}

Below are search-result snippets. Some mention employees/directors; most are page chrome, product lists, job postings, news headlines, or company descriptions.

For each REAL PERSON who works or holds a position at the target company, emit an entry. Ignore:
- company names, brand names, product names
- page navigation / UI text (\"Send Inquiry\", \"Working Days\", \"Add to Cart\")
- job postings and recruitment ads
- news headlines and article titles
- generic role labels with no person name (\"Marketing Manager\" alone is NOT a person)

{items_text}

Respond ONLY with valid JSON using this exact shape:
{{"people": [
  {{"name": "Full Name", "role": "their job title or role", "confidence": "high|medium|low", "linkedin_url": "linkedin profile url or \""}}
]}}

- name: the person's real name as written. Skip if no clear person name.
- role: their position (e.g. \"Director\", \"QA Head\", \"Assistant Account Executive\"). Use \"\" if unknown.
- confidence: high if the result explicitly states they work at the target company, medium if strongly implied, low if uncertain.
- linkedin_url: the LinkedIn profile URL if this result IS their profile, otherwise \"".
Include at most 8 people. If no real people are found, return {{"people": []}}."""
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON. Extract only real people who work at the target company."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1500,
        )
        data = json.loads(completion.choices[0].message.content)
        people = data.get("people", [])
        if not isinstance(people, list):
            return []
        return people
    except Exception as e:
        print(f"Groq people extraction error: {e}")
        return []


def _heuristic_evaluate(company_name: str, profile: dict, people: dict, signals: dict) -> dict:
    website = _pick_best_website(profile.get("website_candidates", [])[:8], company_name)
    linkedin = _pick_linkedin(profile.get("linkedin_candidates", [])[:6], company_name)

    status_items = profile.get("status_candidates", [])
    company_status = "active" if status_items else "unknown"

    company_tokens = _company_tokens(company_name)
    company_clean = clean_company_name(PAREN.sub("", company_name or "")).lower()
    generic = {"pharmaceutical", "pharmaceuticals", "pharma", "private", "limited", "pvt", "ltd"}
    distinctive_tokens = company_tokens - generic

    extracted = people.get("extracted_people") or []
    # LLM output is not provenance-safe: a search snippet can mention the
    # target company while naming an employee of a different company.  Keep a
    # name only when it occurs in a source result that independently identifies
    # the target company.  This is a final defense in depth after the input
    # filtering in extract_people_activity.
    source_candidates = people.get("_combined_candidates", [])
    validated_extracted = []
    for p in extracted:
        pname = (p.get("name") or "").strip()
        if not pname:
            continue
        pname_re = re.compile(r"(?<![A-Za-z])" + re.escape(pname) + r"(?![A-Za-z])", re.IGNORECASE)
        def _supports_person(r):
            title = r.get("title", "") or ""
            snippet = r.get("snippet", "") or ""
            if not pname_re.search(title + " " + snippet):
                return False
            # A LinkedIn profile's title is the profile's own employer signal;
            # snippets often contain "similar people" from unrelated firms.
            if "linkedin.com/in/" in (r.get("url", "") or "").lower():
                return _fuzzy_company_match(company_name, title)
            return _fuzzy_company_match(company_name, title + " " + snippet)
        supporting = next((r for r in source_candidates if _supports_person(r)), None)
        if supporting:
            validated_extracted.append({**p, "source": p.get("source") or "web_search",
                                        "source_url": p.get("source_url") or supporting.get("url", "")})
    extracted = validated_extracted
    decision_makers = []
    seen_names = set()

    # New algo for names: corporate-registry (MCA) directors are the most
    # authoritative source, so always fold them in first regardless of whether
    # the LLM produced anything.
    registry_directors = people.get("_registry_directors") or []
    for d in registry_directors:
        name = (d.get("name") or "").strip()
        if not name or len(name) < 3:
            continue
        name_lower = name.lower()
        if name_lower in seen_names:
            continue
        if not _looks_like_person_name(name):
            continue
        seen_names.add(name_lower)
        decision_makers.append({
            "name": name,
            "role": "Director",
            "role_type": "managing_director",
            "linkedin_url": "",
            "email": "",
            "confidence": "high",
            "source": d.get("source", "corporate_registry"),
            "source_url": d.get("source_url", ""),
        })

    # Website team members (scraped) — second authoritative source.
    for name in (people.get("_website_people") or []):
        name = (name or "").strip()
        if not name or len(name) < 3:
            continue
        name_lower = name.lower()
        if name_lower in seen_names:
            continue
        if not _looks_like_person_name(name):
            continue
        seen_names.add(name_lower)
        decision_makers.append({
            "name": name,
            "role": "Team Member",
            "role_type": "other",
            "linkedin_url": "",
            "email": "",
            "confidence": "medium",
            "source": "company_website",
            "source_url": people.get("_website_url", ""),
        })
        if len(decision_makers) >= 8:
            break

    if extracted:
        # Primary path: trust the LLM-extracted people. Just normalize and
        # de-duplicate; the LLM already filtered out page chrome and non-people.
        for p in extracted:
            name = (p.get("name") or "").strip()
            if not name or len(name) < 3:
                continue
            name_lower = name.lower()
            if name_lower in seen_names:
                continue
            if not _looks_like_person_name(name):
                continue
            if name.isupper():
                name = name.title()
            seen_names.add(name_lower)
            role = (p.get("role") or "").strip()
            confidence = p.get("confidence") or "low"
            if confidence not in ("high", "medium", "low"):
                confidence = "low"
            ln = (p.get("linkedin_url") or "").strip()
            role_type = _role_type_from_text(role)
            decision_makers.append({
                "name": name,
                "role": role or "Employee",
                "role_type": role_type,
                "linkedin_url": ln,
                "email": "",
                "confidence": confidence,
                "source": p.get("source", "web_search"),
                "source_url": p.get("source_url", ""),
            })
            if len(decision_makers) >= 8:
                break
    else:
        # Fallback 1: regex heuristic over the combined pool (LLM unavailable).
        # Handles LinkedIn profiles (name from title) and corporate-registry
        # director lists (ALL CAPS / "Directors are X, Y, Z") reliably.
        fallback = _regex_extract_people(company_name, people.get("_combined_candidates", []))
        for rec in fallback:
            decision_makers.append(rec)
            seen_names.add(rec["name"].lower())
        if not decision_makers:
            # Fallback 2 (last resort): per-item regex over people_candidates.
            people_items = sorted(
                people.get("people_candidates", []),
                key=lambda x: x.get("relevance_score") or 0, reverse=True,
            )
            for it in people_items:
                title = it.get("title", "")
                snippet = it.get("snippet", "")
                hay = (title + " " + snippet).lower()
                candidate_names = _extract_person_names(title)
                candidate_names += _extract_person_names(snippet)
                if not candidate_names:
                    raw_name = title.split("|")[0].split("-")[0].split("·")[0].strip()
                    name = re.sub(r"\s+", " ", raw_name).strip()
                    if name and len(name) >= 3 and _looks_like_person_name(name):
                        candidate_names = [name]
                if not candidate_names:
                    continue
                works_here = _fuzzy_company_match(company_name, title + " " + snippet)
                if not works_here:
                    continue
                role_type = _role_type_from_text(title + " " + snippet)
                linkedin_url = it.get("url", "") if "linkedin.com" in it.get("url", "") else ""
                for name in candidate_names:
                    name_lower = name.lower()
                    if name_lower in seen_names:
                        continue
                    if re.match(r"^\d", name) or "jobs in" in name_lower or "hiring" == name_lower:
                        continue
                    if any(t in name_lower for t in company_tokens if len(t) >= 6) and len(name.split()) >= 4:
                        continue
                    if any(name_lower.startswith(w) for w in ("the ", "our ", "we ", "this ")):
                        continue
                    decision_makers.append({
                        "name": name,
                        "role": title.strip(),
                        "role_type": role_type,
                        "linkedin_url": linkedin_url,
                        "email": "",
                        "confidence": "low",
                    })
                    seen_names.add(name_lower)
                if len(decision_makers) >= 6:
                    break

    intent_signals = []
    for it in signals.get("hiring_candidates", [])[:4]:
        intent_signals.append({**it, "category": "hiring"})
    for it in signals.get("news_candidates", [])[:3]:
        intent_signals.append({**it, "category": "expansion"})
    intent_signals.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)

    trigger_events = []
    for it in signals.get("trigger_candidates", [])[:5]:
        hay = (it.get("title", "") + " " + it.get("snippet", "")).lower()
        cat = "regulatory_action"
        if "nsq" in hay or "not of standard" in hay or "substandard" in hay:
            cat = "nsq_alert"
        elif "warning" in hay or "fda" in hay:
            cat = "warning_letter"
        elif "recall" in hay:
            cat = "recall"
        elif "paper" in hay or "manual" in hay or "documentation" in hay:
            cat = "documentation_issue"
        trigger_events.append({**it, "category": cat})

    return {
        "website": website,
        "linkedin_url": linkedin,
        "company_status": company_status,
        "decision_makers": decision_makers,
        "intent_signals": intent_signals[:6],
        "trigger_events": trigger_events[:5],
        "activity_summary": "",
    }


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _extract_email(text: str, domain_hint: str = "") -> str:
    """Best-effort email extraction from visible text."""
    if not text:
        return ""
    matches = _EMAIL_RE.findall(text)
    for m in matches:
        if domain_hint and domain_hint in m:
            return m
    # Return first non-generic match
    for m in matches:
        if not m.lower().startswith(("noreply", "no-reply", "donotreply", "support@", "info@")):
            return m
    return matches[0] if matches else ""


@activity.defn
async def evaluate_and_save_lead_activity(payload: dict) -> dict:
    """Evaluate all search results via LLM and persist the lead record."""
    def _work() -> dict:
        company_key = payload["company_key"]
        company_name = payload["company_name"]
        profile = payload.get("profile", {})
        people = payload.get("people", {})
        signals = payload.get("signals", {})
        website_data = payload.get("website_data", {}) or {}
        registry = payload.get("registry", {}) or {}

        evaluated = _evaluate_lead(company_name, profile, people, signals)

        for dm in evaluated.get("decision_makers", []):
            dm.setdefault("confidence", "low")
        evaluated["decision_makers"] = sorted(
            evaluated.get("decision_makers", []),
            key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d.get("confidence", "low"), 2),
        )

        # Best-effort email extraction from people snippets
        for dm in evaluated.get("decision_makers", []):
            if not dm.get("email"):
                dm["email"] = ""

        website = evaluated.get("website", "")
        domain_hint = website.split("/")[2] if "//" in website else ""
        for dm in evaluated.get("decision_makers", []):
            if dm.get("email"):
                continue
            for it in people.get("people_candidates", []):
                if it.get("url") == dm.get("linkedin_url") or it.get("title", "") in dm.get("role", ""):
                    email = _extract_email(it.get("snippet", ""), domain_hint)
                    if email:
                        dm["email"] = email
                        break

        db = SessionLocal()
        try:
            row = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
            if row is None:
                row = CompanyLead(company_key=company_key)
                db.add(row)
            row.company_name = company_name
            row.website = website
            row.linkedin_url = evaluated.get("linkedin_url", "")
            row.company_status = evaluated.get("company_status", "unknown")
            row.decision_makers = evaluated.get("decision_makers", [])
            row.intent_signals = evaluated.get("intent_signals", [])
            row.trigger_events = evaluated.get("trigger_events", [])
            row.activity_summary = evaluated.get("activity_summary", "")
            row.hiring = [s for s in evaluated.get("intent_signals", []) if s.get("category") == "hiring"]
            row.hiring_news = [s for s in evaluated.get("intent_signals", []) if s.get("category") != "hiring"]
            row.summary = {
                "searched_at": datetime.utcnow().isoformat(),
                "profile_candidates": len(profile.get("website_candidates", [])) + len(profile.get("linkedin_candidates", [])),
                "people_candidates": len(people.get("people_candidates", [])),
                "signal_candidates": len(signals.get("hiring_candidates", [])) + len(signals.get("news_candidates", [])) + len(signals.get("trigger_candidates", [])),
            }
            row.scraped_data = {
                "pages": website_data.get("pages", []),
                "emails": website_data.get("emails", []),
                "phones": website_data.get("phones", []),
                "address": website_data.get("address", ""),
                "financials": website_data.get("financials", []),
                "team_members": website_data.get("team_members", []),
            }
            row.corporate_registry = {
                "cin": registry.get("cin", ""),
                "directors": registry.get("directors", []),
                "registry_items": registry.get("registry_items", [])[:6],
            }
            row.status = "completed"
            row.error = ""
            row.fetched_at = datetime.utcnow()
            db.commit()
            return {"company_key": company_key, "status": "completed"}
        except Exception as e:
            db.rollback()
            print(f"evaluate_and_save_lead_activity error: {e}")
            return {"company_key": company_key, "status": "failed", "error": str(e)[:500]}
        finally:
            db.close()

    return await asyncio.to_thread(_work)


@activity.defn
def mark_lead_failed_activity(company_key: str, error: str) -> dict:
    db = SessionLocal()
    try:
        row = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
        if row is not None:
            row.status = "failed"
            row.error = (error or "")[:500]
            db.commit()
        return {"company_key": company_key, "status": "failed"}
    except Exception as e:
        db.rollback()
        print(f"mark_lead_failed_activity error: {e}")
        return {"company_key": company_key, "status": "failed"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class LeadResearchWorkflow:
    def __init__(self):
        self._status = "starting"
        self._company_name = ""

    @workflow.query
    def progress(self) -> dict:
        return {"status": self._status, "company_name": self._company_name}

    @workflow.run
    async def run(self, company_key: str, company_name: str) -> dict:
        self._company_name = company_name
        try:
            self._status = "searching_profile"
            profile = await workflow.execute_activity(
                search_company_profile_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "searching_decision_makers"
            people = await workflow.execute_activity(
                search_decision_makers_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "scraping_company_website"
            website = _pick_best_website(profile.get("website_candidates", [])[:8], company_name)
            website_data = await workflow.execute_activity(
                scrape_company_website_activity,
                args=[{"company_name": company_name, "website": website}],
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "searching_corporate_registry"
            registry = await workflow.execute_activity(
                search_corporate_registry_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "extracting_people"
            combined_candidates = (
                people.get("people_candidates", [])
                + profile.get("linkedin_candidates", [])
                + profile.get("status_candidates", [])
            )
            people_extracted = await workflow.execute_activity(
                extract_people_activity,
                args=[{
                    "company_name": company_name,
                    "combined_candidates": combined_candidates,
                }],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            people = {**people, **people_extracted,
                      "_combined_candidates": combined_candidates,
                      "_registry_directors": registry.get("directors", []),
                      "_website_people": website_data.get("team_members", []),
                      "_website_url": website_data.get("website", "") or website}

            self._status = "searching_intent_signals"
            signals = await workflow.execute_activity(
                search_intent_signals_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "evaluating_and_saving"
            saved = await workflow.execute_activity(
                evaluate_and_save_lead_activity,
                args=[{
                    "company_key": company_key,
                    "company_name": company_name,
                    "profile": profile,
                    "people": people,
                    "signals": signals,
                    "website_data": website_data,
                    "registry": registry,
                }],
                start_to_close_timeout=timedelta(minutes=8),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "completed"
            return saved
        except Exception as e:
            self._status = "failed"
            try:
                await workflow.execute_activity(
                    mark_lead_failed_activity,
                    args=[company_key, str(e)],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except Exception as mark_err:
                print(f"mark_lead_failed_activity workflow error: {mark_err}")
            raise
