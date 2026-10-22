"""RBI failed-transaction rules (DPSS.CO.PD No.629/02.01.014/2019-20, 20 Sep 2019, in force 15 Oct 2019).

The model never decides a deadline or an amount. It only says which kind of transaction an SMS describes;
everything below is read from the circular's annex (copy in data/rbi-tat-2019.txt).
"""
from dataclasses import dataclass
from datetime import date, timedelta

CIRCULAR = "RBI/2019-20/67, DPSS.CO.PD No.629/02.01.014/2019-20 (20 Sep 2019)"
PER_DAY = 100  # "₹100/- per day of delay" is the same for every row of the annex


@dataclass(frozen=True)
class Rule:
    kind: str
    annex_row: str
    incident: str
    days: int  # reversal must be done by T + days


RULES = {r.kind: r for r in [
    Rule("atm", "1(a)", "ATM or micro-ATM: account debited but cash not dispensed", 5),
    Rule("card_to_card", "2(a)", "Card to card transfer: beneficiary card not credited", 1),
    Rule("pos", "2(b)", "Card at a shop (PoS): account debited, no charge-slip", 5),
    Rule("card_online", "2(c)", "Card online (e-commerce): debited, merchant got no confirmation", 5),
    Rule("imps", "3(a)", "IMPS: account debited but beneficiary not credited", 1),
    Rule("upi_transfer", "4(a)", "UPI to a person: account debited but beneficiary not credited", 1),
    Rule("upi_merchant", "4(b)", "UPI to a merchant: debited, merchant got no confirmation", 5),
    Rule("aeps", "5(a)", "Aadhaar Pay / AePS: debited, merchant got no confirmation", 5),
    Rule("nach", "7(a)", "NACH: delay in crediting the beneficiary or the reversal", 1),
    Rule("wallet", "8(b)", "Wallet / prepaid card (on-us): not credited or no confirmation", 1),
]}


@dataclass(frozen=True)
class Verdict:
    rule: Rule
    txn_date: date
    deadline: date          # last day the reversal is on time
    days_late: int          # as of `today`
    compensation: int       # rupees owed as of `today`, credited suo moto by the bank (circular para 5)
    can_go_to_rbi_on: date  # RB-IOS 2021: 30 days after the written complaint to the bank, if no reply


def verdict(kind: str, txn_date: date, today: date, reversed_on: date | None = None,
            bank_complaint_on: date | None = None) -> Verdict:
    rule = RULES[kind]
    deadline = txn_date + timedelta(days=rule.days)
    end = reversed_on or today
    days_late = max(0, (end - deadline).days)
    complained = bank_complaint_on or today
    return Verdict(rule, txn_date, deadline, days_late, days_late * PER_DAY, complained + timedelta(days=30))
