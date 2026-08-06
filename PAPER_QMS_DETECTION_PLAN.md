# Paper-QMS Detection — Findings & Build Plan

> Status snapshot captured 2026-08-06. This doc records what we learned, the
> current state of the system, and the layered plan to identify which
> manufacturers use **paper-based quality control (QMS)** vs. digital.

---

## 1. The problem

We want to answer: *"Which companies use paper-based quality control and which
don't?"* — a buying-signal / due-diligence feature for the AIVOA Sentinel
dashboard.

Reality discovered in our data:

- **0 / 1,299 records** have `is_paper_failure = true` in `llm_analysis`.
- CDSCO NSQ failure reasons are almost entirely **chemical quality failures**
  (Dissolution 84, Content 60, pH, Assay, Description, Misbranded 13...).
- CDSCO NSQ notices are **product failures, not documentation audits** — they
  almost never state "this company keeps paper records".

So text scanning of CDSCO data alone cannot identify paper-QMS users. We need a
**layered detection approach** combining external regulatory evidence, proxy
behavioral signals, and company-level aggregation.

---

## 2. Current system state

| Piece | Detail |
|---|---|
| DB | Remote PostgreSQL `216.48.184.249:5432/pharma`, schema **`sdr_data`** |
| Main table | `sdr_data.regulatory_events` — 1064 rows (2026) + 2025 scrape running |
| Columns (recently added) | `reporting_source`, `reported_by` (TEXT) — from CDSCO `str_reporting_source` / `str_reported_by_lab_or_state` |
| Scoring | `temporal_tasks.calculate_base_score`: NSQ=20, SPURIOUS=40, paper=+30, 2026 mandate=+20, recency decay, repeat-offender bonus |
| Stack | FastAPI (`:5000`), Temporal dev server (`:7233`), Python worker, Docker Compose |
| Mode | `VIEW_ONLY=0` locally; `render.yaml` stays `VIEW_ONLY=1` for demo deploy |
| 2025 scrape | Workflow `cdsco-scraper-workflow-2025-20260806061942` (NSQ ~1898 found + spurious) |

Frontend (static/index.html) now has: static year filter (2019-2026),
year-agnostic search, multi-year "combined data" banner.

---

## 3. The 3-layer detection approach

### Layer 1 — Direct external evidence (paper/documentation citations)
Fingerprint language: *manual batch records, missing signatures, uncontrolled
spreadsheets/logbooks, ALCOA+ / data integrity, transcription errors, "not
found in records", batch/date discrepancies*.

Sources:

- **EudraGMDP (EU GMP non-compliance statements)** — PRIMARY candidate.
  - Non-compliance statements explicitly cite documentation/data-integrity
    findings. Covers many Indian manufacturing sites.
  - Search is **login-gated** — a **free EudraGMDP account unlocks it**.
  - Action: user creates account; we build login + search + statement fetch.
- **FDA Warning Letters** — richest language (batch records, data integrity).
  - Public, **no account exists/needed**.
  - Blocker: search data endpoint is an obfuscated JS datatable that 404s, and
    fda.gov rate-limits our current IP.
  - Action: run adapter from an unrestricted network (Render deploy) with
    throttling/retries; it is secondary until then.
- Others probed: MHRA/TGA — no stable public API; openFDA — no warning-letter
  endpoint (confirmed Aug 2024).

### Layer 2 — Proxy behavioral signals (computable now from our data)
- Repeat offenders with the **same failure type** across batches/years → weak
  CAPA → manual processes.
- Multiple batches of the **same product** failing → batch-record inconsistency.
- Label / misbranding / packaging failures → manual label control.
- Manufacture/expiry date anomalies → manual date entry.

### Layer 3 — Company-level aggregation (watchlist)
- Normalize manufacturer via `mfr_key` (exclude CDSCO placeholders like "Under
  Investigation").
- `paper_qms_score = f(explicit flags, fingerprint hits, repeat rate, failure
  diversity, recency)`.
- Serve ranked **Paper-QMS Watchlist** via API + dashboard section (same pattern
  as high-priority signals).

---

## 4. Build plan

### A. Enrichment engine (Layer 1) — source-agnostic
- **Browser-based adapters (Playwright)** — the FDA datatable is JS-driven and
  rate-limits plain HTTP; EudraGMDP requires login. Both are solved with a
  headless Chromium via Playwright:
  - Drive the visible search box, then **intercept the network response**
    (`page.on("response")`) to capture the JSON the datatable fetches from
    `.../warning-letters/datatables-data` — no reverse-engineering needed.
  - Real browser fingerprint (TLS/headers/cookies) passes FDA's anti-bot.
  - One browser adapter covers both FDA (public, no login) and EudraGMDP
    (free-account login + search).
  - Run as a **background Temporal activity** (browser startup is heavy), not in
    the API request path.
  - Image: **`mcr.microsoft.com/playwright/python:v1.62.0-noble`** (worker only,
    via `Dockerfile.worker`; app stays on `python:3.12-slim`) — Chromium +
    system libs preinstalled. Done and verified (Chromium launches inside the
    worker container). Polite throttling + retries.
  - Note: swapping the worker while a Temporal scrape is mid-flight is safe —
    the workflow lives in the Temporal server; the interrupted activity
    retries after its `start_to_close_timeout` (5 min) on the new worker.
- **Adapter pattern**: `REGULATORY_SOURCES` registry → `fda.py`, `eudragmdp.py`.
- **Fingerprint rules engine**: keyword/regex scoring on finding text.
- **LLM classifier**: reuse Groq + structured output (like
  `cognitive_engine.ComplianceAuditResult`) → per-finding paper-QMS verdict +
  evidence quote.
- **DB**: new table `sdr_data.regulatory_evidence`:
  `id, source, firm_name, mfr_key, finding_date, url, evidence_text,
  classification JSONB, paper_qms_score, evidence_quote, fetched_at`.
- **Temporal activity**: `enrich_manufacturers_with_external_evidence` (fed by
  distinct `mfr_key` values already in `regulatory_events`).
- **CLI**: `python enrichment_cli.py --source eudragmdp [--mfr "Captab Biotec"]`.

### B. Proxy scorer (Layer 2)
- `paper_qms_proxy.py`: per-mfr signals from `regulatory_events`
  (repeat-same-type rate, multi-batch rate, label-failure rate, date anomalies).

### C. Watchlist + UI (Layer 3)
- `paper_qms_watchlist.py`: combine Layer 1 evidence + Layer 2 proxies → score.
- API: `GET /api/v1/paper-qms/watchlist`.
- Dashboard: new "Paper QMS" tab with ranked cards + evidence quotes.

### D. Monitor current 2025 scrape
- Confirm NSQ + spurious complete, no `StringDataRightTruncation` (fixed),
  duplicate count = 0.

---

## 5. Open decisions / next actions
- [ ] Create **free EudraGMDP account** → share login flow/credentials so Layer
      1 primary source can be built and verified live.
- [ ] Build **Playwright-based adapters** (FDA + EudraGMDP) — network-response
      interception + real browser fingerprint; run enrichment as a Temporal
      activity from Render (or a non-blocked IP).
- [ ] Decide whether the dashboard gets a dedicated "Paper QMS" tab.
- [ ] Re-run enrichment after each new CDSCO scrape (scheduled Temporal workflow
      or manual CLI).

## 6. Security notes
- `GROQ_API_KEY` is in `.env` (gitignored) — rotate if it ever leaks; committed
  key in earlier messages is visible in this repo's history.
- `db_setup.py` has the **Postgres password hardcoded as a fallback default** —
  move to env-only.
