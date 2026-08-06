import os
import sys
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import or_  # noqa: E402
from db_setup import SessionLocal, WebEvidence, RegulatoryEvent  # noqa: E402
from cognitive_engine import classify_web_evidence  # noqa: E402

FAILED_SUMMARIES = ("Error parsing", "LLM unavailable", "No text")


def main(limit: int = 200):
    db = SessionLocal()
    done = skipped = 0
    try:
        rows = (
            db.query(WebEvidence)
            .filter(
                WebEvidence.fetch_status == "fetched",
                or_(
                    WebEvidence.classification.is_(None),
                    WebEvidence.relevance_score.is_(None),
                    WebEvidence.classification["summary"].astext.in_(FAILED_SUMMARIES),
                    WebEvidence.classification == {},
                ),
            )
            .limit(limit)
            .all()
        )
        if not rows:
            print("No rows match 'fetched + missing classification'.")
            return

        for i, row in enumerate(rows):
            cls = row.classification or {}
            summary = cls.get("summary") or ""
            if cls.get("relevance_score") is not None and summary not in FAILED_SUMMARIES:
                skipped += 1
                continue

            if not row.full_text:
                skipped += 1
                continue

            event = db.query(RegulatoryEvent).filter(
                RegulatoryEvent.event_id == row.event_id
            ).first()
            record_details = (event.raw_details or {}) if event else {}

            new_cls = classify_web_evidence(row.full_text, record_details)
            row.classification = new_cls
            row.relevance_score = new_cls.get("relevance_score") or 0
            done += 1
            print(f"[{done}] {row.title[:60]} -> score={row.relevance_score} "
                  f"act={new_cls.get('regulatory_action')}")

            if done % 10 == 0:
                db.commit()
        db.commit()
    finally:
        db.close()
    print(f"\nReclassified: {done}, skipped: {skipped}")


if __name__ == "__main__":
    main(limit=int(os.getenv("LIMIT", "200")))
