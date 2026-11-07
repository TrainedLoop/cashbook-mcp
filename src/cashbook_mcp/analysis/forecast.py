"""Cash forecast: known commitments projected over the next 30 to 90 days."""

import calendar
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from cashbook_mcp.store.db import Database

from .filters import jsonable
from .metrics import Scope, scope_clause, totals
from .recurring import find_recurring, is_stopped

_ZERO = Decimal("0.00")
_CONFIDENT = ("high", "medium")
_WEEK = 7
_OVERDUE_DAYS = 35
_MAX_DAILY_DAYS = 31


def _month_start(d: date, offset: int) -> date:
    index = d.year * 12 + d.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _on_day(month: date, day: int) -> date:
    return month.replace(day=min(day, calendar.monthrange(month.year, month.month)[1]))


def _usual_day(days: list[int]) -> int:
    """Most frequent day; ties go to the one seen last (the list is oldest first)."""
    counts = Counter(days)
    return max(reversed(counts), key=lambda d: counts[d])


def _inflow(db: Database, today: date, scope: Scope) -> Decimal:
    """Average monthly income over the last three full calendar months."""
    total = _ZERO
    for back in (3, 2, 1):
        first = _month_start(today, -back)
        last = _month_start(today, -back + 1) - timedelta(days=1)
        total += totals(db, first, last, scope)["income"]
    return (total / 3).quantize(_ZERO)


def _scope_accounts(db: Database, scope: Scope) -> tuple[list[dict], list[dict], list[str]]:
    clause, params = scope_clause(scope)
    where = f" AND {clause}" if clause else ""
    cur = db.read()
    try:
        rows = cur.execute(
            "SELECT a.id, a.alias, a.name, a.type, a.balance, coalesce((SELECT s.purpose FROM account_settings s "
            "WHERE s.account_id = a.id), 'regular') FROM v_accounts a "
            f"JOIN connections c ON c.alias = a.alias WHERE a.currency = 'BRL'{where} "
            "ORDER BY a.alias, a.name, a.id",
            params,
        ).fetchall()
    finally:
        cur.close()
    accounts = [dict(zip(("id", "alias", "name", "type", "balance", "purpose"), r, strict=True)) for r in rows]
    regular = [a for a in accounts if a["purpose"] == "regular"]
    investment = sorted({a["alias"] for a in accounts if a["type"] == "BANK" and a["purpose"] != "regular"})
    return (
        [a for a in regular if a["type"] == "BANK"],
        [a for a in regular if a["type"] == "CREDIT"],
        investment,
    )


def _card_facts(db: Database, card_ids: list[str]) -> dict[str, dict]:
    """Per card: usual due day, usual closing day, last due date and the bank account that paid its latest matched bill."""
    facts: dict[str, dict] = {}
    cur = db.read()
    try:
        for card_id in card_ids:
            bills = cur.execute(
                "SELECT due_date, closing_date FROM v_bills WHERE account_id = ? ORDER BY due_date",
                [card_id],
            ).fetchall()
            payer = cur.execute(
                "SELECT t.account_id FROM v_bills b "
                "JOIN v_transactions t ON t.kind = 'card_bill_payment' "
                "AND t.kind_reason = 'matched_card_bill:' || b.id "
                "JOIN v_accounts ba ON ba.id = t.account_id AND ba.type = 'BANK' "
                "WHERE b.account_id = ? ORDER BY b.due_date DESC, t.date DESC, t.id LIMIT 1",
                [card_id],
            ).fetchone()
            closings = [c.day for _, c in bills if c is not None]
            dated = [(d, c) for d, c in bills if c is not None]
            facts[card_id] = {
                # Months between closing and due on the latest bill that has both (default: next month).
                "due_offset": (
                    (dated[-1][0].year * 12 + dated[-1][0].month)
                    - (dated[-1][1].year * 12 + dated[-1][1].month)
                    if dated
                    else 1
                ),
                "due_day": _usual_day([d.day for d, _ in bills]) if bills else None,
                "closing_day": _usual_day(closings) if closings else None,
                "payer": payer[0] if payer else None,
            }
    finally:
        cur.close()
    return facts


