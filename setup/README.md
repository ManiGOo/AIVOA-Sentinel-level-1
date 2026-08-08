# Setup Guide — Cloning and Running the Project from Scratch

Everything you need to get a fresh clone of this repo running on your machine
(or a server). Follow the steps **in order**; each section tells you what
should work at the end of it.

---

## 0. What this project is

A **pharma regulatory intelligence platform** (AIVOA Sentinel). It:

1. **Scrapes** CDSCO (India) drug-failure notices, FDA warning letters, and
   EudraGMDP GMP non-compliance statements.
2. **Enriches** the raw data with AI (Groq) — paper-QMS failure detection,
   scoring, web evidence, lead research, campaigns.
3. Serves it via a **FastAPI** dashboard (`main.py`) and an **MCP server**
   (mounted at `/mcp` on the same API process, or run standalone over stdio).

The heavy lifting (scraping, LLM calls, web search) runs as **Temporal
workflows** on two worker task queues.

### Components / ports

| Component | How to run | Default port |
|---|---|---|
| FastAPI app + dashboard + MCP | `uvicorn main:app` | `5000` |
| Temporal server (dev) | `docker compose up -d temporal` | `7233` (gRPC), `8233` (Web UI) |
| CDSCO scraper worker | `python worker.py` | — (Temporal client) |
| Enricher worker (FDA/EU/web/lead/campaign) | `python enricher_worker.py` | — (Temporal client) |
| PostgreSQL | your own instance (not in compose) | `5432` |

### The two Temporal workers

| Task queue | Worker file | Workflows it runs |
|---|---|---|
| `scraper-task-queue` | `worker.py` | `CDSCOScraperWorkflow`, `CDSCOEnrichmentWorkflow`, failure-mode & Schedule-M backfills |
| `enrichment-task-queue` | `enricher_worker.py` | `EnrichmentWorkflow`, `WebEvidenceWorkflow`, `LeadResearchWorkflow`, `CampaignWorkflow`, `RegulatoryFullPullWorkflow`, `ScrapedRecordCheckWorkflow` |

### Environment variables used by the code

| Variable | Required? | Purpose |
|---|---|---|
| `DATABASE_URL` | **Yes** | PostgreSQL DSN, e.g. `postgresql://user:pass@host:5432/dbname`. (A `postgresql+asyncpg://` prefix is auto-normalized.) |
| `TEMPORAL_HOST` | No | Temporal gRPC address, default `localhost:7233`. Use `temporal:7233` inside Docker. |
| `GROQ_API_KEY` | No (has fallback) | LLM classification for enrichment. Set it for real results. |
| `TAVILY_API_KEY` | No | Web-evidence + lead-research web search. |
| `VIEW_ONLY` | No | `1` = read-only deploy (blocks all scrape/enrich/dispatch triggers). Default `0`. |
| `ENABLE_MCP` | No | `1` = mount MCP at `/mcp` (default). Set `0` to disable. |

---

## 1. Prerequisites

- **Git**
- **Python 3.12+** (`python3 --version`)
- **Docker + Docker Compose** (for Temporal; optional if you run Temporal yourself)
- **PostgreSQL 13+** — either local, or reachable over the network (the repo
  does **not** ship a Postgres container)
- **~2 GB free disk** for Python deps + Playwright Chromium

> If you're running on Windows, use WSL2 or a Linux VM for the smoothest
> experience (Playwright + psycopg2 behave best there).

---

## 2. Clone the repo

```bash
git clone https://github.com/ManiGOo/AIVOA-Sentinel-level-1.git
cd AIVOA-Sentinel-level-1
```

Create a Python virtual environment and activate it:

```bash
python3 -m venv venv
source venv/bin/activate        # Windows (WSL): source venv/bin/activate
```

---

## 3. Create the environment file (`.env`)

The repo **ignores and does not commit** `.env` (secrets). Create it from this
template — the app reads these from the process environment:

```bash
cp .env.example .env   # only if one exists; otherwise create it manually:
```

If you don't have a committed `.env.example`, create `.env` with:

