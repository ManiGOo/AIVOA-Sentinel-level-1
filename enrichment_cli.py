import argparse
import asyncio
import json

from db_setup import init_db
from enrichment_tasks import fetch_external_evidence


async def main():
    parser = argparse.ArgumentParser(
        description="Run the enrichment adapters ad-hoc (bypasses Temporal).")
    parser.add_argument("--source", default="fda",
                        choices=["fda", "eudragmdp"])
    parser.add_argument("--mfr", action="append", required=True,
                        help="Firm/manufacturer to search (repeatable)")
    parser.add_argument("--no-classify", action="store_true",
                        help="Skip Groq paper-QMS classification")
    args = parser.parse_args()

    init_db()
    result = await fetch_external_evidence(
        args.mfr, args.source, classify=not args.no_classify)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
