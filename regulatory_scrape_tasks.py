"""Bulk regulatory record scraping (FDA warning letters, EudraGMDP GMP
statements) + per-company linking.

Two-stage design chosen for the full-pull:
  1. ``RegulatoryFullPullWorkflow`` scrapes EVERY public record for a date
     range (no firm filter) into the ``scraped_regulatory_records`` staging
     table. Scraping is decoupled from classification so it never gets
     rate-limited by per-company LLM calls.
  2. ``ScrapedRecordCheckWorkflow`` runs the on-demand MCP/API check for one
     company: fuzzy-matches staged records, fetches full letter/statement
     bodies, classifies via Groq, and copies matched rows into
     ``regulatory_evidence`` (+ ``enrichment_checks``).
"""
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import asyncio
    import re
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db_setup import (
        SessionLocal,
        ScrapedRegulatoryRecord,
        RegulatoryEvidence,
        EnrichmentCheck,
    )
    from adapters import REGULATORY_SOURCES
    from cognitive_engine import analyze_regulatory_finding
    from company_names import clean_company_name
    from temporal_tasks import mfr_key
    from enrichment_tasks import PAPER_QMS_WEIGHT

LEGAL_STOP = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation",
    "pvt", "private", "co", "company", "ind", "industries", "industry",
    "technologies", "technology", "pharma", "pharmaceuticals",
    "pharmaceutical", "laboratories", "laboratory", "labs", "lab",
    "group", "holdings", "international", "biotec", "biosciences",
}
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _significant_tokens(name: str) -> set[str]:
    tokens = set(_TOKEN_RE.findall(name.lower()))
    return tokens - LEGAL_STOP


def _firm_similarity(a: str, b: str) -> float:
    """Overlap score between two firm names on significant tokens.

    Returns 1.0 when one name's significant tokens are contained in the
    other's, else the Dice coefficient on the significant token sets.
    """
    ta = _significant_tokens(a)
    tb = _significant_tokens(b)
    if not ta or not tb:
        return 0.0
    if ta <= tb or tb <= ta:
        return 1.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    return 2 * inter / (len(ta) + len(tb))


# ---------------------------------------------------------------------------
# Activities — stage 1: full pull into the staging table
# ---------------------------------------------------------------------------

def _finding_to_dict(f) -> dict:
    return {
        "source": f.source,
        "firm_name": f.firm_name,
        "finding_date": f.finding_date,
        "url": f.url,
        "subject": f.subject,
        "evidence_text": f.evidence_text,
    }


@activity.defn
async def scrape_regulatory_records(source: str = "fda",
                                    from_date: str = "2022-01-01",
                                    to_date: str = "2026-12-31",
                                    limit: int = 10000) -> dict:
    """Bulk-pull raw records from one public source over a date range.

    FDA is a plain-HTTP DataTables export (no browser); EudraGMDP requires a
    Playwright session for the date-range form + per-statement drilldowns.
    Returns serializable record dicts for ``save_scraped_records``.
    """
    adapter_cls = REGULATORY_SOURCES.get(source)
    if not adapter_cls:
        raise ValueError(f"Unknown regulatory source '{source}'")
    adapter = adapter_cls()
    source_canonical = adapter.source

    records = []
    if source == "fda":
        findings = await asyncio.to_thread(
            adapter.scrape_all, from_date, to_date, 100)
        records = [_finding_to_dict(f) for f in findings]
    else:
        from playwright.async_api import async_playwright
        findings = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                findings = await adapter.scrape_all(
                    browser, from_date, to_date, limit,
                    heartbeat=lambda d: activity.heartbeat(d))
            finally:
                await browser.close()
        records = [_finding_to_dict(f) for f in findings]

    return {
        "source": source_canonical,
        "from_date": from_date,
        "to_date": to_date,
        "count": len(records),
        "records": records,
    }


@activity.defn
async def save_scraped_records(records: list[dict]) -> dict:
    """Upsert raw records into the staging table (dedup on source+url)."""
    if not records:
        return {"inserted": 0, "skipped": 0}
    db = SessionLocal()
    inserted = 0
    try:
        rows = []
        for r in records:
            rows.append({
                "source": r.get("source") or "FDA",
                "firm_name": (r.get("firm_name") or "").strip() or None,
                "finding_date": r.get("finding_date"),
                "url": r.get("url") or "",
                "subject": r.get("subject") or "",
                "evidence_text": r.get("evidence_text") or "",
                "status": "raw",
            })
        stmt = pg_insert(ScrapedRegulatoryRecord).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[ScrapedRegulatoryRecord.source,
                            ScrapedRegulatoryRecord.url])
        result = db.execute(stmt)
        db.commit()
        inserted = result.rowcount or 0
    finally:
        db.close()
    return {"inserted": inserted, "skipped": max(0, len(records) - inserted)}


