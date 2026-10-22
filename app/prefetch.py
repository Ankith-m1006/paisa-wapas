"""Downloads Temporal's time-skipping test server at image build time, so the first preview isn't slow."""
import asyncio

from temporalio.testing import WorkflowEnvironment


async def main():
    async with await WorkflowEnvironment.start_time_skipping():
        pass


asyncio.run(main())
