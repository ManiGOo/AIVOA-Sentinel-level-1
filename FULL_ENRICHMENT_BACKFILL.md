# Remaining Work: Full FDA + EudraGMDP Enrichment Backfill

Status: **deferred** — on-demand per-card enrichment is live and verified; the full
enrichment of all manufacturers is still pending.

## Current State

- **On-demand flow (working):** the "Check FDA + EudraGMDP" button on each signal
  card calls `POST /api/v1/enrichment/check {event_id}` and polls
  `GET /api/v1/enrichment/status/{workflow_id}`. Verified: Marc Lifesciences → no
  findings; Cipla → 1 FDA warning letter saved with correct `mfr_key` linking.
- **Full backfill (NOT run):** the full runs (`enrichment-fda-*` /
  `enrichment-eudragmdp-*`) were started once, then **terminated** when we pivoted
  to the on-demand flow. They covered only ~63 / 2,476 firms before being stopped.

## Scope of the Remaining Backfill

Source data: `sdr_data.regulatory_events` → `raw_details->>'manufacturer'`.

- **2,476** distinct manufacturer strings (after placeholder exclusion).
- Cleaning pipeline (`company_names.py`, heuristic + Groq LLM fallback):
  - 2,379 pass the heuristic directly.
  - 98 fall through to the LLM extractor (most are address-only/placeholders and
    are dropped correctly).
- Search recall fix already in place: when the clean name (e.g. `Cipla Ltd`)
  returns 0 findings, the activity retries the legal-suffix-stripped variant
  (`Cipla`) before giving up.

### What the backfill produces

- FDA: `WarningLetterAdapter` → `#datatable` rows, company-cell match filter.
  **Precise but low recall** — FDA's `search_api_fulltext` often returns nothing
  even for firms that have letters (Wockhardt, Torrent, Ipca → 0 rows; Lupin → 2,
  Sun Pharma → 2, Cipla → 1). Expect only a handful of hits across the full list.
- EudraGMDP: `EudraGMDPAdapter` (public portal, date-range search + same-page
  drilldown). Complementary coverage; also sparse.
- Each finding is classified via Groq (`openai/gpt-oss-120b`) for
  `is_paper_qms` and stored in `sdr_data.regulatory_evidence` with
  `mfr_key = mfr_key(raw_manufacturer)` for linking back to events.

## How to Run It

Trigger API (app container):

```bash
# FDA then EudraGMDP (one workflow per source), all ~2,476 firms:
curl -X POST http://localhost:5000/api/v1/enrichment/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source": "all", "limit": 2500}'

# or a single source:
curl -X POST http://localhost:5000/api/v1/enrichment/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source": "fda", "limit": 2500}'
```

Blocked in `VIEW_ONLY=1` (403).

## Runtime / Politeness

Current `EnrichmentWorkflow` settings (in `enrichment_tasks.py`):
- `batch_size = 3` firms per activity (each batch = one Chromium launch).
- `workflow.sleep(10s)` between batches.

Measured pace during the aborted run: ~63 firms / 15 min → **~9–10 h per source**,
~18–20 h for both. Before launching, consider:

1. **Speed up** (recommended): raise `batch_size` to ~6–10 and lower the sleep to
   ~3–5s → roughly halves/triples throughput. Trade-off: more concurrent load on
   FDA.gov / EMA.
2. **Order majors first:** `_top_manufacturers` ranks by NSQ event count, so the
   majors most likely to have FDA/EudraGMDP citations (Cipla, Lupin, Sun Pharma,
   Ipca, Alkem, Zydus, Mankind…) sit at the tail of the queue and are searched
   last. Pass an explicit `firms` list to prioritize them.
3. **EudraGMDP is slower** per firm (form + drilldown navigation); budget extra
   time or split it into its own run.

## Known Limitations (accepted)

- FDA fulltext search recall is low; the company-cell match keeps results
  **precise** (no false positives) but many real letters will be missed. If higher
  recall is needed later, explore other endpoints (e.g. Import Alerts, 483s) or
  name-token search strategies.
- Evidence rows are deduped on `(source, firm_name, url)`; re-runs are idempotent
  (existing rows counted as `skipped`, not duplicated).

## Future Phase

See `AGENTIC_WEB_EVIDENCE_PLAN.md` — web-evidence search (news/publications per
record) into `sdr_data.web_evidence` via a `WebEvidenceWorkflow` is the next
signal source after this backfill.
