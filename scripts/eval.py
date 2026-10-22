"""Gemma reader vs a keyword/regex baseline on data/test_sms.jsonl. Writes runs/eval-<model>.json."""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from reader import MODEL, THINKING, check, parse_date, read_sms  # noqa: E402

FAILED = re.compile(r"fail|pending|not (?:been )?(?:credited|dispensed|completed)|could not|declined|timed out|deemed|will be (?:auto )?revers|will be refunded|refund will", re.I)
BACK = re.compile(r"reversal|refund of|credited back|revers(?:ed|al) of", re.I)


def baseline(sms: str):
    """What a careful regex app would do: keywords for the status, the first Rs/INR amount, the first long number."""
    kind = "not_failed"
    if BACK.search(sms) and re.search(r"credited", sms, re.I):
        kind = "reversal"
    elif FAILED.search(sms):
        s = sms.lower()
        if "atm" in s:
            kind = "atm"
        elif "imps" in s:
            kind = "imps"
        elif "aeps" in s:
            kind = "aeps"
        elif "wallet" in s:
            kind = "wallet"
        elif "card" in s:
            kind = "card_online" if "online" in s else "pos"
        else:
            kind = "upi_merchant" if re.search(r"store|swiggy|amazon|zomato|jio|medical|merchant|@", sms, re.I) else "upi_transfer"
    m = re.search(r"(?:Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)", sms)
    amount = float(m.group(1).replace(",", "")) if m else None
    ref = (re.search(r"\b(?:T\d{13}|\d{12})\b", sms) or [""])[0]
    found = None
    for dm in re.finditer(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[-/.](?:\d{1,2}|[A-Z][a-z]{2})[-/.]\d{2,4}|\d{1,2}[A-Z][a-z]{2}\d{2}|\d{1,2} [A-Z][a-z]{2} \d{4})\b", sms):
        found = parse_date(dm.group(0))
        if found:
            break
    return {"kind": kind, "amount": amount, "date": found, "ref": ref}


def score(rows):
    n = len(rows)
    failed = [r for r in rows if r["gold"]["kind"] not in ("not_failed", "reversal")]
    ok = [r for r in rows if r["gold"]["kind"] not in ("not_failed", "reversal")]
    return {
        "kind_exact": sum(r["pred"]["kind"] == r["gold"]["kind"] for r in rows) / n,
        "missed_failed": sum(r["pred"]["kind"] in ("not_failed", "reversal") for r in failed),
        "false_alarm": sum(r["pred"]["kind"] not in ("not_failed", "reversal") for r in rows if r["gold"]["kind"] == "not_failed"),
        "deadline_rule_right": sum(r["pred"]["kind"] == r["gold"]["kind"] for r in ok) / len(ok),
        "amount_exact": sum(r["pred"]["amount"] == r["gold"]["amount"] for r in failed) / len(failed),
        "date_exact": sum((r["pred"]["date"] or "") == r["gold"]["date"] for r in failed) / len(failed),
        "ref_exact": sum(r["pred"]["ref"] == r["gold"]["ref"] for r in failed) / len(failed),
        "sec_per_sms": sum(r["sec"] for r in rows) / n,
    }


def main():
    gold = [json.loads(l) for l in (ROOT / "data/test_sms.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = {}
    label = f"{MODEL} (thinking: {THINKING or 'default'})"
    for name in ["baseline", label]:
        rows = []
        for g in gold:
            t = time.time()
            if name == "baseline":
                p = baseline(g["sms"])
                p["date"] = p["date"].isoformat() if p["date"] else ""
            else:
                r = read_sms(g["sms"])
                p = {"kind": r.kind, "amount": r.amount, "date": r.txn_date.isoformat() if r.txn_date else "", "ref": r.ref, "problems": r.problems}
            rows.append({"id": g["id"], "gold": g, "pred": p, "sec": time.time() - t})
        out[name] = {"score": score(rows), "rows": rows}
        print(name, json.dumps(out[name]["score"], indent=1))
        for r in rows:
            bad = [k for k in ("kind", "amount", "date", "ref") if (r["pred"].get(k) or "") != (r["gold"].get(k) or "")]
            if bad:
                print("  #%d %s gold=%s pred=%s" % (r["id"], bad, [r["gold"][k] for k in bad], [r["pred"].get(k) for k in bad]))
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / f"runs/eval-{MODEL}-{THINKING or 'default'}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
