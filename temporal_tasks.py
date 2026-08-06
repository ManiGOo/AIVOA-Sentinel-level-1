from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from datetime import timedelta, datetime, date
import asyncio
import time

with workflow.unsafe.imports_passed_through():
    import requests
    from bs4 import BeautifulSoup
    from sqlalchemy import func
    from db_setup import SessionLocal, RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck
    from cognitive_engine import analyze_cdsco_failure_batch, classify_failure_modes_batch
    from paper_category import assess_paper_category
    from company_names import clean_company_name

BACKFILL_BATCH = 20

CDSCO_BASE = "https://cdscoonline.gov.in/CDSCO"

_MONTHS = {name: i + 1 for i, name in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
)}

def _tab(event_type: str) -> str:
    return "nsq" if event_type == "NSQ_DRUG" else "spurious"

def parse_reporting_month_year(value) -> str | None:
    """Parses 'JUN-2026' -> '2026-06-01'. Returns None if unparseable."""
    if not value:
        return None
    parts = str(value).strip().split("-")
    if len(parts) != 2:
        return None
    month, year = parts[0].strip().upper(), parts[1].strip()
    if month not in _MONTHS or not year.isdigit():
        return None
    return f"{int(year):04d}-{_MONTHS[month]:02d}-01"

def get_reporting_years(tab: str) -> list:
    res = requests.get(f"{CDSCO_BASE}/reportingYears", params={"tab": tab}, timeout=15)
    res.raise_for_status()
    return [str(y) for y in res.json()]

def get_reporting_months(tab: str, year: str) -> list:
    res = requests.get(f"{CDSCO_BASE}/publicReportingMonths",
                       params={"year": year, "tab": tab}, timeout=15)
    res.raise_for_status()
    return list(res.json())

def fetch_month_records(session, tab: str, month_key: str, retries: int = 3) -> tuple:
    """Fetch one month, retrying with backoff on throttling/timeouts.

    Returns (records, ok). ``ok`` is False when every attempt hit an HTTP
    error or timed out (the caller should warn rather than fail the run).
    """
    endpoint = (f"{CDSCO_BASE}/filteredNsqDrugTable" if tab == "nsq"
                else f"{CDSCO_BASE}/filteredSpuriousDrugTable")
    for attempt in range(retries):
        try:
            res = session.get(endpoint, params={"month": month_key, "source": "All", "tab": tab}, timeout=15)
            if res.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            res.raise_for_status()
            data = res.json()
            return (data.get("aaData") or data.get("data") or []), True
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                break
            time.sleep(2 ** attempt * 2)
    return [], False

def _month_matches(item: dict, month_key: str) -> bool:
    value = str(item.get("dt_reporting_month_year", "")).strip().upper()
    return bool(value) and value == month_key.strip().upper()

def _fetch_month_verified(session, tab: str, month_key: str, attempts: int = 3,
                          empty_retries: int = 2) -> dict:
    """Fetch a month, dropping records that don't belong to it.

    CDSCO intermittently ignores the ``month`` filter (returns records for
    other months) or serves empty pages for months that actually have data.
    Retry in both cases; return {"records", "dropped", "retries", "ok"}.
    """
    last = {"records": [], "dropped": 0, "retries": 0, "ok": True}
    for i in range(attempts):
        records, ok = fetch_month_records(session, tab, month_key)
        matched = [r for r in records if _month_matches(r, month_key)]
        dropped = len(records) - len(matched)
        last = {"records": matched, "dropped": dropped, "retries": i, "ok": ok}
        if not ok:
            return last
        if dropped > 0:
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            return last
        if records:
            return last
        if i < empty_retries:
            time.sleep(3 * (i + 1))
            continue
        return last
    return last

