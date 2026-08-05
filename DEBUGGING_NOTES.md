# Debugging Notes — 2026-08-05

## Root cause (finally)

The scraper workflow was passing activity arguments as a **single tuple** instead of
separate positional args:

```python
# BUG (previous code):
scrape_result = await workflow.execute_activity(
    scrape_cdsco_endpoint,
    (event_type, year),      # tuple passed as ONE argument
    start_to_close_timeout=timedelta(minutes=15),
)
```

Temporalio's `workflow.execute_activity(activity, *args, ...)` expects **positional
arguments**, not a tuple. The tuple `("NSQ_DRUG", "2026")` was decoded as the *single*
first parameter of the activity:

- `event_type` became the tuple `("NSQ_DRUG", "2026")`
- `year` fell back to its default `None`

Because a tuple `!= "NSQ_DRUG"`, `_tab()` returned **`"spurious"`** every time, and
`year=None` triggered a **full backfill**. So every workflow run was silently scraping
the **SPURIOUS dataset** (which has exactly 46 records across 2025–2026), never NSQ.

This is the SAME class of bug we already fixed in `main.py` (`start_workflow(workflow,
args)` → `start_workflow(workflow, *args)`) — we missed it in the workflow's
`execute_activity` call.

### The fix

This SDK's `execute_activity` signature is
`execute_activity(activity, arg=None, *, args=[], ...)` — the first activity
argument is the positional `arg`, and **extra activity arguments go in the
keyword-only `args` list**:

```python
# FIXED:
scrape_result = await workflow.execute_activity(
    scrape_cdsco_endpoint,
    args=[event_type, year],
    start_to_close_timeout=timedelta(minutes=15),
)
```

(Passing extra positional args raises
`execute_activity() takes from 1 to 2 positional arguments...`, and mixing
`arg=` with `args=[...]` raises `Cannot have arg and args`. Multiple activity
arguments must go entirely in the keyword-only `args` list.)

## How the bug misled us (symptom chain)

| Symptom | False conclusion |
|---|---|
| Scrape always returned **46 records** | "CDSCO is throttling to 46" |
| Records dated **2025 + 2026** despite `year="2026"` | "`publicReportingMonths` returns a rolling window" |
| `years: ['2025','2026']` in activity result | "worker is running old code" |
| Warning about "dropped N records for other months" | "CDSCO ignores the month filter when throttled" |

Truth: `46` was simply the **total size of the spurious dataset**. The 2025/2026 mix was
the spurious full-backfill. `years=['2025','2026']` is `get_reporting_years('spurious')`.

Two real bugs were found along the way:

1. **`_month_matches` case bug** — compared `field.upper()` (e.g. `JAN-2026`) against the
   raw mixed-case `month_key` (e.g. `Jan-2026`), so every record was classified as
   "wrong month" and dropped. Fixed by uppercasing both sides.
2. **`_fetch_month_verified` had no empty-month retry** — CDSCO intermittently serves
   empty pages for months that actually have data. Added retries for empty months
   (mismatched responses already retried).

## Verified CDSCO API contract (from the site's own JS)

`https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug` does:

- `GET /CDSCO/reportingYears?tab={nsq|spurious}` → list of years
- `GET /CDSCO/publicReportingMonths?year={Y}&tab={tab}` → list of month names
- `GET /CDSCO/filteredNsqDrugTable?month={Mon}-{Y}&source=All&tab={tab}` →
  records; the month value is built as `"{Month}-{Year}"` (e.g. `Jan-2026`)

The scraper is faithful to this. Healthy responses always tag records with
`dt_reporting_month_year == requested month`.

## Expected counts for a clean 2026 run

- NSQ 2026: **1,060** (Jan=218, Feb=217, Mar=190, Apr=121, May=155, Jun=159)
- Spurious 2026: **13**
- Total: **1,073**

## Environment gotchas

- **Stale worker processes**: a pre-container `python worker.py` (host, root-owned) and
  an orphaned process from a replaced worker container (`61e4988bd154`) were still
  polling `scraper-task-queue` and executing activities with old code.
  - Killed host PID 125838 (`sudo kill -9`).
  - Orphaned container process 130572 killed too.
  - `docker compose up -d --force-recreate temporal` cleared the stale poller
    registration (`1@61e4988bd154` gone; only `1@36bb406b40e4` remains).
- Verify with:
  `docker exec scrapper-temporal-1 temporal task-queue describe -t scraper-task-queue --task-queue-type activity --namespace default`

