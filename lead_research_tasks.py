import os
import asyncio
import json
import re
from datetime import timedelta, datetime
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from tavily import TavilyClient
    from db_setup import SessionLocal, CompanyLead
    from company_names import clean_company_name, PAREN
    from cognitive_engine import client as groq_client, GROQ_API_KEY

_LINKEDIN_DOMAINS = ["linkedin.com"]

def _build_queries(company_name: str) -> list:
    """Deterministic, category-tagged search queries for one company."""
    name = company_name.strip() or "company"
    q = name.replace('"', '').strip()
    return [
        {"category": "website", "query": f'"{q}" official website'},
        {"category": "website", "query": f'"{q}" company pharmaceutical'},
        {"category": "website", "query": f'"{q}" official site'},
        {"category": "linkedin", "query": f'"{q}" linkedin company page'},
        {"category": "linkedin", "query": f'"{q}" linkedin'},
        {"category": "hiring", "query": f'"{q}" careers job openings'},
        {"category": "hiring", "query": f'"{q}" hiring new jobs'},
        {"category": "hiring", "query": f'"{q}" jobs openings'},
        {"category": "hiring", "query": f'"{q}" hiring news growth headcount'},
    ]

def _company_tokens(company_name: str) -> set:
    tokens = set()
    cleaned = clean_company_name(PAREN.sub("", company_name or ""))
    for w in re.findall(r"[a-z0-9]+", cleaned.lower()):
        if len(w) >= 4:
            tokens.add(w)
    return tokens

def _is_relevant(company_name: str, title: str, snippet: str) -> bool:
    tokens = _company_tokens(company_name)
    if not tokens:
        return True
    hay = (title + " " + snippet).lower()
    return any(t in hay for t in tokens)

_DIRECTORY_HOSTS = (
    "indiamart.com", "justdial", "sulekha", "tradeindia", "yellowpages",
    "exportersindia", "yelu", "indiabizclub", "zaubacorp.com", "tofler",
    "tracxn.com", "crediwatch", "registerkaro", "vakilsearch", "ebizprise",
    "emis.com", "crunchbase", "owler", "bizapedia", "opencorporates",
    "thecompanycheck", "quickcompany", "corporationwiki", "buzzfile",
    "moneycontrol", "bloomberg", "dnb.com", "kompass",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "linkedin.com", "glassdoor.com", "indeed.com", "naukri.com",
    "timesjobs.com", "zoominfo",
)

_STRONG_STOPWORDS = {
    "pvt", "ltd", "limited", "private", "inc", "company", "co", "corp",
    "corporation", "group", "industries", "industry", "enterprises",
    "enterprise", "solutions", "services", "international", "global",
    "india", "biotech", "biotechnology", "pharma", "pharmaceutical",
    "pharmaceuticals", "laboratories", "laboratory", "labs", "life",
    "sciences", "science", "health", "healthcare", "medical", "medicals",
    "pharmacy", "drugs", "drug", "chemical", "chemicals", "technologies",
    "technology", "systems", "products",
}

def _strong_tokens(company_name: str) -> set:
    return {t for t in _company_tokens(company_name) if t not in _STRONG_STOPWORDS}

_NEWS_PATH_RE = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/")
_DIRECTORY_PATH_SEGS = ("directory", "listing", "company-profile", "company_profile", "profile", "company/")
_CIN_RE = re.compile(r"[A-Z0-9]{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}")

def _is_clean_host(url: str) -> bool:
    host = url.split("/")[2] if "//" in url else ""
    if not host or any(d in host.lower() for d in _DIRECTORY_HOSTS):
        return False
    if _NEWS_PATH_RE.search(url):
        return False
    path = ""
    if "//" in url:
        after_host = url.split("//", 1)[1]
        if "/" in after_host:
            path = after_host.split("/", 1)[1]
    if any(s in path.lower() for s in _DIRECTORY_PATH_SEGS):
        return False
    if _CIN_RE.search(path.upper()):
        return False
    return True

