# REMINDER: Run the Backfills on the VPS

**Do this next time I'm on the VPS / SSH remote VSCode server — NOT on the local laptop.**

## Why here

- Backfills are long (hours) and hit external APIs (CDSCO, FDA.gov, EMA, Tavily/Groq).
- Needs a persistent connection + no laptop sleep / network drops.
- VPS or SSH'd-in remote VSCode server can run it overnight in the background.

---

## CDSCO: year-by-year backfill strategy (2019 → 2026)

**Never run a full `{"full": true}` backfill.** Go one year at a time so each session
stays inside the Groq token budget (dev plan = **1M tokens/day**) and we can wait for
tokens to refill between runs. The workflow already supports per-year triggers.

```bash
# One year per run (year is REQUIRED). Run at most ONE per day:
curl -X POST http://localhost:5000/api/v1/scraper/trigger \
  -H 'Content-Type: application/json' \
  -d '{"year": "2026"}'
```

### All-year command block (run one line per day, never the full backfill)

```bash
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2026"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2025"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2024"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2023"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2022"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2021"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2020"}'
curl -X POST http://localhost:5000/api/v1/scraper/trigger -H 'Content-Type: application/json' -d '{"year": "2019"}'
```

> ⚠️ `{"full": true}` scrapes ALL years in one session — ~6,000–7,300 records in one
> run. With the 1M/day Groq budget that risks hitting rate limits / burning the whole
> day's tokens and leaving nothing for FDA/EUDRAGMDP. Keep it year-by-year.

### Recommended order

1. **2026** (~1,077 records incl. spurious) → ~216 LLM batches ≈ ~250K tokens ≈ ~1.5 h
2. **2025** (~1,925 records) → ~385 LLM batches ≈ ~440K tokens ≈ ~2.5 h
3. **2024 → 2019** (NSQ only; CDSCO has no spurious data before 2025):
   each year ~500–700 records ≈ ~100–140 batches ≈ ~120–160K tokens ≈ ~40–60 min

Day pacing vs 1M/day budget: 2026 alone is fine, 2025 alone is fine, but **do not run
two back-to-back years in one day** — stay under ~50% of the budget so Groq rate
limits and daily quota never bite.

### Token math (estimates, per batch of 5 records)

- Input prompt ≈ ~450 tokens (instructions + 5 record JSONs)
- Output ≈ ~700 tokens (5 result objects)
- **≈ 1,150 tokens / batch** → ~230 tokens per record
- Groq returns exact `usage` per call; log `prompt_tokens`/`completion_tokens`
  from the response to confirm and tune batch size (5 → 10 halves input share).

### Important

- **2025 + 2026 are already fully scraped and analyzed in the DB (2,989 records).**
  Re-running those years re-burns LLM tokens on records that already exist
  (`save_raw_to_db` skips duplicates on event_type/drug/manufacturer/batch).
  Only re-run them if we want a fresh analysis pass after classifier/LLM changes.
- The actual gap is **NSQ 2019–2024** (~3,000–4,300 records, ~700K–1M tokens total,
  spread across ~5–7 days at the pacing above).
- The scraper is now **split into two Temporal workflows**: `CDSCOScraperWorkflow`
  (scrape + save raw, no LLM) and `CDSCOEnrichmentWorkflow` (Groq analysis + scoring
  over stored rows). See `CDSCO_2022_2024_BACKFILL_REMINDER.md` for the exact
  scrape-then-enrich command sequence.
- Also pending after scraping each year: `run_failure_mode_backfill.py` and
  `run_schedule_m_gap_backfill.py` re-classify the stored rows (Groq-only passes).

---

## FDA + EudraGMDP enrichment (all ~2,476 firms, blocked in VIEW_ONLY=1)

```bash
curl -X POST http://localhost:5000/api/v1/enrichment/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source": "all", "limit": 2500}'
```

Also consider (see FULL_ENRICHMENT_BACKFILL.md):
- Raise `batch_size` / lower the sleep in `enrichment_tasks.py` to speed it up.
- Optionally prioritize top manufacturers (Cipla, Lupin, Sun, Ipca, Alkem, Zydus…).
- Same day-pacing rule: FDA/EUDRAGMDP findings each cost one small Groq call
  (`analyze_regulatory_finding`, max_tokens 512) + a Tavily search per firm.

## Also pending (deferred on purpose)

- Re-classify existing web-evidence rows for the new `regulatory_action` field
  (Groq-only, no Tavily spend) so CLOSURE / LICENCE SUSPENDED badges show on
  already-fetched evidence.
