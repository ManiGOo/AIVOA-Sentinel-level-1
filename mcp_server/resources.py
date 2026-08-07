"""MCP resources — read-only data exposed via URI templates."""
import json
from db_setup import SessionLocal, RegulatoryEvent, WebEvidence


def get_events() -> str:
    """List all regulatory events (summary view)."""
    db = SessionLocal()
    try:
        events = db.query(RegulatoryEvent).order_by(RegulatoryEvent.score.desc()).all()
        items = []
        for e in events:
            items.append({
                "event_id": str(e.event_id),
                "regulator": e.regulator,
                "event_type": e.event_type,
                "score": e.score,
                "company_name": (e.raw_details or {}).get("manufacturer", ""),
                "drug_name": (e.raw_details or {}).get("drug_name", ""),
                "event_date": str(e.event_date) if e.event_date else "",
                "paper_evidence_class": e.paper_evidence_class or "",
            })
        return json.dumps({"total": len(items), "items": items}, default=str)
    finally:
        db.close()


def get_event(event_id: str) -> str:
    """Single regulatory event detail."""
    db = SessionLocal()
    try:
        e = db.query(RegulatoryEvent).filter(RegulatoryEvent.event_id == event_id).first()
        if not e:
            return json.dumps({"error": "Event not found"})
        return json.dumps({
            "event_id": str(e.event_id),
            "regulator": e.regulator,
            "event_type": e.event_type,
            "score": e.score,
            "company_name": (e.raw_details or {}).get("manufacturer", ""),
            "drug_name": (e.raw_details or {}).get("drug_name", ""),
            "batch_no": (e.raw_details or {}).get("batch_no", ""),
            "reason": (e.raw_details or {}).get("reason", ""),
            "event_date": str(e.event_date) if e.event_date else "",
            "llm_analysis": e.llm_analysis or {},
            "raw_details": e.raw_details or {},
            "paper_evidence_class": e.paper_evidence_class or "",
            "paper_confidence": e.paper_confidence or 0,
        }, default=str)
    finally:
        db.close()


def get_event_web_evidence(event_id: str) -> str:
    """Web evidence for a specific regulatory event."""
    db = SessionLocal()
    try:
        evidence = db.query(WebEvidence).filter(
            WebEvidence.event_id == event_id
        ).order_by(WebEvidence.relevance_score.desc()).all()
        items = [
            {
                "id": str(e.id),
                "title": e.title,
                "url": e.url,
                "source": e.source,
                "relevance_score": e.relevance_score,
                "classification": e.classification or {},
                "snippet": e.snippet,
            }
            for e in evidence
        ]
        return json.dumps({"event_id": event_id, "count": len(items), "evidence": items}, default=str)
    finally:
        db.close()


def get_config() -> str:
    """App configuration."""
    import os
    return json.dumps({
        "view_only": os.getenv("VIEW_ONLY", "0").strip().lower() in ("1", "true", "yes", "on"),
        "version": "1.0.0",
    })
