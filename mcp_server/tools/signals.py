"""Read-only signal and company query tools."""
import re
import json
from typing import Optional

from db_setup import SessionLocal, RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck, WebEvidence
from temporal_tasks import MANDATE_START, recency_weight, repeat_offender_bonus, mfr_key
from company_names import clean_company_name, PAREN
from paper_category import assess_paper_category
from sqlalchemy import extract, func, or_


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
_SLUG_NORM = re.compile(r"[^a-z0-9]+")


def _group_key(mfr):
    name = clean_company_name(PAREN.sub("", mfr or ""))
    if not name:
        return ""
    words = re.sub(_GROUP_KEY_NORM, " ", name.lower()).strip().split()
    words = [w for w in words if w not in _LEGAL_WORDS]
    words = [_PLURAL_SING.get(w, w) for w in words]
    return " ".join(words).strip()


def _slug(gkey: str) -> str:
    return _SLUG_NORM.sub("-", (gkey or "").strip()).strip("-")


def _prior_event_counts(db) -> dict:
    mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
    counts = {}
    for mfr, cnt in db.query(mfr_col, func.count(RegulatoryEvent.event_id))\
            .group_by(mfr_col).all():
        key = mfr_key(mfr)
        if key:
            counts[key] = counts.get(key, 0) + cnt
    return counts


def _is_paper_event(event) -> bool:
    cls = getattr(event, "paper_evidence_class", None)
    if cls:
        return cls in ("explicit", "deductive")
    return bool((event.llm_analysis or {}).get("is_paper_failure"))


def _load_enrichment(db, page_keys):
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
    web_by_key = {}
    for w in db.query(WebEvidence).order_by(WebEvidence.relevance_score.desc()).all():
        gkey = _group_key(w.mfr_key or "")
        if gkey:
            web_by_key.setdefault(gkey, []).append(w)
    return web_by_key


def _web_evidence_bonus(items: list) -> int:
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


def company_key(raw: str) -> str:
    if not raw:
        return ""
    return clean_company_name(raw).strip().lower()


def _build_signal_card(event, counts, checks_by_key, evidence_by_key, web_by_key, db) -> dict:
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
    base = 40 if event.event_type == 'SPURIOUS_DRUG' else 20
    pa = assess_paper_category(
        ckey,
        (event.raw_details or {}).get("reason", ""),
        event.reported_by or (event.raw_details or {}).get("reported_by", ""),
        evidence_by_key.get(ckey, []),
        checks_by_key.get(ckey, []),
        (analysis or {}).get("failure_mode", ""),
    )
    if pa["class"] == "explicit":
        paper_bonus = 30
    elif pa["class"] == "deductive":
        paper_bonus = round(20 * pa["confidence"] / 100)
    else:
        paper_bonus = 0
    mandate_flags = [k for k in ('violates_rule_96', 'violates_sub_rule_7', 'violates_schedule_h2') if analysis.get(k)]
    mandate_bonus = 20 if (mandate_flags and event.event_date and event.event_date >= MANDATE_START) else 0
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


