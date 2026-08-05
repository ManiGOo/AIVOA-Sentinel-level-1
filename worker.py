import asyncio
import os
from temporalio.client import Client
from temporalio.worker import Worker
from temporal_tasks import (
    CDSCOScraperWorkflow,
    scrape_cdsco_endpoint,
    process_batch_with_llm,
    save_to_db,
)

async def main():
    # Connect to the Temporal server (host override supported for Docker)
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    
    # Create the worker
    worker = Worker(
        client,
        task_queue="scraper-task-queue",
        workflows=[CDSCOScraperWorkflow],
        activities=[scrape_cdsco_endpoint, process_batch_with_llm, save_to_db],
    )
    
    print("Starting Temporal Worker for CDSCO Scraper on 'scraper-task-queue'...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
