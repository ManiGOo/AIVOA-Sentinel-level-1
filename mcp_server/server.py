"""MCP Server for AIVOA Sentinel — CDSCO Regulatory Intelligence Platform.

Exposes tools, resources, and prompts via the Model Context Protocol.

Transports:
  - stdio (default):  python -m mcp_server.server
  - HTTP:             uvicorn mcp_server.server:http_app --host 0.0.0.0 --port 8001
"""
import json
import sys
import os
import asyncio

# Ensure project root is on sys.path so local imports work
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp.server import MCPServer

# ---------------------------------------------------------------------------
# Initialize MCPServer (v2 high-level API)
# ---------------------------------------------------------------------------
mcp = MCPServer(
    name="AIVOA Sentinel",
    instructions=(
        "Pharmaceutical regulatory intelligence platform. "
        "Query CDSCO drug failure signals, company rankings, enrichment data, "
        "and web evidence. Trigger scraper and enrichment workflows. "
        "Run LLM-powered compliance analysis."
    ),
)

# ---------------------------------------------------------------------------
# Import tool modules (after sys.path fix)
# ---------------------------------------------------------------------------
from mcp_server.tools import signals, scraper, llm, leads
from mcp_server import resources, prompts


# ===== SIGNAL & COMPANY TOOLS (read-only) ====================================

@mcp.tool()
def query_signals(
    min_score: int = 0,
    year: int = None,
    page: int = 1,
    page_size: int = 30,
    q: str = None,
    event_type: str = None,
    is_paper: bool = None,
    paper_class: str = None,
    group_by: str = None,
    rule_96: bool = False,
    sub_rule_7: bool = False,
    schedule_h2: bool = False,
    schedule_m_gap: str = None,
) -> str:
    """Query regulatory signals with filters. Returns paginated results.

    Args:
        min_score: Minimum signal score filter
        year: Filter by event year (e.g. 2026)
        page: Page number (1-indexed)
        page_size: Results per page (max 200)
        q: Text search across drug, manufacturer, batch, reason
        event_type: 'NSQ_DRUG' or 'SPURIOUS_DRUG'
        is_paper: Filter by paper-QMS failure flag
        paper_class: 'explicit', 'deductive', or 'none'
        group_by: 'company' to collapse repeated incidents per company
        rule_96: Filter Rule 96 violations only
        sub_rule_7: Filter Sub-Rule 7 violations only
        schedule_h2: Filter Schedule H2 violations only
        schedule_m_gap: Filter by Schedule M gap label
    """
    result = signals.query_signals(
        min_score=min_score, year=year, page=page, page_size=page_size,
        q=q, event_type=event_type, is_paper=is_paper, paper_class=paper_class,
        group_by=group_by, rule_96=rule_96, sub_rule_7=sub_rule_7,
        schedule_h2=schedule_h2, schedule_m_gap=schedule_m_gap,
    )
    return json.dumps(result, default=str)


@mcp.tool()
def get_company_count() -> str:
    """Count of unique company entities in the database."""
    return json.dumps(signals.get_company_count())


@mcp.tool()
def get_company_ranking(page: int = 1, page_size: int = 10, q: str = None) -> str:
    """Company leaderboard ranked by highest-scoring signal.

    Args:
        page: Page number
        page_size: Results per page
        q: Search filter across company names and related fields
    """
    return json.dumps(signals.get_company_ranking(page=page, page_size=page_size, q=q), default=str)


@mcp.tool()
def get_company_signals(slug: str) -> str:
    """Full company detail page: summary stats + all grouped event cards.

    Args:
        slug: URL-safe company slug (e.g. 'rivpra-formulation')
    """
    return json.dumps(signals.get_company_signals(slug), default=str)


@mcp.tool()
def get_web_evidence(event_id: str) -> str:
    """Retrieve stored web evidence for a regulatory record.

    Args:
        event_id: UUID of the regulatory event
    """
    return json.dumps(signals.get_web_evidence(event_id), default=str)