```env
# Database (REQUIRED) — point at your PostgreSQL instance
DATABASE_URL=postgresql://myuser:mypassword@localhost:5432/pharma

# Temporal
TEMPORAL_HOST=localhost:7233

# LLM + search keys
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
TAVILY_API_KEY=tvly-xxxxxxxxxxxxxxxxxxxx

# Modes
VIEW_ONLY=0
ENABLE_MCP=1
```

> **Important:** Python code reads `DATABASE_URL` etc. **at import time**.
> Before running any script/worker/API, load the file into your shell:
> ```bash
> set -a && source .env && set +a
> ```
> (Or run uvicorn with `--env-file .env` — see §7.)

### 4. Create the database + user (PostgreSQL)

```bash
psql -U postgres -h localhost <<'SQL'
CREATE USER myuser WITH PASSWORD 'mypassword';
CREATE DATABASE pharma OWNER myuser;
SQL
```

Make sure `DATABASE_URL` in `.env` matches. The app creates its tables
automatically in the `sdr_data` schema (see §6) — you do **not** need to run
any migration tool.

---

## 5. Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Install the Playwright browser (used by the EudraGMDP adapter and web
scraping; the FDA full pull does **not** need a browser):

```bash
python -m playwright install chromium
```

Sanity check the imports:

```bash
set -a && source .env && set +a
python -c "import db_setup, main, temporal_tasks; print('imports OK')"
```

> If `main` warns "MCP server failed to mount", run
> `pip install -r requirements_mcp.txt` (installs `mcp[cli]`).

---

## 6. Start Temporal (the workflow engine)

The included `docker-compose.yml` only defines Temporal (and optional app /
worker containers). For a local dev setup you just need Temporal:

```bash
docker compose up -d temporal
```

Verify:

- gRPC: `7233` — Temporal is up
- Web UI: open http://localhost:8233 (workflow list, worker visibility)

The dev server auto-creates the `default` namespace. If you later run the
`app` / `worker` / `enricher` compose services, they connect to
`temporal:7233` over the compose network (see §9).

---

## 7. Initialize the database schema

```bash
set -a && source .env && set +a
python db_setup.py
```

Expected output:
```
Database schema 'sdr_data' and tables created successfully.
```

This creates the `sdr_data` schema and all tables
(`regulatory_events`, `regulatory_evidence`, `enrichment_checks`,
`web_evidence`, `scraped_regulatory_records`, `company_leads`, `campaigns`,
…). It is **idempotent** — safe to re-run.

---

## 8. Start the API + dashboard

```bash
set -a && source .env && set +a
uvicorn main:app --host 0.0.0.0 --port 5000
# or, letting uvicorn load .env itself:
uvicorn --env-file .env main:app --host 0.0.0.0 --port 5000
```

Verify in a browser / curl:

- Dashboard: http://localhost:5000/
- Health/config: http://localhost:5000/api/v1/config → `{"view_only": false}`
- MCP (mounted at `/mcp` when `ENABLE_MCP=1`): POST JSON-RPC to http://localhost:5000/mcp

---

## 9. Start the workers

Workers are what actually execute the Temporal workflows. **Do not skip this
step** — workflows you start will otherwise sit queued forever.

**Terminal A — CDSCO scraper worker** (`scraper-task-queue`):

```bash
set -a && source .env && set +a
python worker.py
```

**Terminal B — enricher worker** (`enrichment-task-queue`):

```bash
set -a && source .env && set +a
python enricher_worker.py
```

> **Playwright note:** EudraGMDP scraping needs Chromium. The host venv needs
> `pip install playwright==1.62.0 && python -m playwright install chromium`.
> FDA full pull, CDSCO, web evidence (Tavily) and lead research run without a
> browser.