def _parse_record(tab: str, item: dict) -> dict:
    if tab == "nsq":
        return {
            "drug_name": item.get("str_product_name", ""),
            "manufacturer": item.get("str_manufactured_by", ""),
            "batch_no": item.get("str_batch_no", ""),
            "reason": item.get("str_nsq_result", ""),
            "event_date": parse_reporting_month_year(item.get("dt_reporting_month_year")),
            "reporting_source": item.get("str_reporting_source", ""),
            "reported_by": item.get("str_reported_by_lab_or_state", ""),
        }
    return {
        "drug_name": item.get("product_name_from_dtl") or item.get("product_name_from_mst", ""),
        "manufacturer": (item.get("str_spurious_manufacturer_name")
                         or item.get("str_spurious_manufactured_by")
                         or item.get("str_manufactured_by", "")),
        "batch_no": item.get("str_batch_no", ""),
        "reason": item.get("str_nsq_result") or item.get("str_nsq_remarks", ""),
        "event_date": parse_reporting_month_year(item.get("dt_reporting_month_year")),
        "reporting_source": item.get("str_reporting_source", ""),
        "reported_by": item.get("str_reported_by_lab_or_state", ""),
    }

MANDATE_START = date(2026, 1, 1)

def _as_date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None

def calculate_base_score(event_type: str, llm_analysis: dict, event_date=None) -> int:
    score = 0
    if event_type == 'SPURIOUS_DRUG':
        score += 40
    elif event_type == 'NSQ_DRUG':
        score += 20

    if llm_analysis.get('is_paper_failure'):
        score += 30

    # 2026 mandates (Rule 96 / Sub-Rule 7 / Schedule H2) only in force from 2026.
    event_d = _as_date(event_date)
    if event_d and event_d >= MANDATE_START and any([
        llm_analysis.get('violates_rule_96'),
        llm_analysis.get('violates_sub_rule_7'),
        llm_analysis.get('violates_schedule_h2')
    ]):
        score += 20

    return score

def recency_weight(event_date) -> float:
    """Fresh signals are more actionable; older ones decay toward 0.6."""
    event_d = _as_date(event_date)
    if not event_d:
        return 1.0
    age_days = (datetime.utcnow().date() - event_d).days
    if age_days <= 182:      # <= 6 months
        return 1.0
    if age_days <= 365:      # <= 1 year
        return 0.9
    if age_days <= 730:      # <= 2 years
        return 0.8
    if age_days <= 1095:     # <= 3 years
        return 0.7
    return 0.6

def repeat_offender_bonus(prior_event_count: int, per_event: int = 10, cap: int = 30) -> int:
    """A manufacturer with a history of failures is higher risk. Capped at +30."""
    return min(max(prior_event_count, 0) * per_event, cap)

# CDSCO publishes placeholder text (not a real manufacturer) for spurious
# products whose maker is under investigation. These must not feed the
# repeat-offender bonus, or every spurious record inflates the others.
PLACEHOLDER_MANUFACTURERS = {
    "under investigation", "not known", "unknown", "not available",
    "nil", "n/a", "na", "not disclosed",
}

def mfr_key(manufacturer: str) -> str:
    """Normalize a manufacturer string for prior-event counts, or '' if
    it is a CDSCO placeholder (no real entity to track as repeat offender)."""
    if not manufacturer:
        return ""
    norm = manufacturer.strip().lower()
    if norm in PLACEHOLDER_MANUFACTURERS:
        return ""
    return norm

def _probe_cdsco(session, tab: str, year: str, attempts: int = 3) -> str:
    """Verify CDSCO serves clean month-filtered data before a long scrape.

    Aborts with a clear error if the site is degraded (mismatched records,
    HTTP errors, or unreachable months). Returns the probed month key.
    """
    months = get_reporting_months(tab, year)
    if not months:
        raise RuntimeError(
            f"CDSCO probe failed for {tab}/{year}: no reporting months returned.")
    month_key = f"{months[0]}-{year}"
    for i in range(attempts):
        records, ok = fetch_month_records(session, tab, month_key)
        dropped = len([r for r in records if not _month_matches(r, month_key)])
        if ok and dropped == 0:
            return month_key
        time.sleep(3 * (i + 1))
    raise RuntimeError(
        f"CDSCO probe failed for {tab}/{month_key}: site returned degraded or no "
        f"data after {attempts} attempts. Aborting before scrape."
    )

