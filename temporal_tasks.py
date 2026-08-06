from temporalio import activity, workflow
from datetime import timedelta, datetime, date
import time

with workflow.unsafe.imports_passed_through():
    import requests
    from bs4 import BeautifulSoup
    from sqlalchemy import func
    from db_setup import SessionLocal, RegulatoryEvent
    from cognitive_engine import analyze_cdsco_failure_batch

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
    
    # We will pass the list of items to cognitive_engine
    llm_results = analyze_cdsco_failure_batch(items)
    
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
