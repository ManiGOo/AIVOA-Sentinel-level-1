"""FDA openFDA + EMA ePI/UPD API-based regulatory scraping workflows.

Two-stage design matching CDSCO:
  1. ``FDAEScraperWorkflow`` scrapes all public records via official APIs
     (no browser needed) and saves them into ``regulatory_events`` as raw
     rows (llm_analysis={}, score=0).
  2. ``FDAEEnrichmentWorkflow`` runs LLM analysis + scoring over stored
     rows, identical to CDSCOEnrichmentWorkflow but for FDA/EU event types.
"""
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import asyncio
    import requests
    from sqlalchemy import func
    from db_setup import (
        SessionLocal,
        RegulatoryEvent,
    )
    from cognitive_engine import (
        analyze_regulatory_finding,
        classify_failure_modes_batch,
    )
    from company_names import clean_company_name, looks_like_company
    from temporal_tasks import (
        mfr_key,
        recency_weight,
        repeat_offender_bonus,
        calculate_base_score,
        MANDATE_START,
        BACKFILL_BATCH,
    )
    from adapters import API_SOURCES, REGULATORY_SOURCES
    from db_setup import (
        FDAEvent,
        EUEvent,
        RegulatoryEvent,
    )


# ---------------------------------------------------------------------------
# Activities — Stage 1: bulk-pull via official APIs into the separate
# FDA (USA) / EU (EMA) signal areas (NOT merged with CDSCO regulatory_events)
# ---------------------------------------------------------------------------

def _finding_to_event(item: dict, source: str) -> dict:
    """Convert a Finding dict into a row dict for the FDA/EU event tables."""
    return {
        "event_type": item.get("source", source),
        "firm_name": item.get("firm_name", ""),
        "product_name": item.get("subject", ""),
        "finding_date": item.get("finding_date"),
        "url": item.get("url", ""),
        "subject": item.get("subject", ""),
        "evidence_text": item.get("evidence_text", ""),
        "reporting_source": item.get("source", source),
        "raw_details": item,
    }


def _resolve_target_table(source: str):
    """Route a source to its dedicated event table: FDA_* -> fda_events,
    EMA_*/EudraGMDP -> eu_events. Accepts both the API request aliases
    (openfda, ema_epi, ema_upd, eudragmdp) and the tags adapters emit
    (FDA_Drug, FDA_FAERS, FDA_Device, EMA_ePI, EMA_UPD, EudraGMDP)."""
    s = (source or "").lower()
    if s.startswith("fda") or s == "openfda":
        return FDAEvent, "fda_events"
    if s.startswith("ema") or s.startswith("eudra") or s == "eudragmdp" or s == "eu":
        return EUEvent, "eu_events"
    return None, None


@activity.defn
async def scrape_fda_e_records(
    source: str = "openfda",
    from_date: str = "2022-01-01",
    to_date: str = "2026-12-31",
    max_records: int = 10000,
) -> dict:
    """Bulk-pull raw records from a source into record dicts for
    ``save_fda_e_raw``.

    API sources (openfda, ema_epi, ema_upd) are pulled directly. The
    EudraGMDP source requires a Playwright browser session (no anonymous API).
    """
    # EudraGMDP needs a browser; everything else is an API call.
    if source == "eudragmdp":
        from playwright.async_api import async_playwright
        adapter_cls = REGULATORY_SOURCES.get(source)
        if not adapter_cls:
            raise ValueError(f"Unknown source '{source}'")
        adapter = adapter_cls()
        records = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                findings = await adapter.scrape_all(
                    browser, from_date, to_date, max_records,
                    heartbeat=lambda d: activity.heartbeat(d))
            finally:
                await browser.close()
        for f in findings:
            records.append({
                "source": f.source,
                "firm_name": f.firm_name or "",
                "finding_date": f.finding_date,
                "url": f.url,
                "subject": f.subject or "",
                "evidence_text": f.evidence_text or "",
            })
        return {
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
            "count": len(records),
            "records": records,
        }

    adapter_cls = API_SOURCES.get(source)
    if not adapter_cls:
        raise ValueError(f"Unknown API source '{source}'")
    adapter = adapter_cls()

    findings = await adapter.scrape_all(from_date, to_date, max_records)
    records = []
    for f in findings:
        records.append({
            "source": f.source,
            "firm_name": f.firm_name or "",
            "finding_date": f.finding_date,
            "url": f.url,
            "subject": f.subject or "",
            "evidence_text": f.evidence_text or "",
        })

    return {
        "source": source,
        "from_date": from_date,
        "to_date": to_date,
        "count": len(records),
        "records": records,
    }