def _next_usual_due(due_day: int, after: date, *, strict: bool) -> date:
    """First date on the card's usual due day after `after` (or on it, unless strict)."""
    due = _on_day(after, due_day)
    if due < after or (strict and due == after):
        due = _on_day(_month_start(after, 1), due_day)
    return due


def _bill_due(fact: dict, d: date) -> date:
    """Due date of the bill a purchase on `d` falls into.

    With a known closing day: the bill closing on or after `d`, due `due_offset` months later. Pluggy
    lists closed bills only (closing_date is usually empty), so otherwise: the card's next due date after `d`.
    """
    if fact["closing_day"] is None:
        return _next_usual_due(fact["due_day"], d, strict=True)
    closing_month = _month_start(d, 0 if d.day <= fact["closing_day"] else 1)
    return _on_day(_month_start(closing_month, fact["due_offset"]), fact["due_day"])


def _build_events(db: Database, today: date, end: date, banks: list[dict], cards: list[dict]):
    """All events in (today..end] as (date, amount, source, confidence, account_id), plus notes."""
    notes: list[str] = []
    events: list[tuple] = []
    bank_ids = {b["id"] for b in banks}
    card_ids = [c["id"] for c in cards]
    facts = _card_facts(db, card_ids)
    fallback = max(banks, key=lambda b: b["balance"])["id"] if banks else None
    unassigned = []

    def payer_of(card_id: str) -> str | None:
        paid_by = facts[card_id]["payer"]
        if paid_by in bank_ids:
            return paid_by
        unassigned.append(card_id)
        return fallback

    # Projections for a card start after this date: the last unpaid bill, or the balance standing in for it.
    cover: dict[str, date] = {}
    balance_used = False

    cur = db.read()
    try:
        for card in cards:
            card_id = card["id"]
            unpaid = cur.execute(
                "SELECT b.due_date, b.total_amount FROM v_bills b WHERE b.account_id = ? "
                "AND b.currency = 'BRL' AND b.due_date >= ? AND b.total_amount > 0 "
                "AND NOT EXISTS (SELECT 1 FROM v_transactions t WHERE t.kind = 'card_bill_payment' "
                "AND t.kind_reason = 'matched_card_bill:' || b.id) ORDER BY b.due_date",
                [card_id, today],
            ).fetchall()
            for due, total in unpaid:
                if due <= end:
                    events.append((due, -total, "bill", "high", payer_of(card_id)))
            if unpaid:
                cover[card_id] = unpaid[-1][0]
            elif facts[card_id]["due_day"] is not None:
                due = _next_usual_due(facts[card_id]["due_day"], today, strict=False)
                cover[card_id] = due
                if due <= end and card["balance"] != 0:
                    events.append((due, -abs(card["balance"]), "card_balance", "low", payer_of(card_id)))
                    balance_used = True

            # Unpaid bills that fell due recently: maybe paid without a match, so low confidence.
            overdue = cur.execute(
                "SELECT b.due_date, b.total_amount FROM v_bills b WHERE b.account_id = ? "
                "AND b.currency = 'BRL' AND b.due_date >= ? AND b.due_date < ? AND b.total_amount > 0 "
                "AND NOT EXISTS (SELECT 1 FROM v_transactions t WHERE t.kind = 'card_bill_payment' "
                "AND t.kind_reason = 'matched_card_bill:' || b.id) ORDER BY b.due_date",
                [card_id, today - timedelta(days=_OVERDUE_DAYS), today],
            ).fetchall()
            for due, total in overdue:
                notes.append(f"Unpaid bill without a matched payment: {card['alias']}, due {due}, {total} BRL.")
            if overdue:
                events.append((today, -overdue[-1][1], "bill", "low", payer_of(card_id)))

        # Installments: the numbers after the highest one seen, one per later bill.
        if card_ids:
            marks = ", ".join("?" * len(card_ids))
            series = cur.execute(
                "SELECT t.account_id, t.installment_total, max(t.installment_number), "
                "arg_max(abs(t.amount), t.installment_number), arg_max(b.due_date, t.installment_number), "
                "arg_max(t.date, t.installment_number) "
                "FROM v_transactions t LEFT JOIN v_bills b ON b.id = t.bill_id "
                f"WHERE t.account_id IN ({marks}) AND t.currency = 'BRL' "
                "AND t.installment_total > 1 AND t.installment_number IS NOT NULL "
                "AND t.purchase_date IS NOT NULL AND t.amount < 0 "
                "GROUP BY t.account_id, t.purchase_date, t.installment_total",
                card_ids,
            ).fetchall()
            for card_id, total, last_no, amount, last_due, last_date in series:
                fact = facts[card_id]
                if fact["due_day"] is None:
                    continue
                # The next installment lands in the bill after the one holding the last seen.
                month = _month_start(last_due if last_due is not None else _bill_due(fact, last_date), 1)
                for n in range(last_no + 1, total + 1):
                    when = _on_day(_month_start(month, n - last_no - 1), fact["due_day"])
                    if today <= when <= end and when > cover[card_id]:
                        events.append((when, -amount, "installment", "high", payer_of(card_id)))

        card_set = set(card_ids)
        bill_sums: dict[tuple[str, date], list] = {}
        for item in find_recurring(db, today):
            if item["confidence"] not in _CONFIDENT or item["cadence_days"] < 1:
                continue
            if item["account_type"] == "CREDIT" and item["account_id"] not in card_set:
                continue
            if item["account_type"] == "BANK" and item["account_id"] not in bank_ids:
                continue
            if item["account_type"] not in ("BANK", "CREDIT"):
                continue
            marks = ", ".join("?" * len(item["evidence_ids"]))
            if cur.execute(
                f"SELECT count(*) FROM v_transactions WHERE id IN ({marks}) AND installment_total > 1",
                item["evidence_ids"],
            ).fetchone()[0]:
                continue  # installment series are projected from their numbers
            sign = -1 if item["direction"] == "out" else 1
            amount = sign * Decimal(item["typical_amount"])
            cadence = item["cadence_days"]
            when = date.fromisoformat(item["next_expected"])
            if when < today:
                if is_stopped(when, cadence, today):
                    continue  # past due by more than half a cadence: stopped
                dates = [today]  # a little late: expect it today
                when += timedelta(days=cadence)
            else:
                dates = []
            while when <= end:
                dates.append(when)
                when += timedelta(days=cadence)
            for d in dates:
                if item["account_type"] == "BANK":
                    events.append((d, amount, "recurring", item["confidence"], item["account_id"]))
                else:
                    _into_bill(facts[item["account_id"]], cover, item, d, amount, bill_sums)
    finally:
        cur.close()

    for (card_id, due), (amount, confidence) in bill_sums.items():
        if today <= due <= end:
            events.append((due, amount, "recurring", confidence, payer_of(card_id)))
    if bill_sums:
        notes.append(
            "Recurring card charges are added to that card's estimated future bill, due on the card's "
            "most frequent due day: with a known closing day, the same number of months after the charge's "
            "closing as the card's latest bill; otherwise on the next due date after the charge."
        )
    if balance_used:
        notes.append("Pluggy lists closed bills only; the card balance stands in for the open bill.")
    if unassigned:
        notes.append(
            "Some cards have no matched bill payment to a bank account in scope; their events are "
            "debited from the scope's bank account with the highest balance."
        )
    skipped = [c["alias"] for c in cards if facts[c["id"]]["due_day"] is None]
    if skipped:
        notes.append("Cards with no bills on record have no estimated future bills: " + ", ".join(sorted(set(skipped))) + ".")
    return events, notes