@activity.defn
async def scrape_cdsco_endpoint(event_type: str, year: str = None) -> dict:
    tab = _tab(event_type)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'Referer': 'https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest'
    })

    # A year=None means full backfill across all reporting years.
    years = [str(year)] if year else get_reporting_years(tab)

    _probe_cdsco(session, tab, years[-1])

    events_to_parse = []
    warnings = []
    for y in years:
        months = get_reporting_months(tab, y)
        before = len(events_to_parse)
        for month in months:
            month_key = f"{month}-{y}"
            fetched = _fetch_month_verified(session, tab, month_key)
            events_to_parse.extend(_parse_record(tab, item) for item in fetched["records"])
            if not fetched["ok"]:
                warnings.append(f"{tab}/{month_key}: CDSCO unreachable after retries, skipped month.")
            elif fetched["dropped"]:
                warnings.append(
                    f"{tab}/{month_key}: dropped {fetched['dropped']} records for other months "
                    f"(degraded CDSCO response)."
                )
            elif tab == "nsq" and fetched["retries"] and not fetched["records"]:
                warnings.append(f"{tab}/{month_key}: empty after retries (CDSCO throttling).")
        if len(events_to_parse) == before:
            warnings.append(
                f"{tab}/{y}: scraped {len(months)} months but found 0 records — "
                f"CDSCO likely degraded."
            )

    return {"event_type": event_type, "items": events_to_parse, "years": years,
            "warnings": warnings}

@activity.defn
async def process_batch_with_llm(data: dict) -> dict:
    # data: {"items": [...], "event_type": ...}
    items = data["items"]
    event_type = data["event_type"]
    
    # We will pass the list of items to cognitive_engine.
    # Run in a thread so the blocking Groq HTTP call cannot freeze the
    # worker's event loop (which previously left the activity stuck until
    # the start-to-close timeout redelivered it).
    llm_results = await asyncio.to_thread(analyze_cdsco_failure_batch, items)
    
    processed_items = []
    for i, item in enumerate(items):
        analysis = llm_results.get(str(i), {})
        event_date = item.pop("event_date", None)
        score = calculate_base_score(event_type, analysis, event_date)
        processed_items.append({
            "event_type": event_type,
            "raw_details": item,
            "llm_analysis": analysis,
            "score": score,
            "event_date": event_date
        })
        
    return {"processed_items": processed_items}

@activity.defn
async def save_to_db(data: dict) -> str:
    processed_items = data.get("processed_items", [])
    if not processed_items:
        return "No items to save."
        
    db_session = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        # Prior-event counts per manufacturer (from already-committed rows).
        # Placeholder manufacturers (e.g. "Under Investigation") map to ''
        # so they never accumulate a repeat-offender bonus.
        mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        counts: dict = {}
        for mfr, cnt in db_session.query(
                mfr_col.label('mfr'), func.count(RegulatoryEvent.event_id)).group_by('mfr').all():
            key = mfr_key(mfr)
            if key:
                counts[key] = counts.get(key, 0) + cnt

        for item in processed_items:
            raw = item["raw_details"]
            drug_name = raw.get("drug_name", "")
            manufacturer = raw.get("manufacturer", "")
            batch_no = raw.get("batch_no", "")

            if drug_name or manufacturer or batch_no:
                existing = db_session.query(RegulatoryEvent).filter(
                    RegulatoryEvent.event_type == item["event_type"],
                    func.coalesce(RegulatoryEvent.raw_details['drug_name'].astext, '') == drug_name,
                    func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '') == manufacturer,
                    func.coalesce(RegulatoryEvent.raw_details['batch_no'].astext, '') == batch_no,
                ).first()
                if existing:
                    skipped += 1
                    continue

            event_date = item.get("event_date")
            final_score = round(item["score"] * recency_weight(event_date)) \
                + repeat_offender_bonus(counts.get(mfr_key(manufacturer), 0))
            new_event = RegulatoryEvent(
                event_type=item["event_type"],
                raw_details=raw,
                llm_analysis=item["llm_analysis"],
                score=final_score,
                reporting_source=raw.get("reporting_source", ""),
                reported_by=raw.get("reported_by", ""),
                event_date=datetime.fromisoformat(event_date).date() if event_date else datetime.utcnow().date()
            )
            db_session.add(new_event)
            inserted += 1
        db_session.commit()
        return f"Committed {inserted} records (skipped {skipped} duplicates)."
    finally:
        db_session.close()

