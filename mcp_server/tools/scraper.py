"""Workflow trigger tools — scraper, enrichment, web evidence."""
import os
from datetime import datetime

from temporalio.client import Client
from temporalio.exceptions import FailureError

from db_setup import SessionLocal, RegulatoryEvent
from sqlalchemy import func


TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
VIEW_ONLY = os.getenv("VIEW_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")


def _mfr_key(mfr: str) -> str:
    """Normalized manufacturer key (mirrors temporal_tasks.mfr_key)."""
    if not mfr:
        return ""
    low = mfr.strip().lower()
    skip = {"under investigation", "not disclosed", "na", "n/a", "", "data not available"}
    if low in skip:
        return ""
    return low


def _top_manufacturers(limit: int = 50) -> list[str]:
    db = SessionLocal()
    try:
        mfr_expr = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        rows = db.query(
            mfr_expr.label('mfr'),
            func.count(RegulatoryEvent.event_id).label('cnt'),
        ).group_by(mfr_expr).order_by(func.count(RegulatoryEvent.event_id).desc()).all()
        firms = []
        for mfr, _cnt in rows:
            if _mfr_key(mfr):
                firms.append(mfr)
            if len(firms) >= limit:
                break
        return firms
    finally:
        db.close()


async def trigger_scraper(year: str | None = None, full: bool = False) -> dict:
    """Start the CDSCO scraper workflow. Pass year='2026' or full=True for backfill."""
    if VIEW_ONLY:
        return {"error": "Scraper execution disabled (view-only mode)."}
    try:
        client = await Client.connect(TEMPORAL_HOST)
        args = [] if full else [year or str(datetime.utcnow().year)]
        workflow_id = f"cdsco-scraper-mcp-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        handle = await client.start_workflow(
            "CDSCOScraperWorkflow",
            *args,
            id=workflow_id,
            task_queue="scraper-task-queue",
        )
        return {"status": "started", "workflow_id": handle.id, "args": args}
    except Exception as e:
        return {"error": str(e)}


async def get_scraper_status() -> dict:
    """Get live scraper workflow progress and ETA."""
    if VIEW_ONLY:
        return {"status": "disabled"}
    try:
        client = await Client.connect(TEMPORAL_HOST)
        found = None
        async for e in client.list_workflows(
            query="WorkflowType = 'CDSCOScraperWorkflow' AND ExecutionStatus = 'Running'",
            limit=1,
        ):
            found = e
        if not found:
            return {"status": "idle", "total": 0, "processed": 0, "percent": 0}

        handle = client.get_workflow_handle(found.id)
        progress = await handle.query("progress")
        total = progress.get("total", 0)
        processed = progress.get("processed", 0)
        started = progress.get("started_at")
        eta_seconds = None
        if started and processed > 0:
            started_dt = datetime.fromisoformat(started)
            elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
            rate = processed / elapsed if elapsed > 0 else 0
            if rate > 0 and total > processed:
                eta_seconds = (total - processed) / rate
        return {
            "status": "running" if not progress.get("finished") else "finished",
            "workflow_id": found.id,
            "total": total,
            "processed": processed,
            "percent": round(processed / total * 100) if total else 0,
            "eta_seconds": round(eta_seconds) if eta_seconds else None,
        }
    except Exception as e:
        return {"error": str(e)}


async def trigger_enrichment(
    source: str = "fda",
    limit: int = 50,
    firms: list[str] | None = None,
) -> dict:
    """Start enrichment workflows. source='fda'|'eudragmdp'|'all'."""
    if VIEW_ONLY:
        return {"error": "Enrichment disabled (view-only mode)."}
    if source not in ("fda", "eudragmdp", "all"):
        return {"error": "source must be 'fda', 'eudragmdp', or 'all'"}
    firm_list = firms or _top_manufacturers(limit)
    if not firm_list:
        return {"status": "no manufacturers to enrich"}
    sources = ["fda", "eudragmdp"] if source == "all" else [source]
    try:
        client = await Client.connect(TEMPORAL_HOST)
        workflow_ids = []
        for src in sources:
            workflow_id = f"enrichment-mcp-{src}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "EnrichmentWorkflow",
                args=[firm_list, src],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {
            "status": "started",
            "firm_count": len(firm_list),
            "sources": sources,
            "workflow_ids": workflow_ids,
        }
    except Exception as e:
        return {"error": str(e)}


async def check_single_firm(event_id: str, source: str = "all") -> dict:
    """On-demand enrichment for a single signal card's manufacturer."""
    if VIEW_ONLY:
        return {"error": "Enrichment disabled (view-only mode)."}
    if source not in ("fda", "eudragmdp", "all"):
        return {"error": "source must be 'fda', 'eudragmdp', or 'all'"}
    db = SessionLocal()
    try:
        event = db.query(RegulatoryEvent).filter(
            RegulatoryEvent.event_id == event_id).first()
        if not event:
            return {"error": "Signal event not found"}
        mfr = (event.raw_details or {}).get("manufacturer", "")
        if not mfr or not _mfr_key(mfr):
            return {"error": "Manufacturer is a placeholder or missing"}
    finally:
        db.close()

    sources = ["fda", "eudragmdp"] if source == "all" else [source]
    try:
        client = await Client.connect(TEMPORAL_HOST)
        workflow_ids = []
        for src in sources:
            workflow_id = f"check-mcp-{src}-{event_id[:8]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            handle = await client.start_workflow(
                "EnrichmentWorkflow",
                args=[[mfr], src],
                id=workflow_id,
                task_queue="enrichment-task-queue",
            )
            workflow_ids.append(handle.id)
        return {
            "status": "started",
            "manufacturer": mfr,
            "workflow_ids": workflow_ids,
        }
    except Exception as e:
        return {"error": str(e)}


async def get_enrichment_status(workflow_id: str) -> dict:
    """Poll enrichment workflow state, progress, and results."""
    try:
        client = await Client.connect(TEMPORAL_HOST)
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

        return {"workflow_id": workflow_id, "state": state, "progress": progress, "result": result, "error": error}
    except Exception as e:
        return {"error": str(e)}


async def trigger_web_evidence(event_id: str) -> dict:
    """Start the WebEvidenceWorkflow for a specific record."""
    if VIEW_ONLY:
        return {"error": "Web evidence disabled (view-only mode)."}
    db = SessionLocal()
    try:
        event = db.query(RegulatoryEvent).filter(
            RegulatoryEvent.event_id == event_id).first()
        if not event:
            return {"error": "Signal event not found"}
    finally:
        db.close()
    try:
        client = await Client.connect(TEMPORAL_HOST)
        workflow_id = f"web-evidence-mcp-{event_id[:8]}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        handle = await client.start_workflow(
            "WebEvidenceWorkflow",
            args=[event_id],
            id=workflow_id,
            task_queue="enrichment-task-queue",
        )
        return {"status": "started", "event_id": event_id, "workflow_id": handle.id}
    except Exception as e:
        return {"error": str(e)}
