from temporalio import activity, workflow
from datetime import timedelta, datetime
import asyncio

with workflow.unsafe.imports_passed_through():
    import os
    from sqlalchemy import func
    from db_setup import SessionLocal, RegulatoryEvidence, EnrichmentCheck
    from adapters import REGULATORY_SOURCES
    from cognitive_engine import analyze_regulatory_finding
    from company_names import clean_company_name, looks_like_company, clean_company_names_batch, strip_legal_suffix
    from temporal_tasks import mfr_key

PAPER_QMS_WEIGHT = 40


def _save_findings(findings, checks) -> tuple[int, int]:
    """Persist evidence rows + one enrichment_checks row per firm (recorded
    even when a firm yields no findings)."""
    inserted = 0
    skipped = 0
    inserted_by_mfr = {}
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
                mfr_key=f.mfr_key or (f.firm_name or "").strip().lower(),
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
            inserted_by_mfr[f.mfr_key] = inserted_by_mfr.get(f.mfr_key, 0) + 1
        for c in checks:
            if inserted_by_mfr.get(c["mfr_key"]):
                c = {**c, "inserted_count": inserted_by_mfr[c["mfr_key"]]}
            db.add(EnrichmentCheck(**c))
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
    skipped_firms = []

    plans = []      # (search_name, raw)
    needs_llm = []
    for raw in firm_names:
        search_name = clean_company_name(raw)
        if search_name and looks_like_company(search_name):
            plans.append((search_name, raw))
        else:
            needs_llm.append(raw)
    if needs_llm:
        try:
            llm_out = await asyncio.to_thread(clean_company_names_batch, needs_llm)
            for raw, name in zip(needs_llm, llm_out):
                if name and looks_like_company(name, llm_trusted=True):
                    plans.append((name, raw))
        except Exception as e:  # noqa: BLE001
            errors.append(f"llm_clean: {type(e).__name__}: {e}")
    skipped_firms = [raw for raw in firm_names if not any(raw == r for _, r in plans)]

    # per-firm stats keyed by mfr_key
    raw_by_key = {}
    firm_stats = {}
    for search_name, raw in plans:
        key = mfr_key(raw)
        raw_by_key[key] = raw
        firm_stats[key] = {"searched": search_name, "findings": 0, "paper": 0,
                           "error": ""}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            seen = set()
            for search_name, raw in plans:
                if search_name.lower() in seen:
                    continue
                seen.add(search_name.lower())
                key = mfr_key(raw)
                try:
                    queries = [search_name]
                    stripped = strip_legal_suffix(search_name)
                    if stripped and stripped.lower() != search_name.lower():
                        queries.append(stripped)
                    found = []
                    for query in queries:
                        found = await adapter.search(browser, query)
                        if found:
                            break
                    for f in found:
                        f.mfr_key = key
                        if classify:
                            verdict = await asyncio.to_thread(
                                analyze_regulatory_finding, f.evidence_text, search_name)
                            f.classification = verdict
                            f.evidence_quote = verdict.get("evidence_quote", "")
                            f.paper_qms_score = PAPER_QMS_WEIGHT \
                                if verdict.get("is_paper_qms") else 0
                    findings.extend(found)
                    firm_stats[key]["findings"] += len(found)
                    firm_stats[key]["paper"] += sum(1 for f in found if f.paper_qms_score)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{search_name}: {type(e).__name__}: {e}")
                    firm_stats[key]["error"] = f"{type(e).__name__}: {e}"
        finally:
            await browser.close()

    checks = []
    for key, stats in firm_stats.items():
        checks.append({
            "mfr_key": key,
            "source": source,
            "searched_name": stats["searched"],
            "findings_count": stats["findings"],
            "inserted_count": 0,
            "paper_qms_count": stats["paper"],
            "status": "error" if stats["error"] else "completed",
            "error": stats["error"],
        })
    for raw in skipped_firms:
        checks.append({
            "mfr_key": mfr_key(raw),
            "source": source,
            "searched_name": "",
            "findings_count": 0,
            "inserted_count": 0,
            "paper_qms_count": 0,
            "status": "skipped",
            "error": "no valid company name",
        })

    inserted, skipped = await asyncio.to_thread(_save_findings, findings, checks)
    return {
        "source": source,
        "firms": firm_names,
        "searched": [name for name, _ in plans],
        "findings": len(findings),
        "inserted": inserted,
        "skipped": skipped,
        "paper_qms_findings": sum(1 for f in findings if f.paper_qms_score),
        "skipped_firms": skipped_firms,
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
