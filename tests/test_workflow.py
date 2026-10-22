import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from workflow import Case, Draft, PaisaWapas  # noqa: E402

T = date(2026, 10, 18)


@activity.defn(name="draft_letter")
async def fake_letter(d: Draft) -> str:
    return f"letter to {d.to}: Rs {d.compensation}"


def case(kind="upi_transfer"):
    return Case(str(uuid.uuid4()), kind, 1250.0, T.isoformat(), "229145670123", "SBI", "Ramesh Kumar", "sms")


async def start(env, c):
    return await env.client.start_workflow(PaisaWapas.run, c, id=c.case_id, task_queue="q")


async def status_at(env, h, day: date, stage: str):
    """Moves the clock to `day`, then gives the worker a moment to run what the timer woke up."""
    await sleep_to(env, day)
    for _ in range(50):
        s = await h.query(PaisaWapas.get_status)
        if s.stage == stage:
            break
        await asyncio.sleep(0.1)
    return s


async def sleep_to(env, day: date):
    target = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    await env.sleep(target - await env.get_current_time())


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        await sleep_to(e, T)
        async with Worker(e.client, task_queue="q", workflows=[PaisaWapas], activities=[fake_letter]):
            yield e


async def test_reversed_on_time_closes_with_nothing_owed(env):
    h = await start(env, case())
    await sleep_to(env, T + timedelta(days=1))
    await h.signal(PaisaWapas.money_back, (T + timedelta(days=1)).isoformat())
    s = await h.result()
    assert s.stage == "closed" and s.compensation == 0 and "bank" not in s.letters


async def test_late_reversal_drafts_bank_then_rbi_letter_and_counts_compensation(env):
    h = await start(env, case())
    s = await status_at(env, h, T + timedelta(days=3), "letter_ready")
    assert s.stage == "letter_ready" and s.deadline == "2026-10-19" and "bank" in s.letters

    await h.signal(PaisaWapas.complaint_sent, (T + timedelta(days=3)).isoformat())
    s = await status_at(env, h, T + timedelta(days=34), "rbi_ready")
    assert s.stage == "rbi_ready" and "rbi" in s.letters

    await h.signal(PaisaWapas.money_back, (T + timedelta(days=36)).isoformat())
    s = await h.result()
    assert s.days_late == 35 and s.compensation == 3500 and s.stage == "compensation_due"


async def test_merchant_upi_gets_five_days(env):
    h = await start(env, case("upi_merchant"))
    s = await status_at(env, h, T + timedelta(days=5), "waiting")
    assert s.stage == "waiting" and s.deadline == "2026-10-23"
    await h.signal(PaisaWapas.money_back, (T + timedelta(days=5)).isoformat())
    assert (await h.result()).compensation == 0
