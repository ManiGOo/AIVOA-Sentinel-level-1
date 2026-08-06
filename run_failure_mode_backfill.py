"""Start FailureModeBackfillWorkflow on Temporal and stream its per-chunk
progress until completion.

Run:  venv/bin/python run_failure_mode_backfill.py
"""
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from temporalio.client import Client

from temporal_tasks import FailureModeBackfillWorkflow

WORKFLOW_ID = f"failure-mode-backfill-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


async def main():
    client = await Client.connect(os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    handle = await client.start_workflow(
        FailureModeBackfillWorkflow.run,
        id=WORKFLOW_ID,
        task_queue="scraper-task-queue",
    )
    print(f"started {handle.id}", flush=True)
    while True:
        p = await handle.query(FailureModeBackfillWorkflow.get_progress)
        print(
            f"  phase={p['phase']} chunks={p['chunks_done']}/{p['chunks_total']} "
            f"events={p['events_total']} unique={p['unique_total']} "
            f"updated={p['updated']} finished={p['finished']}",
            flush=True,
        )
        if p["finished"]:
            print("final:", p["final"], flush=True)
            break
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
