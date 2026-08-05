import argparse
import time

from db_setup import SessionLocal, RegulatoryEvent
from cognitive_engine import analyze_cdsco_failure_batch
from temporal_tasks import calculate_base_score, recency_weight, repeat_offender_bonus


def main():
    parser = argparse.ArgumentParser(description="Re-analyze CDSCO records in-place (no re-scrape).")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-wait-seconds", type=int, default=900)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(RegulatoryEvent).all()
    except Exception:
        db.rollback()
        raise

    pending = [r for r in rows if not (r.llm_analysis or {})]
    print(f"total={len(rows)} pending_analysis={len(pending)}")

    updated = 0
    skipped = 0
    i = 0
    while i < len(pending):
        batch = pending[i:i + args.batch_size]
        i += len(batch)

        items = [{
            "drug_name": (r.raw_details or {}).get("drug_name", ""),
            "manufacturer": (r.raw_details or {}).get("manufacturer", ""),
            "batch_no": (r.raw_details or {}).get("batch_no", ""),
            "reason": (r.raw_details or {}).get("reason", ""),
        } for r in batch]

        llm_results = {}
        attempts = 0
        while True:
            llm_results = analyze_cdsco_failure_batch(items)
            if any(llm_results.get(str(j)) for j in range(len(batch))):
                break
            attempts += 1
            wait = 60 * attempts
            if wait > args.max_wait_seconds:
                print("rate-limited for too long; aborting, backlog remains")
                db.commit()
                db.close()
                return
            print(f"rate-limited; sleeping {wait}s")
            time.sleep(wait)

        for j, rec in enumerate(batch):
            analysis = llm_results.get(str(j), {})
            if not analysis:
                skipped += 1
                continue
            raw = rec.raw_details or {}
            mfr = raw.get("manufacturer", "")
            prior = sum(
                1 for r in rows
                if (r.raw_details or {}).get("manufacturer", "") == mfr
                and r.event_id != rec.event_id
            )
            base = calculate_base_score(rec.event_type, analysis, rec.event_date)
            rec.llm_analysis = analysis
            rec.score = round(base * recency_weight(rec.event_date)) + repeat_offender_bonus(prior)
            updated += 1
        db.commit()
        print(f"processed {len(batch)} (updated={updated} skipped={skipped})")

    db.close()
    print(f"DONE updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
