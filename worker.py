import asyncio
from concurrent.futures import ThreadPoolExecutor
from temporal_connect import connect_with_retry
from temporalio.worker import Worker
from temporal_tasks import (
    CDSCOScraperWorkflow,
    CDSCOEnrichmentWorkflow,
    FailureModeBackfillWorkflow,
    ScheduleMGapBackfillWorkflow,
    scrape_cdsco_endpoint,
    save_raw_to_db,
    load_enrichment_candidates,
    process_batch_with_llm,
    apply_enrichment_to_db,
    load_backfill_candidates,
    classify_failure_modes_activity,
    apply_failure_modes,
    classify_schedule_m_gap_activity,
    apply_schedule_m_gaps,
)

async def main():
    # Connect to the Temporal server (retries on transient DNS/connect errors)
    client = await connect_with_retry()
    
    # Create the worker
    worker = Worker(
        client,
        task_queue="scraper-task-queue",
        workflows=[
            CDSCOScraperWorkflow,
            CDSCOEnrichmentWorkflow,
            FailureModeBackfillWorkflow,
            ScheduleMGapBackfillWorkflow,
        ],
        activities=[
            scrape_cdsco_endpoint, save_raw_to_db,
            load_enrichment_candidates, process_batch_with_llm, apply_enrichment_to_db,
            load_backfill_candidates, classify_failure_modes_activity, apply_failure_modes,
            classify_schedule_m_gap_activity, apply_schedule_m_gaps,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=20),
    )
    
    print("Starting Temporal Worker for CDSCO Scraper on 'scraper-task-queue'...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
