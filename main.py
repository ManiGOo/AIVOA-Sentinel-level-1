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

from db_setup import SessionLocal, RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck, WebEvidence
from temporal_tasks import MANDATE_START, recency_weight, repeat_offender_bonus, mfr_key
from company_names import clean_company_name, PAREN
from paper_category import assess_paper_category


def company_key(raw: str) -> str:
    """Entity-level key: cleaned company name (all raw variants of a company
    map to the same key). Empty when no company name can be extracted."""
    if not raw:
        return ""
    return clean_company_name(raw).strip().lower()

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
    company_name: str = ""
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
    
    class Config:
        from_attributes = True
        
class SignalPageResponse(BaseModel):
    items: List[RegulatorySignalResponse]
    total: int
    page: int
    page_size: int
    pages: int
    paper_count: int


_GROUP_KEY_NORM = re.compile(r"[^a-z0-9]+")
_LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "corp",
                "corporation", "co", "company"}
_PLURAL_SING = {
    "formulations": "formulation", "laboratories": "laboratory",
    "industries": "industry", "enterprises": "enterprise",
    "sciences": "science", "pharmaceuticals": "pharmaceutical",
    "chemicals": "chemical", "biologicals": "biological",
    "diagnostics": "diagnostic", "remedies": "remedy",
    "botanicals": "botanical", "devices": "device",
}


def _group_key(mfr):
    """Company-grouping key: the cleaned trading name (reusing
    clean_company_name for M/s prefixes, addresses, parentheticals), then fully
    normalized so spelling variants ('Pvt. Ltd.'/'Pvt.Ltd'/'Pvt ltd'),
    legal-suffix differences ('Zee Laboratories' vs 'Zee Laboratories Ltd') and
    plural/singular forms ('Rivpra Formulations' vs 'Rivpra Formulation') all
    collapse into a single card.

    Parenthetical descriptors are removed from the raw string FIRST: otherwise
    a suffix like '(A WHO - GMP Certified Company)' sitting between the name
    and the address marker confuses the company-cut and survives as trailing
    noise."""
    name = clean_company_name(PAREN.sub("", mfr or ""))
    if not name:
        return ""
    words = re.sub(_GROUP_KEY_NORM, " ", name.lower()).strip().split()
    words = [w for w in words if w not in _LEGAL_WORDS]
    words = [_PLURAL_SING.get(w, w) for w in words]
    return " ".join(words).strip()


def _load_enrichment(db, page_keys):
    """Enrichment state for a set of company_keys: latest check per source
    (incl. 'checked, no findings') + stored evidence rows. All raw manufacturer
    variants of one company share the same company_key."""
    checks_by_key = {}
    evidence_by_key = {}
    if page_keys:
        for c in db.query(EnrichmentCheck).filter(
                EnrichmentCheck.company_key.in_(page_keys))\
                .order_by(EnrichmentCheck.checked_at.desc()).all():
            checks_by_key.setdefault(c.company_key or "", []).append(c)
        for e in db.query(RegulatoryEvidence).filter(
                RegulatoryEvidence.company_key.in_(page_keys))\
                .order_by(RegulatoryEvidence.fetched_at.desc()).all():
            evidence_by_key.setdefault(e.company_key or "", []).append(e)
    return checks_by_key, evidence_by_key


