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
import re
import uuid
from contextlib import asynccontextmanager

from db_setup import SessionLocal, RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck, WebEvidence, CompanyLead, Campaign, CampaignLead
from temporal_tasks import MANDATE_START, recency_weight, repeat_offender_bonus, mfr_key
from company_names import clean_company_name, PAREN
from paper_category import assess_paper_category
from signal_scoring import (
    company_key,
    _group_key,
    _load_enrichment,
    _load_web_evidence,
    _slug,
    _prior_event_counts,
    _is_paper_event,
    _event_max_possible,
    _web_evidence_bonus,
    _build_signal_card,
)


# View-only mode (Render demo deploy): serves the dashboard + read API only.
# Scraper triggers, full backfill, and dispatch actions are blocked.
VIEW_ONLY = os.getenv("VIEW_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")

# MCP (Model Context Protocol) session manager placeholder. Populated when the
# MCP server is mounted at the bottom of this file; the FastAPI lifespan (below)
# starts the MCP session task group before serving requests, because Starlette
# does not run a mounted sub-app's own lifespan.
_MCP_SESSION_MANAGER = None
_MCP_RUNNER = None

if os.getenv("ENABLE_MCP", "1").strip().lower() in ("1", "true", "yes", "on"):
    @asynccontextmanager
    async def _mcp_lifespan(app):
        global _MCP_RUNNER
        if _MCP_SESSION_MANAGER is not None:
            _MCP_RUNNER = _MCP_SESSION_MANAGER.run()
            await _MCP_RUNNER.__aenter__()
        try:
            yield
        finally:
            if _MCP_SESSION_MANAGER is not None and _MCP_RUNNER is not None:
                try:
                    await _MCP_RUNNER.__aexit__(None, None, None)
                except Exception:
                    pass
else:
    async def _mcp_lifespan(app):
        yield  # type: ignore

app = FastAPI(title="AIVOA Project Sentinel - Signal API", version="1.0.0", lifespan=_mcp_lifespan)

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
    max_possible_score: int = 0
    company_name: str = ""
    slug: str = ""
    company_key: str = ""
    llm_analysis: dict
    raw_details: dict
    event_date: str
    reporting_source: str = ""
    reported_by: str = ""
    score_breakdown: dict = {}
    enrichment: dict = {}
    paper_assessment: dict = {}
    event_count: int = 1
    events: list = []
    web_evidence: list = []
    
    class Config:
        from_attributes = True
        
class SignalPageResponse(BaseModel):
    items: List[RegulatorySignalResponse]
    total: int
    page: int
    page_size: int
    pages: int
    paper_count: int


class CompanyRankingItem(BaseModel):
    company_key: str
    name: str
    slug: str
    score: int
    max_possible_score: int = 0
    event_count: int
    avg_score: float
    latest_date: str = ""
    regulators: list = []
    paper_count: int = 0
    mandate_count: int = 0


class CompanyRankingResponse(BaseModel):
    items: List[CompanyRankingItem]
    total: int
    page: int
    page_size: int
    pages: int


class CompanySummary(BaseModel):
    company_key: str
    name: str
    slug: str
    score: int
    max_possible_score: int = 0
    event_count: int
    avg_score: float
    latest_date: str = ""
    regulators: list = []
    years: list = []
    paper_count: int = 0
    mandate_count: int = 0
    evidence_count: int = 0
    web_evidence_count: int = 0


class CompanySignalsResponse(BaseModel):
    company: CompanySummary
    card: RegulatorySignalResponse


