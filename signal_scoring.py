"""Shared signal scoring: company-grouping keys + event-score logic used by
both the API (main.py) and the enrichment/evidence workers. Kept free of
FastAPI so workers can recompute stored scores when new web evidence arrives
(keeping the ranking leaderboard current without a read-time recompute)."""

import re
from sqlalchemy import func

from db_setup import RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck, WebEvidence
from temporal_tasks import MANDATE_START, recency_weight, repeat_offender_bonus, mfr_key
from company_names import clean_company_name, PAREN
from paper_category import assess_paper_category


def company_key(raw: str) -> str:
    """Entity-level key: cleaned company name (all raw variants of a company
    map to the same key). Empty when no company name can be extracted."""
    if not raw:
        return ""
    return clean_company_name(raw).strip().lower()


_GROUP_KEY_NORM = re.compile(r"[^a-z0-9]+")
_LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "corp",
                "corporation", "co", "company"}
_PLURAL_SING = {
    "formulations": "formulation", "laboratories": "laboratory",
    "industries": "industry", "enterprises": "enterprise",
    "sciences": "science", "pharmaceuticals": "pharmaceutical",
    "chemicals": "chemical", "biologicals": "biological",
    "diagnostics": "diagnostic", "remedies": "remedy",
    "botanicals": "botanical", "devices": "device",
}


def _group_key(mfr):
    """Company-grouping key: the cleaned trading name (reusing
    clean_company_name for M/s prefixes, addresses, parentheticals), then fully
    normalized so spelling variants ('Pvt. Ltd.'/'Pvt.Ltd'/'Pvt ltd'),
    legal-suffix differences ('Zee Laboratories' vs 'Zee Laboratories Ltd') and
    plural/singular forms ('Rivpra Formulations' vs 'Rivpra Formulation') all
    collapse into a single card.

    Parenthetical descriptors are removed from the raw string FIRST: otherwise
    a suffix like '(A WHO - GMP Certified Company)' sitting between the name
    and the address marker confuses the company-cut and survives as trailing
    noise."""
    name = clean_company_name(PAREN.sub("", mfr or ""))
    if not name:
        return ""
    words = re.sub(_GROUP_KEY_NORM, " ", name.lower()).strip().split()
    words = [w for w in words if w not in _LEGAL_WORDS]
    words = [_PLURAL_SING.get(w, w) for w in words]
    return " ".join(words).strip()


def _load_enrichment(db, page_keys):
    """Enrichment state for a set of company_keys: latest check per source
    (incl. 'checked, no findings') + stored evidence rows. All raw manufacturer
    variants of one company share the same company_key."""
    checks_by_key = {}
    evidence_by_key = {}
    if page_keys:
        for c in db.query(EnrichmentCheck).filter(
                EnrichmentCheck.company_key.in_(page_keys))\
                .order_by(EnrichmentCheck.checked_at.desc()).all():
            checks_by_key.setdefault(c.company_key or "", []).append(c)
        for e in db.query(RegulatoryEvidence).filter(
                RegulatoryEvidence.company_key.in_(page_keys))\
                .order_by(RegulatoryEvidence.fetched_at.desc()).all():
            evidence_by_key.setdefault(e.company_key or "", []).append(e)
    return checks_by_key, evidence_by_key


def _load_web_evidence(db):
    """All persisted agentic web evidence, grouped by the card-level company
    key (the same _group_key that drives card grouping) so evidence fetched
    for any name variant of a company surfaces on all of its cards."""
    web_by_key = {}
    for w in db.query(WebEvidence).order_by(WebEvidence.relevance_score.desc()).all():
        gkey = _group_key(w.mfr_key or "")
        if gkey:
            web_by_key.setdefault(gkey, []).append(w)
    return web_by_key


_SLUG_NORM = re.compile(r"[^a-z0-9]+")


def _slug(gkey: str) -> str:
    """URL-safe slug from a _group_key: 'rivpra formulation' -> 'rivpra-formulation'."""
    return _SLUG_NORM.sub("-", (gkey or "").strip()).strip("-")


def _prior_event_counts(db) -> dict:
    """Per-manufacturer incident counts (repeat-offender input), mirroring the
    count built inside get_high_priority_signals."""
    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    counts = {}
    for mfr, cnt in db.query(mfr_col, func.count(RegulatoryEvent.event_id))\
            .group_by(mfr_col).all():
        key = mfr_key(mfr)
        if key:
            counts[key] = counts.get(key, 0) + cnt
    return counts


def _is_paper_event(event) -> bool:
    """Whether an event is paper-QMS (explicit or deductive). Prefer the
    persisted paper_evidence_class (the same source the card scoring uses);
    fall back to the LLM flag for rows written before that column existed."""
    cls = getattr(event, "paper_evidence_class", None)
    if cls:
        return cls in ("explicit", "deductive")
    return bool((event.llm_analysis or {}).get("is_paper_failure"))


