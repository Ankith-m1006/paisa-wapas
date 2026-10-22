"""Reads one bank / UPI SMS. Gemma says what happened; code checks every value it claims against the SMS text."""
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

from rules import RULES

MODEL = os.environ.get("PAISA_MODEL", "gemma-4-26b-a4b-it")
# "minimal" answers in ~3 s instead of ~20 s; scripts/eval.py measures what it costs in accuracy.
THINKING = os.environ.get("PAISA_THINKING", "minimal")
KINDS = list(RULES) + ["reversal", "not_failed"]


def _env(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    for line in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise KeyError(name)


PROMPT = """You read one SMS from an Indian bank, card or UPI app and fill a JSON object. Copy values exactly as they appear in the SMS; never guess.

kind, one of:
- atm: ATM or micro-ATM withdrawal where cash was not dispensed but the account was debited
- card_to_card: card to card transfer, beneficiary card not credited
- pos: card swiped/tapped at a shop, debited but the payment failed or no slip
- card_online: card used online, debited but the order/payment failed
- imps: IMPS transfer debited but not credited to the beneficiary
- upi_transfer: UPI payment to a PERSON (a name or a personal VPA) that failed or is pending but the money was debited
- upi_merchant: UPI payment to a SHOP / business / app / bill that failed or is pending but the money was debited
- aeps: Aadhaar Pay / AePS debited but failed
- nach: NACH / ECS / mandate credit or reversal delayed
- wallet: wallet or prepaid card payment debited but failed
- reversal: money given BACK for an earlier failed transaction (refund, reversal, reversed, credited back)
- not_failed: anything else (a normal successful payment, a salary credit, an OTP, an offer, a reminder)

Fields: {"kind": ..., "amount": "<the amount exactly as written, digits only with optional decimals>", "date": "<the transaction date exactly as written, or empty>", "ref": "<UPI ref / UTR / RRN / transaction id exactly as written, or empty>", "bank": "<bank or app name, or empty>", "payee": "<who it was paid to, or empty>"}
Return only the JSON object.

SMS:
"""


@dataclass
class Reading:
    kind: str
    amount: float | None
    txn_date: date | None
    ref: str
    bank: str
    payee: str
    problems: list[str]

    def as_json(self):
        d = asdict(self)
        d["txn_date"] = self.txn_date.isoformat() if self.txn_date else None
        return d


def gemma(prompt: str, json_mode: bool = True) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={_env('GEMINI_API_KEY')}"
    config = {"temperature": 0.0}
    if json_mode:
        config["responseMimeType"] = "application/json"
    if THINKING:
        config["thinkingConfig"] = {"thinkingLevel": THINKING}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"] if not p.get("thought"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 4:
                time.sleep(8 * (attempt + 1))
                continue
            raise
        except (TimeoutError, urllib.error.URLError):
            if attempt < 4:
                continue
            raise


DATE_FORMATS = ["%d-%m-%y", "%d-%m-%Y", "%d/%m/%y", "%d/%m/%Y", "%d%b%y", "%d-%b-%y", "%d-%b-%Y", "%d %b %Y",
                "%d %b %y", "%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%b %d, %Y"]


def parse_date(text: str) -> date | None:
    text = text.strip().rstrip(".,").replace("Sept", "Sep")
    text = re.split(r"\s+\d{1,2}:\d{2}", text)[0].strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            pass
    return None


def _digits(s: str) -> str:
    return re.sub(r"[^\d.]", "", s.replace(",", ""))


def check(sms: str, raw: dict, sent_on: date | None = None) -> Reading:
    """The guard: a value Gemma returns is kept only if it is really in the SMS."""
    problems = []
    kind = raw.get("kind") if raw.get("kind") in KINDS else "not_failed"
    if raw.get("kind") not in KINDS:
        problems.append(f"unknown kind {raw.get('kind')!r}")

    amount = None
    a = _digits(str(raw.get("amount") or ""))
    flat = sms.replace(",", "")
    if a and a.count(".") <= 1 and any(float(n) == float(a) for n in re.findall(r"\d+(?:\.\d+)?", flat)):
        amount = float(a)
    elif a:
        problems.append(f"amount {a} is not in the SMS")

    ref = str(raw.get("ref") or "").strip()
    if ref and ref not in sms:
        problems.append(f"ref {ref} is not in the SMS")
        ref = ""

    d_text = str(raw.get("date") or "")
    txn_date = parse_date(d_text) if d_text and d_text in sms else None
    if d_text and d_text not in sms:
        problems.append(f"date {d_text!r} is not in the SMS")
    if txn_date is None and sent_on and kind in RULES:
        txn_date = sent_on
        problems.append("no date in the SMS, used the day it arrived")

    return Reading(kind, amount, txn_date, ref, str(raw.get("bank") or ""), str(raw.get("payee") or ""), problems)


def read_sms(sms: str, sent_on: date | None = None) -> Reading:
    out = gemma(PROMPT + sms)
    start = out.find("{")
    try:
        raw, _ = json.JSONDecoder().raw_decode(out[start:]) if start >= 0 else ({}, 0)
    except json.JSONDecodeError:
        raw = {}
    reading = check(sms, raw if isinstance(raw, dict) else {}, sent_on)
    if not raw:
        reading.problems.append("the model's answer couldn't be read; nothing was assumed")
    return reading
