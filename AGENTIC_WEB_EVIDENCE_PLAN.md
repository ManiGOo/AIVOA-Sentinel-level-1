# Agentic Web Evidence Search — Findings & Build Plan

> Status: **PROPOSED (for a later phase).** No code written yet. This doc scopes
> an agentic web-search layer that finds news reports / publications about a
> specific regulatory record (or manufacturer), stores them, and feeds the
> paper-QMS / due-diligence scoring.

---

## 1. The problem

Today the dashboard's evidence for a record is:

1. The CDSCO notice itself (`regulatory_events.raw_details`), and
2. Structured external sources (FDA warning letters, EudraGMDP statements) via
   the enricher (`regulatory_evidence`).

Neither captures **what the world is saying** about that product / batch /
manufacturer: news coverage, recall announcements, magazine reports, trade
press, social/media commentary, regulator press releases. That context:

- **Corroborates** a CDSCO failure (was this batch recalled in the press? was
  the site inspected by MHRA the same month?)
- **Surfaces paper-QMS / data-integrity stories** that CDSCO reasons never
  state outright (e.g., a US FDA import alert on the same site)
- Gives the "why does this matter" narrative layer for due-diligence reports.

The goal: for each record (or manufacturer), an **agent** searches the web,
filters noise, fetches the actual articles, summarizes + classifies each with
the LLM, saves everything, and links it back to the record.

---

## 2. Design

### 2.1 Data model — new table `sdr_data.web_evidence`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `event_id` | UUID (FK → `regulatory_events`, nullable) | link to the specific record |
| `mfr_key` | TEXT, indexed | normalized manufacturer (see `temporal_tasks.mfr_key`) |
| `query` | TEXT | the exact search query used |
| `title` | TEXT | article headline |
| `url` | TEXT, unique-ish | canonical article URL |
| `source` | TEXT | domain / outlet (timesofindia.indiatimes.com …) |
| `published_date` | DATE, nullable | |
| `snippet` | TEXT | search-engine snippet |
| `full_text` | TEXT | fetched article body (readability-extracted) |
| `classification` | JSONB | LLM verdict (relevant? paper-QMS? recall? severity) |
| `relevance_score` | INT | 0-100 |
| `fetch_status` | TEXT | `meta` / `fetched` / `blocked` / `failed` |
| `fetched_at` | DATETIME | |

Dedupe on `(mfr_key, url)`; an article about several records stores one row per
`(event_id)` link or uses a `web_evidence_links` join table later.

### 2.2 Search strategy — API-first, browser fallback

- **Primary: structured search API.** Brave Search API (free tier ~2k
  queries/mo), Tavily (research-tuned, returns article bodies), or SerpAPI.
  Structured JSON, no anti-bot, one call per query. **Recommended: Tavily** for
  research relevance + built-in body extraction; fall back to Brave for raw
  news.
- **Article body fetch: reuse the Playwright enricher container.** For the top
  N results per query, fetch the URL in headless Chromium (same infra as
  `adapters/fda.py`) and extract readable text (readability-style) so the LLM
  sees the article, not just the snippet.
- **Hard 429/CAPTCHA wall on search engines** is why the API is primary; the
  browser path is only for article fetching from the result URLs.

### 2.3 The agentic loop (per record)

```
record (manufacturer, product, batch, reason, date)
   │
   ├─ 1. QUERY GENERATION (Groq)
   │     build 3-6 query variants:
   │       "<product> <mfr> recall", "<mfr> <batch>", "<mfr> GMP",
   │       "<mfr> CDSCO", "<product> not of standard quality", ….
   │     (multi-language option: Hindi/regional press)
   │
   ├─ 2. SEARCH (API)
   │     run each query → top 5-10 hits, dedupe by URL, keep within a
   │     ±date window around the event.
   │
   ├─ 3. FETCH (Playwright)
   │     fetch full text of top hits (limit 3-5 / query); status per URL.
   │
   ├─ 4. CLASSIFY (Groq)
   │     per article: relevance (0-100), corroborates_failure (bool),
   │     is_paper_qms (bool), recall/action (bool), severity, 1-line summary.
   │
   ├─ 5. SAVE → sdr_data.web_evidence
   │
   └─ 6. EXPAND (agentic)
        if <1 relevant hit → broaden: manufacturer-level search, alternate
        spellings, wider date window, trade-press-only filters. Max 2 rounds.
```

