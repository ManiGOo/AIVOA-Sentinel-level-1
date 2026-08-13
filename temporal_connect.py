import asyncio
import os

from temporalio.client import Client


async def connect_with_retry(max_retries=None, base_delay=2.0, max_delay=60.0):
    """Connect to Temporal, retrying on transient errors (e.g. DNS flakiness,
    server not up yet) so the worker never dies on a temporary failure."""
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    attempt = 0
    while True:
        try:
            return await Client.connect(host)
        except Exception as exc:
            attempt += 1
            if max_retries is not None and attempt >= max_retries:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            print(
                f"[{host}] Temporal connect attempt {attempt} failed: {exc}; "
                f"retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
