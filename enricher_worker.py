import asyncio
import os

from db_setup import init_db
from temporalio.client import Client
from temporalio.worker import Worker
from enrichment_tasks import EnrichmentWorkflow, fetch_external_evidence
from web_evidence_tasks import (
    WebEvidenceWorkflow,
    generate_queries_activity,
    search_web_for_queries,
    fetch_and_classify_articles,
)


async def main():
    init_db()
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    worker = Worker(
        client,
        task_queue="enrichment-task-queue",
        workflows=[EnrichmentWorkflow, WebEvidenceWorkflow],
        activities=[
            fetch_external_evidence,
            generate_queries_activity,
            search_web_for_queries,
            fetch_and_classify_articles,
        ],
    )
    print("Starting Temporal Worker for Enrichment on 'enrichment-task-queue'...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