All steps are LLM-driven decisions (which queries, which URLs matter, when to
stop) → genuinely agentic rather than a fixed pipeline.

### 2.4 Temporal + API wiring

- New activities in `enrichment_tasks.py` (or `web_evidence_tasks.py`):
  `generate_search_queries`, `search_web_for_query`, `fetch_article_body`,
  `classify_web_evidence`. Browser work stays on the **enricher** task queue;
  light API/search activities can run there too.
- New workflow `WebEvidenceWorkflow` (per record or batched) with a
  `progress` query like the scraper workflow.
- App endpoints (view-only gated):
  - `POST /api/v1/web-evidence/search` — `{event_id}` or `{firm}` → starts the
    workflow for that record.
  - `POST /api/v1/web-evidence/backfill` — all 2025 records, chunked, throttled.
  - `GET /api/v1/records/{id}/web-evidence` — fetch stored evidence for UI.
- Dashboard: "Web Evidence" collapsible on each record card (title, source,
  date, 1-line summary, link), plus an aggregate count on the manufacturer.

### 2.5 Politeness & ethics

- Respect `robots.txt` for fetched article URLs (news sites generally allow
  single-article reads; never hit login-walls).
- Throttle: API tier limits; random 2-5s delays on browser fetches; bounded
  per-record cost (max ~10 articles/record).
- Cite + link every stored article (never present scraped text as our own).
- Store only metadata + fetched body for research; honor paywall/login walls by
  recording `fetch_status='blocked'` and keeping the snippet instead.

---

## 3. Build phases

1. **P0 — Search API + save metadata.** Tavily (or Brave) key, `web_evidence`
   table, `search_web_for_query` activity, workflow for one record, dedupe.
   *Fast, small; proves the loop minus full text.*
2. **P1 — Full-text fetch + LLM classify.** Playwright article fetch in the
   enricher, readability extraction, Groq classification, relevance scoring.
3. **P2 — Agentic expansion.** Multi-round query generation/broadening,
   per-manufacturer aggregation, multi-language, paper-QMS tie-in into
   `paper_qms_watchlist`.
4. **P3 — UI.** Web-evidence panel on record cards + manufacturer view,
   backfill job, filters (source, date, relevance).

---

## 4. Open decisions / open questions

- [ ] Which search API to commit to (Tavily vs Brave vs SerpAPI) — decide by
      free-tier limits and article-body support.
- [ ] Scope of one run: per-record (expensive, ~10-20 web calls/record) vs
      per-manufacturer then link back by name match.
- [ ] LLM budget: web classification adds Groq tokens per article — reuse the
      paid key, or a cheaper model (`llama-3.3-70b`) for bulk classification.
- [ ] Tie `web_evidence` into the watchlist score (e.g., +weight when press
      corroborates CDSCO failure), or keep it purely informational at first.
- [ ] Regional languages (Hindi/Telugu news) worth the added query overhead?

---

## 5. Related work already in the repo

- `adapters/` (enricher): Playwright FDA + EudraGMDP adapters, `REGULATORY_SOURCES`
  registry — the browser-fetch infra to reuse for article bodies.
- `enrichment_tasks.py`: Temporal activity/workflow pattern + `progress` query
  to copy for `WebEvidenceWorkflow`.
- `cognitive_engine.analyze_regulatory_finding`: template for the article
  classifier.
- `db_setup.RegulatoryEvidence`: pattern for the new `web_evidence` model +
  idempotent migrations.
