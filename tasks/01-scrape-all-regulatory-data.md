# Task 1 — Scrape ALL Regulatory Data (FDA Warning Letters + EudraGMDP + CDSCO)

This runbook tells the executing agent how to trigger and monitor **every
scraping workflow** so that all raw regulatory data is pulled into the
database before enrichment. You run the three scrape jobs "continuously" —
i.e. one after another (or as the infra allows), waiting for each to finish
before moving on, and recording results.

---

## 0. Architecture context (read first)

The pipeline is split into **scrape** and **enrich** stages so scraping never
gets rate-limited by LLM calls:

```
SCRAPE (this task)                         ENRICH (tasks 2 + 3)
──────────────────────────                 ─────────────────────────
FDA full pull  ──┐
EudraGMDP pull ──┼──> sdr_data.scraped_regulatory_records  ──> Task 3 links/classifies
CDSCO scrape  ───┘                         sdr_data.regulatory_events      ──> Task 2 AI-enriches
```

**Tables involved**
- `sdr_data.scraped_regulatory_records` — raw FDA warning letters + EudraGMDP
  statements, stored **before** any per-company enrichment. Columns:
  `source` ('FDA' | 'EudraGMDP'), `firm_name`, `finding_date`, `url`,
  `subject`, `evidence_text`, `status` ('raw' | 'linked' | 'skipped'),
  `fetched_at`. Unique index on `(source, url)` — re-running a full pull is
  idempotent (skips duplicates).
- `sdr_data.regulatory_events` — CDSCO NSQ / spurious drug notices. The scraper
  stores raw JSON in `raw_details` (includes `manufacturer`) with empty
  `llm_analysis` and `score = 0`; Task 2 fills those in.

**Workers (must be running)**
| Task queue | Worker file | Workflows registered |
|---|---|---|
| `scraper-task-queue` | `worker.py` | `CDSCOScraperWorkflow`, `CDSCOEnrichmentWorkflow`, `FailureModeBackfillWorkflow`, `ScheduleMGapBackfillWorkflow` |
| `enrichment-task-queue` | `enricher_worker.py` | `EnrichmentWorkflow`, `WebEvidenceWorkflow`, `LeadResearchWorkflow`, `CampaignWorkflow`, `RegulatoryFullPullWorkflow`, `ScrapedRecordCheckWorkflow` |

**Environment needed** (already in `.env`)
- `DATABASE_URL` — PostgreSQL connection.
- `TEMPORAL_HOST` — Temporal server (default `localhost:7233`).
- `GROQ_API_KEY` — only needed at enrichment time (Tasks 2/3), not for scraping.
- **Playwright + Chromium** — required for EudraGMDP (date-range form +
  drilldown). The `enricher` Docker image (`Dockerfile.enricher`,
  `mcr.microsoft.com/playwright/python`) has it. The FDA full pull needs **no
  browser** (plain HTTP DataTables export). CDSCO needs only `requests`.

> **Reminder / gotcha:** if the worker was started on the host venv (no
> playwright), the FDA + CDSCO jobs will run but **EudraGMDP will fail**.
> Run EudraGMDP on the Docker `enricher` worker or install
> `pip install playwright==1.62.0 && python -m playwright install chromium`.

---

## 1. FDA Warning Letters — full pull (2022-01-01 → 2026-12-31)

**What it does:** the FDA warning-letters table is a *server-side* DataTables
view (3,651 rows total). `FDAWarningLetterAdapter.scrape_all()` (in
`adapters/fda.py`) walks the `/datatables/views/ajax` endpoint directly (~37
requests at 100 rows/page) and returns every letter whose **posted date** is
inside the range — **2,997 rows** for 2022–2026. Each row keeps a summary
`evidence_text` (subject + issuing office + dates) and the letter detail `url`;
the full letter body is fetched later, only for matched companies (Task 3).

**Trigger — pick ONE of:**

a) **MCP tool** `trigger_regulatory_full_pull(source="fda", from_date="2022-01-01", to_date="2026-12-31", max_records=10000)`

b) **REST** `POST /api/v1/regulatory/trigger`
```bash
curl -X POST http://localhost:5000/api/v1/regulatory/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source":"fda","from_date":"2022-01-01","to_date":"2026-12-31","max_records":10000}'
```

c) **Direct Temporal** (from `python`, venv activated, `.env` sourced)
```python
import asyncio
from temporalio.client import Client
async def go():
    c = await Client.connect("localhost:7233")
    h = await c.start_workflow(
        "RegulatoryFullPullWorkflow",
        args=["fda", "2022-01-01", "2026-12-31", 10000],
        id=f"regulatory-pull-fda-<timestamp>",
        task_queue="enrichment-task-queue")
    print(h.id)
asyncio.run(go())
```

This starts `RegulatoryFullPullWorkflow`, which runs the
`scrape_regulatory_records` activity (phase `scraping`) then upserts into
staging in chunks of 250 (phase `saving`). The whole FDA pull takes ~1–3 min.

**Monitor:**
- MCP: `get_regulatory_full_pull_status()` → returns `running[]` with
  `phase`, `total`, `processed`, `inserted`, `skipped`.
- REST: `GET /api/v1/regulatory/status`.
- Poll until each workflow shows `phase: "done"` and `finished: true`.