@mcp.tool()
def get_lead(company_name: str) -> str:
    """Retrieve researched lead data for a company: decision makers, contacts,
    hiring, activity signals, QMS triggers, website and status.

    Args:
        company_name: Company name or key (e.g. 'R.P. Biotech Pvt. Ltd').
            Matches the company used on the Leads page.
    """
    return json.dumps(leads.get_lead(company_name), default=str)


@mcp.tool()
def get_company_phones(company_name: str) -> str:
    """Retrieve phone numbers scraped from a company's own website, each
    labelled with what it's for (Mobile, Office, Fax, Sales, Support, ...).

    Args:
        company_name: Company name or key (e.g. 'R.P. Biotech Pvt. Ltd').
    """
    return json.dumps(leads.get_company_phones(company_name), default=str)


# ===== WORKFLOW TRIGGER TOOLS (async) ========================================

@mcp.tool()
async def trigger_scraper(year: str = None, full: bool = False) -> str:
    """Start the CDSCO scraper workflow.

    Args:
        year: Year to scrape (e.g. '2026'). Defaults to current year.
        full: True for full historical backfill (2019-2026).
    """
    return json.dumps(await scraper.trigger_scraper(year=year, full=full))


@mcp.tool()
async def get_scraper_status() -> str:
    """Get live scraper workflow progress, processed count, and ETA."""
    return json.dumps(await scraper.get_scraper_status())


@mcp.tool()
async def trigger_cdsco_enrichment(
    year_start: str = None,
    year_end: str = None,
    only_missing: bool = True,
    limit: int = None,
) -> str:
    """Start the CDSCO enrichment workflow (AI analysis + scoring over stored rows).

    Args:
        year_start: Inclusive start year of event_date (e.g. '2024')
        year_end: Inclusive end year of event_date (e.g. '2022')
        only_missing: Only enrich rows with empty llm_analysis (default True)
        limit: Cap the number of rows to enrich
    """
    return json.dumps(await scraper.trigger_cdsco_enrichment(
        year_start=year_start, year_end=year_end,
        only_missing=only_missing, limit=limit,
    ))


@mcp.tool()
async def get_cdsco_enrichment_status() -> str:
    """Get live CDSCO enrichment workflow progress, processed count, and ETA."""
    return json.dumps(await scraper.get_cdsco_enrichment_status())


@mcp.tool()
async def trigger_enrichment(
    source: str = "fda",
    limit: int = 50,
    firms: list = None,
) -> str:
    """Start enrichment workflows against FDA and/or EudraGMDP.

    Args:
        source: 'fda', 'eudragmdp', or 'all'
        limit: Max firms to enrich (when firms not specified)
        firms: Explicit list of firm names (overrides limit)
    """
    return json.dumps(await scraper.trigger_enrichment(source=source, limit=limit, firms=firms))


@mcp.tool()
async def check_single_firm(event_id: str, source: str = "all") -> str:
    """On-demand enrichment for a single signal card's manufacturer.

    Args:
        event_id: UUID of the regulatory event
        source: 'fda', 'eudragmdp', or 'all'
    """
    return json.dumps(await scraper.check_single_firm(event_id=event_id, source=source))


@mcp.tool()
async def get_enrichment_status(workflow_id: str) -> str:
    """Poll enrichment workflow state, progress, and results.

    Args:
        workflow_id: The Temporal workflow ID to poll
    """
    return json.dumps(await scraper.get_enrichment_status(workflow_id))


@mcp.tool()
async def trigger_web_evidence(event_id: str) -> str:
    """Start web evidence search + classification for a specific record.

    Args:
        event_id: UUID of the regulatory event
    """
    return json.dumps(await scraper.trigger_web_evidence(event_id))


# ===== REGULATORY FULL-PULL TOOLS (async) ====================================