## Other fixes in this session

- `main.py`: `found.workflow_id` → `found.id` (Temporal `WorkflowExecution` uses `.id`).
- `main.py`: `start_workflow(workflow, args)` → `start_workflow(workflow, *args)`.
- `main.py`: `WorkflowFailureError` → `FailureError` (correct import for this SDK).
- Throttle safeguards: retries + backoff on 429/5xx/timeout, per-month integrity check,
  empty-month retry, warnings surfaced via `progress()` query, `/api/v1/scraper/status`,
  and the dashboard queue panel.
- Pre-flight probe (`_probe_cdsco`) aborts early with a clear message if the site is
  degraded; `/api/v1/scraper/status` reports a failed last run with the reason.

## Spurious "repeat offender" artifact (fixed)

**Symptom:** all SPURIOUS cards scored 70/66 instead of ~40.

**Root cause:** CDSCO publishes `manufacturer = "Under Investigation"` for spurious
products — it's a **placeholder**, not a real company. The repeat-offender bonus was
keyed on the raw manufacturer string, so all 13 spurious records counted each other as
"prior events" → every one got `+30` (40 → 70, or 40×0.9 → 66).

**Fix (both containers rebuilt together):**
- `temporal_tasks.py`: `PLACEHOLDER_MANUFACTURERS` + `mfr_key()` — placeholder names
  normalize to `''` and are excluded from prior-event counts at insert time.
- `main.py`: breakdown uses the same `mfr_key()` and counts over **all** rows (not just
  the page), so the tooltip always equals the stored score even across case-variant
  spellings (`CIPCO` vs `Cipco`, trailing `.` vs none).
- `recompute_scores.py`: idempotent in-place score normalization tool (also in repo).
  `docker exec scrapper-app-1 python3 recompute_scores.py --dry-run` first.
- `static/index.html`: `splitManufacturer` renders placeholders as "Manufacturer under
  investigation / Identity withheld by regulator" instead of a bare company name.

**Guardrail for every future run:** rebuild **worker + app together**
(`docker compose up -d --build`) before starting a fresh scrape — a stale worker still
inserts with the old scoring. Never rebuild the worker mid-run.

## Status at time of writing

- The tuple-arg bug is fixed in `temporal_tasks.py`; worker rebuilt.
- DB was wiped. A fresh 2026 run was being (re)triggered — **verify total = 1,073
  (1,060 NSQ + 13 spurious) with zero 2025 records before trusting the data.**
- Current DB state (after the fix): **1,064 rows** (1,051 NSQ + 13 spurious) — 9 NSQ
  short of the expected 1,060. If a clean run must hit 1,073, re-scrape after a
  worker+app rebuild; dedupe on (event_type, drug_name, manufacturer, batch_no)
  prevents doubles.
- Spurious scores verified: 40 (×1.0) / 36 (×0.9), `prior_events=0, repeat_offender_bonus=0`.

## Post-scrape analysis backlog (Groq daily token cap)

- The scrape itself captures all raw CDSCO data fine. The LLM classification pass
  (`analyze_cdsco_failure_batch`, model `openai/gpt-oss-120b`) exhausts the free-tier
  **200,000 tokens/day** budget mid-run (used ~199,951; "Rate limit reached ... on
  tokens per day (TPD)"). Every subsequent `process_batch_with_llm` activity returns
  429, `analyze_cdsco_failure_batch` falls back to `{}`, and records are saved with
  `llm_analysis = {}` and base-only scores.
- Full dataset needs ~450k tokens (215 batches x ~2,100), so analysis can't finish in
  one day even on a clean run. Mitigations: bigger Groq tier, smaller prompt/bigger
  batch, or run the analysis pass over multiple days.
- **Recovery without re-scraping:** `reanalyze_records.py` (copied into the app
  container) reads `raw_details` from the DB, re-runs the LLM analysis in batches, and
  updates `llm_analysis` + `score` in place. Run it AFTER the scrape finishes and
  after the daily quota resets:
  `docker exec scrapper-app-1 python3 reanalyze_records.py --max-wait-seconds 900`
- Dashboard filters + card layout (company name, small address, multi classification
  tags) are client-side in `static/index.html`; deployed via `docker cp` (no restart).
  The `/api/v1/signals/high-priority` `.limit(50)` was bumped to a `limit` param
  (default 1000) in `main.py` — takes effect on the next app restart.
