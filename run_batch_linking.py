import asyncio
import os
import time
from sqlalchemy import func
from db_setup import SessionLocal, RegulatoryEvent, EnrichmentCheck
from temporalio.client import Client
from temporal_tasks import mfr_key
from company_names import clean_company_name

def get_firms():
    db = SessionLocal()
    try:
        # Get already checked company keys
        checked = {r[0] for r in db.query(EnrichmentCheck.company_key).filter(EnrichmentCheck.status == 'completed').all() if r[0]}
        
        # Get distinct manufacturer names ordered by event count descending
        mfr_expr = func.coalesce(RegulatoryEvent.raw_details['manufacturer'].astext, '')
        rows = db.query(mfr_expr.label('mfr'), func.count(RegulatoryEvent.event_id))\
                 .group_by(mfr_expr).order_by(func.count(RegulatoryEvent.event_id).desc()).all()
                 
        # Filter out empty, placeholder keys, and already checked keys
        firms_to_check = []
        for m, _ in rows:
            if not mfr_key(m):
                continue
            ckey = clean_company_name(m).strip().lower() or mfr_key(m)
            if ckey in checked:
                continue
            firms_to_check.append(m)
        return firms_to_check
    finally:
        db.close()

async def main():
    # Connect to Temporal
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    print(f"Connecting to Temporal at {temporal_host}...")
    c = await Client.connect(temporal_host)
    
    firms = get_firms()
    print("Firms to check:", len(firms))
    
    # Trigger ScrapedRecordCheckWorkflow for each firm
    ids = []
    for i, f in enumerate(firms):
        wid = f"regulatory-check-batch-{i}-{int(time.time())}"
        await c.start_workflow(
            "ScrapedRecordCheckWorkflow",
            args=[f, "all"],
            id=wid,
            task_queue="enrichment-task-queue"
        )
        ids.append(wid)
        if (i + 1) % 20 == 0:
            print(f"Started {i + 1} / {len(firms)} workflows...")
            await asyncio.sleep(0.5)  # gentle pacing
            
    print(f"All {len(ids)} workflows triggered successfully.")

if __name__ == "__main__":
    asyncio.run(main())