@mcp.tool()
async def trigger_regulatory_full_pull(
    source: str = "fda",
    from_date: str = "2022-01-01",
    to_date: str = "2026-12-31",
    max_records: int = 10000,
) -> str:
    """Bulk-scrape every FDA warning letter / EudraGMDP statement in a date
    range into the raw staging table — no per-company filter needed.

    Args:
        source: 'fda', 'eudragmdp', or 'all'
        from_date: Inclusive start date (YYYY-MM-DD)
        to_date: Inclusive end date (YYYY-MM-DD)
        max_records: Cap on rows scraped per source
    """
    return json.dumps(await scraper.trigger_regulatory_full_pull(
        source=source, from_date=from_date, to_date=to_date,
        max_records=max_records))


@mcp.tool()
async def get_regulatory_full_pull_status() -> str:
    """Poll the running full-pull workflow(s): phase, rows, inserted."""
    return json.dumps(await scraper.get_regulatory_full_pull_status())


@mcp.tool()
async def check_scraped_records(
    firm_name: str = None,
    event_id: str = None,
    source: str = "all",
) -> str:
    """Check whether we already have scraped regulatory data for a company.

    Fuzzy-matches the firm against the raw staging table, fetches full letter/
    statement bodies, classifies them, and links matches into regulatory
    evidence. Use this AFTER trigger_regulatory_full_pull to see if a specific
    company is covered.

    Args:
        firm_name: Company to check (e.g. 'Dabur India Ltd')
        event_id: Alternative — resolve the manufacturer from a CDSCO event
        source: 'fda', 'eudragmdp', or 'all'
    """
    return json.dumps(await scraper.check_scraped_records(
        firm_name=firm_name, event_id=event_id, source=source))


@mcp.tool()
async def get_regulatory_check_status(workflow_id: str) -> str:
    """Poll a ScrapedRecordCheckWorkflow started by check_scraped_records.

    Args:
        workflow_id: The Temporal workflow ID to poll
    """
    return json.dumps(await scraper.get_regulatory_check_status(workflow_id))


# ===== LLM ANALYSIS TOOLS ====================================================

@mcp.tool()
def analyze_cdsco_failure(items: list) -> str:
    """Classify CDSCO failure items for compliance flags (paper failure, Rule 96, Sub-Rule 7, Schedule H2).

    Args:
        items: List of dicts with drug_name, manufacturer, reason, batch_no, etc.
    """
    return json.dumps(llm.analyze_cdsco_failure(items), default=str)


@mcp.tool()
def classify_regulatory_finding(evidence_text: str, firm_name: str) -> str:
    """Classify an external regulatory finding for paper-QMS fingerprints.

    Args:
        evidence_text: Text of the FDA/EudraGMDP finding
        firm_name: Name of the firm
    """
    return json.dumps(llm.classify_regulatory_finding(evidence_text, firm_name))


@mcp.tool()
def classify_web_article(article_text: str, record_details: dict) -> str:
    """Classify a web article for relevance, severity, and regulatory action.

    Args:
        article_text: Full text of the article
        record_details: Dict with 'manufacturer' and 'drug_name' keys
    """
    return json.dumps(llm.classify_web_article(article_text, record_details))


@mcp.tool()
def classify_failure_modes(items: list) -> str:
    """Classify NSQ items into manual_process / formulation / unclear.

    Args:
        items: List of dicts with 'drug_name' and 'reason' keys
    """
    return json.dumps(llm.classify_failure_modes(items))


@mcp.tool()
def classify_schedule_m_gap(items: list) -> str:
    """Map NSQ notices to revised Schedule M Part A requirement areas.

    Args:
        items: List of dicts with 'drug_name' and 'reason' keys
    """
    return json.dumps(llm.classify_schedule_m_gap(items))


@mcp.tool()
def clean_company_names(raw_names: list) -> str:
    """Clean messy CDSCO manufacturer strings into trading names via LLM.

    Args:
        raw_names: List of raw manufacturer strings from CDSCO
    """
    return json.dumps(llm.clean_company_names(raw_names))


@mcp.tool()
def generate_search_queries(record_details: dict) -> str:
    """Generate 3-5 search queries for web evidence discovery.

    Args:
        record_details: Dict with 'manufacturer', 'drug_name', 'batch_no' keys
    """
    return json.dumps(llm.gen_search_queries(record_details))


