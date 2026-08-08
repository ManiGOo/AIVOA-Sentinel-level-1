# Task 3 — Enrich FDA + EudraGMDP Data and Link It to CDSCO Companies

This runbook tells the executing agent how to run the FDA/EU enrichment **on
top of the CDSCO data**: for each CDSCO company, check whether we already
scraped a matching FDA warning letter / EudraGMDP statement, and if so **match
and link** it into the evidence tables with a paper-QMS classification.

**Prerequisites**
- Task 1 completed: `sdr_data.scraped_regulatory_records` holds the FDA (~2,997
  rows) + EudraGMDP raw records.
- Task 2 completed (recommended, not strictly required): CDSCO rows enriched.
- Workers polling `enrichment-task-queue` (see Task 1 §0).

---

## 1. What "link" does (ScrapedRecordCheckWorkflow)

For one firm + one source, `link_scraped_records_for_firm` (in
`regulatory_scrape_tasks.py`) does:

1. **Fuzzy-match** the firm against `scraped_regulatory_records` rows whose
   `status = 'raw'` for that source. Similarity is token-based
   (`_firm_similarity`): legal suffixes (`ltd`, `limited`, `llc`, `inc`,
   `corp`, `pvt`, `pharma`, `laboratories`, …) are dropped, then a name whose
   significant tokens are contained in the other's scores **1.0**; otherwise
   the Dice coefficient on significant tokens is used, matched at **≥ 0.6**.
   Example: CDSCO `Dabur India Ltd` ↔ FDA `Dabur India Limited` → match.
2. **Fetch the full letter body** for FDA matches (`FDAWarningLetterAdapter
   .fetch_letter_body`, extracted from `Dear …` to the closing), and cache it
   back into the staging row.
3. **Classify** via Groq (`analyze_regulatory_finding`) → `is_paper_qms`,
   `evidence_quote`.
4. **Upsert** into `sdr_data.regulatory_evidence` keyed by
   `mfr_key`/`company_key` of the CDSCO firm (existing rows are **refreshed**
   with the fetched body + classification), plus one
   `sdr_data.enrichment_checks` row per (company, source).
5. Mark the matched staging rows `status = 'linked'`.

If there are **no matches**, it still records an `enrichment_checks` row with
`findings_count = 0` — so the answer to "do we have data for this company?" is
always recorded ("checked, no data").

> The older **live-search** path (`EnrichmentWorkflow` /
> `fetch_external_evidence`) still exists and searches FDA/EudraGMDP live per
> firm. Prefer the staging path above; use live search only as a fallback for
> firms the full pull may have missed.

---

## 2. Single-company check — pick ONE of:

a) **MCP tool** `check_scraped_records(firm_name="Dabur India Ltd", source="all")`
   - `source="all"` runs both FDA + EudraGMDP; or `"fda"` / `"eudragmdp"`.
   - Alternatively pass `event_id=<UUID>` to resolve the manufacturer from a
     CDSCO event automatically.

b) **REST** `POST /api/v1/regulatory/check`
```bash
# by company name:
curl -X POST http://localhost:5000/api/v1/regulatory/check \
  -H 'Content-Type: application/json' \
  -d '{"firm_name":"Dabur India Ltd","source":"all"}'

# by CDSCO event (resolves manufacturer):
curl -X POST http://localhost:5000/api/v1/regulatory/check \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"<event-uuid>","source":"all"}'
```

c) **Direct Temporal** — start `ScrapedRecordCheckWorkflow` with
   `args=[firm_name, source]` on `enrichment-task-queue`.

**Monitor:** MCP `get_regulatory_check_status(workflow_id)` or REST
`GET /api/v1/regulatory/check/status/{workflow_id}` (same contract as
`/api/v1/enrichment/status/{workflow_id}`).