Confirm both appear in the Temporal UI (http://localhost:8233 → **Workers**):
you should see pollers on `scraper-task-queue` and `enrichment-task-queue`.

---

## 10. Optional — run everything with Docker Compose

Instead of terminals, build and run all services in Docker (the `enricher`
image ships Playwright, so EudraGMDP works out of the box):

```bash
docker compose up -d --build temporal app worker enricher
```

- `app` — uvicorn on port `5000` (uses `Dockerfile`).
- `worker` — CDSCO scraper (`Dockerfile.worker`).
- `enricher` — enrichment worker (`Dockerfile.enricher`, Playwright image).

Note: the first build downloads the Playwright base image and can take
several minutes. The containers connect to Temporal via the `temporal` hostname
and read `.env` (secrets stay out of git).

---

## 11. Verify the full stack is healthy

```bash
# 1) DB tables exist
set -a && source .env && set +a
python -c "
from db_setup import engine
from sqlalchemy import text
with engine.connect() as c:
    print([r[0] for r in c.execute(text(\"select tablename from pg_tables where schemaname='sdr_data'\"))])"

# 2) Temporal reachable + workers polling
#    open http://localhost:8233 -> Workers tab (or "Temporal CLI" below)

# 3) API responds
curl -s http://localhost:5000/api/v1/config
```

---

## 12. Run the data pipeline

Scraping and enrichment are triggered through the API / MCP / Temporal, never
at repo start-up. Full, agent-ready instructions live in the **`tasks/`**
folder:

| File | What it does |
|---|---|
| `tasks/01-scrape-all-regulatory-data.md` | FDA full pull, EudraGMDP full pull, CDSCO scrape (run continuously) |
| `tasks/02-enrich-cdsco-ai.md` | AI enrichment + scoring over scraped CDSCO rows |
| `tasks/03-enrich-and-link-fda-eu-to-cdsco.md` | Match + link FDA/EU records to CDSCO companies |

Quick example — trigger an FDA full pull via the REST API:

```bash
curl -X POST http://localhost:5000/api/v1/regulatory/trigger \
  -H 'Content-Type: application/json' \
  -d '{"source":"fda","from_date":"2022-01-01","to_date":"2026-12-31"}'
curl -s http://localhost:5000/api/v1/regulatory/status
```

---

## 13. MCP server (for AI assistants / Claude / IDEs)

Two ways to expose the MCP tools (`query_signals`, `trigger_scraper`,
`trigger_regulatory_full_pull`, `check_scraped_records`, …):

- **Streamable HTTP (recommended):** already mounted on the API at
  `http://localhost:5000/mcp` (point your MCP client there).
- **Standalone stdio:** `python -m mcp_server.server`

Available tools (from `mcp_server/server.py`):

| Category | Tools |
|---|---|
| Signals | `query_signals`, `get_company_count`, `get_company_ranking`, `get_company_signals`, `get_web_evidence` |
| Scraper / enrichment | `trigger_scraper`, `get_scraper_status`, `trigger_cdsco_enrichment`, `get_cdsco_enrichment_status`, `trigger_enrichment`, `check_single_firm`, `get_enrichment_status`, `trigger_web_evidence` |
| Regulatory full pull | `trigger_regulatory_full_pull`, `get_regulatory_full_pull_status`, `check_scraped_records`, `get_regulatory_check_status` |
| LLM analysis | `analyze_cdsco_failure`, `classify_regulatory_finding`, `classify_web_article`, `classify_failure_modes`, `classify_schedule_m_gap`, `clean_company_names`, `generate_search_queries`, `assess_paper_category` |

Resources: `resource_events`, `resource_event`, `resource_event_evidence`,
`resource_config`. Prompts: `investigate_company`, `compliance_audit`,
`enrich_and_report`.

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: DATABASE_URL is not set` | `set -a && source .env && set +a` before the command |
| Workers never pick up workflows | Start `worker.py` + `enricher_worker.py`; check Temporal UI → Workers |
| EudraGMDP activity fails | Install playwright + Chromium (`python -m playwright install chromium`) |
| `psycopg2` install error | It's `psycopg2-binary` in requirements; use Python 3.12 on Linux/WSL |
| API blocks scrape triggers | `VIEW_ONLY` is `1` — set it to `0` |
| "MCP server failed to mount" | `pip install -r requirements_mcp.txt` |
| Temporal can't connect from Docker | Use `TEMPORAL_HOST=temporal:7233` (compose hostname), not `localhost` |
| Schema missing in Postgres | `python db_setup.py` (idempotent) |

---

## 15. Deploying to Render (view-only demo)

`render.yaml` ships a **read-only** dashboard deploy (no workers, no Temporal).
Set `VIEW_ONLY=1`, `DATABASE_URL`, `GROQ_API_KEY` in the Render dashboard —
scraper/enrich/dispatch actions are blocked by design there.