@mcp.tool()
def assess_paper_category(
    company_key: str,
    reason: str,
    reported_by: str,
    evidence_rows: list,
    check_rows: list,
    llm_failure_mode: str = "",
) -> str:
    """Deterministic paper-QMS category assessment (Category 1/2/none).

    Args:
        company_key: Cleaned company name key
        reason: CDSCO failure reason text
        reported_by: Who reported the failure
        evidence_rows: List of RegulatoryEvidence-like dicts
        check_rows: List of EnrichmentCheck-like dicts
        llm_failure_mode: LLM failure mode label if available
    """
    return json.dumps(llm.paper_category(
        company_key, reason, reported_by, evidence_rows, check_rows, llm_failure_mode
    ))


# ===== RESOURCES =============================================================

@mcp.resource("regulatory://events")
def resource_events() -> str:
    """List all regulatory events."""
    return resources.get_events()


@mcp.resource("regulatory://events/{event_id}")
def resource_event(event_id: str) -> str:
    """Single regulatory event detail."""
    return resources.get_event(event_id)


@mcp.resource("regulatory://events/{event_id}/web-evidence")
def resource_event_evidence(event_id: str) -> str:
    """Web evidence for a specific event."""
    return resources.get_event_web_evidence(event_id)


@mcp.resource("config://sentinel")
def resource_config() -> str:
    """App configuration."""
    return resources.get_config()


# ===== PROMPTS ================================================================

@mcp.prompt(title="Investigate Company")
def investigate_company(company_slug: str) -> str:
    """Generate an investigation report for a specific company."""
    return prompts.investigate_company_prompt(company_slug)


@mcp.prompt(title="Compliance Audit")
def compliance_audit(cdsco_text: str) -> str:
    """Run a compliance audit on raw CDSCO failure text."""
    return prompts.compliance_audit_prompt(cdsco_text)


@mcp.prompt(title="Enrich and Report")
def enrich_and_report(manufacturer: str, source: str = "all") -> str:
    """Enrich a manufacturer and produce a summary report."""
    return prompts.enrich_and_report_prompt(manufacturer, source)


# ===== TRANSPORT ENTRY POINTS ================================================

def run_stdio():
    """Run the MCP server on stdio (default for local dev)."""
    asyncio.run(mcp.run_stdio_async())


def get_http_app(
    *,
    streamable_http_path: str = "/mcp",
    stateless: bool = False,
    disable_transport_security: bool = True,
):
    """Return a Starlette ASGI app for the Streamable HTTP transport.

    Args:
        streamable_http_path: JSON-RPC endpoint path registered on the app.
            Defaults to ``/mcp`` (the canonical MCP endpoint). When mounting the
            app into a parent FastAPI/Starlette app that already provides the
            prefix, register the inner route at ``/mcp`` and mount the whole app
            at the site root so the public path becomes ``/mcp`` without path
            doubling or trailing-slash redirects.
        stateless: When ``True`` the server treats every request as stateless
            (no MCP session required). Use this when the caller speaks raw
            JSON-RPC over HTTP without managing sessions (e.g. the sales-app
            backend's lightweight client).
        disable_transport_security: Disable DNS-rebinding protection so the app
            is reachable under arbitrary ``Host`` headers (required when it is
            mounted behind a gateway that uses a non-localhost hostname, e.g.
            ``http://app:5000`` from the sales-app backend).
    """
    from mcp.server.streamable_http import TransportSecuritySettings

    transport_security = None
    if disable_transport_security:
        transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return mcp.streamable_http_app(
        streamable_http_path=streamable_http_path,
        stateless_http=stateless,
        transport_security=transport_security,
    )


def get_session_manager():
    """Return the MCP session manager, initializing it if needed.

    The session manager is created lazily by ``streamable_http_app()``, so this
    must be called *after* ``get_http_app()``. The parent ASGI app's lifespan
    should enter ``session_manager.run()`` to start the task group before serving
    requests (mounted sub-apps do not run their own lifespan in Starlette).
    """
    return mcp.session_manager


# Allow running directly: python -m mcp_server.server
if __name__ == "__main__":
    run_stdio()
