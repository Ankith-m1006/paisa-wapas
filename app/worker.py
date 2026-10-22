"""Real-mode worker: `temporal server start-dev`, then `python app/worker.py` (PAISA_TEMPORAL defaults to localhost:7233)."""
import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from workflow import PaisaWapas, draft_letter


async def main():
    client = await Client.connect(os.environ.get("PAISA_TEMPORAL", "localhost:7233"))
    async with Worker(client, task_queue="paisa-wapas", workflows=[PaisaWapas], activities=[draft_letter]):
        print("Paisa Wapas worker running. Waiting, patiently.")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