def _website_plausible(url: str, company_name: str) -> bool:
    """A candidate website must be a clean host AND match the company name:
    a distinctive token when available, otherwise a meaningful shared
    substring (rejects wrong-company directory pages)."""
    if not _is_clean_host(url):
        return False
    strong = _strong_tokens(company_name)
    if strong:
        return any(t in url.lower() for t in strong)
    clean = re.sub(r"[^a-z0-9]", "", company_name.lower())
    return _meaningful_shared(_lcs(_url_key(url), clean))

def _pick_best_website(items: list, company_name: str) -> str:
    """Prefer a non-social, non-directory official-looking URL. Directory and
    aggregator pages are never returned as the company website. For generic
    names (no distinctive tokens) a URL is only accepted if it shares a
    meaningful substring with the company name, so wrong-company directory
    pages are not guessed."""
    strong = _strong_tokens(company_name)
    for it in items:
        url = it.get("url", "")
        if _website_plausible(url, company_name):
            return url
    if strong:
        for it in items:
            url = it.get("url", "")
            if _is_clean_host(url):
                return url
    return ""


def _linkedin_slug(url: str) -> str:
    low = url.lower().rstrip("/")
    return low.split("linkedin.com/", 1)[-1] if "linkedin.com" in low else ""

def _lcs(a: str, b: str) -> str:
    """Longest common substring between two short strings."""
    if not a or not b:
        return ""
    best = ""
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > len(best):
                best = a[i:i + k]
    return best

def _meaningful_shared(shared: str) -> bool:
    """True if the shared substring is long enough and retains non-generic
    content after stripping industry stopwords (e.g. "biotechpvtltd" -> "")."""
    if len(shared) < 6:
        return False
    residue = shared
    for sw in _STRONG_STOPWORDS:
        residue = residue.replace(sw, "")
    return len(residue) >= 2

def _url_key(url: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (url.split("//", 1)[-1] if "//" in url else url).lower())

def _pick_linkedin(items: list, company_name: str) -> str:
    """Pick the company's LinkedIn page from search results. Prefer a URL whose
    slug carries a distinctive (non-generic) company token; otherwise fall back
    to the longest meaningful shared-substring with the company name. Never
    guess a random /company/ or employee page."""
    strong = _strong_tokens(company_name)
    clean = re.sub(r"[^a-z0-9]", "", company_name.lower())

    scored = []
    for it in items:
        url = it.get("url", "")
        slug = _linkedin_slug(url)
        if not slug or any(m in slug for m in ("/posts/", "/jobs/", "/feed")):
            continue
        score = 0
        if strong:
            score += sum(2 for t in strong if t in slug)
        shared = _lcs(slug, clean)
        if _meaningful_shared(shared):
            score += len(shared)
        if score > 0:
            scored.append((score, slug.startswith("company/"), url))

    if scored:
        scored.sort(key=lambda x: (-x[0], not x[1]))
        return scored[0][2]
    return ""


_JOB_HOST_HINTS = (
    "indeed", "workindia", "naukri", "timesjobs", "glassdoor", "monster",
    "linkedin.com/jobs", "shine", "apna.co", "hire", "talent", "jobs",
    "careers", "career",
)


def _fallback_hirings(items: list) -> list:
    out = []
    for it in items[:10]:
        url = it.get("url", "").lower()
        if any(h in url for h in _JOB_HOST_HINTS):
            out.append({
                "title": (it.get("title", "") or "").strip() or "Job opening",
                "location": "",
                "posted": "",
                "url": it.get("url", ""),
            })
    return out[:10]


def _fallback_hiring_news(items: list) -> list:
    out = []
    for it in items[:8]:
        url = it.get("url", "").lower()
        if any(h in url for h in _JOB_HOST_HINTS):
            continue
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "source": it.get("source", ""),
            "snippet": it.get("snippet", ""),
            "date": "",
        })
    return out[:8]

