"""Category 1 vs Category 2 evidence classification.

Category 1 (explicit): a regulator (FDA / EudraGMDP) explicitly cited paper /
manual data manipulation for the company — direct-quote evidence, e.g. an FDA
Warning Letter finding "torn paper in the trash".

Category 2 (deductive): no government audit quote exists. CDSCO NSQ alerts
publish only the final chemical test failure (e.g. "failed dissolution test"),
never IT-infrastructure audits. "Paper-based / Hybrid" for these firms is a
high-confidence analytical inference from the proxy indicators standard to
pharmaceutical market intelligence:

  P1  SME revenue tier — eQMS/eBMR adoption in this tier is <15%; baseline
      operations default to Tally/Busy + paper batch records.
  P2  Failure nature — dissolution/assay/weight-variation failures trace to
      manual weighing/dispensing and missing in-process interlocks.
  P2b "Release gap" — batch passed internal QA, reached the market, and was
      caught by a state lab → manual/siloed release workflow.
  P3  Revised Schedule M — the mandate exists precisely because this cohort's
      baseline is paper/hybrid (context, not a per-firm differentiator).
  P4  Employment footprint — no CSV/eQMS roles advertised (phase 2: needs a
      Naukri/LinkedIn scraper; reported as not-assessed for now).

All of this is deterministic and runs at request time (no extra LLM spend).
"""
import re

# P2 — failure modes characteristic of manual process gaps (weighing,
# dispensing, in-process control) vs formulation / API-quality failures.
MANUAL_FAILURE_RE = re.compile(
    r"dissolution|assay|content uniform|weight variation|uniformity of weight|"
    r"disintegration|hardness|friabilit|label claim", re.I)
FORMULATION_FAILURE_RE = re.compile(
    r"microbial|bacterial|sterility|endotoxin|preservative|moisture|related "
    r"substance|impurit|degradation|pyrogen|particulate", re.I)

# P1 — large-cap Indian pharma whose eQMS spend is not in question. Matched as
# substrings on company_key; these are never Category 2.
MAJOR_INDIAN_PHARMA = (
    "sun pharma", "sun pharmaceutical", "cipla", "dr. reddy", "aurobindo",
    "lupin", "torrent", "cadila", "zydus", "alkem", "mankind", "glenmark",
    "intas", "micro labs", "abbott", "glaxosmithkline", "pfizer", "sanofi",
    "novartis", "biocon", "divis", "ipca", "piramal", "ajanta", "emcure",
    "fdc", "gland", "hetero", "mylan", "natco", "wockhardt", "panacea",
    "lupin ltd",
)

SME_SUFFIX_RE = re.compile(
    r"\b(pvt\s*\.?\s*ltd|ltd|llp|private\s+limited|limited)\b", re.I)

DEDUCTIVE_THRESHOLD = 50


def _sme_proxy(company_key: str) -> bool:
    if any(m in company_key for m in MAJOR_INDIAN_PHARMA):
        return False
    return bool(SME_SUFFIX_RE.search(company_key))


def _failure_proxy(reason: str, llm_mode: str = "") -> tuple[bool, str]:
    """Returns (is_manual_indicative, proxy_label). Uses the LLM's
    failure-mode classification when available (it reads the full reason
    text), falling back to keyword rules."""
    if llm_mode in ("manual_process", "formulation", "unclear"):
        return (True, "manual_failure_mode") if llm_mode == "manual_process" \
            else (False, f"llm_{llm_mode}")
    if not reason:
        return False, "failure_mode_unknown"
    if MANUAL_FAILURE_RE.search(reason):
        return True, "manual_failure_mode"
    if FORMULATION_FAILURE_RE.search(reason):
        return False, "formulation_failure_mode"
    return False, "failure_mode_neutral"


def _release_gap_proxy(reported_by: str) -> tuple[bool, str]:
    """Batch reached market and was caught externally → manual/siloed release."""
    if not reported_by:
        return False, "release_gap_unknown"
    return True, "release_gap"


def assess_paper_category(company_key: str, reason: str, reported_by: str,
                          evidence_rows: list, check_rows: list,
                          llm_failure_mode: str = "") -> dict:
    """Classify a company/event as explicit (Cat 1), deductive (Cat 2), or
    none, with a confidence score, the satisfied proxies, and the sales copy
    to use when approaching the company."""
    explicit = any(getattr(e, "paper_qms_score", 0) or 0 > 0
                   or (getattr(e, "classification", None) or {}).get("is_paper_qms")
                   for e in evidence_rows)

    is_sme = _sme_proxy(company_key)

    proxies = []
    confidence = 0

    if explicit:
        proxies.append("explicit_regulator_quote")
        confidence = 100
    elif not is_sme:
        # Large-cap pharma (or a firm we can't tie to the SME tier): Category 2
        # never applies — eQMS spend is not in question for these.
        proxies.append("not_sme_confirmed")
        confidence = 0
    else:
        manual, p2 = _failure_proxy(reason, llm_failure_mode)
        confidence += 55 if manual else (15 if p2 == "formulation_failure_mode" else 25)
        proxies.append(p2)
        confidence += 25
        proxies.append("sme_revenue_tier")
        release, p2b = _release_gap_proxy(reported_by)
        if release:
            confidence += 10
            proxies.append(p2b)
        else:
            proxies.append("release_gap_unknown")
        confidence = min(confidence, 100)

    external_checked = any(getattr(c, "status", None) in ("completed", "error")
                           for c in check_rows)

    if explicit:
        cls = "explicit"
        basis = ("FDA/EudraGMDP published finding explicitly cites paper or "
                 "manual data manipulation for this manufacturer.")
        sales_message = (
            "Category 1 — quote the regulator directly: the FDA/EudraGMDP "
            "record explicitly caught them manipulating paper records. Name "
            "the letter, the date, the exact language.")
    elif not is_sme:
        cls = "none"
        basis = ("Large-cap pharma (or SME status not confirmed) — Category 2 "
                 "deduction does not apply; no explicit paper evidence either.")
        sales_message = ""
    elif confidence >= DEDUCTIVE_THRESHOLD:
        cls = "deductive"
        checked_note = ("External sources checked — no explicit findings." if external_checked
                        else "External sources not yet checked — provisional.")
        clean_reason = ' '.join((reason or '').split()) or 'an NSQ failure'
        if len(clean_reason) > 120:
            clean_reason = clean_reason[:117].rstrip() + '...'
        basis = (
            f"CDSCO NSQ alert flags '{clean_reason}'; no government "
            f"audit quote exists. High-confidence deduction from proxy indicators "
            f"({', '.join(p for p in proxies if not p.endswith('_unknown')) or 'none'}). "
            f"{checked_note}")
        sales_message = (
            "Category 2 — deductive positioning: 'Recent CDSCO NSQ alerts often "
            "trace back to transcription errors in manual batch records and "
            "disconnected QA workflows. As the revised Schedule M deadlines "
            "approach, our AI-native QMS replaces those paper gaps before your "
            "next state audit.'")
    else:
        cls = "none"
        basis = "No explicit regulatory evidence and proxies too weak to infer paper-based operations."
        sales_message = ""

    return {
        "class": cls,
        "confidence": confidence,
        "proxies": proxies,
        "external_sources_checked": external_checked,
        "basis": basis,
        "sales_message": sales_message,
    }
