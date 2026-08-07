# MCP Server Plan — AIVOA Sentinel

## Overview
Expose the entire CDSCO scraper + enrichment + signal database as an MCP server
using the Python `mcp` SDK (v2.0.0, `FastMCP` API). Runs on **stdio** for local
dev (Claude Desktop / Cursor) and **Streamable HTTP** for production (mounted on
the existing FastAPI app).

## File Structure
```
mcp_server/
├── __init__.py
├── server.py          # FastMCP entry point, transport config
├── tools/
│   ├── __init__.py
│   ├── signals.py     # Read-only signal/company query tools
│   ├── scraper.py     # Workflow trigger tools (scraper, enrichment)
│   ├── enrichment.py  # Enrichment + web evidence tools
│   └── llm.py         # Cognitive engine tools (classify, analyze, extract)
├── resources.py       # MCP resources (regulatory_events, web_evidence URIs)
└── prompts.py         # Reusable prompt templates
requirements_mcp.txt   # mcp[cli] dependency
```

---

## Tools to Expose

### Read-Only (tools/signals.py)
| Tool | Source | Description |
|---|---|---|
| `query_signals` | `main.get_high_priority_signals` | Paginated signal search with filters |
| `get_company_ranking` | `main.get_company_ranking` | Company leaderboard |
| `get_company_signals` | `main.get_company_signals` | Full company detail page |
| `get_company_count` | `main.get_company_count` | Unique company count |
| `get_web_evidence` | `main.get_web_evidence` | Stored web evidence for a record |
| `get_scraper_status` | `main.scraper_status` | Live scraper workflow progress |
| `get_enrichment_status` | `main.enrichment_status` | Enrichment workflow progress |

### Write / Action (tools/scraper.py + tools/enrichment.py)
| Tool | Source | Description |
|---|---|---|
| `trigger_scraper` | `main.trigger_scraper` | Start scraper for a year or full backfill |
| `trigger_enrichment` | `main.trigger_enrichment` | Enrich top manufacturers against FDA/EudraGMDP |
| `check_single_firm` | `main.check_event_enrichment` | On-demand enrichment for one firm |
| `trigger_web_evidence` | `main.trigger_web_evidence_search` | Search + classify web evidence for one record |

### LLM Analysis (tools/llm.py)
| Tool | Source | Description |
|---|---|---|
| `analyze_cdsco_failure` | `cognitive_engine.analyze_cdsco_failure_batch` | Classify CDSCO items for compliance flags |
| `classify_regulatory_finding` | `cognitive_engine.analyze_regulatory_finding` | Paper-QMS classification of external findings |
| `classify_web_article` | `cognitive_engine.classify_web_evidence` | Relevance/severity of a web article |
| `classify_failure_modes` | `cognitive_engine.classify_failure_modes_batch` | NSQ item failure mode classification |
| `classify_schedule_m_gap` | `cognitive_engine.classify_schedule_m_gap_batch` | Schedule M gap mapping |
| `clean_company_names` | `cognitive_engine.extract_company_names_batch` | LLM-assisted company name cleaning |
| `generate_search_queries` | `cognitive_engine.generate_search_queries` | Generate Tavily queries for an event |
| `assess_paper_category` | `paper_category.assess_paper_category` | Paper-QMS category assessment |

---

## Resources (resources.py)
| URI Template | Data |
|---|---|
| `regulatory://events` | List of all regulatory events |
| `regulatory://events/{event_id}` | Single event detail |
| `regulatory://events/{event_id}/web-evidence` | Web evidence for an event |
| `companies://{slug}` | Company summary + events |
| `config://sentinel` | App config (view_only flag) |

---

## Prompts (prompts.py)
| Prompt | Use Case |
|---|---|
| `investigate_company` | Given a company slug, pull signals + enrichment + evidence and produce an investigation report |
| `compliance_audit` | Given raw CDSCO text, run the full compliance audit pipeline |
| `enrich_and_report` | Trigger enrichment for a firm, then generate a summary |

---

## Implementation Steps

1. **Create `mcp_server/` directory** with `__init__.py`
2. **Install dependency**: add `mcp[cli]` to `requirements_mcp.txt`
3. **Write `server.py`**: FastMCP initialization, transport setup (stdio + HTTP mount)
4. **Write `tools/signals.py`**: Wrap the 7 read-only FastAPI endpoints as MCP tools (import DB models directly)
5. **Write `tools/scraper.py`**: Wrap Temporal workflow triggers as MCP tools
6. **Write `tools/enrichment.py`**: Wrap enrichment + web evidence triggers
7. **Write `tools/llm.py`**: Wrap cognitive engine functions directly (import from `cognitive_engine.py`)
8. **Write `resources.py`**: Expose DB queries as MCP resources
9. **Write `prompts.py`**: Define the 3 reusable prompt templates
10. **Add mount point in `main.py`**: Mount the MCP HTTP app alongside existing FastAPI routes
11. **Test with `mcp dev`**: Verify all tools show up in MCP Inspector

---

## Key Design Decisions

- **Direct imports vs HTTP calls**: LLM tools import `cognitive_engine` directly (no HTTP overhead). Workflow triggers use Temporal client directly. Read-only queries import DB models directly.
- **Auth**: Reads existing `.env` for `GROQ_API_KEY`, `DATABASE_URL`, `TAVILY_API_KEY`. No new secrets.
- **Concurrency**: `asyncio` for Temporal calls; `FastMCP` supports async natively.
- **Transport**: `mcp.run()` for stdio; `mcp.streamable_http_app()` mounted into FastAPI for HTTP.