@app.get("/api/v1/signals/high-priority", response_model=SignalPageResponse)
def get_high_priority_signals(
    min_score: int = 0,
    year: int = None,
    page: int = 1,
    page_size: int = 30,
    q: str = None,
    event_type: str = None,
    is_paper: bool = None,
    paper_class: str = None,
    group_by: str = None,
    rule_96: bool = False,
    sub_rule_7: bool = False,
    schedule_h2: bool = False,
    schedule_m_gap: str = None,
    db: Session = Depends(get_db),
):
    """
    Paginated signals with server-side filtering.
    Params: min_score, year, page, page_size, q (substring search across
    drug/manufacturer/address/batch/reason/type), event_type, is_paper,
    rule_96, sub_rule_7, schedule_h2, schedule_m_gap.
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
    if paper_class in ("explicit", "deductive", "none"):
        query = query.filter(RegulatoryEvent.paper_evidence_class == paper_class)
    if rule_96:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_rule_96'].astext == 'true')
    if sub_rule_7:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_sub_rule_7'].astext == 'true')
    if schedule_h2:
        query = query.filter(RegulatoryEvent.llm_analysis['violates_schedule_h2'].astext == 'true')
    if schedule_m_gap:
        query = query.filter(RegulatoryEvent.llm_analysis['schedule_m_gap'].astext == schedule_m_gap)
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

    # Prior-event counts per manufacturer (used for the repeat-offender bonus).
    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    counts = {}
    for mfr, cnt in db.query(mfr_col, func.count(RegulatoryEvent.event_id))\
            .group_by(mfr_col).all():
        key = mfr_key(mfr)
        if key:
            counts[key] = counts.get(key, 0) + cnt

    web_by_key = _load_web_evidence(db)

    # Group by company: one card per company_key, with every repeat incident
    # embedded in `events` for the card's dropdown. Placeholder manufacturers
    # ("under investigation", etc.) are unknown entities — each stays its own card.
    if group_by == "company":
        matching = query.order_by(RegulatoryEvent.score.desc()).all()
        groups = []
        group_of = {}
        for event in matching:
            mfr = (event.raw_details or {}).get('manufacturer', '')
            key = _group_key(mfr) if mfr_key(mfr) else f"__evt__{event.event_id}"
            if key not in group_of:
                group_of[key] = len(groups)
                groups.append({"events": [event]})
            else:
                groups[group_of[key]]["events"].append(event)

        total = len(groups)
        paper_count = 0
        if is_paper is None:
            paper_count = sum(
                1 for g in groups
                if any((e.llm_analysis or {}).get('is_paper_failure') for e in g["events"]))

        page_groups = groups[(page - 1) * page_size: page * page_size]

        page_keys = set()
        for g in page_groups:
            ckey = company_key((g["events"][0].raw_details or {}).get('manufacturer', ''))
            if ckey:
                page_keys.add(ckey)
        checks_by_key, evidence_by_key = _load_enrichment(db, page_keys)

        response = []
        for g in page_groups:
            g["events"].sort(key=lambda e: e.score, reverse=True)
            cards = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
                     for e in g["events"]]
            card = cards[0]
            if len(cards) > 1:
                card["event_count"] = len(cards)
                card["events"] = cards[1:]
            response.append(card)
    else:
        events = query.order_by(RegulatoryEvent.score.desc())\
                    .offset((page - 1) * page_size)\
                    .limit(page_size)\
                    .all()

        page_keys = set()
        for event in events:
            ckey = company_key((event.raw_details or {}).get('manufacturer', ''))
            if ckey:
                page_keys.add(ckey)
        checks_by_key, evidence_by_key = _load_enrichment(db, page_keys)

        response = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
                    for e in events]

    db.commit()
    return {
        "items": response,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "paper_count": paper_count,
    }

@app.get("/api/v1/companies/count")
def get_company_count(db: Session = Depends(get_db)):
    """Total unique company entities (same _group_key the ranking uses). Cheap:
    one column scan, dedupe in Python."""
    keys = set()
    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    for (mfr,) in db.query(mfr_col).all():
        gkey = _group_key(mfr)
        if gkey:
            keys.add(gkey)
    return {"total": len(keys)}


@app.get("/api/v1/companies/ranking", response_model=CompanyRankingResponse)
def get_company_ranking(
    page: int = 1,
    page_size: int = 10,
    q: str = None,
    db: Session = Depends(get_db),
):
    """Company leaderboard: every company entity (same _group_key as the card
    grouping) ranked by its highest-scoring signal, then by name. Paginated for
    the sidebar's 'view more' and the full directory page."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    q = (q or "").strip().lower()

    events = db.query(RegulatoryEvent).order_by(RegulatoryEvent.score.desc()).all()
    counts = _prior_event_counts(db)
    groups = {}
    for e in events:
        mfr = (e.raw_details or {}).get("manufacturer", "")
        gkey = _group_key(mfr)
        if not gkey:
            continue
        if q:
            hay = " ".join(
                str((e.raw_details or {}).get(k, "")) for k in
                ("manufacturer", "drug_name", "reason", "batch_no")
            ).lower()
            if q not in hay:
                continue
        g = groups.get(gkey)
        if g is None:
            g = {
                "gkey": gkey,
                "name": clean_company_name(PAREN.sub("", mfr)) or gkey,
                "slug": _slug(gkey),
                "score": 0,
                "peak": None,
                "event_count": 0,
                "sum_score": 0,
                "latest": None,
                "reg_set": set(),
                "paper": 0,
                "mandates": 0,
            }
            groups[gkey] = g
        g["event_count"] += 1
        g["sum_score"] += e.score or 0
        if (e.score or 0) > g["score"]:
            g["score"] = e.score or 0
            g["peak"] = e
        d = e.event_date
        if d and (g["latest"] is None or d > g["latest"]):
            g["latest"] = d
        g["reg_set"].add(e.regulator or "CDSCO")
        if _is_paper_event(e):
            g["paper"] += 1
        a = e.llm_analysis or {}
        if (e.event_date and e.event_date >= MANDATE_START) and any(
                a.get(k) for k in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2")):
            g["mandates"] += 1

    items = [{
        "company_key": g["gkey"],
        "name": g["name"],
        "slug": g["slug"],
        "score": g["score"],
        "max_possible_score": _event_max_possible(g["peak"], counts) if g["peak"] else 0,
        "event_count": g["event_count"],
        "avg_score": round(g["sum_score"] / g["event_count"], 1),
        "latest_date": str(g["latest"]) if g["latest"] else "",
        "regulators": sorted(g["reg_set"]),
        "paper_count": g["paper"],
        "mandate_count": g["mandates"],
    } for g in groups.values()]
    items.sort(key=lambda x: (-x["score"], x["name"].lower()))

    total = len(items)
    page_items = items[(page - 1) * page_size: page * page_size]
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@app.get("/api/v1/companies/{slug}/signals", response_model=CompanySignalsResponse)
def get_company_signals(slug: str, db: Session = Depends(get_db)):
    """Full company page payload: summary + the same grouped card the dashboard
    renders, with every incident embedded for the card's dropdown."""
    events = db.query(RegulatoryEvent).all()
    groups = {}
    for e in events:
        mfr = (e.raw_details or {}).get("manufacturer", "")
        gkey = _group_key(mfr)
        if gkey:
            groups.setdefault(gkey, []).append(e)

    target = None
    for gkey, evs in groups.items():
        if _slug(gkey) == slug:
            target = (gkey, evs)
            break
    if target is None:
        raise HTTPException(status_code=404, detail="Company not found")
    gkey, evs = target
    evs.sort(key=lambda e: e.score, reverse=True)

    mfr0 = (evs[0].raw_details or {}).get("manufacturer", "")
    ckey = company_key(mfr0)
    counts = _prior_event_counts(db)
    web_by_key = _load_web_evidence(db)
    checks_by_key, evidence_by_key = _load_enrichment(db, {ckey} if ckey else set())

    cards = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
             for e in evs]
    card = cards[0]
    if len(cards) > 1:
        card["event_count"] = len(cards)
        card["events"] = cards[1:]

    dates = [e.event_date for e in evs if e.event_date]
    summary = {
        "company_key": gkey,
        "name": clean_company_name(PAREN.sub("", mfr0)) or gkey,
        "slug": slug,
        "score": card["score"],
        "max_possible_score": card.get("max_possible_score", 0),
        "event_count": len(evs),
        "avg_score": round(sum(e.score or 0 for e in evs) / len(evs), 1),
        "latest_date": str(max(dates)) if dates else "",
        "regulators": sorted({e.regulator or "CDSCO" for e in evs}),
        "years": sorted({str(d)[:4] for d in dates}),
        "paper_count": sum(1 for e in evs if _is_paper_event(e)),
        "mandate_count": sum(1 for e in evs if
            e.event_date and e.event_date >= MANDATE_START and any(
                (e.llm_analysis or {}).get(k)
                for k in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2"))),
        "evidence_count": len(evidence_by_key.get(ckey, [])),
        "web_evidence_count": sum(len(v) for k, v in web_by_key.items() if k == gkey),
    }

    db.commit()
    return {"company": summary, "card": card}


@app.get("/companies")
@app.get("/companies/{path:path}")
def serve_company_frontend(path: str = ""):
    """SPA shell: the router in company-view.js reads location.pathname and
    renders the directory / individual company page client-side."""
    return FileResponse("static/index.html")


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

class EnrichmentBackfillRequest(BaseModel):
    year_start: Optional[str] = None   # inclusive, e.g. "2024"
    year_end: Optional[str] = None     # inclusive, e.g. "2022"
    only_missing: bool = True          # skip rows that already have llm_analysis
    limit: Optional[int] = None        # cap candidates (paced multi-day backfills)


@app.get("/api/v1/scraper/enrichment/status")
async def cdsco_enrichment_status():
    """
    Live CDSCO enrichment workflow status (AI analysis + scoring over stored
    raw rows). Mirrors /api/v1/scraper/status but for CDSCOEnrichmentWorkflow.
    """
    if VIEW_ONLY:
        return {"status": "disabled", "detail": "Scraper execution disabled (view-only deployment)."}
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))

        found = None
        async for e in client.list_workflows(
            query="WorkflowType = 'CDSCOEnrichmentWorkflow' AND ExecutionStatus = 'Running'",
            limit=1,
        ):
            found = e
        if not found:
            last = None
            async for e in client.list_workflows(
                query="WorkflowType = 'CDSCOEnrichmentWorkflow'", limit=1
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
            "phase": progress.get("phase", ""),
            "total": total,
            "processed": processed,
            "percent": percent,
            "elapsed_seconds": round(elapsed) if elapsed is not None else None,
            "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
            "warnings": progress.get("warnings", []),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/v1/scraper/enrichment/trigger")
async def trigger_cdsco_enrichment(req: Optional[EnrichmentBackfillRequest] = None):
    """
    Runs AI enrichment + scoring over already-scraped CDSCO rows.
    - No body                 -> all rows with empty llm_analysis.
    - {"year_start": "2024", "year_end": "2022"} -> that year range only.
    - {"only_missing": false} -> re-enrich every row in range.
    - {"limit": N}            -> cap the batch (pace multi-day backfills).
    """
    req = req or EnrichmentBackfillRequest()
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Scraper execution is disabled in view-only mode.")
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))

        workflow_id = f"cdsco-enrichment-workflow-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        handle = await client.start_workflow(
            "CDSCOEnrichmentWorkflow",
            args=[req.year_start, req.year_end, req.only_missing, req.limit],
            id=workflow_id,
            task_queue="scraper-task-queue",
        )
        return {
            "status": "SUCCESS",
            "message": f"Enrichment workflow started with ID: {handle.id}",
            "workflow_id": handle.id,
        }
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


