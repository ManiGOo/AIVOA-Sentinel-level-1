"""Lead research query tools."""
from db_setup import SessionLocal, CompanyLead
from mcp_server.tools.signals import _group_key


def get_lead(company_name: str) -> dict:
    """Retrieve researched lead data for a company: decision makers, contacts,
    hiring, activity signals, QMS triggers, website and status.

    Args:
        company_name: Company name or key (e.g. 'R.P. Biotech Pvt. Ltd').
            Matches the company used on the Leads page.
    """
    db = SessionLocal()
    try:
        key = _group_key(company_name)
        row = db.query(CompanyLead).filter(CompanyLead.company_key == key).first()
        if row is None:
            rows = db.query(CompanyLead).all()
            for r in rows:
                if key and (key == (r.company_key or "") or key in (r.company_key or "") or (r.company_key or "") in key):
                    row = r
                    break
            if row is None:
                for r in rows:
                    if (r.company_name or "").lower().strip() == company_name.lower().strip():
                        row = r
                        break
        if row is None:
            return {"error": "No researched lead found for this company. Research it first via the Leads page."}
        return {
            "company_key": row.company_key,
            "company_name": row.company_name,
            "company_status": row.company_status,
            "website": row.website,
            "linkedin_url": row.linkedin_url,
            "decision_makers": row.decision_makers or [],
            "hiring": row.hiring or [],
            "hiring_news": row.hiring_news or [],
            "intent_signals": row.intent_signals or [],
            "trigger_events": row.trigger_events or [],
            "activity_summary": row.activity_summary,
            "scraped_data": row.scraped_data or {},
            "corporate_registry": row.corporate_registry or {},
            "status": row.status,
        }
    finally:
        db.close()
