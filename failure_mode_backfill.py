"""Backfill `failure_mode` into existing regulatory_events via a fast,
deduplicated LLM pass, then recompute the full lead score.

Run:  venv/bin/python failure_mode_backfill.py
Score = round((base + paper_bonus(class-aware) + mandate_bonus) * recency) + repeat_offender_bonus
"""
import os
from dotenv import load_dotenv
load_dotenv()

from db_setup import SessionLocal, RegulatoryEvent, RegulatoryEvidence, EnrichmentCheck
from cognitive_engine import classify_failure_modes_batch
from paper_category import assess_paper_category
from temporal_tasks import MANDATE_START, mfr_key, recency_weight, repeat_offender_bonus
from company_names import clean_company_name

ck = lambda raw: clean_company_name(raw or "").strip().lower()


def main():
    db = SessionLocal()
    try:
        ev_by_key, ch_by_key = {}, {}
        for e in db.query(RegulatoryEvidence).all():
            ev_by_key.setdefault(e.company_key or "", []).append(e)
        for c in db.query(EnrichmentCheck).all():
            ch_by_key.setdefault(c.company_key or "", []).append(c)

        events = db.query(RegulatoryEvent).all()
        # 1) Dedupe inputs by (manufacturer, drug_name, reason) — many events
        #    share identical NSQ failure text.
        unique = {}
        order = []
        for ev in events:
            rd = ev.raw_details or {}
            key = (rd.get("manufacturer", ""), rd.get("drug_name", ""), rd.get("reason", ""))
            if key not in unique:
                unique[key] = {"manufacturer": key[0], "drug_name": key[1], "reason": key[2]}
                order.append(key)
        print(f"{len(events)} events -> {len(order)} unique inputs", flush=True)

        # 2) LLM classify failure modes (deduped, lightweight prompt).
        items = [unique[k] for k in order]
        labels = classify_failure_modes_batch(
            items,
            on_chunk=lambda i, n: print(f"  classified chunk {i}/{n}", flush=True))
        label_by_key = {k: labels[i] for i, k in enumerate(order)}
        from collections import Counter
        print("failure_mode distribution:", dict(Counter(v for v in label_by_key.values())), flush=True)

        # 3) Apply per event: merge failure_mode, recompute class + score.
        mfr_counts = {}
        for ev in events:
            mfr = (ev.raw_details or {}).get("manufacturer", "")
            k = mfr_key(mfr)
            if k:
                mfr_counts[k] = mfr_counts.get(k, 0) + 1

        updated = 0
        for ev in events:
            rd = ev.raw_details or {}
            key = (rd.get("manufacturer", ""), rd.get("drug_name", ""), rd.get("reason", ""))
            fm = label_by_key.get(key, "")
            analysis = dict(ev.llm_analysis or {})
            if fm:
                analysis["failure_mode"] = fm
            ev.llm_analysis = analysis

            k = ck(rd.get("manufacturer", ""))
            pa = assess_paper_category(
                k, rd.get("reason", ""),
                ev.reported_by or rd.get("reported_by", ""),
                ev_by_key.get(k, []), ch_by_key.get(k, []), fm)
            paper = 30 if pa["class"] == "explicit" \
                else (round(20 * pa["confidence"] / 100) if pa["class"] == "deductive" else 0)
            base = 40 if ev.event_type == "SPURIOUS_DRUG" else 20
            flags = [f for f in ("violates_rule_96", "violates_sub_rule_7", "violates_schedule_h2")
                     if analysis.get(f)]
            mandate = 20 if (flags and ev.event_date and ev.event_date >= MANDATE_START) else 0
            prior = max(mfr_counts.get(mfr_key(rd.get("manufacturer", "")), 1) - 1, 0)
            ev.score = round((base + paper + mandate) * recency_weight(ev.event_date)) \
                + repeat_offender_bonus(prior)
            ev.paper_evidence_class = pa["class"]
            ev.paper_confidence = pa["confidence"]
            ev.paper_proxies = pa["proxies"]
            updated += 1
            if updated % 500 == 0:
                db.commit()
                print(f"  {updated} events updated...", flush=True)
        db.commit()
        print(f"Done. {updated} events updated.", flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
