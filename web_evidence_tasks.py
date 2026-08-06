import os
import asyncio
from datetime import timedelta, datetime
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import json
    from tavily import TavilyClient
    from sqlalchemy import func
    from db_setup import SessionLocal, WebEvidence, RegulatoryEvent
    from cognitive_engine import generate_search_queries, classify_web_evidence
    from temporal_tasks import mfr_key

@activity.defn
async def generate_queries_activity(event_id: str) -> list[str]:
    db = SessionLocal()
    try:
        event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
        if not event or not event.raw_details:
            return []
        queries = generate_search_queries(event.raw_details)
        return queries
    finally:
        db.close()

@activity.defn
async def search_web_for_queries(queries: list[str]) -> list[dict]:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("Warning: TAVILY_API_KEY not set")
        return []
    
    tavily_client = TavilyClient(api_key=api_key)
    
    all_results = []
    seen_urls = set()
    
    for query in queries:
        try:
            # Execute search
            response = await asyncio.to_thread(
                tavily_client.search, 
                query=query, 
                search_depth="advanced", 
                max_results=5,
                include_raw_content=False
            )
            
            for result in response.get("results", []):
                url = result.get("url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append({
                        "query": query,
                        "title": result.get("title", ""),
                        "url": url,
                        "snippet": result.get("content", ""),
                        "source": url.split("/")[2] if "//" in url else ""
                    })
        except Exception as e:
            print(f"Tavily search error for '{query}': {e}")
            
    return all_results

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
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            processed = 0
            for item in search_results:
                url = item["url"]
                fetch_status = "failed"
                full_text = ""
                classification = {}
                relevance_score = 0
                
                try:
                    response = await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    if response and response.ok:
                        html_content = await page.content()
                        doc = Document(html_content)
                        h = html2text.HTML2Text()
                        h.ignore_links = True
                        h.ignore_images = True
                        full_text = h.handle(doc.summary())
                        fetch_status = "fetched"
                        
                        classification = await asyncio.to_thread(
                            classify_web_evidence, full_text, record_details
                        )
                        relevance_score = classification.get("relevance_score", 0)
                    else:
                        fetch_status = "blocked"
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
            args=[queries],
            start_to_close_timeout=timedelta(minutes=5)
        )
        self._results_count = len(search_results)
        
        if not search_results:
            self._status = "completed - no results"
            return {"status": "no results"}
            
        self._status = "fetching_and_classifying"
        
        # Limit to top 5 results for now to save time/budget
        top_results = search_results[:5]
        
        fetch_stats = await workflow.execute_activity(
            fetch_and_classify_articles,
            args=[event_id, top_results],
            start_to_close_timeout=timedelta(minutes=15)
        )
        
        self._status = "completed"
        return fetch_stats
