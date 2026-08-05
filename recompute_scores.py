"""Recompute stored scores to match the current scoring logic (idempotent).

Reads every row, recomputes score = round(base * recency) + repeat_offender_bonus
using the same helpers as main.py's breakdown, and updates in place. Placeholder
manufacturers ("Under Investigation", ...) are excluded from repeat-offender counts.

Run from the repo dir (needs db_setup.py + temporal_tasks.py):
    python3 recompute_scores.py [--dry-run]
"""
import sys

from db_setup import SessionLocal, RegulatoryEvent
from temporal_tasks import (
    mfr_key,
    calculate_base_score,
    recency_weight,
    repeat_offender_bonus,
)

dry_run = "--dry-run" in sys.argv

db = SessionLocal()
try:
    rows = db.query(RegulatoryEvent).all()
    counts = {}
    for e in rows:
        mfr = (e.raw_details or {}).get('manufacturer', '')
        key = mfr_key(mfr)
        if key:
            counts[key] = counts.get(key, 0) + 1

    changed = 0
    for e in rows:
        mfr = (e.raw_details or {}).get('manufacturer', '')
        key = mfr_key(mfr)
        prior = max(counts.get(key, 0) - 1, 0)
        base = calculate_base_score(e.event_type, e.llm_analysis or {}, e.event_date)
        new_score = round(base * recency_weight(e.event_date)) + repeat_offender_bonus(prior)
        if new_score != e.score:
            if not dry_run:
                e.score = new_score
            changed += 1
            print(f"{e.event_type} mfr={mfr!r} prior={prior} {e.score} -> {new_score}")
    if not dry_run:
        db.commit()
    print(f"{'would update' if dry_run else 'updated'} {changed} rows")
finally:
    db.close()