def _build_signal_card(event, counts, checks_by_key, evidence_by_key) -> dict:
    """Recompute the class-aware score for one event and build its card dict.
    Mutates event.score/paper_* on the ORM object (caller commits)."""
    analysis = event.llm_analysis or {}
    mfr = (event.raw_details or {}).get('manufacturer', '')
    key = mfr_key(mfr)
    ckey = company_key(mfr)

    latest_checks = {}
    for c in checks_by_key.get(ckey, []):
        if c.source not in latest_checks:
            latest_checks[c.source] = {
                "status": c.status,
                "checked_at": str(c.checked_at) if c.checked_at else "",
                "searched_name": c.searched_name or "",
                "findings_count": c.findings_count or 0,
                "paper_qms_count": c.paper_qms_count or 0,
            }

    prior = max(counts.get(key, 0) - 1, 0)
    base = 40 if event.event_type == 'SPURIOUS_DRUG' else 20
    pa = assess_paper_category(
        ckey,
        (event.raw_details or {}).get("reason", ""),
        event.reported_by or (event.raw_details or {}).get("reported_by", ""),
        evidence_by_key.get(ckey, []),
        checks_by_key.get(ckey, []),
        (analysis or {}).get("failure_mode", ""),
    )
    # Class-aware paper bonus: explicit regulator quote = full weight;
    # deductive (Category 2) scales with proxy confidence; none = 0.
    if pa["class"] == "explicit":
        paper_bonus = 30
    elif pa["class"] == "deductive":
        paper_bonus = round(20 * pa["confidence"] / 100)
    else:
        paper_bonus = 0
    mandate_flags = [k for k in ('violates_rule_96', 'violates_sub_rule_7', 'violates_schedule_h2') if analysis.get(k)]
    mandate_bonus = 20 if (mandate_flags and event.event_date and event.event_date >= MANDATE_START) else 0
    recency = recency_weight(event.event_date)
    repeat_bonus = repeat_offender_bonus(prior)
    new_score = round((base + paper_bonus + mandate_bonus) * recency) + repeat_bonus

    event.paper_evidence_class = pa["class"]
    event.paper_confidence = pa["confidence"]
    event.paper_proxies = pa["proxies"]
    event.score = new_score

    return {
        "event_id": str(event.event_id),
        "regulator": event.regulator,
        "event_type": event.event_type,
        "score": new_score,
        "company_name": clean_company_name((event.raw_details or {}).get('manufacturer', '')),
        "llm_analysis": analysis,
        "raw_details": event.raw_details or {},
        "event_date": str(event.event_date) if event.event_date else "",
        "reporting_source": event.reporting_source or (event.raw_details or {}).get("reporting_source", ""),
        "reported_by": event.reported_by or (event.raw_details or {}).get("reported_by", ""),
        "paper_assessment": pa,
        "score_breakdown": {
            "base": base,
            "paper_bonus": paper_bonus,
            "paper_bonus_class": pa["class"],
            "mandate_bonus": mandate_bonus,
            "mandate_flags": mandate_flags,
            "recency_weight": recency,
            "repeat_offender_bonus": repeat_bonus,
            "prior_events": prior,
        },
        "enrichment": {
            "checks": latest_checks,
            "evidence": [
                {
                    "source": e.source,
                    "firm_name": e.firm_name,
                    "finding_date": str(e.finding_date) if e.finding_date else "",
                    "url": e.url or "",
                    "paper_qms_score": e.paper_qms_score or 0,
                    "evidence_quote": e.evidence_quote or "",
                    "is_explicit": bool((e.paper_qms_score or 0) > 0),
                }
                for e in evidence_by_key.get(ckey, [])
            ],
        },
    }

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
            cards = [_build_signal_card(e, counts, checks_by_key, evidence_by_key)
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

        response = [_build_signal_card(e, counts, checks_by_key, evidence_by_key)
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
    """Retrieve stored web evidence for a record."""
    evidence = db.query(WebEvidence).filter(
        WebEvidence.event_id == event_id
    ).order_by(WebEvidence.relevance_score.desc()).all()
    
    return {
        "event_id": event_id,
        "evidence": [
            {
                "id": str(e.id),
                "title": e.title,
                "url": e.url,
                "source": e.source,
                "published_date": str(e.published_date) if e.published_date else None,
                "snippet": e.snippet,
                "classification": e.classification or {},
                "relevance_score": e.relevance_score,
                "fetch_status": e.fetch_status,
                "fetched_at": str(e.fetched_at) if e.fetched_at else None
            }
            for e in evidence
        ]
    }