def query_signals(
    min_score: int = 0,
    year: Optional[int] = None,
    page: int = 1,
    page_size: int = 30,
    q: Optional[str] = None,
    event_type: Optional[str] = None,
    is_paper: Optional[bool] = None,
    paper_class: Optional[str] = None,
    group_by: Optional[str] = None,
    rule_96: bool = False,
    sub_rule_7: bool = False,
    schedule_h2: bool = False,
    schedule_m_gap: Optional[str] = None,
) -> dict:
    """Paginated regulatory signals with filtering. Use group_by='company' to collapse repeated incidents per company."""
    db = SessionLocal()
    try:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)

        query = db.query(RegulatoryEvent)
        if year:
            query = query.filter(extract('year', RegulatoryEvent.event_date) == year)
        if min_score:
            query = query.filter(RegulatoryEvent.score >= min_score)
        if event_type:
            query = query.filter(RegulatoryEvent.event_type == event_type)
        if is_paper is not None:
            query = query.filter(RegulatoryEvent.llm_analysis['is_paper_failure'].astext == str(is_paper).lower())
        if paper_class in ("explicit", "deductive", "none"):
            query = query.filter(RegulatoryEvent.paper_evidence_class == paper_class)
        if rule_96:
            query = query.filter(RegulatoryEvent.llm_analysis['violates_rule_96'].astext == 'true')
        if sub_rule_7:
            query = query.filter(RegulatoryEvent.llm_analysis['violates_sub_rule_7'].astext == 'true')
        if schedule_h2:
            query = query.filter(RegulatoryEvent.llm_analysis['violates_schedule_h2'].astext == 'true')
        if schedule_m_gap:
            query = query.filter(RegulatoryEvent.llm_analysis['schedule_m_gap'].astext == schedule_m_gap)
        if q:
            like = f"%{q.strip().lower()}%"
            query = query.filter(or_(
                func.lower(func.coalesce(RegulatoryEvent.raw_details['drug_name'].astext, '')).like(like),
                func.lower(func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')).like(like),
                func.lower(func.coalesce(RegulatoryEvent.raw_details['batch_no'].astext, '')).like(like),
                func.lower(func.coalesce(RegulatoryEvent.raw_details['reason'].astext, '')).like(like),
                func.lower(RegulatoryEvent.event_type).like(like),
            ))

        total = query.count()
        mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        counts = {}
        for mfr, cnt in db.query(mfr_col, func.count(RegulatoryEvent.event_id))\
                .group_by(mfr_col).all():
            key = mfr_key(mfr)
            if key:
                counts[key] = counts.get(key, 0) + cnt

        web_by_key = _load_web_evidence(db)

        if group_by == "company":
            matching = query.order_by(RegulatoryEvent.score.desc()).all()
            groups = []
            group_of = {}
            for event in matching:
                mfr = (event.raw_details or {}).get('manufacturer', '')
                key = _group_key(mfr) if mfr_key(mfr) else f"__evt__{event.event_id}"
                if key not in group_of:
                    group_of[key] = len(groups)
                    groups.append({"events": [event]})
                else:
                    groups[group_of[key]]["events"].append(event)

            total = len(groups)
            page_groups = groups[(page - 1) * page_size: page * page_size]

            page_keys = set()
            for g in page_groups:
                ckey = company_key((g["events"][0].raw_details or {}).get('manufacturer', ''))
                if ckey:
                    page_keys.add(ckey)
            checks_by_key, evidence_by_key = _load_enrichment(db, page_keys)

            response = []
            for g in page_groups:
                g["events"].sort(key=lambda e: e.score, reverse=True)
                cards = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
                         for e in g["events"]]
                card = cards[0]
                if len(cards) > 1:
                    card["event_count"] = len(cards)
                    card["events"] = cards[1:]
                response.append(card)
        else:
            events = query.order_by(RegulatoryEvent.score.desc())\
                        .offset((page - 1) * page_size)\
                        .limit(page_size)\
                        .all()

            page_keys = set()
            for event in events:
                ckey = company_key((event.raw_details or {}).get('manufacturer', ''))
                if ckey:
                    page_keys.add(ckey)
            checks_by_key, evidence_by_key = _load_enrichment(db, page_keys)

            response = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
                        for e in events]

        db.commit()
        return {
            "items": response,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
        }
    finally:
        db.close()


def get_company_count() -> dict:
    """Count of unique company entities."""
    db = SessionLocal()
    try:
        keys = set()
        mfr_col = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        for (mfr,) in db.query(mfr_col).all():
            gkey = _group_key(mfr)
            if gkey:
                keys.add(gkey)
        return {"total": len(keys)}
    finally:
        db.close()


