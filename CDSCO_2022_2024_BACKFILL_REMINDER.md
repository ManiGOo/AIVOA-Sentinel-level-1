# NEXT TASK: CDSCO Backfill 2022-2024 (with the new split pipeline)

**Reminder for my future self.** 2026 and 2025 are already scraped AND enriched in the DB.
The remaining gap is **2024, 2023, 2022** (NSQ mostly; CDSCO has no spurious before 2025).

The scraper is now split into **two Temporal workflows** (see `temporal_tasks.py`):

1. `CDSCOScraperWorkflow` — scrape raw CDSCO data only, save rows with `llm_analysis={}`, `score=0`.
2. `CDSCOEnrichmentWorkflow` — run Groq AI analysis + scoring over the saved rows (no re-scrape).

Because enrichment is decoupled, scraping is fast and never blocked by Groq rate limits.
Run scrape per year first, then run enrichment per year (or all years at once).

---

## Step 1 — Scrape (raw data, fast)

One year per run (year is REQUIRED):

```bash
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2024"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2023"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2022"}'
```

Progress: `GET /api/v1/scraper/status`  (or MCP `get_scraper_status`)

> ⚠️ Do NOT use `{"full": true}` — it scrapes all years in one session and CDSCO throttles.
> Scraping is idempotent: re-running a year skips duplicate rows on
> `(event_type, drug_name, manufacturer, batch_no)`.

## Step 2 — Enrich + score (Groq, pace to token budget)

Run enrichment AFTER scraping, scoped to the year range. Default `only_missing=true`
means already-enriched rows (2026/2025) are skipped automatically:

```bash
curl -X POST http://localhost:5000/api/v1/scraper/enrichment/trigger -H 'Content-Type: application/json' \
  -d '{"year_start": "2024", "year_end": "2022"}'
```

Progress: `GET /api/v1/scraper/enrichment/status`  (or MCP `get_cdsco_enrichment_status`)

Useful variants:
- All years in one go: no body → `{}` (only rows missing `llm_analysis`).
- Re-enrich everything (e.g. after LLM prompt changes): `{"only_missing": false}`.
- Pace across days: `{"year_start": "2024", "year_end": "2024", "limit": 600}`.

## Token budget math (from `BACKFILL_REMINDER.md`)

- ~1,150 tokens per batch of 5 records; ~230 tokens/record.
- 2024 → 2019: each year ~500-700 records ≈ 100-140 batches ≈ ~120-160K tokens ≈ 40-60 min.
- Keep two years' enrichment per day under the budget; don't run scrape+enrich of a
  whole year in the same day if it risks the Groq daily quota.

## After each year is enriched

Run the two Groq-only classifier backfills over the stored rows (no re-scrape):

```bash
python3 run_failure_mode_backfill.py
python3 run_schedule_m_gap_backfill.py
```

## Reminders

- Rebuild **worker + app together** (`docker compose up -d --build`) before any run so
  the worker executes the new split workflows.
- Recovery if Groq quota exhausts mid-enrichment: just re-run Step 2 with the same
  year range — `only_missing=true` picks up exactly the rows left behind.