@activity.defn
async def search_lead_web_activity(company_name: str) -> dict:
    """Search Tavily for website / LinkedIn / hiring info on a company and
    return category-tagged, relevance-gated results. Blocking API calls run in
    a thread so they cannot freeze the worker event loop."""

    def _work() -> dict:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            print("Warning: TAVILY_API_KEY not set")
            return {"company_name": company_name, "website": [], "linkedin": [], "hiring": []}

        tavily = TavilyClient(api_key=api_key)
        by_cat = {"website": [], "linkedin": [], "hiring": []}
        seen = set()

        for spec in _build_queries(company_name):
            cat = spec["category"]
            kwargs = dict(
                query=spec["query"],
                search_depth="advanced",
                max_results=8,
                include_raw_content=False,
            )
            if cat == "linkedin":
                kwargs["include_domains"] = _LINKEDIN_DOMAINS
            try:
                response = tavily.search(**kwargs)
            except Exception as e:
                print(f"Tavily search error for '{spec['query']}': {e}")
                continue

            for result in response.get("results", []):
                url = result.get("url")
                if not url or url in seen:
                    continue
                title = result.get("title", "")
                snippet = result.get("content", "")
                if not _is_relevant(company_name, title, snippet):
                    continue
                seen.add(url)
                item = {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": url.split("/")[2] if "//" in url else "",
                }
                by_cat[cat].append(item)

        return {"company_name": company_name, **by_cat}

    return await asyncio.to_thread(_work)

