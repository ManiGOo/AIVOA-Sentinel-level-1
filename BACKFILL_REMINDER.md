# REMINDER: Run the Full Backfill on the VPS

**Do this next time I'm on the VPS / SSH remote VSCode server — NOT on the local laptop.**

## Why here

- Full backfill is long (~9-10 h per source, up to ~20 h total) and hits external
  APIs (FDA.gov, EMA, Tavily/Groq).
- Needs a persistent connection + no laptop sleep / network drops.
- VPS or SSH'd-in remote VSCode server can run it overnight in the background.

## What to run

```bash
# FDA + EudraGMDP for all ~2,476 firms (blocked in VIEW_ONLY=1):
curl -X POST http://localhost:5000/api/v1/enrichment/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source": "all", "limit": 2500}'
```

Also consider (see FULL_ENRICHMENT_BACKFILL.md):
- Raise `batch_size` / lower the sleep in `enrichment_tasks.py` to speed it up.
- Optionally prioritize top manufacturers (Cipla, Lupin, Sun, Ipca, Alkem, Zydus…).

## Also pending (deferred on purpose)

- Re-classify existing web-evidence rows for the new `regulatory_action` field
  (Groq-only, no Tavily spend) so CLOSURE / LICENCE SUSPENDED badges show on
  already-fetched evidence.