def _into_bill(fact: dict, cover: dict, item: dict, when: date, amount: Decimal, bill_sums: dict) -> None:
    if fact["due_day"] is None:
        return
    due = _bill_due(fact, when)
    if due <= cover[item["account_id"]]:
        return  # already inside a bill that is in the curve
    key = (item["account_id"], due)
    prev = bill_sums.get(key)
    confidence = "medium" if item["confidence"] == "medium" or (prev and prev[1] == "medium") else "high"
    bill_sums[key] = ((prev[0] if prev else _ZERO) + amount, confidence)


def _curve(start: Decimal, events: list[tuple], today: date, days: int) -> dict:
    end = today + timedelta(days=days)
    by_day: dict[date, Decimal] = {}
    for when, amount, *_ in events:
        by_day[when] = by_day.get(when, _ZERO) + amount
    running = start
    daily = []
    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        running += by_day.get(day, _ZERO)
        daily.append((day, running))
    lowest = min(daily, key=lambda p: (p[1], p[0]))
    negative = next((d for d, b in daily if b < 0), None)
    if days > _MAX_DAILY_DAYS:
        points = [p for i, p in enumerate(daily) if i % _WEEK == 0 or p[0] == end]
    else:
        points = daily
    return {
        "start_balance": start,
        "events": events,
        "balance": [{"date": d, "balance": b} for d, b in points],
        "lowest": {"date": lowest[0], "balance": lowest[1]},
        "negative_on": negative,
    }