**Expected result** for the 2022–2026 FDA pull:
```json
{
  "count": 2997,
  "inserted": 2997,          // 0 on re-runs (already staged)
  "skipped": 0,              // 2997 on re-runs
  "errors": [],
  "source": "FDA"
}
```
Note the workflow returns `"source": "FDA"` (canonical case used in the DB).

---

## 2. EudraGMDP GMP Non-Compliance — full pull (2022-01-01 → 2026-12-31)

**What it does:** `EudraGMDPAdapter.scrape_all()` (in `adapters/eudragmdp.py`)
opens the public date-range search form
(`https://eudragmdp.ema.europa.eu/inspections/gmpc/searchGMPNonCompliance.do`),
submits the full date range with **no firm filter**, reads every results row,
then drills into each statement page (the session is page-scoped, so it stays
on the same tab) to capture the statement body. Throttled with 2–5 s random
delays and heartbeats so Temporal doesn't time the activity out.

**Prerequisite:** a worker with **playwright** must be polling
`enrichment-task-queue` (the Docker `enricher` container; NOT a bare host venv
without playwright).

**Trigger — pick ONE of:**

a) **MCP tool** `trigger_regulatory_full_pull(source="eudragmdp", from_date="2022-01-01", to_date="2026-12-31", max_records=10000)`

b) **REST** `POST /api/v1/regulatory/trigger`
```bash
curl -X POST http://localhost:5000/api/v1/regulatory/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source":"eudragmdp","from_date":"2022-01-01","to_date":"2026-12-31","max_records":10000}'
```

c) **Direct Temporal** — same as §1(c) but `args=["eudragmdp", "2022-01-01", "2026-12-31", 10000]`.

**Monitor:** `get_regulatory_full_pull_status()` / `GET /api/v1/regulatory/status`
until `phase: "done"`.

**Expected:** hundreds of statements (EudraGMDP issues ~100–200/year). If the
result page paginates, you may need to call narrower year ranges
(e.g. 2022–2023, 2024–2025, 2026) — the adapter reads the first result page,
so check `count` is non-trivial and sanity-check the row count against the
known statements for the range.

---

## 3. CDSCO — scrape all failure notices (2019–2026 backfill)

**What it does:** `CDSCOScraperWorkflow` (in `temporal_tasks.py`) scrapes the
CDSCO public failure pages for both event types (`NSQ_DRUG` and
`SPURIOUS_DRUG`), looping all months. `year=None` = **full historical
backfill (2019–2026)**. Rows are saved with `llm_analysis = {}` and
`score = 0` — no LLM during scraping.

**Trigger — pick ONE of:**

a) **MCP tool** `trigger_scraper(full=True)`   (or `trigger_scraper(year="2026")` for a single year)

b) **REST** `POST /api/v1/scraper/trigger`
```bash
curl -X POST http://localhost:5000/api/v1/scraper/trigger \
  -H 'Content-Type: application/json' -d '{"full": true}'
# single year:
curl -X POST http://localhost:5000/api/v1/scraper/trigger \
  -H 'Content-Type: application/json' -d '{"year": "2026"}'
```

c) **Direct Temporal** — start `CDSCOScraperWorkflow` with `args=[]` (full)
   or `args=["2026"]` on `scraper-task-queue`.

**Monitor:**
- MCP: `get_scraper_status()`
- REST: `GET /api/v1/scraper/status` → `status`, `total`, `processed`,
  `percent`, `event_type`, `warnings`.

**Expected:** all NSQ + spurious rows saved into `sdr_data.regulatory_events`
for 2019–2026. This can take a while (many months × 2 event types) — poll until
`status: "finished"`.

---

## 4. "Continuously" — recommended execution order

Run the three scrapes back to back, waiting for each to finish:

1. **CDSCO scrape** (Task 1 §3) — longest job; start it first, let it run.
2. **FDA full pull** (§1) — independent; ~2–3 min.
3. **EudraGMDP full pull** (§2) — independent; run on the playwright worker.

These run on **different task queues** (`scraper-task-queue` vs
`enrichment-task-queue`) and touch **different tables**, so they can also run
in parallel without conflicts. Do not start Task 2 / Task 3 until the scrapes
they depend on are complete.

---

## 5. Verification after scraping

Run these and record the numbers in your final report:

```sql
-- Staged FDA / EudraGMDP rows
SELECT source, status, count(*), min(finding_date), max(finding_date)
FROM sdr_data.scraped_regulatory_records
GROUP BY source, status ORDER BY source;

-- CDSCO events saved
SELECT event_type, count(*),
       count(*) FILTER (WHERE llm_analysis::text <> '{}'::text) AS enriched
FROM sdr_data.regulatory_events GROUP BY event_type;

-- Sanity: no duplicate (source,url)
SELECT source, count(*) FROM (
  SELECT source, url FROM sdr_data.scraped_regulatory_records GROUP BY source, url HAVING count(*) > 1
) dup GROUP BY source;
```

**Success criteria for Task 1**
- `scraped_regulatory_records` has ~2,997 `FDA` rows (status `raw`) spanning
  2022-01-03 → 2026-08-04, plus the EudraGMDP statements.
- `regulatory_events` has all NSQ + spurious rows for the requested years.
- No duplicates by `(source, url)`; no unresolved workflow errors.