class EnrichmentCheckRequest(BaseModel):
    event_id: str
    source: str = "all"   # fda | eudragmdp | all


@app.post("/api/v1/enrichment/check")
async def check_event_enrichment(req: EnrichmentCheckRequest,
                                 db: Session = Depends(get_db)):
    """
    On-demand enrichment for a single signal card. Resolves the card's
    manufacturer, cleans it, and runs the enrichment workflow for that one
    firm. The frontend polls /api/v1/enrichment/status/{workflow_id}.
    """
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Enrichment execution is disabled in view-only mode.")
    if req.source not in ("fda", "eudragmdp", "all"):
        raise HTTPException(status_code=400, detail="source must be 'fda', 'eudragmdp' or 'all'")

    event = db.query(RegulatoryEvent).filter(
        RegulatoryEvent.event_id == req.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Signal event not found")

    mfr = (event.raw_details or {}).get("manufacturer", "")
    if not mfr or not mfr_key(mfr):
        raise HTTPException(status_code=400, detail="Manufacturer is a placeholder or missing")

    sources = ["fda", "eudragmdp"] if req.source == "all" else [req.source]
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        workflow_ids = []
        for source in sources:
            workflow_id = f"check-{source}-{req.event_id[:8]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "EnrichmentWorkflow",
                args=[[mfr], source],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {
            "status": "SUCCESS",
            "manufacturer": mfr,
            "workflow_ids": workflow_ids,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/enrichment/status/{workflow_id}")
async def enrichment_status(workflow_id: str):
    """
    Poll endpoint for a single-firm enrichment workflow: returns the workflow
    state, progress, and (when finished) the batch results.
    """
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
        state = desc.status.name
    except Exception:
        return {"workflow_id": workflow_id, "state": "UNKNOWN"}

    progress = {}
    if state in ("RUNNING", "COMPLETED"):
        try:
            progress = await handle.query("progress")
        except Exception:
            progress = {}

    result = None
    error = None
    if state in ("COMPLETED", "FAILED", "TERMINATED", "CANCELED"):
        try:
            result = await handle.result()
        except FailureError as e:
            error = str(e)
        except Exception as e:
            error = str(e)

    summary = {}
    if result:
        for key, batch in result.items():
            if isinstance(batch, dict):
                summary.update({
                    "findings": summary.get("findings", 0) + batch.get("findings", 0),
                    "inserted": summary.get("inserted", 0) + batch.get("inserted", 0),
                    "paper_qms_findings": summary.get("paper_qms_findings", 0) + batch.get("paper_qms_findings", 0),
                    "errors": summary.get("errors", []) + batch.get("errors", []),
                    "firms": summary.get("firms", []) + batch.get("firms", []),
                    "searched": summary.get("searched", []) + batch.get("searched", []),
                    "skipped_firms": summary.get("skipped_firms", []) + batch.get("skipped_firms", []),
                })

    return {
        "workflow_id": workflow_id,
        "state": state,
        "progress": progress,
        "summary": summary,
        "result": result,
        "error": error,
    }

class RegulatoryPullRequest(BaseModel):
    source: str = "fda"                 # fda | eudragmdp | all
    from_date: str = "2022-01-01"
    to_date: str = "2026-12-31"
    max_records: int = 10000


@app.post("/api/v1/regulatory/trigger")
async def trigger_regulatory_pull(req: Optional[RegulatoryPullRequest] = None):
    """Stage 1: bulk-scrape every FDA warning letter / EudraGMDP statement in
    the date range into the raw staging table (no per-company filter)."""
    req = req or RegulatoryPullRequest()
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Scraping is disabled in view-only mode.")
    if req.source not in ("fda", "eudragmdp", "all"):
        raise HTTPException(status_code=400, detail="source must be 'fda', 'eudragmdp' or 'all'")
    sources = ["fda", "eudragmdp"] if req.source == "all" else [req.source]
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        workflow_ids = []
        for source in sources:
            workflow_id = f"regulatory-pull-{source}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "RegulatoryFullPullWorkflow",
                args=[source, req.from_date, req.to_date, req.max_records],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {
            "status": "SUCCESS",
            "message": f"Full pull started for {sources} ({req.from_date} -> {req.to_date})",
            "workflow_ids": workflow_ids,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/regulatory/status")
async def regulatory_pull_status():
    """Poll the running RegulatoryFullPullWorkflow(s): phase, rows, inserted."""
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    running = []
    async for e in client.list_workflows(
        query="WorkflowType = 'RegulatoryFullPullWorkflow' AND ExecutionStatus = 'Running'",
        limit=10,
    ):
        try:
            handle = client.get_workflow_handle(e.id)
            progress = await handle.query("progress")
        except Exception:
            progress = {}
        running.append({"workflow_id": e.id, **progress})
    return {"running": running}


class RegulatoryCheckRequest(BaseModel):
    firm_name: Optional[str] = None     # company to check (preferred)
    event_id: Optional[str] = None      # or resolve manufacturer from a CDSCO event
    source: str = "all"                 # fda | eudragmdp | all


@app.post("/api/v1/regulatory/check")
async def check_scraped_records(req: RegulatoryCheckRequest,
                                db: Session = Depends(get_db)):
    """Stage 2: check one company against the scraped staging records —
    fuzzy-match, fetch letter bodies, classify, and link into
    regulatory_evidence. The frontend polls
    /api/v1/regulatory/check/status/{workflow_id}.
    """
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Check disabled in view-only mode.")
    if req.source not in ("fda", "eudragmdp", "all"):
        raise HTTPException(status_code=400, detail="source must be 'fda', 'eudragmdp' or 'all'")
    firm = req.firm_name
    if req.event_id:
        event = db.query(RegulatoryEvent).filter(
            RegulatoryEvent.event_id == req.event_id).first()
        if not event:
            raise HTTPException(status_code=404, detail="Signal event not found")
        firm = (event.raw_details or {}).get("manufacturer", "")
    if not firm or not mfr_key(firm):
        raise HTTPException(status_code=400, detail="Manufacturer is a placeholder or missing")

    sources = ["fda", "eudragmdp"] if req.source == "all" else [req.source]
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        workflow_ids = []
        for source in sources:
            workflow_id = f"regulatory-check-{source}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "ScrapedRecordCheckWorkflow",
                args=[firm, source],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {"status": "SUCCESS", "firm_name": firm, "workflow_ids": workflow_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/regulatory/check/status/{workflow_id}")
async def regulatory_check_status(workflow_id: str):
    """Poll a ScrapedRecordCheckWorkflow (same contract as enrichment/status)."""
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
        state = desc.status.name
    except Exception:
        return {"workflow_id": workflow_id, "state": "UNKNOWN"}
    progress = {}
    if state in ("RUNNING", "COMPLETED"):
        try:
            progress = await handle.query("progress")
        except Exception:
            progress = {}
    result = None
    error = None
    if state in ("COMPLETED", "FAILED", "TERMINATED", "CANCELED"):
        try:
            result = await handle.result()
        except FailureError as e:
            error = str(e)
        except Exception as e:
            error = str(e)
    return {
        "workflow_id": workflow_id,
        "state": state,
        "progress": progress,
        "result": result,
        "error": error,
    }


@app.post("/api/v1/web-evidence/search/{event_id}")
async def trigger_web_evidence_search(event_id: str, db: Session = Depends(get_db)):
    """Starts the WebEvidenceWorkflow for a specific record."""
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Web evidence execution is disabled in view-only mode.")
    
    event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Signal event not found")
        
    try:
        client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
        workflow_id = f"web-evidence-{event_id[:8]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        handle = await client.start_workflow(
            "WebEvidenceWorkflow",
            args=[event_id],
            id=workflow_id,
            task_queue="enrichment-task-queue",
        )
        return {
            "status": "SUCCESS",
            "message": f"Web evidence search started for {event_id}",
            "workflow_id": handle.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/records/{event_id}/web-evidence")
def get_web_evidence(event_id: str, db: Session = Depends(get_db)):
    """Retrieve stored web evidence for a record. Evidence is company-level
    (grouped by _group_key of the manufacturer, matching _load_web_evidence and
    the card-scoring source), so evidence fetched for ANY of a company's
    incidents is surfaced here too."""
    event = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Signal event not found")

    mfr = (event.raw_details or {}).get('manufacturer', '')
    gkey = _group_key(mfr)
    web_by_key = _load_web_evidence(db)
    items = web_by_key.get(gkey, [])

    return {
        "event_id": event_id,
        "evidence": [
            {
                "id": str(w.id),
                "title": w.title or w.url,
                "url": w.url,
                "source": w.source or "",
                "published_date": str(w.published_date) if w.published_date else None,
                "snippet": w.snippet or "",
                "classification": w.classification or {},
                "relevance_score": int(w.relevance_score or (w.classification or {}).get("relevance_score", 0) or 0),
                "fetch_status": w.fetch_status or "",
                "fetched_at": str(w.fetched_at) if w.fetched_at else None,
                "corroborates_failure": bool((w.classification or {}).get("corroborates_failure", False)),
                "recall_action": bool((w.classification or {}).get("recall_action", False)),
                "severity": (w.classification or {}).get("severity", ""),
                "regulatory_action": (w.classification or {}).get("regulatory_action", ""),
                "is_paper_qms": bool((w.classification or {}).get("is_paper_qms", False)),
                "summary": (w.classification or {}).get("summary", ""),
            }
            for w in items
        ],
    }


# ---------------------------------------------------------------------------
# Lead research — website / LinkedIn / hiring for selected companies
# ---------------------------------------------------------------------------

MAX_LEADS_PER_BATCH = 10

class LeadResearchRequest(BaseModel):
    company_keys: List[str] = []

@app.post("/api/v1/leads/research")
async def trigger_lead_research(req: LeadResearchRequest, db: Session = Depends(get_db)):
    """Starts a LeadResearchWorkflow per company (max 10 per batch). The page
    polls /api/v1/leads/status for progress."""
    if VIEW_ONLY:
        raise HTTPException(status_code=403, detail="Lead research is disabled in view-only mode.")

    keys = [k for k in (req.company_keys or []) if k]
    if not keys:
        raise HTTPException(status_code=422, detail="Provide at least one company_key.")
    if len(keys) > MAX_LEADS_PER_BATCH:
        raise HTTPException(status_code=422, detail=f"Select at most {MAX_LEADS_PER_BATCH} companies at a time.")

    # Build company_key -> display name with a single column scan.
    names = {}
    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    for (mfr,) in db.query(mfr_col).all():
        gkey = _group_key(mfr)
        if not gkey or gkey in names:
            continue
        names[gkey] = clean_company_name(PAREN.sub("", mfr)) or gkey

    missing = [k for k in keys if k not in names]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown company keys: {missing[:5]}")

    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    started = []
    for key in keys:
        row = db.query(CompanyLead).filter(CompanyLead.company_key == key).first()
        if row is None:
            row = CompanyLead(company_key=key)
            db.add(row)
        row.company_name = names[key]
        row.status = "running"
        row.error = ""
        row.workflow_id = ""
        db.commit()

        workflow_id = f"lead-research-{key[:16]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        try:
            handle = await client.start_workflow(
                "LeadResearchWorkflow",
                args=[key, names[key]],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            row.workflow_id = handle.id
            db.commit()
            row_status = row.status
        except Exception as e:
            row.status = "failed"
            row.error = str(e)
            db.commit()
            row_status = "failed"
            print(f"lead research start failed for {key}: {e}")

        started.append({"company_key": key, "status": row_status, "workflow_id": row.workflow_id})

    return {"started": started, "count": len(started)}


@app.get("/api/v1/leads/status")
def get_lead_status(db: Session = Depends(get_db)):
    """All researched companies with their lead data + status, newest first."""
    rows = db.query(CompanyLead)\
        .order_by(CompanyLead.fetched_at.desc().nullslast(), CompanyLead.company_key)\
        .all()
    return {"items": [_lead_payload(r) for r in rows]}


@app.get("/api/v1/leads/{company_key}")
def get_lead_detail(company_key: str, db: Session = Depends(get_db)):
    row = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return _lead_payload(row)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@app.get("/api/v1/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    campaigns = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
    items = []
    for c in campaigns:
        lead_count = db.query(CampaignLead).filter(CampaignLead.campaign_id == c.campaign_id).count()
        items.append(_campaign_payload(c, lead_count))
    return {"items": items}


@app.get("/api/v1/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, db: Session = Depends(get_db)):
    c = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    leads = db.query(CampaignLead).filter(CampaignLead.campaign_id == campaign_id).all()
    lead_count = len(leads)
    payload = _campaign_payload(c, lead_count)
    payload["leads"] = [_campaign_lead_payload(cl) for cl in leads]
    return payload


class CreateCampaignRequest(BaseModel):
    name: str
    leads: list = []  # [{company_key, decision_maker}]
    sequence_config: list = []
    created_by: str = ""


@app.post("/api/v1/campaigns")
def create_campaign(req: CreateCampaignRequest, db: Session = Depends(get_db)):
    if not req.leads:
        raise HTTPException(status_code=422, detail="Select at least one lead.")
    campaign_id = f"campaign-{uuid.uuid4().hex[:12]}"

    from campaign_tasks import create_campaign_activity
    result = create_campaign_activity({
        "campaign_id": campaign_id,
        "name": req.name or "Untitled Campaign",
        "leads": req.leads,
        "sequence_config": req.sequence_config,
        "created_by": req.created_by,
    })
    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to create campaign"))
    return result


@app.post("/api/v1/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str, db: Session = Depends(get_db)):
    c = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.status == "running":
        raise HTTPException(status_code=400, detail="Campaign already running")

    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    workflow_id = f"campaign-{campaign_id}"
    try:
        handle = await client.start_workflow(
            "CampaignWorkflow",
            args=[campaign_id],
            id=workflow_id,
            task_queue="enrichment-task-queue",
        )
        c.workflow_id = handle.id
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start campaign: {str(e)[:300]}")
    return {"campaign_id": campaign_id, "status": "running", "workflow_id": handle.id}


def _campaign_payload(c, lead_count: int = 0) -> dict:
    return {
        "campaign_id": c.campaign_id,
        "name": c.name,
        "status": c.status,
        "lead_count": lead_count,
        "sequence_config": c.sequence_config or [],
        "created_at": str(c.created_at) if c.created_at else None,
        "started_at": str(c.started_at) if c.started_at else None,
        "completed_at": str(c.completed_at) if c.completed_at else None,
        "workflow_id": c.workflow_id or "",
    }


def _campaign_lead_payload(cl) -> dict:
    return {
        "company_key": cl.company_key,
        "status": cl.status,
        "current_step": cl.current_step,
        "decision_maker": cl.decision_maker or {},
        "messages": cl.messages or [],
        "last_contact_at": str(cl.last_contact_at) if cl.last_contact_at else None,
        "replied_at": str(cl.replied_at) if cl.replied_at else None,
    }


def _lead_payload(r) -> dict:
    return {
        "company_key": r.company_key,
        "company_name": r.company_name,
        "website": r.website,
        "linkedin_url": r.linkedin_url,
        "company_status": r.company_status or "unknown",
        "decision_makers": r.decision_makers or [],
        "intent_signals": r.intent_signals or [],
        "trigger_events": r.trigger_events or [],
        "activity_summary": r.activity_summary or "",
        "hiring": r.hiring or [],
        "hiring_news": r.hiring_news or [],
        "hiring_headline": (r.summary or {}).get("hiring_headline", ""),
        "summary": r.summary or {},
        "status": r.status,
        "error": r.error,
        "workflow_id": r.workflow_id,
        "fetched_at": str(r.fetched_at) if r.fetched_at else None,
    }


# ---------------------------------------------------------------------------
# MCP Server — Streamable HTTP transport mounted at /mcp
# ---------------------------------------------------------------------------
try:
    from mcp_server.server import get_http_app, get_session_manager

    # stateless=True: the sales-app backend speaks raw JSON-RPC over HTTP without
    #   managing MCP sessions (each POST is independent).
    # disable_transport_security=True: the backend reaches us under the `app`
    #   hostname, which DNS-rebinding protection would reject.
    mcp_http_app = get_http_app(stateless=True, disable_transport_security=True)

    # The MCP session manager (created lazily by streamable_http_app) must be
    # started in the parent FastAPI lifespan — mounted sub-apps do not run their
    # own lifespan in Starlette. The lifespan defined above calls run().__aenter__.
    _MCP_SESSION_MANAGER = get_session_manager()

    # Mount the *whole* Starlette app at the site root so the inner `/mcp` route
    # is exposed at the public path `/mcp` (no path doubling, no trailing-slash
    # redirect). Parent routes registered above still take precedence.
    app.mount("/", app=mcp_http_app)
except ImportError:
    # mcp package not installed — skip MCP mount
    pass
except Exception as _mcp_err:
    import warnings
    warnings.warn(f"MCP server failed to mount: {_mcp_err}")
