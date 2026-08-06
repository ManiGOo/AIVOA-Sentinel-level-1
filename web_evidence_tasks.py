import os
import asyncio
import re
from datetime import timedelta, datetime
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import json
    from tavily import TavilyClient
    from sqlalchemy import func
    from db_setup import SessionLocal, WebEvidence, RegulatoryEvent
    from cognitive_engine import generate_search_queries, classify_web_evidence
    from temporal_tasks import mfr_key
    from company_names import clean_company_name, PAREN

@activity.defn
async def generate_queries_activity(event_id: str) -> list[str]:
    """Generate search queries for one event. The blocking DB query + Groq LLM
    call run in a thread so they cannot freeze the worker event loop."""

    def _work() -> list[str]:
        db = SessionLocal()
        try:
            event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
            if not event or not event.raw_details:
                return []
            return generate_search_queries(event.raw_details)
        finally:
            db.close()

    return await asyncio.to_thread(_work)

_REGULATORY_KEYWORDS = (
    "recall", "recalled", "violation", "gmp", "cdsco", "warning letter",
    "penalty", "banned", "substandard", "not of standard", "adulterat",
    "seiz", "suspend", "revoke", "regulatory", "investigation", "prosecut",
    "warning", "compliance", "audit", "deficiency", "market withdrawal",
    "stop production", "notice",
)

_REGULATORY_HOST_HINTS = (
    ".gov", "ema.europa.eu", "who.int", "nhs.uk", "europa.eu", "cdsco", ".ac.in",
)

_COMMON_DRUG_WORDS = {
    "injection", "injections", "tablet", "tablets", "capsule", "capsules",
    "syrup", "powder", "oral", "dosage", "form", "solution", "suspension",
    "drop", "drops", "cream", "ointment", "gel", "mg", "gm", "liquid",
    "i.p.", "ip", "bp", "usp", "uspx", "injectable",
}

_LEGAL_WORDS_SEARCH = {
    "ltd", "limited", "pvt", "private", "co", "company", "inc", "llp", "llc",
    "corp", "corporation", "group", "labs", "lab", "biosciences", "industries",
    "industriesltd",
}


def _search_tokens(mfr: str, drug_name: str, reason: str = "") -> dict:
    """Distinctive manufacturer + product tokens for relevance gating."""
    mfr_tokens = set()
    cleaned = clean_company_name(PAREN.sub("", mfr or ""))
    for w in re.findall(r"[a-z0-9]+", cleaned.lower()):
        if len(w) >= 3 and w not in _LEGAL_WORDS_SEARCH:
            mfr_tokens.add(w)

    product_terms = set()
    text = " ".join([drug_name or "", reason or ""])
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) >= 4 and w not in _COMMON_DRUG_WORDS:
            product_terms.add(w)
    # Handle the common amoxycillin/amoxicillin spelling split.
    if any(t.startswith("amox") for t in product_terms):
        product_terms.add("amox")
    if any("clavul" in t for t in product_terms):
        product_terms.add("clavul")
    return {"mfr": mfr_tokens, "product": product_terms}


def _search_score(item: dict, tokens: dict) -> int:
    title_snippet = (item["title"] + " " + item["snippet"]).lower()
    has_mfr = any(t in title_snippet for t in tokens["mfr"])
    has_product = any(t in title_snippet for t in tokens["product"])
    has_reg = any(k in title_snippet for k in _REGULATORY_KEYWORDS)
    reg_host = any(h in item["source"].lower() for h in _REGULATORY_HOST_HINTS)

    if has_mfr:
        return 5 + (2 if has_product else 0) + (1 if has_reg else 0) + (1 if reg_host else 0)
    if has_product and has_reg:
        return 3 + (1 if reg_host else 0)
    if has_product and reg_host:
        return 2
    return 0


@activity.defn
async def search_web_for_queries(event_id: str, queries: list[str]) -> list[dict]:
    """Search Tavily for each query and return only results that plausibly
    relate to this manufacturer/product. Blocking DB + API calls run in a
    thread so they cannot freeze the worker event loop."""

    def _work() -> list[dict]:
        db = SessionLocal()
        try:
            event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
            rd = (event.raw_details or {}) if event else {}
        finally:
            db.close()

        tokens = _search_tokens(
            rd.get("manufacturer", ""),
            rd.get("drug_name", ""),
            rd.get("reason", ""),
        )

        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            print("Warning: TAVILY_API_KEY not set")
            return []

        tavily_client = TavilyClient(api_key=api_key)
        all_results = []
        seen_urls = set()

        for query in queries:
            try:
                response = tavily_client.search(
                    query=query,
                    search_depth="advanced",
                    max_results=8,
                    include_raw_content=False,
                )
            except Exception as e:
                print(f"Tavily search error for '{query}': {e}")
                continue

            for result in response.get("results", []):
                url = result.get("url")
                if not url or url in seen_urls:
                    continue
                item = {
                    "query": query,
                    "title": result.get("title", ""),
                    "url": url,
                    "snippet": result.get("content", ""),
                    "source": url.split("/")[2] if "//" in url else "",
                }
                score = _search_score(item, tokens)
                if score <= 0:
                    continue
                item["score"] = score
                seen_urls.add(url)
                all_results.append(item)

        all_results.sort(key=lambda r: r["score"], reverse=True)
        return all_results

    return await asyncio.to_thread(_work)