def get_company_ranking(page: int = 1, page_size: int = 10, q: Optional[str] = None) -> dict:
    """Company leaderboard ranked by highest-scoring signal."""
    db = SessionLocal()
    try:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        q = (q or "").strip().lower()

        events = db.query(RegulatoryEvent).order_by(RegulatoryEvent.score.desc()).all()
        counts = _prior_event_counts(db)
        groups = {}
        for e in events:
            mfr = (e.raw_details or {}).get("manufacturer", "")
            gkey = _group_key(mfr)
            if not gkey:
                continue
            if q:
                hay = " ".join(
                    str((e.raw_details or {}).get(k, "")) for k in
                    ("manufacturer", "drug_name", "reason", "batch_no")
                ).lower()
                if q not in hay:
                    continue
            g = groups.get(gkey)
            if g is None:
                g = {
                    "gkey": gkey,
                    "name": clean_company_name(PAREN.sub("", mfr)) or gkey,
                    "slug": _slug(gkey),
                    "score": 0,
                    "peak": None,
                    "event_count": 0,
                    "sum_score": 0,
                    "latest": None,
                    "reg_set": set(),
                    "paper": 0,
                    "mandates": 0,
                }
                groups[gkey] = g
            g["event_count"] += 1
            g["sum_score"] += e.score or 0
            if (e.score or 0) > g["score"]:
                g["score"] = e.score or 0
                g["peak"] = e
            d = e.event_date
            if d and (g["latest"] is None or d > g["latest"]):
                g["latest"] = d
            g["reg_set"].add(e.regulator or "CDSCO")
            if _is_paper_event(e):
                g["paper"] += 1
            a = e.llm_analysis or {}
            if (e.event_date and e.event_date >= MANDATE_START) and any(
                    a.get(k) for k in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2")):
                g["mandates"] += 1

        items = [{
            "company_key": g["gkey"],
            "name": g["name"],
            "slug": g["slug"],
            "score": g["score"],
            "event_count": g["event_count"],
            "avg_score": round(g["sum_score"] / g["event_count"], 1),
            "latest_date": str(g["latest"]) if g["latest"] else "",
            "regulators": sorted(g["reg_set"]),
            "paper_count": g["paper"],
            "mandate_count": g["mandates"],
        } for g in groups.values()]
        items.sort(key=lambda x: (-x["score"], x["name"].lower()))

        total = len(items)
        page_items = items[(page - 1) * page_size: page * page_size]
        return {
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
        }
    finally:
        db.close()


def get_company_signals(slug: str) -> dict:
    """Full company detail page: summary + all grouped event cards."""
    db = SessionLocal()
    try:
        events = db.query(RegulatoryEvent).all()
        groups = {}
        for e in events:
            mfr = (e.raw_details or {}).get("manufacturer", "")
            gkey = _group_key(mfr)
            if gkey:
                groups.setdefault(gkey, []).append(e)

        target = None
        for gkey, evs in groups.items():
            if _slug(gkey) == slug:
                target = (gkey, evs)
                break
        if target is None:
            return {"error": "Company not found"}
        gkey, evs = target
        evs.sort(key=lambda e: e.score, reverse=True)

        mfr0 = (evs[0].raw_details or {}).get("manufacturer", "")
        ckey = company_key(mfr0)
        counts = _prior_event_counts(db)
        web_by_key = _load_web_evidence(db)
        checks_by_key, evidence_by_key = _load_enrichment(db, {ckey} if ckey else set())

        cards = [_build_signal_card(e, counts, checks_by_key, evidence_by_key, web_by_key, db)
                 for e in evs]
        card = cards[0]
        if len(cards) > 1:
            card["event_count"] = len(cards)
            card["events"] = cards[1:]

        dates = [e.event_date for e in evs if e.event_date]
        summary = {
            "company_key": gkey,
            "name": clean_company_name(PAREN.sub("", mfr0)) or gkey,
            "slug": slug,
            "score": card["score"],
            "event_count": len(evs),
            "avg_score": round(sum(e.score or 0 for e in evs) / len(evs), 1),
            "latest_date": str(max(dates)) if dates else "",
            "regulators": sorted({e.regulator or "CDSCO" for e in evs}),
            "years": sorted({str(d)[:4] for d in dates}),
            "paper_count": sum(1 for e in evs if _is_paper_event(e)),
            "mandate_count": sum(1 for e in evs if
                e.event_date and e.event_date >= MANDATE_START and any(
                    (e.llm_analysis or {}).get(k)
                    for k in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2"))),
            "evidence_count": len(evidence_by_key.get(ckey, [])),
            "web_evidence_count": sum(len(v) for k, v in web_by_key.items() if k == gkey),
        }

        db.commit()
        return {"company": summary, "card": card}
    finally:
        db.close()


def get_web_evidence(event_id: str) -> dict:
    """Retrieve stored web evidence for a regulatory record."""
    db = SessionLocal()
    try:
        evidence = db.query(WebEvidence).filter(
            WebEvidence.event_id == event_id
        ).order_by(WebEvidence.relevance_score.desc()).all()

        return {
            "event_id": event_id,
            "evidence": [
                {
                    "id": str(e.id),
                    "title": e.title,
                    "url": e.url,
                    "source": e.source,
                    "published_date": str(e.published_date) if e.published_date else None,
                    "snippet": e.snippet,
                    "classification": e.classification or {},
                    "relevance_score": e.relevance_score,
                    "fetch_status": e.fetch_status,
                    "fetched_at": str(e.fetched_at) if e.fetched_at else None,
                }
                for e in evidence
            ],
        }
    finally:
        db.close()