def _event_max_possible(event, counts: dict) -> int:
    """Grounded score ceiling for ONE event, identical to the card formula in
    _build_signal_card: only the bonuses this record can actually earn."""
    base = _base_score_for_event_type(event.event_type)
    analysis = event.llm_analysis or {}
    mandate_flags = [k for k in ('violates_rule_96', 'violates_sub_rule_7', 'violates_schedule_h2')
                     if analysis.get(k)]
    mandate_bonus = 20 if (mandate_flags and event.event_date and event.event_date >= MANDATE_START) else 0
    mfr = (event.raw_details or {}).get('manufacturer', '')
    prior = max(counts.get(mfr_key(mfr), 0) - 1, 0)
    repeat_bonus = repeat_offender_bonus(prior)
    recency = recency_weight(event.event_date)
    return round((base + 30 + mandate_bonus) * recency) + repeat_bonus + 25


def _base_score_for_event_type(event_type: str) -> int:
    """Base score by event type. FDA/EU enforcement = 40, adverse events = 30,
    shortages = 20, others = 20 (matches CDSCO NSQ)."""
    if event_type == 'SPURIOUS_DRUG':
        return 40
    if event_type in ('openfda', 'FDA_Drug', 'FDA_Device'):
        return 40
    if event_type == 'FDA_FAERS':
        return 30
    if event_type in ('ema_epi', 'EMA_ePI', 'EMA_Referral', 'EMA_Shortage'):
        return 30
    if event_type in ('ema_upd', 'EMA_UPD', 'EMA_Variation', 'EudraGMDP'):
        return 40
    return 20


def _web_evidence_bonus(items: list) -> int:
    """Capped, add-only lead-score bonus derived from stored web evidence.
    Absence of evidence never penalises; external corroboration adds urgency
    (a plant closure today is urgent even for an old CDSCO entry)."""
    if not items:
        return 0
    bonus = 0
    for it in items:
        if (it.get("relevance_score") or 0) >= 50:
            bonus += 2
        if it.get("corroborates_failure"):
            bonus += 15
        if it.get("severity") == "high":
            bonus += 8
        act = it.get("regulatory_action")
        if act in ("closure", "licence_suspension"):
            bonus += 8
        elif act in ("recall", "warning_letter", "prosecution"):
            bonus += 5
    return min(bonus, 25)