@activity.defn
async def fetch_and_classify_articles(event_id: str, search_results: list[dict]) -> dict:
    from playwright.async_api import async_playwright
    from readability import Document
    import html2text

    db = SessionLocal()
    try:
        event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
        if not event:
            return {"status": "error", "message": "Event not found"}
            
        mfr = (event.raw_details or {}).get("manufacturer", "")
        key = mfr_key(mfr)
        record_details = event.raw_details or {}

        api_key = os.getenv("TAVILY_API_KEY")
        tavily_client = TavilyClient(api_key=api_key) if api_key else None

        async def _fetch_pdf(url: str) -> tuple:
            """PDFs cannot be parsed by readability; Tavily extract returns
            readable text for them."""
            if not tavily_client:
                return "", "failed"
            try:
                resp = await asyncio.to_thread(tavily_client.extract, urls=[url])
                for r in resp.get("results") or []:
                    content = (r.get("raw_content") or "").strip()
                    if content:
                        return content, "fetched"
                return "", "failed"
            except Exception as e:
                print(f"Tavily extract failed for {url}: {e}")
                return "", "failed"

        need_browser = any(not u["url"].lower().endswith(".pdf") for u in search_results)
        processed = 0

        async with async_playwright() as p:
            browser = None
            page = None
            if need_browser:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

            for item in search_results:
                url = item["url"]
                fetch_status = "failed"
                full_text = ""
                classification = {}
                relevance_score = 0

                try:
                    if url.lower().endswith(".pdf"):
                        full_text, fetch_status = await _fetch_pdf(url)
                    elif page is not None:
                        response = await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                        if response and response.ok:
                            html_content = await page.content()
                            doc = Document(html_content)
                            h = html2text.HTML2Text()
                            h.ignore_links = True
                            h.ignore_images = True
                            full_text = h.handle(doc.summary())
                            fetch_status = "fetched"
                        else:
                            fetch_status = "blocked"
                    else:
                        fetch_status = "failed"

                    if fetch_status == "fetched" and full_text:
                        classification = await asyncio.to_thread(
                            classify_web_evidence, full_text, record_details
                        )
                        relevance_score = classification.get("relevance_score", 0)
                except Exception as e:
                    print(f"Failed to fetch {url}: {e}")
                    fetch_status = "failed"

                # Save to DB
                existing = db.query(WebEvidence).filter(
                    WebEvidence.event_id == event_id,
                    WebEvidence.url == url
                ).first()
                
                if not existing:
                    db.add(WebEvidence(
                        event_id=event_id,
                        mfr_key=key,
                        query=item["query"],
                        title=item["title"],
                        url=url,
                        source=item["source"],
                        snippet=item["snippet"],
                        full_text=full_text,
                        classification=classification,
                        relevance_score=relevance_score,
                        fetch_status=fetch_status,
                        fetched_at=datetime.utcnow()
                    ))
                    processed += 1
                    
                # Small delay to be polite
                await asyncio.sleep(2)

            if browser is not None:
                await browser.close()

            db.commit()
            return {"status": "success", "processed": processed}
    finally:
        db.close()
        

@workflow.defn
class WebEvidenceWorkflow:
    def __init__(self):
        self._status = "starting"
        self._queries = []
        self._results_count = 0
        
    @workflow.query
    def progress(self) -> dict:
        return {
            "status": self._status,
            "queries": self._queries,
            "results_found": self._results_count
        }

    @workflow.run
    async def run(self, event_id: str) -> dict:
        self._status = "generating_queries"
        
        queries = await workflow.execute_activity(
            generate_queries_activity,
            args=[event_id],
            start_to_close_timeout=timedelta(minutes=2)
        )
        self._queries = queries
        
        if not queries:
            self._status = "failed - no queries"
            return {"error": "Could not generate queries"}
            
        self._status = "searching_web"
        
        search_results = await workflow.execute_activity(
            search_web_for_queries,
            args=[event_id, queries],
            start_to_close_timeout=timedelta(minutes=5)
        )
        self._results_count = len(search_results)
        
        if not search_results:
            self._status = "completed - no results"
            return {"status": "no results"}
            
        self._status = "fetching_and_classifying"
        
        # Results are already relevance-ranked by the search activity.
        top_results = search_results[:6]
        
        fetch_stats = await workflow.execute_activity(
            fetch_and_classify_articles,
            args=[event_id, top_results],
            start_to_close_timeout=timedelta(minutes=15)
        )
        
        self._status = "completed"
        return fetch_stats