# ---------------------------------------------------------------------------
# Activities — stage 2: per-company check against the staging table
# ---------------------------------------------------------------------------

@activity.defn
async def link_scraped_records_for_firm(firm_name: str,
                                        source: str = "fda") -> dict:
    """Check whether we already scraped this firm and link the matches.

    Fuzzy-matches ``firm_name`` against ``scraped_regulatory_records`` for the
    source, fetches the full letter/statement body for FDA rows (cached back
    into staging), classifies each finding via Groq, and copies it into
    ``regulatory_evidence`` + one ``enrichment_checks`` row. Recorded even when
    there are no matches so cards can show 'checked, no data'.
    """
    adapter_cls = REGULATORY_SOURCES.get(source)
    if not adapter_cls:
        raise ValueError(f"Unknown regulatory source '{source}'")
    adapter = adapter_cls()
    source_canonical = adapter.source
    key = mfr_key(firm_name)
    company_key = clean_company_name(firm_name).strip().lower() or key
    search_name = clean_company_name(firm_name)

    db = SessionLocal()
    findings = []
    matched_urls = []
    try:
        staged = db.query(ScrapedRegulatoryRecord).filter(
            ScrapedRegulatoryRecord.source == source_canonical,
            ScrapedRegulatoryRecord.status == "raw",
        ).all()
        for rec in staged:
            if _firm_similarity(rec.firm_name or "", firm_name) < 0.6:
                continue
            matched_urls.append(rec)
            evidence = rec.evidence_text or ""
            if source == "fda":
                body = adapter.fetch_letter_body(rec.url)
                if body:
                    evidence = body
                    rec.evidence_text = body
                    db.add(rec)
            findings.append({
                "source": source_canonical,
                "firm_name": rec.firm_name or "",
                "mfr_key": key,
                "company_key": company_key,
                "finding_date": rec.finding_date,
                "url": rec.url or "",
                "evidence_text": evidence,
                "evidence_quote": "",
                "subject": rec.subject or "",
                "classification": None,
                "paper_qms_score": 0,
            })
        db.commit()
    finally:
        db.close()

    # Classify the matched findings via Groq (paper-QMS detection).
    paper_count = 0
    for f in findings:
        if not f["evidence_text"]:
            continue
        verdict = await asyncio.to_thread(
            analyze_regulatory_finding, f["evidence_text"], search_name)
        f["classification"] = verdict
        f["evidence_quote"] = verdict.get("evidence_quote", "")
        f["paper_qms_score"] = PAPER_QMS_WEIGHT if verdict.get("is_paper_qms") else 0
        paper_count += 1 if f["paper_qms_score"] else 0

    checks = [{
        "mfr_key": key,
        "company_key": company_key,
        "source": source,
        "searched_name": search_name,
        "findings_count": len(findings),
        "inserted_count": 0,
        "paper_qms_count": paper_count,
        "status": "completed",
        "error": "",
    }]

    # Upsert into regulatory_evidence (refresh stale rows with the freshly
    # fetched body + classification) + one enrichment_checks row.
    from datetime import datetime as _dt
    inserted = 0
    updated = 0
    db = SessionLocal()
    try:
        for f in findings:
            existing = db.query(RegulatoryEvidence).filter(
                RegulatoryEvidence.source == f["source"],
                RegulatoryEvidence.firm_name == f["firm_name"],
                func.coalesce(RegulatoryEvidence.url, "") == f["url"],
            ).first()
            if existing:
                if f["evidence_text"]:
                    existing.evidence_text = f["evidence_text"]
                if f["classification"]:
                    existing.classification = f["classification"]
                if f["evidence_quote"]:
                    existing.evidence_quote = f["evidence_quote"]
                if f["paper_qms_score"]:
                    existing.paper_qms_score = f["paper_qms_score"]
                existing.mfr_key = f["mfr_key"] or existing.mfr_key
                existing.company_key = f["company_key"] or existing.company_key
                db.add(existing)
                updated += 1
            else:
                db.add(RegulatoryEvidence(
                    source=f["source"],
                    firm_name=f["firm_name"],
                    mfr_key=f["mfr_key"],
                    company_key=f["company_key"],
                    finding_date=f["finding_date"],
                    url=f["url"],
                    evidence_text=f["evidence_text"],
                    classification=f["classification"],
                    paper_qms_score=f["paper_qms_score"],
                    evidence_quote=f["evidence_quote"],
                    fetched_at=_dt.utcnow(),
                ))
                inserted += 1
        for c in checks:
            existing = db.query(EnrichmentCheck).filter(
                EnrichmentCheck.company_key == c["company_key"],
                EnrichmentCheck.source == c["source"],
            ).first()
            if existing:
                existing.findings_count = c["findings_count"]
                existing.inserted_count = c["inserted_count"]
                existing.paper_qms_count = c["paper_qms_count"]
                existing.status = c["status"]
                existing.error = c["error"]
                existing.searched_name = c["searched_name"] or existing.searched_name
                existing.mfr_key = c["mfr_key"] or existing.mfr_key
                existing.checked_at = _dt.utcnow()
            else:
                db.add(EnrichmentCheck(**c))
        db.commit()
    finally:
        db.close()
    skipped = max(0, len(findings) - inserted)

    # Mark matched staging rows as linked so repeated checks don't rework them.
    db = SessionLocal()
    try:
        for rec in matched_urls:
            rec.status = "linked"
            db.add(rec)
        db.commit()
    finally:
        db.close()

    return {
        "source": source,
        "firm_name": firm_name,
        "search_name": search_name,
        "mfr_key": key,
        "company_key": company_key,
        "matched": len(findings),
        "inserted": inserted,
        "skipped": skipped,
        "paper_qms_findings": paper_count,
        "evidence": [
            {
                "firm_name": f["firm_name"],
                "finding_date": f["finding_date"].isoformat()
                if hasattr(f["finding_date"], "isoformat") else f["finding_date"],
                "url": f["url"],
                "subject": f["subject"],
                "is_paper_qms": bool((f["classification"] or {}).get("is_paper_qms")),
            }
            for f in findings
        ],
    }


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