def _groq_extract(company_name: str, data: dict) -> dict:
    """Ask Groq to structure website/LinkedIn/hiring from the raw search."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return {}

    compact = {k: v[:8] for k, v in data.items()}
    prompt = f"""
    You are a sales-intelligence researcher. From the raw web search results for
    company "{company_name}", extract:
    - website: the company's official website URL. Prefer a real corporate
      domain; SKIP aggregators/directories (indiamart, zaubacorp, tofler,
      traxn, crediwatch, facebook, social profiles). Empty string only if no
      plausible official site exists.
    - linkedin_url: the company's own LinkedIn page. Note: small companies
      often use an "/in/" profile-style URL whose slug contains the company
      name (e.g. /in/saintlife-pharamceuticals-ltd-587961226). Prefer a
      LinkedIn URL whose slug matches the company name over random employee
      profiles. Empty string only if no company LinkedIn page exists.
    - hirings: a list of current job openings {{title, location, posted, url}}
      found in the results (job boards like indeed/workindia, LinkedIn jobs,
      or the company careers page). Use the snippet date like "3 weeks ago"
      for posted when available.
    - hiring_news: a list of recent hiring/expansion news mentions
      {{title, url, source, snippet, date}} (headcount growth, new facility,
      expansions, key hires). Include ONLY genuinely hiring-related items.
    - hiring_headline: a one-line summary of the company's hiring momentum, or
      "" if unclear.

    Results to analyze:
    {json.dumps(compact, indent=1)[:12000]}

    Respond ONLY with valid JSON:
    {{"website": "", "linkedin_url": "", "hirings": [], "hiring_news": [],
      "hiring_headline": ""}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=1200,
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        print(f"Groq lead extraction error: {e}")
        return {}

def _tavily_extract(urls: list) -> dict:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key or not urls:
        return {}
    try:
        tavily = TavilyClient(api_key=api_key)
        resp = tavily.extract(urls=urls)
        out = {}
        for r in resp.get("results") or []:
            url = r.get("url")
            raw = (r.get("raw_content") or "").strip()
            if url and raw:
                out[url] = raw[:6000]
        return out
    except Exception as e:
        print(f"Tavily extract error: {e}")
        return {}

@activity.defn
async def extract_lead_details_activity(data: dict) -> dict:
    """Extract top pages (Tavily extract) and let Groq structure the final
    lead record: website, LinkedIn URL, job postings and hiring news."""

    def _work() -> dict:
        company_name = data.get("company_name", "")
        cats = data.get("results", {})

        extract_urls = []
        for cat in ("website", "linkedin", "hiring"):
            for it in cats.get(cat, [])[:2]:
                extract_urls.append(it["url"])
        extract_urls = list(dict.fromkeys(extract_urls))[:6]

        page_text = _tavily_extract(extract_urls)
        enriched = {}
        for cat in ("website", "linkedin", "hiring"):
            enriched[cat] = []
            for it in cats.get(cat, []) or []:
                row = dict(it)
                if it["url"] in page_text:
                    row["content"] = page_text[it["url"]][:3000]
                enriched[cat].append(row)

        structured = _groq_extract(company_name, enriched)
        if structured:
            website = str(structured.get("website", "") or "").strip()
            linkedin = str(structured.get("linkedin_url", "") or "").strip()
            hirings = structured.get("hirings") or []
            hiring_news = structured.get("hiring_news") or []
            headline = str(structured.get("hiring_headline", "") or "").strip()
        else:
            website, linkedin = "", ""
            hirings, hiring_news, headline = [], [], ""

        # Groq is a hint, never a lossy gate: fill any empty field from the
        # deterministic pickers so found URLs are never dropped.
        if not website or not _website_plausible(website, company_name):
            website = _pick_best_website(cats.get("website", []), company_name)
        if not linkedin or "/posts/" in linkedin.lower():
            candidate = _pick_linkedin(cats.get("linkedin", []), company_name)
            if candidate:
                linkedin = candidate
        if not hirings:
            hirings = _fallback_hirings(cats.get("hiring", []))
        if not hiring_news:
            hiring_news = _fallback_hiring_news(cats.get("hiring", []))

        return {
            "company_name": company_name,
            "website": website,
            "linkedin_url": linkedin,
            "hiring": [dict(h) for h in hirings][:10],
            "hiring_news": [dict(h) for h in hiring_news][:8],
            "hiring_headline": headline,
            "summary": {"searched_at": datetime.utcnow().isoformat()},
        }

    return await asyncio.to_thread(_work)

@activity.defn
def save_lead_research_activity(data: dict) -> dict:
    """Upsert the researched lead record. Plain def: blocking DB work must run
    in the worker's thread pool, not on the event loop."""
    db = SessionLocal()
    try:
        row = db.query(CompanyLead).filter(CompanyLead.company_key == data["company_key"]).first()
        if row is None:
            row = CompanyLead(company_key=data["company_key"])
            db.add(row)
        row.company_name = data.get("company_name", "") or row.company_name
        row.website = data.get("website", "") or row.website
        row.linkedin_url = data.get("linkedin_url", "") or row.linkedin_url
        row.hiring = data.get("hiring", []) or []
        row.hiring_news = data.get("hiring_news", []) or []
        row.summary = data.get("summary", {}) or {}
        row.status = "completed"
        row.error = ""
        row.fetched_at = datetime.utcnow()
        db.commit()
        return {"company_key": data["company_key"], "status": "completed"}
    except Exception as e:
        db.rollback()
        print(f"save_lead_research_activity error: {e}")
        return {"company_key": data.get("company_key", ""), "status": "failed"}
    finally:
        db.close()

@activity.defn
def mark_lead_failed_activity(company_key: str, error: str) -> dict:
    """Mark a lead's workflow run as failed so the UI stops showing
    "Researching..." forever after an unrecoverable error."""
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
            self._status = "searching"
            results = await workflow.execute_activity(
                search_lead_web_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

            self._status = "extracting"
            data = await workflow.execute_activity(
                extract_lead_details_activity,
                args=[{"company_name": company_name, "results": results}],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

            self._status = "saving"
            saved = await workflow.execute_activity(
                save_lead_research_activity,
                args=[{"company_key": company_key, **data}],
                start_to_close_timeout=timedelta(minutes=2),
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
