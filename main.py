from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import extract, func, or_
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timezone
import os

from db_setup import SessionLocal, RegulatoryEvent
from temporal_tasks import MANDATE_START, recency_weight, repeat_offender_bonus, mfr_key

app = FastAPI(title="AIVOA Project Sentinel - Signal API", version="1.0.0")

# View-only mode (Render demo deploy): serves the dashboard + read API only.
# Scraper triggers, full backfill, and dispatch actions are blocked.
VIEW_ONLY = os.getenv("VIEW_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")

# Enable CORS for React Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Pydantic Schema for Lead Output
class RegulatorySignalResponse(BaseModel):
    event_id: str
    regulator: str
    event_type: str
    score: int
    llm_analysis: dict
    raw_details: dict
    event_date: str
    reporting_source: str = ""
    reported_by: str = ""
    score_breakdown: dict = {}
    
    class Config:
        from_attributes = True
        
class SignalPageResponse(BaseModel):
    items: List[RegulatorySignalResponse]
    total: int
    page: int
    page_size: int
    pages: int
    paper_count: int

@app.get("/api/v1/signals/high-priority", response_model=SignalPageResponse)
def get_high_priority_signals(
    min_score: int = 0,
    year: int = None,
    page: int = 1,
    page_size: int = 30,
    q: str = None,
    event_type: str = None,
    is_paper: bool = None,
    rule_96: bool = False,
    sub_rule_7: bool = False,
    schedule_h2: bool = False,
    db: Session = Depends(get_db),
):
    """
    Paginated signals with server-side filtering.
    Params: min_score, year, page, page_size, q (substring search across
    drug/manufacturer/address/batch/reason/type), event_type, is_paper,
    rule_96, sub_rule_7, schedule_h2.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    query = db.query(RegulatoryEvent)
    if year:
        query = query.filter(extract('year', RegulatoryEvent.event_date) == year)
    if min_score:
        query = query.filter(RegulatoryEvent.score >= min_score)
    if event_type:
        query = query.filter(RegulatoryEvent.event_type == event_type)
    if is_paper is not None:
        query = query.filter(RegulatoryEvent.llm_analysis['is_paper_failure'].astext == str(is_paper).lower())
    if rule_96:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_rule_96'].astext == 'true')
    if sub_rule_7:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_sub_rule_7'].astext == 'true')
    if schedule_h2:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_schedule_h2'].astext == 'true')
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(
            func.lower(func.coalesce(RegulatoryEvent.raw_details['drug_name'].astext, '')).like(like),
            func.lower(func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')).like(like),
            func.lower(func.coalesce(RegulatoryEvent.raw_details['batch_no'].astext, '')).like(like),
            func.lower(func.coalesce(RegulatoryEvent.raw_details['reason'].astext, '')).like(like),
            func.lower(RegulatoryEvent.event_type).like(like),
        ))

    total = query.count()

    paper_count = 0
    if is_paper is None:
        paper_count = query.filter(
            RegulatoryEvent.llm_analysis['is_paper_failure'].astext == 'true'
        ).count()

    events = query.order_by(RegulatoryEvent.score.desc())\
                .offset((page - 1) * page_size)\
                .limit(page_size)\
                .all()

    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    counts = {}
    for mfr, cnt in db.query(mfr_col, func.count(RegulatoryEvent.event_id))\
            .group_by(mfr_col).all():
        key = mfr_key(mfr)
        if key:
            counts[key] = counts.get(key, 0) + cnt

    response = []
    for event in events:
        analysis = event.llm_analysis or {}
        mfr = (event.raw_details or {}).get('manufacturer', '')
        prior = max(counts.get(mfr_key(mfr), 0) - 1, 0)
        base = 40 if event.event_type == 'SPURIOUS_DRUG' else 20
        paper_bonus = 30 if analysis.get('is_paper_failure') else 0
        mandate_flags = [k for k in ('violates_rule_96', 'violates_sub_rule_7', 'violates_schedule_h2') if analysis.get(k)]
        mandate_bonus = 20 if (mandate_flags and event.event_date and event.event_date >= MANDATE_START) else 0
        recency = recency_weight(event.event_date)
        repeat_bonus = repeat_offender_bonus(prior)

        response.append({
            "event_id": str(event.event_id),
            "regulator": event.regulator,
            "event_type": event.event_type,
            "score": event.score,
            "llm_analysis": analysis,
            "raw_details": event.raw_details or {},
            "event_date": str(event.event_date) if event.event_date else "",
            "reporting_source": event.reporting_source or (event.raw_details or {}).get("reporting_source", ""),
            "reported_by": event.reported_by or (event.raw_details or {}).get("reported_by", ""),
            "score_breakdown": {
                "base": base,
                "paper_bonus": paper_bonus,
                "mandate_bonus": mandate_bonus,
                "mandate_flags": mandate_flags,
                "recency_weight": recency,
                "repeat_offender_bonus": repeat_bonus,
                "prior_events": prior,
            },
        })
    return {
        "items": response,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "paper_count": paper_count,
    }

@app.post("/api/v1/campaigns/{event_id}/approve")
def approve_outbound_campaign(event_id: str, db: Session = Depends(get_db)):
    """
    Human-in-the-Loop trigger: SDR clicks "Approve & Dispatch" 
    on high-score items.
    """
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Outreach dispatch is disabled in view-only mode.")
    event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Signal event not found")
        
    # Trigger or resume Temporal workflow here
    # await temporal_client.signal_workflow(...)
    
    return {"status": "SUCCESS", "message": f"Outreach sequence initiated for event {event_id}"}

@app.get("/api/v1/config")
def app_config():
    """Deployment mode the frontend uses to hide action buttons."""
    return {"view_only": VIEW_ONLY}

@app.get("/api/v1/scraper/status")
async def scraper_status():
    """
    Live scraper queue status: progress + ETA estimated from the actual
    processing rate of the running workflow.
    """
    if VIEW_ONLY:
        return {"status": "disabled", "detail": "Scraper execution disabled (view-only deployment)."}
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))

        found = None
        async for e in client.list_workflows(
            query="WorkflowType = 'CDSCOScraperWorkflow' AND ExecutionStatus = 'Running'",
            limit=1,
        ):
            found = e
        if not found:
            last = None
            async for e in client.list_workflows(
                query="WorkflowType = 'CDSCOScraperWorkflow'", limit=1
            ):
                last = e
            if last:
                try:
                    await client.get_workflow_handle(last.id).result()
                except FailureError as exc:
                    return {
                        "status": "failed",
                        "workflow_id": last.id,
                        "detail": str(exc).splitlines()[0][:400],
                    }
            return {"status": "idle", "total": 0, "processed": 0, "percent": 0}

        handle = client.get_workflow_handle(found.id)
        progress = await handle.query("progress")

        total = progress.get("total", 0)
        processed = progress.get("processed", 0)
        started = progress.get("started_at")

        elapsed = None
        eta_seconds = None
        if started:
            started_dt = datetime.fromisoformat(started)
            elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
            rate = (processed / elapsed) if (elapsed > 0 and processed > 0) else 0
            if rate > 0 and total > processed:
                eta_seconds = (total - processed) / rate

        percent = round(processed / total * 100) if total else 0
        return {
            "status": "finished" if progress.get("finished") else "running",
            "workflow_id": found.id,
            "total": total,
            "processed": processed,
            "percent": percent,
            "event_type": progress.get("event_type", ""),
            "elapsed_seconds": round(elapsed) if elapsed is not None else None,
            "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
            "warnings": progress.get("warnings", []),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}

from temporalio.client import Client
from temporalio.exceptions import FailureError

class ScraperTriggerRequest(BaseModel):
    year: Optional[str] = None   # e.g. "2026" (defaults to current year)
    full: bool = False           # True = full backfill across all reporting years

@app.post("/api/v1/scraper/trigger")
async def trigger_scraper(req: Optional[ScraperTriggerRequest] = None):
    """
    Starts the CDSCO scraper workflow.
    - No body / {"year": "2026"} -> scrape that year only (default current year).
    - {"full": true}            -> full historical backfill (2019-2026).
    """
    req = req or ScraperTriggerRequest()
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Scraper execution is disabled in view-only mode.")
    if req.full:
        args = []
    else:
        args = [req.year or str(datetime.utcnow().year)]
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        
        # Unique workflow id so re-runs don't collide
        workflow_id = f"cdsco-scraper-workflow-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        handle = await client.start_workflow(
            "CDSCOScraperWorkflow",
            *args,
            id=workflow_id,
            task_queue="scraper-task-queue",
        )
        return {"status": "SUCCESS", "message": f"Scraper workflow started with ID: {handle.id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _top_manufacturers(limit: int = 50) -> List[str]:
    """Distinct real manufacturers from regulatory_events, ranked by event
    count. CDSCO placeholders ('Under Investigation', etc.) are excluded."""
    db = SessionLocal()
    try:
        mfr_expr = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        rows = db.query(
            mfr_expr.label('mfr'),
            func.count(RegulatoryEvent.event_id).label('cnt'),
        ).group_by(mfr_expr).order_by(func.count(RegulatoryEvent.event_id).desc()).all()
    finally:
        db.close()
    firms = []
    for mfr, _cnt in rows:
        if mfr_key(mfr):   # '' for placeholders/empty -> skipped
            firms.append(mfr)
        if len(firms) >= limit:
            break
    return firms

class EnrichmentTriggerRequest(BaseModel):
    source: str = "fda"                # fda | eudragmdp | all
    limit: int = 50                    # max firms when firms is not given
    firms: Optional[List[str]] = None  # explicit firm names (overrides limit)

@app.post("/api/v1/enrichment/trigger")
async def trigger_enrichment(req: Optional[EnrichmentTriggerRequest] = None):
    """
    Starts EnrichmentWorkflow(s) on the enricher task queue.
    - Default: top N manufacturers (by event count) against one source.
    - {"source": "all"} -> one workflow per source.
    - {"firms": ["Captab Biotec"]} -> only those firms (limit ignored).
    """
    req = req or EnrichmentTriggerRequest()
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Enrichment execution is disabled in view-only mode.")
    if req.source not in ("fda", "eudragmdp", "all"):
        raise HTTPException(status_code=400, detail="source must be 'fda', 'eudragmdp' or 'all'")

    firms = req.firms or _top_manufacturers(req.limit)
    if not firms:
        return {"status": "SUCCESS", "message": "No manufacturers to enrich."}

    sources = ["fda", "eudragmdp"] if req.source == "all" else [req.source]
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        workflow_ids = []
        for source in sources:
            workflow_id = f"enrichment-{source}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "EnrichmentWorkflow",
                args=[firms, source],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {
            "status": "SUCCESS",
            "message": f"Enrichment started for {len(firms)} firms on source(s) {sources}",
            "workflow_ids": workflow_ids,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