@workflow.defn
class RegulatoryFullPullWorkflow:
    """Stage 1: scrape every record from a public source into staging."""

    def __init__(self):
        self._phase = "idle"
        self._total = 0
        self._processed = 0
        self._finished = False
        self._inserted = 0
        self._skipped = 0
        self._errors = []

    @workflow.query
    def progress(self) -> dict:
        return {
            "phase": self._phase,
            "total": self._total,
            "processed": self._processed,
            "finished": self._finished,
            "inserted": self._inserted,
            "skipped": self._skipped,
            "errors": list(self._errors),
        }

    @workflow.run
    async def run(self, source: str = "fda",
                  from_date: str = "2022-01-01",
                  to_date: str = "2026-12-31",
                  max_records: int = 10000) -> dict:
        self._phase = "scraping"
        try:
            result = await workflow.execute_activity(
                scrape_regulatory_records,
                args=[source, from_date, to_date, max_records],
                start_to_close_timeout=timedelta(minutes=60),
                heartbeat_timeout=timedelta(minutes=10),
            )
        except Exception as e:  # noqa: BLE001
            self._errors.append(f"scrape: {type(e).__name__}: {e}")
            self._finished = True
            return {"source": source, "errors": list(self._errors),
                    "count": 0, "inserted": 0, "skipped": 0}

        records = result.get("records", [])
        self._total = len(records)
        self._phase = "saving"
        chunk_size = 250
        for i in range(0, self._total, chunk_size):
            chunk = records[i:i + chunk_size]
            try:
                saved = await workflow.execute_activity(
                    save_scraped_records,
                    args=[chunk],
                    start_to_close_timeout=timedelta(minutes=5),
                )
                self._inserted += saved.get("inserted", 0)
                self._skipped += saved.get("skipped", 0)
            except Exception as e:  # noqa: BLE001
                self._errors.append(f"save chunk {i}: {type(e).__name__}: {e}")
            self._processed += len(chunk)
            await workflow.sleep(timedelta(seconds=1))

        self._phase = "done"
        self._finished = True
        return {
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
            "count": self._total,
            "inserted": self._inserted,
            "skipped": self._skipped,
            "errors": list(self._errors),
        }


@workflow.defn
class ScrapedRecordCheckWorkflow:
    """Stage 2: check one firm against the scraped staging records."""

    def __init__(self):
        self._finished = False
        self._source = ""

    @workflow.query
    def progress(self) -> dict:
        return {"source": self._source, "finished": self._finished}

    @workflow.run
    async def run(self, firm_name: str, source: str = "all") -> dict:
        sources = ["fda", "eudragmdp"] if source == "all" else [source]
        results = {}
        for src in sources:
            self._source = src
            results[src] = await workflow.execute_activity(
                link_scraped_records_for_firm,
                args=[firm_name, src],
                start_to_close_timeout=timedelta(minutes=15),
            )
        self._finished = True
        return results