def _event_dicts(events: list[tuple], alias_of: dict[str, str]) -> list[dict]:
    ordered = sorted(events, key=lambda e: (e[0], e[2], e[4] or "", e[1]))
    return [
        {"date": d, "amount": a, "source": s, "confidence": c, "alias": alias_of.get(acc), "account_id": acc}
        for d, a, s, c, acc in ordered
    ]


def cash_forecast(
    db: Database, today: date, *, scope: Scope = "personal", days: int = 30, by_account: bool = False
) -> dict:
    scope_clause(scope)  # validates
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    end = today + timedelta(days=days)
    banks, cards, investment = _scope_accounts(db, scope)
    events, notes = _build_events(db, today, end, banks, cards)
    start = sum((b["balance"] for b in banks), _ZERO)
    alias_of = {b["id"]: b["alias"] for b in banks}
    if investment:
        notes.append("Investment-purpose bank accounts are left out of start_balance (connections: " + ", ".join(investment) + ").")
    if not banks:
        notes.append("No bank account in scope: the start balance is zero.")
    inflow = _inflow(db, today, scope)
    notes.append(
        f"typical_monthly_inflow ({inflow} BRL, the average income of the last 3 full months) is not in the "
        "curve; company withdrawals and other irregular inflows are not either. New card spending beyond "
        "recurring charges is not included. Only BRL is summed."
    )
    curve = _curve(start, events, today, days)
    result = {
        "scope": scope,
        "start_balance": curve["start_balance"],
        "events": _event_dicts(events, alias_of),
        "balance": curve["balance"],
        "lowest": curve["lowest"],
        "negative_on": curve["negative_on"],
        "typical_monthly_inflow": inflow,
        "notes": notes,
    }
    if by_account:
        result["accounts"] = []
        for b in banks:
            own = [e for e in events if e[4] == b["id"]]
            c = _curve(b["balance"], own, today, days)
            result["accounts"].append(
                {
                    "account_id": b["id"],
                    "alias": b["alias"],
                    "name": b["name"],
                    "start_balance": c["start_balance"],
                    "events": _event_dicts(own, alias_of),
                    "balance": c["balance"],
                    "lowest": c["lowest"],
                    "negative_on": c["negative_on"],
                }
            )
    return _json(result)


def _json(value):
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return jsonable(value)