@activity.defn
async def save_fda_e_raw(records: list[dict]) -> dict:
    """Insert freshly scraped FDA/EU rows with NO AI enrichment yet.

    Rows are routed to their dedicated area: FDA (openfda) -> fda_events,
    EU (ema_*/EudraGMDP) -> eu_events. Saved with llm_analysis={} and
    score=0; the separate FDAEEnrichmentWorkflow fills those in afterwards.
    Dedup on (event_type, firm_name, product_name, source).
    """
    if not records:
        return {"inserted": 0, "skipped": 0}

    db = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        for item in records:
            source = item.get("source", "")
            target_table, table_name = _resolve_target_table(source)
            if target_table is None:
                # Fall back to the India regulatory_events table for anything
                # unexpected (keeps the workflow from dropping data).
                target_table = RegulatoryEvent
                table_name = "regulatory_events"

            firm = item.get("firm_name", "")
            product = item.get("product_name", "")

            if firm or product:
                existing = db.query(target_table).filter(
                    target_table.event_type == source,
                    func.coalesce(target_table.firm_name, "") == firm,
                    func.coalesce(target_table.product_name, "") == product,
                ).first()
                if existing:
                    skipped += 1
                    continue

            event_date = item.get("finding_date")
            parsed_date = (
                __import__("datetime").datetime.strptime(event_date, "%Y-%m-%d").date()
                if event_date
                else __import__("datetime").datetime.utcnow().date()
            )
            if target_table is RegulatoryEvent:
                new_event = RegulatoryEvent(
                    event_type=source,
                    regulator=source.split("_")[0] if "_" in source else source,
                    raw_details=item,
                    llm_analysis={},
                    score=0,
                    reporting_source=source,
                    event_date=parsed_date,
                )
            else:
                new_event = target_table(
                    event_type=source,
                    firm_name=firm,
                    product_name=product,
                    finding_date=parsed_date,
                    url=item.get("url", ""),
                    subject=item.get("subject", ""),
                    evidence_text=item.get("evidence_text", ""),
                    llm_analysis={},
                    score=0,
                    reporting_source=source,
                    event_date=parsed_date,
                    raw_details=item,
                )
            db.add(new_event)
            inserted += 1
        db.commit()
        return {"inserted": inserted, "skipped": skipped}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Activities — Stage 2: LLM enrichment + scoring
# ---------------------------------------------------------------------------

FDA_SOURCES = ("openfda", "FDA_Drug", "FDA_FAERS", "FDA_Device")
EU_SOURCES = ("ema_epi", "ema_upd", "eudragmdp", "EudraGMDP", "EMA_ePI", "EMA_UPD")


def _region_for_source(source: str) -> str:
    s = (source or "").lower()
    if s.startswith("fda") or s == "openfda":
        return "fda"
    if s.startswith("ema") or s.startswith("eudra") or s == "eudragmdp" or s == "eu":
        return "eu"
    if s == "reg":
        return "reg"
    return ""


@activity.defn
async def load_fda_e_enrichment_candidates(
    source_filter: str = "",
    year_start: str = "",
    year_end: str = "",
    only_missing: bool = True,
    limit: int = None,
) -> dict:
    """Load stored FDA/EU rows that still need AI enrichment + scoring.

    Queries the dedicated fda_events / eu_events tables (not regulatory_events).
    Filters by event_type (source) and event_date year range. Each candidate
    carries a ``region`` so the apply step knows which table to update.
    """
    db = SessionLocal()
    try:
        region = _region_for_source(source_filter) if source_filter else ""
        tables = []
        if region == "fda":
            tables = [(FDAEvent, "fda")]
        elif region == "eu":
            tables = [(EUEvent, "eu")]
        elif region == "reg":
            tables = [(RegulatoryEvent, "reg")]
        else:
            tables = [(FDAEvent, "fda"), (EUEvent, "eu")]

        candidates = []
        for table, reg in tables:
            query = db.query(table)
            if source_filter:
                query = query.filter(table.event_type == source_filter)
            if year_start:
                query = query.filter(
                    table.event_date >= __import__("datetime").datetime.strptime(
                        f"{year_start}-01-01", "%Y-%m-%d"
                    ).date()
                )
            if year_end:
                query = query.filter(
                    table.event_date <= __import__("datetime").datetime.strptime(
                        f"{year_end}-12-31", "%Y-%m-%d"
                    ).date()
                )
            rows = query.order_by(table.event_date.asc()).all()
            if only_missing:
                rows = [r for r in rows if not (r.llm_analysis or {})]
            for ev in rows:
                raw = ev.raw_details or {}
                event_date = ev.event_date.isoformat() if ev.event_date else None
                event_type = ev.event_type or ""
                pk = getattr(ev, "fda_event_id", None) or getattr(ev, "eu_event_id", None) \
                    or getattr(ev, "event_id", None)
                candidates.append({
                    "event_id": str(pk),
                    "region": reg,
                    "event_type": event_type,
                    "event_date": event_date,
                    "item": {
                        "firm_name": getattr(ev, "firm_name", "") or raw.get("firm_name", ""),
                        "product_name": getattr(ev, "product_name", "") or raw.get("product_name", ""),
                        "evidence_text": getattr(ev, "evidence_text", "") or raw.get("evidence_text", ""),
                        "event_date": event_date,
                        "event_type": event_type,
                    },
                })
        if limit:
            candidates = candidates[:limit]
        return {"candidates": candidates, "total_rows": len(candidates)}
    finally:
        db.close()


