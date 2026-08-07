"""Reusable MCP prompt templates for Sentinel workflows."""


def investigate_company_prompt(company_slug: str) -> str:
    """Investigate a company: pull signals, enrichment, and evidence into a report."""
    return f"""You are a pharmaceutical regulatory intelligence analyst.

Investigate the company with slug '{company_slug}' in the AIVOA Sentinel database.

Steps:
1. Use the `get_company_signals` tool with slug='{company_slug}' to retrieve all regulatory signals, enrichment checks, and web evidence for this company.
2. Analyze the data:
   - Total signals, score distribution, and trend over time
   - Whether any signals are paper-QMS related (Category 1 explicit or Category 2 deductive)
   - FDA/EudraGMDP enrichment findings and their paper-QMS implications
   - Web evidence: recalls, closures, warning letters, licence suspensions
   - Repeat-offender pattern (multiple CDSCO failures)
   - Revised Schedule M compliance gaps
3. Produce a structured investigation report with:
   - Executive Summary (2-3 sentences)
   - Risk Assessment (High/Medium/Low with justification)
   - Paper-QMS Finding (explicit/deductive/none, with evidence)
   - Regulatory Exposure (which regulators, what actions)
   - Recommended Approach (sales or compliance angle)
   - Key Evidence Quotes (from regulators or news)

Be specific. Quote the regulator. Cite dates. Do not speculate without labeling it as inference."""


def compliance_audit_prompt(cdsco_text: str) -> str:
    """Run a compliance audit on raw CDSCO failure text."""
    return f"""You are a Pharmaceutical Compliance Auditor.

Analyze the following CDSCO failure notice text and extract structured compliance flags.

Text:
{cdsco_text}

Produce a JSON-compatible analysis covering:
1. entity_name: company name
2. is_paper_failure: TRUE only if text explicitly indicates paper-based QMS issue
3. evidence_quote: exact text fragment supporting your booleans
4. violates_rule_96: TRUE only if text mentions QR/barcode/serialization on APIs
5. violates_sub_rule_7: TRUE only if text mentions excipient controls
6. violates_schedule_h2: TRUE only if text mentions antimicrobial track-and-trace
7. root_cause_summary: concise summary of the recorded failure
8. failure_mode: manual_process | formulation | unclear

RULE: Base every boolean strictly on the text. NEVER infer a regulatory violation from a generic quality term."""


def enrich_and_report_prompt(manufacturer: str, source: str = "all") -> str:
    """Trigger enrichment for a manufacturer and produce a summary report."""
    return f"""You are a pharmaceutical regulatory intelligence analyst.

Enrich and report on the manufacturer '{manufacturer}' using external regulatory sources.

Steps:
1. Use `check_single_firm` or `trigger_enrichment` to start enrichment against {source} sources.
2. Use `get_enrichment_status` to poll until the workflow completes.
3. Once complete, analyze the findings:
   - FDA Warning Letters or EudraGMDP non-compliance statements found
   - Paper-QMS evidence (explicit documentation/data-integrity citations)
   - Regulatory actions taken (closures, suspensions, recalls, warning letters)
   - Timeline of findings
4. Produce a structured enrichment report with:
   - Manufacturer Summary
   - Findings Count by Source
   - Paper-QMS Assessment (Category 1 explicit evidence found? Quote the regulator.)
   - Regulatory Action Summary
   - Risk Implications for CDSCO signals
   - Recommended Follow-up"""
