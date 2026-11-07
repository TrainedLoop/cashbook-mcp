"""Cash forecast: known commitments projected over the next 30 to 90 days."""

import calendar
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from cashbook_mcp.store.db import Database

from .filters import jsonable
from .metrics import Scope, scope_clause, totals
from .recurring import find_recurring

_ZERO = Decimal("0.00")
_CONFIDENT = ("high", "medium")
_WEEK = 7
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


def _scope_accounts(db: Database, scope: Scope) -> tuple[list[dict], list[dict]]:
    clause, params = scope_clause(scope)
    where = f" AND {clause}" if clause else ""
    cur = db.read()
    try:
        rows = cur.execute(
            "SELECT a.id, a.alias, a.name, a.type, a.balance FROM v_accounts a "
            "JOIN connections c ON c.alias = a.alias "
            "WHERE a.currency = 'BRL' AND coalesce((SELECT s.purpose FROM account_settings s "
            f"WHERE s.account_id = a.id), 'regular') = 'regular'{where} ORDER BY a.alias, a.name, a.id",
            params,
        ).fetchall()
    finally:
        cur.close()
    accounts = [dict(zip(("id", "alias", "name", "type", "balance"), r, strict=True)) for r in rows]
    return (
        [a for a in accounts if a["type"] == "BANK"],
        [a for a in accounts if a["type"] == "CREDIT"],
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

    cur = db.read()
    try:
        # Open bills still to pay; a matched card_bill_payment row marks a bill as paid.
        for card_id in card_ids:
            rows = cur.execute(
                "SELECT b.due_date, b.total_amount FROM v_bills b WHERE b.account_id = ? "
                "AND b.currency = 'BRL' AND b.due_date >= ? AND b.due_date <= ? AND b.total_amount > 0 "
                "AND NOT EXISTS (SELECT 1 FROM v_transactions t WHERE t.kind = 'card_bill_payment' "
                "AND t.kind_reason = 'matched_card_bill:' || b.id)",
                [card_id, today, end],
            ).fetchall()
            for due, total in rows:
                events.append((due, -total, "bill", "high", payer_of(card_id)))

        # Installments: the numbers after the highest one seen, one per later bill.
        if card_ids:
            marks = ", ".join("?" * len(card_ids))
            series = cur.execute(
                "SELECT t.account_id, t.purchase_date, t.installment_total, abs(t.amount), "
                "max(t.installment_number), arg_max(b.due_date, t.installment_number), max(t.date) "
                "FROM v_transactions t LEFT JOIN v_bills b ON b.id = t.bill_id "
                f"WHERE t.account_id IN ({marks}) AND t.currency = 'BRL' "
                "AND t.installment_total > 1 AND t.installment_number IS NOT NULL "
                "AND t.purchase_date IS NOT NULL AND t.amount < 0 "
                "GROUP BY t.account_id, t.purchase_date, t.installment_total, abs(t.amount)",
                card_ids,
            ).fetchall()
            for card_id, _purchase, total, amount, last_no, last_due, last_date in series:
                due_day = facts[card_id]["due_day"]
                if due_day is None:
                    continue
                if last_due is not None:
                    month = _month_start(last_due, 1)
                else:
                    month = _month_start(last_date, 0)
                    if _on_day(month, due_day) <= last_date:
                        month = _month_start(month, 1)
                for n in range(last_no + 1, total + 1):
                    when = _on_day(_month_start(month, n - last_no - 1), due_day)
                    if today <= when <= end:
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
            when = date.fromisoformat(item["next_expected"])
            while when <= end:
                if when >= today:
                    if item["account_type"] == "BANK":
                        events.append((when, amount, "recurring", item["confidence"], item["account_id"]))
                    else:
                        _into_bill(facts[item["account_id"]], item, when, amount, bill_sums)
                when += timedelta(days=item["cadence_days"])
    finally:
        cur.close()

    for (card_id, due), (amount, confidence) in bill_sums.items():
        if today <= due <= end:
            events.append((due, amount, "recurring", confidence, payer_of(card_id)))
    if bill_sums:
        notes.append(
            "Recurring card charges are added to that card's estimated future bill, due on the card's "
            "most frequent due day, the same number of months after the charge's closing as the card's latest "
            "bill; closing is taken from the card's most frequent closing day, or from the charge date "
            "when unknown."
        )
    if unassigned:
        notes.append(
            "Some cards have no matched bill payment to a bank account in scope; their events are "
            "debited from the scope's bank account with the highest balance."
        )
    skipped = [c["alias"] for c in cards if facts[c["id"]]["due_day"] is None]
    if skipped:
        notes.append("Cards with no bills on record have no estimated future bills: " + ", ".join(sorted(set(skipped))) + ".")
    return events, notes


def _into_bill(fact: dict, item: dict, when: date, amount: Decimal, bill_sums: dict) -> None:
    due_day = fact["due_day"]
    if due_day is None:
        return
    close_day = fact["closing_day"]
    closing_month = _month_start(when, 0 if close_day is None or when.day <= close_day else 1)
    due = _on_day(_month_start(closing_month, fact["due_offset"]), due_day)
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
    banks, cards = _scope_accounts(db, scope)
    events, notes = _build_events(db, today, end, banks, cards)
    start = sum((b["balance"] for b in banks), _ZERO)
    alias_of = {b["id"]: b["alias"] for b in banks}
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