@workflow.defn
class CDSCOScraperWorkflow:
    def __init__(self):
        self._total = 0
        self._processed = 0
        self._event_type = ""
        self._finished = False
        self._warnings = []

    @workflow.query
    def progress(self) -> dict:
        start_time = workflow.info().start_time
        return {
            "total": self._total,
            "processed": self._processed,
            "event_type": self._event_type,
            "finished": self._finished,
            "warnings": list(self._warnings),
            "started_at": start_time.isoformat() if start_time else None,
        }

    @workflow.run
    async def run(self, year: str = None) -> dict:
        self._total = 0
        self._processed = 0
        self._event_type = ""
        self._finished = False
        self._warnings = []
        self._finished = False

        results = {}
        for event_type in ["NSQ_DRUG", "SPURIOUS_DRUG"]:
            self._event_type = event_type
            # 1. Scrape data (loop all months; year=None = full backfill)
            scrape_result = await workflow.execute_activity(
                scrape_cdsco_endpoint,
                args=[event_type, year],
                start_to_close_timeout=timedelta(minutes=15),
            )
            
            items = scrape_result.get("items", [])
            total_items = len(items)
            self._total += total_items
            month_warnings = scrape_result.get("warnings", [])
            self._warnings.extend(month_warnings)
            for warning in month_warnings:
                workflow.logger.warning(warning)
            results[event_type] = {
                "total_found": total_items,
                "processed": 0,
                "years": scrape_result.get("years", []),
                "warnings": month_warnings,
            }
            
            # 2. Chunk items into batches of 5
            batch_size = 5
            batches = [items[i:i + batch_size] for i in range(0, total_items, batch_size)]
            
            for batch in batches:
                # 3. Process LLM
                llm_result = await workflow.execute_activity(
                    process_batch_with_llm,
                    {"items": batch, "event_type": event_type},
                    start_to_close_timeout=timedelta(minutes=5),
                )
                
                # 4. Save to DB
                await workflow.execute_activity(
                    save_to_db,
                    llm_result,
                    start_to_close_timeout=timedelta(minutes=1),
                )
                
                results[event_type]["processed"] += len(batch)
                self._processed += len(batch)
                
                # Sleep between batches to respect the 8000 TPM limit (max ~3 requests per min)
                await workflow.sleep(timedelta(seconds=20))
                
        self._event_type = ""
        self._finished = True
        return results


@activity.defn
def load_backfill_candidates() -> dict:
    """Read all events and return the deduped (manufacturer, drug_name, reason)
    input list the failure-mode LLM pass must classify. Short-lived SELECTs only
    (no long-held transaction), so the remote Postgres never drops the conn.
    Plain def: blocking DB work must run in the worker's thread pool, not on
    the event loop (async def + blocking calls would freeze queries/tasks)."""
    db = SessionLocal()
    try:
        events = db.query(RegulatoryEvent).all()
        unique = {}
        order = []
        for ev in events:
            rd = ev.raw_details or {}
            key = (rd.get("manufacturer", ""), rd.get("drug_name", ""), rd.get("reason", ""))
            if key not in unique:
                unique[key] = {"manufacturer": key[0], "drug_name": key[1], "reason": key[2]}
                order.append(key)
        return {"events_total": len(events),
                "unique": [unique[k] for k in order]}
    finally:
        db.close()


@activity.defn
async def classify_failure_modes_activity(chunk: list) -> dict:
    """One LLM batch (<=20 items). Runs in a thread so the blocking Groq call
    cannot freeze the worker event loop."""
    labels = await asyncio.to_thread(classify_failure_modes_batch, chunk)
    return {"labels": [labels.get(i, "") for i in range(len(chunk))]}


