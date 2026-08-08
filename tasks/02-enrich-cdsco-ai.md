# Task 2 — AI Enrich the Scraped CDSCO Data

This runbook tells the executing agent how to run the **AI enrichment +
scoring stage** over the CDSCO rows that Task 1 scraped.

**Prerequisite:** Task 1 §3 (CDSCO scrape) completed — rows exist in
`sdr_data.regulatory_events` with empty `llm_analysis` and `score = 0`.

---

## 1. What the workflow does

`CDSCOEnrichmentWorkflow` (in `temporal_tasks.py`) is the second stage of the
split CDSCO pipeline:

1. `load_enrichment_candidates` — loads saved rows that still need AI
   analysis (default: `llm_analysis` empty), optionally filtered by event-date
   year range and capped by `limit`.
2. `process_batch_with_llm` — sends each batch (size 5) to Groq to produce
   `llm_analysis` (failure flags: `is_paper_failure`, `violates_rule_96`,
   `violates_sub_rule_7`, `violates_schedule_h2`, etc.).
3. `apply_enrichment_to_db` — writes `llm_analysis` + computed `score` back to
   `sdr_data.regulatory_events`.

It is paced to Groq's token budget and **decoupled from scraping**, so it can
be re-run / backfilled without ever touching CDSCO again.

---

## 2. Trigger — pick ONE of:

a) **MCP tool** `trigger_cdsco_enrichment(year_start=None, year_end=None, only_missing=True, limit=None)`
   - No args → enrich **all** rows with empty `llm_analysis`.
   - `year_start="2024", year_end="2022"` → only that event-date range
     (note: start year is the *later* year).
   - `only_missing=False` → re-enrich every row in range (re-classify).
   - `limit=500` → cap the batch for a paced multi-day backfill.

b) **REST** `POST /api/v1/scraper/enrichment/trigger`
```bash
# everything missing:
curl -X POST http://localhost:5000/api/v1/scraper/enrichment/trigger \
  -H 'Content-Type: application/json' -d '{}'

# a capped, year-bounded run:
curl -X POST http://localhost:5000/api/v1/scraper/enrichment/trigger \
  -H 'Content-Type: application/json' \
  -d '{"year_start":"2024","year_end":"2022","only_missing":true,"limit":500}'
```

c) **Direct Temporal** — start `CDSCOEnrichmentWorkflow` with
   `args=[year_start, year_end, only_missing, limit]` on `scraper-task-queue`.

The workflow ID looks like `cdsco-enrichment-workflow-YYYYMMDDHHMMSS`.

---

## 3. Monitor

- MCP: `get_cdsco_enrichment_status()`
- REST: `GET /api/v1/scraper/enrichment/status`

Response shape: `status` (`running` / `finished` / `idle`), `phase`
(`loading` → `enriching`), `total` (candidates), `processed`, `percent`,
`eta_seconds`, `warnings`.

Poll until `status: "finished"`. If `total` is large (thousands), plan a
multi-run backfill using `limit` (e.g. 500–1,000 per run) and re-trigger until
no candidates remain.

---

## 4. Verification after enrichment

```sql
-- How many CDSCO rows are now enriched vs still missing
SELECT event_type,
       count(*)                                                       AS total,
       count(*) FILTER (WHERE llm_analysis::text <> '{}'::text)       AS enriched,
       count(*) FILTER (WHERE score > 0)                              AS scored
FROM sdr_data.regulatory_events
GROUP BY event_type;

-- Paper-failure detections produced by this stage
SELECT count(*) AS paper_failure_events
FROM sdr_data.regulatory_events
WHERE (llm_analysis->>'is_paper_failure')::boolean IS TRUE;
```

**Success criteria for Task 2**
- Every (requested) CDSCO row has a non-empty `llm_analysis` and a `score`.
- No unresolved workflow errors in `warnings`.
- Report the enriched/scored counts per event type.