**Expected per-source result** (workflow returns a dict per source):
```json
{
  "fda": {
    "firm_name": "Dabur India Ltd",
    "search_name": "Dabur India Ltd",
    "matched": 1,          // staging rows matched
    "inserted": 0,         // new evidence rows written
    "skipped": 1,          // matched rows already in regulatory_evidence (refreshed)
    "paper_qms_findings": 1,
    "evidence": [{"firm_name": "Dabur India Limited", "finding_date": "2026-08-04", "url": "...", "subject": "...", "is_paper_qms": true}]
  }
}
```

---

## 3. Batch run over all CDSCO companies (the actual backfill)

Pull the distinct real manufacturers from `sdr_data.regulatory_events`
(placeholders are excluded — see `_top_manufacturers` in `main.py`), then run
one `ScrapedRecordCheckWorkflow` per firm. Example driver (run from `python`,
venv active, `.env` sourced):

```python
import asyncio, time
from temporalio.client import Client
from sqlalchemy import func
from db_setup import SessionLocal, RegulatoryEvent
from temporal_tasks import mfr_key

def get_firms(limit=2000):
    db = SessionLocal()
    try:
        mfr_expr = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        rows = db.query(mfr_expr.label('mfr'), func.count(RegulatoryEvent.event_id))\
                 .group_by(mfr_expr).order_by(func.count(RegulatoryEvent.event_id).desc()).all()
        return [m for m, _ in rows if mfr_key(m)]
    finally:
        db.close()

async def main():
    c = await Client.connect("localhost:7233")
    firms = get_firms(limit=2000)
    print("firms to check:", len(firms))
    ids = []
    for i, f in enumerate(firms):
        wid = f"regulatory-check-batch-{i}-{int(time.time())}"
        await c.start_workflow(
            "ScrapedRecordCheckWorkflow", args=[f, "all"],
            id=wid, task_queue="enrichment-task-queue")
        ids.append(wid)
        if (i + 1) % 20 == 0:
            print("started", i + 1)
            await asyncio.sleep(1)          # gentle pacing
    print("all started:", len(ids))

asyncio.run(main())
```

- `source="all"` doubles the work (FDA + EudraGMDP per firm). If the
  EudraGMDP worker (playwright) isn't available, use `source="fda"`.
- Pace the starts so Groq classification doesn't hit token/rate limits; the
  per-firm check is short unless it must fetch FDA letter bodies.
- Poll with `get_regulatory_check_status(workflow_id)`; a handful of firms at a
  time is enough to eyeball progress.

---

## 4. Verification after linking

```sql
-- FDA/EU evidence now linked to CDSCO companies
SELECT e.source,
       count(*)                                   AS evidence_rows,
       count(*) FILTER (WHERE e.paper_qms_score > 0) AS paper_qms_rows,
       count(DISTINCT e.company_key)              AS distinct_companies
FROM sdr_data.regulatory_evidence e
GROUP BY e.source ORDER BY e.source;

-- Check outcomes per source (incl. 'checked, no data')
SELECT source, status, count(*) AS checks,
       sum(paper_qms_count) AS total_paper_qms
FROM sdr_data.enrichment_checks
GROUP BY source, status ORDER BY source;

-- Staging rows consumed by linking
SELECT source, status, count(*) FROM sdr_data.scraped_regulatory_records
GROUP BY source, status ORDER BY source;

-- Example: which CDSCO companies have a linked paper-QMS FDA finding
SELECT e.company_key, e.firm_name, e.finding_date, e.url
FROM sdr_data.regulatory_evidence e
WHERE e.source = 'FDA' AND e.paper_qms_score > 0
ORDER BY e.finding_date DESC;
```

**Success criteria for Task 3**
- Every CDSCO company has an `enrichment_checks` row (matched or "no data").
- Matched staging rows are `status = 'linked'`; their evidence is in
  `regulatory_evidence` with a Groq `classification` and `paper_qms_score`.
- Report: total firms checked, firms with ≥1 match, paper-QMS findings by
  source, and any errors.
