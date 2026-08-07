"""LLM analysis tools — cognitive engine wrappers."""
from typing import List, Optional

from cognitive_engine import (
    analyze_cdsco_failure_batch,
    analyze_regulatory_finding,
    classify_failure_modes_batch,
    classify_schedule_m_gap_batch,
    extract_company_names_batch,
    generate_search_queries,
    classify_web_evidence,
)
from paper_category import assess_paper_category


def analyze_cdsco_failure(items: List[dict]) -> dict:
    """Classify CDSCO failure items for compliance flags (paper failure, Rule 96, Sub-Rule 7, Schedule H2).

    Each item should have: drug_name, manufacturer, reason, batch_no, etc.
    Returns per-item analysis with is_paper_failure, violates_rule_96, etc.
    """
    return analyze_cdsco_failure_batch(items)


def classify_regulatory_finding(evidence_text: str, firm_name: str) -> dict:
    """Classify an external regulatory finding (FDA/EudraGMDP) for paper-QMS fingerprints.

    Returns is_paper_qms, evidence_quote, confidence, reason.
    """
    return analyze_regulatory_finding(evidence_text, firm_name)


def classify_web_article(article_text: str, record_details: dict) -> dict:
    """Classify a web article for relevance, severity, and regulatory action.

    record_details should have manufacturer, drug_name keys.
    Returns relevance_score, is_relevant, corroborates_failure, severity, regulatory_action, summary.
    """
    return classify_web_evidence(article_text, record_details)


def classify_failure_modes(items: List[dict]) -> dict:
    """Classify NSQ items into manual_process / formulation / unclear.

    Each item should have: drug_name, reason.
    Returns {index: label}.
    """
    return classify_failure_modes_batch(items)


def classify_schedule_m_gap(items: List[dict]) -> dict:
    """Map NSQ notices to revised Schedule M Part A requirement areas.

    Labels: process_control, contamination_control, stability,
    labeling_packaging, data_integrity, unclear.
    Returns {index: label}.
    """
    return classify_schedule_m_gap_batch(items)


def clean_company_names(raw_names: List[str]) -> List[str]:
    """Clean messy CDSCO manufacturer strings into trading names via LLM.

    Returns one clean name per input ('' when unparseable).
    """
    return extract_company_names_batch(raw_names)


def gen_search_queries(record_details: dict) -> List[str]:
    """Generate 3-5 Tavily/Google search queries for web evidence discovery.

    record_details should have: manufacturer, drug_name, batch_no.
    """
    return generate_search_queries(record_details)


def paper_category(
    company_key: str,
    reason: str,
    reported_by: str,
    evidence_rows: list,
    check_rows: list,
    llm_failure_mode: str = "",
) -> dict:
    """Deterministic paper-QMS category assessment (Category 1 explicit, Category 2 deductive, or none).

    Returns class, confidence, proxies, basis, sales_message.
    """
    return assess_paper_category(company_key, reason, reported_by, evidence_rows, check_rows, llm_failure_mode)