@activity.defn
async def apply_fda_e_enrichment_to_db(data: dict) -> str:
    """Persist one batch of LLM analyses + scores back onto stored FDA/EU rows.

    Routes each item to fda_events / eu_events based on its ``region``
    (carried from the candidate loader). Applies recency weight and
    repeat-offender bonus using the firm_name.
    """
    processed_items = data.get("processed_items", [])
    if not processed_items:
        return "No items to update."

    db = SessionLocal()
    updated = 0
    missing = 0
    try:
        # Prior-event counts per firm across both international tables.
        counts = {}
        for table in (FDAEvent, EUEvent):
            col = func.coalesce(table.firm_name, "")
            pk = table.fda_event_id if table is FDAEvent else table.eu_event_id
            for mfr, cnt in db.query(col.label("mfr"), func.count(pk)).group_by("mfr").all():
                key = mfr_key(mfr)
                if key:
                    counts[key] = counts.get(key, 0) + cnt

        for item in processed_items:
            event_id = item.get("event_id")
            region = item.get("region", "fda")
            if not event_id:
                continue
            if region == "fda":
                table, pk_col = FDAEvent, "fda_event_id"
            elif region == "eu":
                table, pk_col = EUEvent, "eu_event_id"
            else:
                table, pk_col = RegulatoryEvent, "event_id"
            ev = db.query(table).filter(getattr(table, pk_col) == event_id).first()
            if not ev:
                missing += 1
                continue
            raw = ev.raw_details or {}
            firm = getattr(ev, "firm_name", "") or raw.get("firm_name", "")
            ev.llm_analysis = item.get("llm_analysis", {})
            base = item.get("score", 0)
            ev.score = round(base * recency_weight(ev.event_date)) + repeat_offender_bonus(
                counts.get(mfr_key(firm), 0)
            )
            if hasattr(ev, "paper_qms_score"):
                ev.paper_qms_score = item.get("paper_qms_score", 0)
            updated += 1
        db.commit()
        return f"Updated {updated} records (missing {missing})."
    finally:
        db.close()


@activity.defn
async def enrich_fda_e_findings(items: list[dict]) -> dict:
    """Classify each FDA/EU finding for paper-QMS fingerprints via Groq.

    Uses ``analyze_regulatory_finding`` (explicit documentation / data-integrity
    detection), NOT the CDSCO-specific batch analyzer. Returns one result per
    input item aligned by index: {llm_analysis, score, paper_qms_score}.
    """
    results = []
    for item in items:
        firm = item.get("firm_name", "")
        evidence = item.get("evidence_text", "")
        verdict = await asyncio.to_thread(
            analyze_regulatory_finding, evidence, firm)
        is_paper = bool(verdict.get("is_paper_qms"))
        # FDA enforcement / GMP findings score higher than adverse-event reports.
        base = 40 if is_paper else 20
        results.append({
            "llm_analysis": verdict,
            "score": base,
            "paper_qms_score": 40 if is_paper else 0,
        })
    return {"results": results}


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

