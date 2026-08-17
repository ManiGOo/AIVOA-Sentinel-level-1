"""Scrape labelled phone numbers from the company websites already saved in
``company_leads`` and store them in ``company_phones``.

Usage:
    venv/bin/python phone_backfill.py               # every lead with a website
    venv/bin/python phone_backfill.py --keys "cap"  # only matching company keys
    venv/bin/python phone_backfill.py --dry-run     # scrape but don't save
"""
import argparse
import sys

from db_setup import SessionLocal, CompanyLead, CompanyPhone
from lead_research_tasks import _scrape_site_text, _extract_labeled_phones


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*", default=[], help="company_key substring filter")
    ap.add_argument("--dry-run", action="store_true", help="scrape but don't write")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(CompanyLead).filter(
            CompanyLead.website.isnot(None),
            CompanyLead.website != "",
        )
        if args.keys:
            q = q.filter(CompanyLead.company_key.contains(args.keys[0]))
        rows = q.order_by(CompanyLead.company_key).all()
    finally:
        db.close()

    if not rows:
        print("No company leads with a website found.")
        sys.exit(0)

    print(f"Scraping {len(rows)} company website(s) for labelled phone numbers...\n")
    total = 0
    for row in rows:
        pages = _scrape_site_text(row.website)
        labeled = _extract_labeled_phones(pages)
        total += len(labeled)
        print(f"{row.company_key}: {len(labeled)} phone(s)  ({row.website})")
        for p in labeled:
            print(f"    [{p['label']:<12}] {p['phone']:<20} {p.get('page_url', '')}")
            if p.get("tel_href"):
                print(f"        tel-link: {p['tel_href']}")
        if labeled and not args.dry_run:
            save_labeled(row.company_key, labeled)
    print(f"\nDone. {total} labelled phone(s) found.")

    if args.dry_run:
        print("(--dry-run: nothing written to company_phones)")


def save_labeled(company_key: str, labeled: list):
    """Replace company_key's phone rows with freshly scraped ones."""
    db = SessionLocal()
    try:
        db.query(CompanyPhone).filter(
            CompanyPhone.company_key == company_key).delete(synchronize_session=False)
        for p in labeled:
            db.add(CompanyPhone(
                company_key=company_key,
                phone=p.get("phone", ""),
                phone_clean=p.get("phone_clean", ""),
                label=p.get("label", "Unlabeled"),
                page_url=p.get("page_url", ""),
                context=p.get("context", ""),
                tel_href=p.get("tel_href", ""),
                source="company_website",
            ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"    ERROR saving phones for {company_key}: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
