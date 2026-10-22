"""Paisa Wapas web app: read a failed-payment SMS, then follow it with a Temporal workflow.

Simulation (default, what the live demo uses): the real workflow runs on Temporal's time-skipping test
server, so 40 days of waiting play out in seconds. Real mode: set PAISA_TEMPORAL=host:port (for example
`temporal server start-dev` on your laptop) and the same workflow waits the real days.
"""
import asyncio
import os
import uuid
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import reader
from rules import CIRCULAR, PER_DAY, RULES, verdict
from workflow import Case, Draft, PaisaWapas, draft_letter

app = FastAPI(title="Paisa Wapas")
STATIC = Path(__file__).parent / "static"
IST = timezone(timedelta(hours=5, minutes=30))
REAL = os.environ.get("PAISA_TEMPORAL", "")
sim_lock = asyncio.Lock()  # one time-skipping server at a time keeps a small instance responsive


class ReadBody(BaseModel):
    sms: str = Field(min_length=10, max_length=1200)
    received: str | None = None  # ISO date the SMS arrived, used only when the SMS has no date


class SimBody(BaseModel):
    reading: dict
    back_after_days: int | None = Field(default=None, ge=0, le=60)  # None = the money never comes back
    letter_sent_after_days: int = Field(default=1, ge=0, le=10)  # days after the bank letter is ready


def today_ist() -> date:
    return datetime.now(IST).date()


@app.post("/api/read")
def read(body: ReadBody):
    received = date.fromisoformat(body.received) if body.received else today_ist()
    try:
        r = reader.read_sms(body.sms, received)
    except Exception as e:  # model or network trouble is shown, not hidden
        raise HTTPException(503, f"Couldn't read the SMS right now ({type(e).__name__}). Try again in a minute.")
    out = {"reading": r.as_json(), "sms": body.sms}
    if r.kind in RULES and r.txn_date:
        v = verdict(r.kind, r.txn_date, today_ist())
        out["verdict"] = {"rule": asdict(v.rule), "deadline": v.deadline.isoformat(), "days_late": v.days_late,
                          "compensation": v.compensation, "per_day": PER_DAY, "circular": CIRCULAR}
    return out


def case_from(reading: dict, sms: str) -> Case:
    if reading.get("kind") not in RULES or not reading.get("txn_date") or not reading.get("amount"):
        raise HTTPException(422, "This SMS isn't a failed debit with an amount and a date, so there is nothing to follow.")
    return Case(uuid.uuid4().hex[:12], reading["kind"], float(reading["amount"]), reading["txn_date"],
                reading.get("ref") or "", reading.get("bank") or "", reading.get("payee") or "", sms)


async def at_day(env, txn: date, days: int) -> None:
    target = datetime(txn.year, txn.month, txn.day, 12, tzinfo=timezone.utc) + timedelta(days=days)
    for _ in range(60):
        gap = target - await env.get_current_time()
        if gap.total_seconds() <= 0:
            return
        try:
            await env.sleep(gap)
        except RPCError:  # the test server won't skip time while an activity (a Gemma letter) is running
            await asyncio.sleep(1)


async def settle(handle, want=None, tries=60):
    """Gives the worker a moment to run what a timer just woke up."""
    s = await handle.query(PaisaWapas.get_status)
    for _ in range(tries):
        if want is None or s.stage in want:
            break
        await asyncio.sleep(0.1)
        s = await handle.query(PaisaWapas.get_status)
    return s


@app.post("/api/simulate")
async def simulate(body: SimBody):
    case = case_from(body.reading, body.reading.get("sms", ""))
    requests: list[Draft] = []

    # The time-skipping server keeps skipping while a slow call is in flight, which would time a real
    # model call out mid-preview. So the preview records each letter request instantly, and Gemma writes
    # those exact letters once the run is over. Real mode runs the real activity inside Temporal.
    @activity.defn(name="draft_letter")
    async def record_letter(d: Draft) -> str:
        requests.append(d)
        return f"letter:{len(requests) - 1}"

    async with sim_lock:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            # The test clock can't go back, so the preview starts as if the SMS arrived today.
            txn = (await env.get_current_time()).date()
            case = replace(case, txn_date=txn.isoformat())
            await at_day(env, txn, 0)
            async with Worker(env.client, task_queue="sim", workflows=[PaisaWapas], activities=[record_letter]):
                status, still_open = await run_story(env, case, txn, body)

    for key, text in list(status.letters.items()):
        if text.startswith("letter:"):
            status.letters[key] = await draft_letter(requests[int(text.split(":")[1])])
    out = {"status": asdict(status), "case": asdict(case)}
    if still_open:
        out["open"] = True
    return out


async def run_story(env, case: Case, txn: date, body: SimBody):
    h = await env.client.start_workflow(PaisaWapas.run, case, id=case.case_id, task_queue="sim")
    deadline_days = RULES[case.kind].days
    back = body.back_after_days
    if back is not None and back <= deadline_days:
        await at_day(env, txn, back)
        await h.signal(PaisaWapas.money_back, (txn + timedelta(days=back)).isoformat())
        return await h.result(), False
    await at_day(env, txn, deadline_days + 1)
    await settle(h, {"letter_ready"})
    sent = deadline_days + 1 + body.letter_sent_after_days
    await at_day(env, txn, sent)
    await h.signal(PaisaWapas.complaint_sent, (txn + timedelta(days=sent)).isoformat())
    if back is None or back > sent + 30:
        await at_day(env, txn, sent + 31)
        await settle(h, {"rbi_ready"})
    if back is None:
        return await settle(h), True
    await at_day(env, txn, back)
    await h.signal(PaisaWapas.money_back, (txn + timedelta(days=back)).isoformat())
    return await h.result(), False


_client = None


async def real_client():
    global _client
    if not REAL:
        raise HTTPException(400, "Real mode is off on this server. Run it on your laptop with PAISA_TEMPORAL set.")
    if _client is None:
        _client = await Client.connect(REAL)
    return _client


@app.post("/api/follow")
async def follow(body: SimBody):
    c = await real_client()
    case = case_from(body.reading, body.reading.get("sms", ""))
    await c.start_workflow(PaisaWapas.run, case, id=case.case_id, task_queue="paisa-wapas")
    return {"case_id": case.case_id}


@app.get("/api/case/{case_id}")
async def status(case_id: str):
    c = await real_client()
    return asdict(await c.get_workflow_handle(case_id).query(PaisaWapas.get_status))


@app.post("/api/case/{case_id}/{event}")
async def event(case_id: str, event: str, on: str | None = None):
    c = await real_client()
    h = c.get_workflow_handle(case_id)
    day = on or today_ist().isoformat()
    signals = {"money-back": PaisaWapas.money_back, "letter-sent": PaisaWapas.complaint_sent}
    if event not in signals:
        raise HTTPException(404, "Unknown event.")
    await h.signal(signals[event], day)
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"real": bool(REAL), "model": reader.MODEL, "today": today_ist().isoformat()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