@workflow.defn
class FDAEScraperWorkflow:
    """Stage 1: scrape every record from API sources into regulatory_events.

    Mirrors CDSCOScraperWorkflow but for openfda/ema_epi/ema_upd.
    No LLM here: AI enrichment is a separate workflow.
    """

    def __init__(self):
        self._total = 0
        self._processed = 0
        self._source = ""
        self._finished = False
        self._warnings = []

    @workflow.query
    def progress(self) -> dict:
        return {
            "total": self._total,
            "processed": self._processed,
            "source": self._source,
            "finished": self._finished,
            "warnings": list(self._warnings),
            "started_at": workflow.info().start_time.isoformat()
            if workflow.info().start_time
            else None,
        }

    @workflow.run
    async def run(
        self,
        source: str = "openfda",
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        max_records: int = 10000,
    ) -> dict:
        self._total = 0
        self._processed = 0
        self._source = source
        self._finished = False
        self._warnings = []

        sources = ["openfda", "ema_epi", "ema_upd", "eudragmdp"] if source == "all" else [source]
        results = {}

        for src in sources:
            self._source = src
            self._warnings.append(f"Scraping {src} from {from_date} to {to_date}...")

            scrape_result = await workflow.execute_activity(
                scrape_fda_e_records,
                args=[src, from_date, to_date, max_records],
                start_to_close_timeout=timedelta(minutes=60),
                heartbeat_timeout=timedelta(minutes=10),
            )

            records = scrape_result.get("records", [])
            total_records = len(records)
            self._total += total_records
            self._warnings.append(
                f"{src}: found {total_records} records"
            )

            # Save raw rows (dedup) in chunks of 250
            chunk_size = 250
            saved_total = 0
            for i in range(0, total_records, chunk_size):
                chunk = records[i : i + chunk_size]
                save_result = await workflow.execute_activity(
                    save_fda_e_raw,
                    args=[chunk],
                    start_to_close_timeout=timedelta(minutes=5),
                )
                saved_total += save_result.get("inserted", 0)
                self._processed += len(chunk)
                await workflow.sleep(timedelta(seconds=1))

            results[src] = {
                "total_found": total_records,
                "inserted": saved_total,
                "from_date": from_date,
                "to_date": to_date,
            }
            self._warnings.append(f"{src}: saved {saved_total} records")

        self._source = ""
        self._finished = True
        return results


@workflow.defn
class FDAEEnrichmentWorkflow:
    """Stage 2: run LLM enrichment + scoring over FDA/EU rows.

    Mirrors CDSCOEnrichmentWorkflow. Loads rows with empty llm_analysis,
    classifies via Groq, computes scores, and updates in place.
    """

    def __init__(self):
        self._phase = "idle"
        self._total = 0
        self._processed = 0
        self._finished = False
        self._warnings = []

    @workflow.query
    def progress(self) -> dict:
        return {
            "phase": self._phase,
            "total": self._total,
            "processed": self._processed,
            "finished": self._finished,
            "warnings": list(self._warnings),
            "started_at": workflow.info().start_time.isoformat()
            if workflow.info().start_time
            else None,
        }

    @workflow.run
    async def run(
        self,
        source_filter: str = "",
        year_start: str = "",
        year_end: str = "",
        only_missing: bool = True,
        limit: int = None,
    ) -> dict:
        self._phase = "loading"
        self._total = 0
        self._processed = 0
        self._finished = False
        self._warnings = []

        loaded = await workflow.execute_activity(
            load_fda_e_enrichment_candidates,
            args=[source_filter, year_start, year_end, only_missing, limit],
            start_to_close_timeout=timedelta(minutes=10),
        )
        candidates = loaded.get("candidates", [])
        self._total = len(candidates)
        self._warnings.append(f"Loaded {len(candidates)} candidates")

        self._phase = "enriching"

        batch_size = 5
        updated = 0
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            items = [c["item"] for c in batch]

            enrich_result = await workflow.execute_activity(
                enrich_fda_e_findings,
                args=[items],
                start_to_close_timeout=timedelta(minutes=5),
            )

            processed_items = []
            for j, res in enumerate(enrich_result.get("results", [])):
                processed_items.append({
                    "event_id": batch[j]["event_id"],
                    "region": batch[j]["region"],
                    "llm_analysis": res["llm_analysis"],
                    "score": res["score"],
                    "paper_qms_score": res["paper_qms_score"],
                })

            await workflow.execute_activity(
                apply_fda_e_enrichment_to_db,
                {"processed_items": processed_items},
                start_to_close_timeout=timedelta(minutes=1),
            )

            updated += len(processed_items)
            self._processed += len(batch)
            await workflow.sleep(timedelta(seconds=20))

        self._phase = "done"
        self._finished = True
        return {
            "candidates_total": len(candidates),
            "updated": updated,
            "warnings": list(self._warnings),
        }
