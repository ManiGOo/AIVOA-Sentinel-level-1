# AIVOA Sentinel — Architecture & MCP Integration

## System Architecture

```
┌─────────────────────────────┐
│        Sales App            │
│                             │
│  ┌───────────┐  ┌────────┐  │
│  │ Dashboard  │  │ Chat UI│  │
│  │ (REST)     │  │ (MCP)  │  │
│  └─────┬─────┘  └───┬────┘  │
│        │             │       │
└────────┼─────────────┼───────┘
         │             │
    REST │         MCP │  (both hit the same Sentinel server)
         │             │
┌────────┼─────────────┼───────┐
│        ▼             ▼       │
│  ┌──────────────────────┐    │
│  │   Sentinel API       │    │
│  │   FastAPI + MCP      │    │
│  │   Port 5000          │    │
│  │   /api/v1/...  (REST)│    │
│  │   /mcp        (MCP)  │    │
│  └──────────┬───────────┘    │
│             │                │
│     ┌───────┼───────┐       │
│     ▼       ▼       ▼       │
│   DB    Temporal    LLM     │
└─────────────────────────────┘
```

## Protocol Roles

| Layer              | Protocol | Use Case                                                                  |
| ------------------ | -------- | ------------------------------------------------------------------------- |
| Dashboard (REST)   | FastAPI  | Traditional UI — signal cards, company ranking, filters                   |
| Chatbot (MCP)      | MCP      | "Show me Sun Pharma's highest-scored failures" — AI decides which tools   |
| External AI (MCP)  | MCP      | Claude Desktop / Cursor users investigating companies                     |

## Why MCP in Sentinel

The sales app will include an **agentic chatbot**. MCP is the right protocol for
AI clients to call tools — it handles tool discovery, capability negotiation, and
structured arguments. Sentinel exposes all its capabilities as MCP tools so any
AI client can query the database, run LLM analysis, or trigger workflows without
custom integration code.

### What MCP is designed for

```
AI Client (Claude/Cursor) ──MCP──> Your Server
```

It's a protocol for AI agents to call tools. It has JSON-RPC framing, tool
discovery, capability negotiation — all designed for an LLM to figure out what's
available.

### What MCP is NOT designed for

```
Sales App (Next.js/React) ──REST──> Sentinel API
```

Traditional web apps should call REST endpoints directly. MCP adds unnecessary
overhead (JSON-RPC layer) for zero benefit when two services know exactly which
endpoints exist.

## MCP Tools Available

### Read-Only Signals (tools/signals.py)

| Tool                    | Description                                         |
| ----------------------- | --------------------------------------------------- |
| `query_signals`         | Paginated signal search with filters                |
| `get_company_count`     | Unique company count                                |
| `get_company_ranking`   | Company leaderboard                                 |
| `get_company_signals`   | Full company detail page                            |
| `get_web_evidence`      | Stored web evidence for a record                    |

### Workflow Triggers (tools/scraper.py)

| Tool                     | Description                                    |
| ------------------------ | ---------------------------------------------- |
| `trigger_scraper`        | Start CDSCO scraper for a year or backfill     |
| `get_scraper_status`     | Live scraper progress and ETA                  |
| `trigger_enrichment`     | Start FDA/EudraGMDP enrichment                 |
| `check_single_firm`      | On-demand enrichment for one firm              |
| `get_enrichment_status`  | Poll enrichment workflow                       |
| `trigger_web_evidence`   | Search + classify web evidence for a record    |

### LLM Analysis (tools/llm.py)

| Tool                        | Description                                      |
| --------------------------- | ------------------------------------------------ |
| `analyze_cdsco_failure`     | Classify CDSCO items for compliance flags        |
| `classify_regulatory_finding` | Paper-QMS classification of external findings  |
| `classify_web_article`      | Relevance/severity of a web article              |
| `classify_failure_modes`    | NSQ failure mode classification                  |
| `classify_schedule_m_gap`   | Schedule M gap mapping                           |
| `clean_company_names`       | LLM-assisted company name cleaning               |
| `generate_search_queries`   | Generate Tavily queries for web evidence         |
| `assess_paper_category`     | Deterministic paper-QMS category assessment      |

### Resources

| URI                                | Description                        |
| ---------------------------------- | ---------------------------------- |
| `regulatory://events`              | List all regulatory events         |
| `regulatory://events/{event_id}`   | Single event detail                |
| `config://sentinel`                | App configuration                  |

### Prompts

| Prompt              | Description                                        |
| ------------------- | -------------------------------------------------- |
| `investigate_company` | Pull signals + enrichment + evidence into a report |
| `compliance_audit`    | Run full compliance audit on CDSCO text            |
| `enrich_and_report`   | Trigger enrichment and produce a summary report    |

## Deployment

### Docker (recommended)

```bash
docker compose up --build
```

Services:
- `app` — FastAPI + MCP on port 5000
- `worker` — CDSCO scraper Temporal worker
- `enricher` — FDA/EudraGMDP + web evidence worker
- `temporal` — Temporal server

MCP endpoint: `http://localhost:5000/mcp`

### Local dev (stdio)

```bash
cd /home/many-wallnut/Desktop/scrapper
venv/bin/python -m mcp_server.server
```

Connects via stdin/stdout — works with Claude Desktop, Cursor.

### MCP Inspector (testing)

```bash
venv/bin/mcp dev mcp_server/server.py
```

Opens a web UI to test all tools interactively.

## Sales App Integration

When building the sales app chatbot:

1. **REST endpoints** — Dashboard calls `/api/v1/signals/high-priority` etc. directly
2. **MCP chatbot** — Connect to `http://sentinel:5000/mcp` and let the AI call tools
3. **Both hit the same server** — no data duplication, single source of truth

Example chatbot flow:
```
User: "Show me all spurious drug failures from Sun Pharma in 2026"
  → Chatbot sends to LLM
  → LLM calls MCP tool: query_signals(q="Sun Pharma", event_type="SPURIOUS_DRUG", year=2026)
  → Sentinel returns results
  → LLM formats and presents to user
```
