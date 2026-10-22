"""One failed transaction = one Temporal workflow that waits, for weeks if it has to, so the owner does not.

Day T: the SMS is read. T + 1 (or T + 5): if no reversal arrived, the bank letter is drafted and the RBI
compensation clock is running. 30 days after the owner sends that letter with no answer: the RBI complaint
is drafted. Every date and rupee comes from rules.py; Gemma only writes the wording.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from rules import CIRCULAR, PER_DAY, RULES, verdict


@dataclass
class Case:
    case_id: str
    kind: str
    amount: float
    txn_date: str  # ISO date
    ref: str
    bank: str
    payee: str
    sms: str
    owner: str = "Account holder"


@dataclass
class Event:
    on: str
    what: str
    detail: str = ""


@dataclass
class Status:
    stage: str
    deadline: str
    days_late: int
    compensation: int
    timeline: list[Event] = field(default_factory=list)
    letters: dict[str, str] = field(default_factory=dict)


@dataclass
class Draft:
    to: str
    case: Case
    days_late: int
    compensation: int
    deadline: str
    complaint_sent_on: str = ""


LETTER_PROMPT = """Write a short, polite, firm complaint in simple English from an Indian bank customer. Use ONLY the facts given, keep every number exactly as written, do not add any fact, rule or amount that is not listed. 120-180 words, no subject line, start with "Dear Sir/Madam,".

Facts:
"""


@activity.defn
async def draft_letter(d: Draft) -> str:
    from reader import gemma
    c = d.case
    rule = RULES[c.kind]
    facts = [
        f"To: {d.to}",
        f"Account holder: {c.owner}",
        f"Transaction: Rs {c.amount:,.2f} on {c.txn_date}, reference {c.ref or 'not shown in the SMS'}, paid to {c.payee or 'unknown'}",
        f"What happened: {rule.incident}",
        f"Rule: RBI circular {CIRCULAR}, annex row {rule.annex_row}: reversal due by {d.deadline}",
        f"Days late so far: {d.days_late}; compensation due so far at Rs {PER_DAY} per day: Rs {d.compensation}",
        "Request: reverse the amount and credit the compensation, which the circular says must be paid without waiting for a claim",
    ]
    if d.complaint_sent_on:
        facts.append(f"I complained to the bank in writing on {d.complaint_sent_on} and had no resolution within 30 days")
    text = (await asyncio.to_thread(gemma, LETTER_PROMPT + "\n".join(facts), False)).strip()
    missing = [s for s in [f"{c.amount:,.2f}", str(d.compensation), d.deadline] if s not in text]
    if missing:  # the model dropped or changed a number: fall back to the plain letter built from the facts
        text = "Dear Sir/Madam,\n\n" + "\n".join(facts[1:]) + "\n\nYours faithfully,\n" + c.owner
    return text


# A letter is a draft a person sends later, so a slow model is fine; days (not minutes) also keeps the
# time-skipping preview from timing the call out while it fast-forwards past it.
LETTER_OPTS = {"start_to_close_timeout": timedelta(days=2), "retry_policy": RetryPolicy(maximum_attempts=3)}


@workflow.defn
class PaisaWapas:
    def __init__(self) -> None:
        self.reversed_on: date | None = None
        self.complaint_sent_on: date | None = None
        self.compensation_paid = 0
        self.status = Status("waiting", "", 0, 0)

    def _today(self) -> date:
        return workflow.now().date()

    def _log(self, what: str, detail: str = "") -> None:
        self.status.timeline.append(Event(self._today().isoformat(), what, detail))

    def _refresh(self, case: Case) -> None:
        v = verdict(case.kind, date.fromisoformat(case.txn_date), self._today(), self.reversed_on, self.complaint_sent_on)
        self.status.deadline, self.status.days_late, self.status.compensation = v.deadline.isoformat(), v.days_late, v.compensation

    @workflow.run
    async def run(self, case: Case) -> Status:
        self._refresh(case)
        rule = RULES[case.kind]
        self._log("Read the SMS", f"{rule.incident}. Rs {case.amount:,.2f}, ref {case.ref or '-'}")
        self._log("Waiting quietly", f"RBI says the money must be back by {self.status.deadline} (T + {rule.days}). Nothing to do until then.")

        deadline_end = date.fromisoformat(self.status.deadline) + timedelta(days=1)
        if await self._wait_until(deadline_end, lambda: self.reversed_on is not None):
            return self._close(case, "Money came back on time")

        self._refresh(case)
        self.status.stage = "late"
        self._log("Deadline passed", f"Not reversed. Rs {PER_DAY} a day is now owed to you, paid without asking (circular para 5).")
        self.status.letters["bank"] = await workflow.execute_activity(
            draft_letter, Draft(f"The Nodal Officer, {case.bank or 'my bank'}", case, self.status.days_late, self.status.compensation, self.status.deadline),
            **LETTER_OPTS)
        self.status.stage = "letter_ready"
        self._log("Bank letter ready", "Send it from the bank's complaint page or email. Tap 'sent' when done.")

        while self.reversed_on is None:
            if self.complaint_sent_on is None:
                await workflow.wait_condition(lambda: self.complaint_sent_on is not None or self.reversed_on is not None)
                continue
            rbi_day = self.complaint_sent_on + timedelta(days=30)
            if await self._wait_until(rbi_day, lambda: self.reversed_on is not None):
                break
            self._refresh(case)
            self.status.letters["rbi"] = await workflow.execute_activity(
                draft_letter, Draft("The RBI Ombudsman (cms.rbi.org.in)", case, self.status.days_late, self.status.compensation,
                                    self.status.deadline, self.complaint_sent_on.isoformat()),
                **LETTER_OPTS)
            self.status.stage = "rbi_ready"
            self._log("RBI complaint ready", "30 days with no fix. File it at cms.rbi.org.in; it is free.")
            await workflow.wait_condition(lambda: self.reversed_on is not None)

        return self._close(case, "Money came back late")

    async def _wait_until(self, day: date, done) -> bool:
        """Sleeps until `day` (or until `done()`); a durable timer, so a laptop restart does not lose it."""
        seconds = (workflow.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=(day - self._today()).days) - workflow.now()).total_seconds()
        try:
            await workflow.wait_condition(done, timeout=timedelta(seconds=max(1, seconds)))
            return True
        except TimeoutError:
            return done()

    def _close(self, case: Case, what: str) -> Status:
        self._refresh(case)
        owed = self.status.compensation - self.compensation_paid
        self.status.stage = "closed" if owed <= 0 else "compensation_due"
        detail = f"Reversed on {self.reversed_on}."
        if self.status.compensation:
            detail += f" {self.status.days_late} day(s) late: Rs {self.status.compensation} compensation is owed, Rs {self.compensation_paid} received."
        self._log(what, detail)
        return self.status

    @workflow.signal
    def money_back(self, on: str) -> None:
        self.reversed_on = date.fromisoformat(on)

    @workflow.signal
    def complaint_sent(self, on: str) -> None:
        self.complaint_sent_on = date.fromisoformat(on)
        self._log("You sent the bank letter", "The RBI clock starts: 30 days.")

    @workflow.signal
    def compensation_received(self, amount: int) -> None:
        self.compensation_paid += amount

    @workflow.query
    def get_status(self) -> Status:
        return self.status
