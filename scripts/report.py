"""Markdown table from runs/eval-*.json. SMS 31-40 were written after the regex baseline, so they are held out for it."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from rules import RULES  # noqa: E402

HELD_OUT = set(range(31, 41))


def days(kind):
    return RULES[kind].days if kind in RULES else None


def ready(r):
    p, g = r["pred"], r["gold"]
    return days(p["kind"]) == days(g["kind"]) and p["amount"] == g["amount"] and (p["date"] or "") == g["date"]


def stats(rows):
    failed = [r for r in rows if r["gold"]["kind"] in RULES]
    return {
        "ready": f"{sum(ready(r) for r in failed)}/{len(failed)}",
        "chase": f"{sum((r['pred']['kind'] in RULES) != (r['gold']['kind'] in RULES) for r in rows)}/{len(rows)}",
        "amount": f"{sum(r['pred']['amount'] == r['gold']['amount'] for r in failed)}/{len(failed)}",
        "sec": sum(r["sec"] for r in rows) / len(rows),
    }


systems = {}
for f in sorted((ROOT / "runs").glob("eval-*-*.json")):
    for name, v in json.loads(f.read_text(encoding="utf-8")).items():
        systems.setdefault(name, v["rows"])

print("| | Letter-ready (deadline, amount and date right) | Wrong chase / don't-chase call | Amount right | Held-out 31-40: letter-ready | Time per SMS |")
print("|---|---|---|---|---|---|")
for name, rows in systems.items():
    a, h = stats(rows), stats([r for r in rows if r["id"] in HELD_OUT])
    t = "instant" if a["sec"] < 0.01 else f"{a['sec']:.1f} s"
    print(f"| {name} | {a['ready']} | {a['chase']} | {a['amount']} | {h['ready']} | {t} |")
