from temporalio import activity, workflow
from datetime import timedelta, datetime
import asyncio

with workflow.unsafe.imports_passed_through():
    import os
    from sqlalchemy import func
    from db_setup import SessionLocal, RegulatoryEvidence
    from adapters import REGULATORY_SOURCES
    from cognitive_engine import analyze_regulatory_finding

PAPER_QMS_WEIGHT = 40


def _save_findings(findings) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    db = SessionLocal()
    try:
        for f in findings:
            existing = db.query(RegulatoryEvidence).filter(
                RegulatoryEvidence.source == f.source,
                RegulatoryEvidence.firm_name == f.firm_name,
                func.coalesce(RegulatoryEvidence.url, "") == f.url,
            ).first()
            if existing:
                skipped += 1
                continue
            db.add(RegulatoryEvidence(
                source=f.source,
                firm_name=f.firm_name,
                mfr_key=(f.firm_name or "").strip().lower(),
                finding_date=datetime.fromisoformat(f.finding_date).date()
                if isinstance(f.finding_date, str) and f.finding_date else None,
                url=f.url,
                evidence_text=f.evidence_text,
                classification=f.classification,
                paper_qms_score=f.paper_qms_score,
                evidence_quote=f.evidence_quote,
                fetched_at=datetime.utcnow(),
            ))
            inserted += 1
        db.commit()
    finally:
        db.close()
    return inserted, skipped


@activity.defn
async def fetch_external_evidence(firm_names: list[str], source: str = "fda",
                                  classify: bool = True) -> dict:
    """Run one browser session across the given firms against one source.

    Launches a single Chromium per call (browser startup is heavy, so this is
    a background Temporal activity, not an API call). Classifies each finding
    via Groq and persists to ``sdr_data.regulatory_evidence``.
    """
    from playwright.async_api import async_playwright

    adapter_cls = REGULATORY_SOURCES.get(source)
    if not adapter_cls:
        raise ValueError(f"Unknown regulatory source '{source}'")
    adapter = adapter_cls()

    findings = []
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for firm in firm_names:
                try:
                    found = await adapter.search(browser, firm)
                    for f in found:
                        if classify:
                            verdict = await asyncio.to_thread(
                                analyze_regulatory_finding, f.evidence_text, firm)
                            f.classification = verdict
                            f.evidence_quote = verdict.get("evidence_quote", "")
                            f.paper_qms_score = PAPER_QMS_WEIGHT \
                                if verdict.get("is_paper_qms") else 0
                    findings.extend(found)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{firm}: {type(e).__name__}: {e}")
        finally:
            await browser.close()

    inserted, skipped = await asyncio.to_thread(_save_findings, findings)
    return {
        "source": source,
        "firms": firm_names,
        "findings": len(findings),
        "inserted": inserted,
        "skipped": skipped,
        "paper_qms_findings": sum(1 for f in findings if f.paper_qms_score),
        "errors": errors,
    }


@workflow.defn
class EnrichmentWorkflow:
    def __init__(self):
        self._total = 0
        self._processed = 0
        self._source = ""
        self._finished = False
        self._errors = []

    @workflow.query
    def progress(self) -> dict:
        return {
            "total": self._total,
            "processed": self._processed,
            "source": self._source,
            "finished": self._finished,
            "errors": list(self._errors),
        }

    @workflow.run
    async def run(self, firm_names: list[str], source: str = "fda") -> dict:
        self._total = len(firm_names)
        self._processed = 0
        self._source = source
        self._finished = False
        self._errors = []

        results = {}
        batch_size = 3
        batches = [firm_names[i:i + batch_size]
                   for i in range(0, self._total, batch_size)]
        for batch in batches:
            result = await workflow.execute_activity(
                fetch_external_evidence,
                args=[batch, source],
                start_to_close_timeout=timedelta(minutes=20),
            )
            results[f"batch_{self._processed // batch_size + 1}"] = result
            self._processed += len(batch)
            self._errors.extend(result.get("errors", []))
            await workflow.sleep(timedelta(seconds=10))

        self._finished = True
        return results