def _build_signal_card(event, counts, checks_by_key, evidence_by_key, web_by_key, db) -> dict:
    """Recompute the class-aware score for one event and build its card dict.
    Mutates event.score/paper_* on the ORM object (caller commits)."""
    analysis = event.llm_analysis or {}
    mfr = (event.raw_details or {}).get('manufacturer', '')
    key = mfr_key(mfr)
    ckey = company_key(mfr)
    gkey = _group_key(mfr)
    slug = _slug(gkey) if gkey else ""

    latest_checks = {}
    for c in checks_by_key.get(ckey, []):
        if c.source not in latest_checks:
            latest_checks[c.source] = {
                "status": c.status,
                "checked_at": str(c.checked_at) if c.checked_at else "",
                "searched_name": c.searched_name or "",
                "findings_count": c.findings_count or 0,
                "paper_qms_count": c.paper_qms_count or 0,
            }

    prior = max(counts.get(key, 0) - 1, 0)
    base = _base_score_for_event_type(event.event_type)
    pa = assess_paper_category(
        ckey,
        (event.raw_details or {}).get("reason", ""),
        event.reported_by or (event.raw_details or {}).get("reported_by", ""),
        evidence_by_key.get(ckey, []),
        checks_by_key.get(ckey, []),
        (analysis or {}).get("failure_mode", ""),
    )
    # Class-aware paper bonus: explicit regulator quote = full weight;
    # deductive (Category 2) scales with proxy confidence; none = 0.
    if pa["class"] == "explicit":
        paper_bonus = 30
    elif pa["class"] == "deductive":
        paper_bonus = round(20 * pa["confidence"] / 100)
    else:
        paper_bonus = 0
    mandate_flags = [k for k in ('violates_rule_96', 'violates_sub_rule_7', 'violates_schedule_h2') if analysis.get(k)]
    mandate_bonus = 20 if (mandate_flags and event.event_date and event.event_date >= MANDATE_START) else 0
    excluded = []
    if not (mandate_flags and event.event_date and event.event_date >= MANDATE_START):
        excluded.append({
            "row": "2026 Mandate",
            "max": 20,
            "reason": ("event predates the 2026 mandate start" if mandate_flags
                       else "no Rule 96 / Sub-Rule 7 / Schedule H2 violation on this record"),
        })
    recency = recency_weight(event.event_date)
    repeat_bonus = repeat_offender_bonus(prior)

    seen_urls = set()
    card_web_evidence = []
    for w in web_by_key.get(_group_key(mfr), []):
        if w.url in seen_urls:
            continue
        seen_urls.add(w.url)
        c = w.classification or {}
        card_web_evidence.append({
            "id": w.id,
            "url": w.url,
            "title": w.title or w.url,
            "source": w.source or "",
            "fetch_status": w.fetch_status or "",
            "relevance_score": int(w.relevance_score or c.get("relevance_score", 0) or 0),
            "corroborates_failure": bool(c.get("corroborates_failure", False)),
            "recall_action": bool(c.get("recall_action", False)),
            "severity": c.get("severity", ""),
            "regulatory_action": c.get("regulatory_action", ""),
            "is_paper_qms": bool(c.get("is_paper_qms", False)),
            "is_relevant": bool(c.get("is_relevant", False)),
            "summary": c.get("summary", ""),
        })
        if len(card_web_evidence) >= 10:
            break

    web_bonus = _web_evidence_bonus(card_web_evidence)
    new_score = round((base + paper_bonus + mandate_bonus) * recency) + repeat_bonus + web_bonus
    # Grounded ceiling for THIS card: only bonuses it can actually earn.
    # paper caps at 30 (explicit), mandate only if a flag applies (it's 0 or 20),
    # repeat is already capped at this company's prior-incident count, web caps at 25.
    max_base = 40 if event.event_type == 'SPURIOUS_DRUG' else 20
    max_possible = round((max_base + 30 + mandate_bonus) * recency) + repeat_bonus + 25

    event.paper_evidence_class = pa["class"]
    event.paper_confidence = pa["confidence"]
    event.paper_proxies = pa["proxies"]
    event.score = new_score

    return {
        "event_id": str(event.event_id),
        "regulator": event.regulator,
        "event_type": event.event_type,
        "score": new_score,
        "max_possible_score": max_possible,
        "company_name": clean_company_name((event.raw_details or {}).get('manufacturer', '')),
        "slug": slug,
        "company_key": ckey,
        "llm_analysis": analysis,
        "raw_details": event.raw_details or {},
        "event_date": str(event.event_date) if event.event_date else "",
        "reporting_source": event.reporting_source or (event.raw_details or {}).get("reporting_source", ""),
        "reported_by": event.reported_by or (event.raw_details or {}).get("reported_by", ""),
        "paper_assessment": pa,
        "score_breakdown": {
            "base": base,
            "paper_bonus": paper_bonus,
            "paper_bonus_class": pa["class"],
            "mandate_bonus": mandate_bonus,
            "mandate_flags": mandate_flags,
            "recency_weight": recency,
            "repeat_offender_bonus": repeat_bonus,
            "prior_events": prior,
            "web_evidence_bonus": web_bonus,
            "web_evidence_sources": len(card_web_evidence),
            "max_base": max_base,
            "max_paper_bonus": 30,
            "max_mandate_bonus": mandate_bonus,
            "max_recency_weight": recency,
            "max_repeat_bonus": repeat_bonus,
            "max_web_bonus": 25,
            "max_possible": max_possible,
            "excluded": excluded,
        },
        "enrichment": {
            "checks": latest_checks,
            "evidence": [
                {
                    "source": e.source,
                    "firm_name": e.firm_name,
                    "finding_date": str(e.finding_date) if e.finding_date else "",
                    "url": e.url or "",
                    "paper_qms_score": e.paper_qms_score or 0,
                    "evidence_quote": e.evidence_quote or "",
                    "is_explicit": bool((e.paper_qms_score or 0) > 0),
                }
                for e in evidence_by_key.get(ckey, [])
            ],
        },
        "web_evidence": card_web_evidence,
    }


def recompute_scores_for_group_keys(db, group_keys: set) -> int:
    """Recompute stored scores for every event whose company-group key is in
    `group_keys` (called after new web evidence is persisted so the ranking
    reflects the web bonus without waiting for a read-time recompute)."""
    if not group_keys:
        return 0
    counts = _prior_event_counts(db)
    web_by_key = _load_web_evidence(db)
    affected = []
    ckeys = set()
    for e in db.query(RegulatoryEvent).all():
        mfr = (e.raw_details or {}).get("manufacturer", "")
        if _group_key(mfr) in group_keys:
            affected.append(e)
            ck = company_key(mfr)
            if ck:
                ckeys.add(ck)
    if not affected:
        return 0
    checks_by_key, evidence_by_key = _load_enrichment(db, ckeys)
    for e in affected:
        _build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
    db.commit()
    return len(affected)