@activity.defn
def apply_failure_modes(data: dict) -> dict:
    """Merge the classified failure modes into every event, recompute the
    class-aware paper assessment and full lead score, commit every 500.
    Plain def: blocking DB work must run in the worker's thread pool, not on
    the event loop (async def + blocking calls would freeze queries/tasks)."""
    from collections import Counter
    unique_list = data["unique"]
    labels_list = data["labels"]  # aligned with unique_list
    label_by_key = {
        (u["manufacturer"], u["drug_name"], u["reason"]): labels_list[i]
        for i, u in enumerate(unique_list)
    }
    ck = lambda raw: clean_company_name(raw or "").strip().lower()
    db = SessionLocal()
    try:
        ev_by_key, ch_by_key = {}, {}
        for e in db.query(RegulatoryEvidence).all():
            ev_by_key.setdefault(e.company_key or "", []).append(e)
        for c in db.query(EnrichmentCheck).all():
            ch_by_key.setdefault(c.company_key or "", []).append(c)

        events = db.query(RegulatoryEvent).all()
        mfr_counts = {}
        for ev in events:
            k = mfr_key((ev.raw_details or {}).get("manufacturer", ""))
            if k:
                mfr_counts[k] = mfr_counts.get(k, 0) + 1

        updated = 0
        dist = Counter()
        class_dist = Counter()
        commits = []
        for ev in events:
            rd = ev.raw_details or {}
            key = (rd.get("manufacturer", ""), rd.get("drug_name", ""), rd.get("reason", ""))
            fm = label_by_key.get(key, "")
            analysis = dict(ev.llm_analysis or {})
            if fm:
                analysis["failure_mode"] = fm
            ev.llm_analysis = analysis

            k = ck(rd.get("manufacturer", ""))
            pa = assess_paper_category(
                k, rd.get("reason", ""),
                ev.reported_by or rd.get("reported_by", ""),
                ev_by_key.get(k, []), ch_by_key.get(k, []), fm)
            paper = 30 if pa["class"] == "explicit" \
                else (round(20 * pa["confidence"] / 100) if pa["class"] == "deductive" else 0)
            base = 40 if ev.event_type == "SPURIOUS_DRUG" else 20
            flags = [f for f in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2")
                     if analysis.get(f)]
            mandate = 20 if (flags and ev.event_date and ev.event_date >= MANDATE_START) else 0
            prior = max(mfr_counts.get(mfr_key(rd.get("manufacturer", "")), 1) - 1, 0)
            ev.score = round((base + paper + mandate) * recency_weight(ev.event_date)) \
                + repeat_offender_bonus(prior)
            ev.paper_evidence_class = pa["class"]
            ev.paper_confidence = pa["confidence"]
            ev.paper_proxies = pa["proxies"]
            dist[fm or "unknown"] += 1
            class_dist[pa["class"]] += 1
            updated += 1
            if updated % 500 == 0:
                db.commit()
                commits.append(dict(dist))
        db.commit()
        return {"updated": updated,
                "failure_mode_distribution": dict(dist),
                "paper_class_distribution": dict(class_dist),
                "commits": commits}
    finally:
        db.close()


@workflow.defn
class FailureModeBackfillWorkflow:
    def __init__(self):
        self._phase = "idle"
        self._events_total = 0
        self._unique_total = 0
        self._chunks_total = 0
        self._chunks_done = 0
        self._updated = 0
        self._finished = False
        self._final = None

    @workflow.query
    def get_progress(self) -> dict:
        return {
            "phase": self._phase,
            "events_total": self._events_total,
            "unique_total": self._unique_total,
            "chunks_total": self._chunks_total,
            "chunks_done": self._chunks_done,
            "updated": self._updated,
            "finished": self._finished,
            "final": self._final,
        }

    @workflow.run
    async def run(self) -> dict:
        self._phase = "loading"
        loaded = await workflow.execute_activity(
            load_backfill_candidates,
            start_to_close_timeout=timedelta(minutes=10),
        )
        unique_list = loaded["unique"]
        self._events_total = loaded["events_total"]
        self._unique_total = len(unique_list)
        self._phase = "classifying"
        self._chunks_total = (len(unique_list) + BACKFILL_BATCH - 1) // BACKFILL_BATCH
        labels_list = [""] * len(unique_list)
        for start in range(0, len(unique_list), BACKFILL_BATCH):
            chunk = unique_list[start:start + BACKFILL_BATCH]
            res = await workflow.execute_activity(
                classify_failure_modes_activity,
                args=[chunk],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            for j, label in enumerate(res["labels"]):
                labels_list[start + j] = label
            self._chunks_done += 1
        self._phase = "applying"
        result = await workflow.execute_activity(
            apply_failure_modes,
            {"unique": unique_list, "labels": labels_list},
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        self._updated = result["updated"]
        self._final = result
        self._finished = True
        self._phase = "done"
        return result
