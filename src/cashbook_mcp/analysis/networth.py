"""Net worth series from daily balance snapshots, with estimated bank history."""

import calendar
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from cashbook_mcp.store.db import Database

from .filters import jsonable, months_ago
from .metrics import SCOPES, Scope

Granularity = Literal["day", "week", "month"]
_ZERO = Decimal("0.00")
MAX_DAILY_POINTS = 400
_COMPONENTS = ("bank", "investment", "card", "loan")


def _alias_filter(scope: Scope) -> tuple[str, list]:
    if scope not in SCOPES:
        raise ValueError(f"unsupported scope: {scope}")
    if scope == "consolidated":
        return "", []
    return "AND s.alias IN (SELECT alias FROM connections WHERE role = ?)", [scope]


def _period_ends(start: date, end: date, granularity: str) -> list[date]:
    """Last day of each period that overlaps [start, end], clipped to end."""
    if granularity not in ("day", "week", "month"):
        raise ValueError(f"unsupported granularity: {granularity}")
    ends: list[date] = []
    day = start
    while day <= end:
        if granularity == "day":
            period_end = day
        elif granularity == "week":
            period_end = day + timedelta(days=6 - day.weekday())
        else:
            period_end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        ends.append(min(period_end, end))
        day = period_end + timedelta(days=1)
    return ends


class _Series:
    """Balances over time per id; the value on a day is the last snapshot on or before it."""

    def __init__(self) -> None:
        self.days: dict[str, list[date]] = defaultdict(list)
        self.balances: dict[str, list[Decimal]] = defaultdict(list)

    def add(self, key: str, day: date, balance: Decimal) -> None:
        self.days[key].append(day)
        self.balances[key].append(balance)

    def at(self, key: str, day: date) -> Decimal | None:
        i = bisect_right(self.days[key], day)
        return self.balances[key][i - 1] if i else None

    def first_day(self, key: str) -> date:
        return self.days[key][0]


def _load(cur, sql: str, params: list, foreign: set[str]) -> dict[str, _Series]:
    """Snapshot rows grouped by type ('BANK', 'CREDIT', ...); non-BRL ids go to `foreign`."""
    series: dict[str, _Series] = defaultdict(_Series)
    for kind, key, day, balance, currency in cur.execute(sql, params).fetchall():
        if currency != "BRL":
            foreign.add(key)
            continue
        series[kind].add(key, day, balance)
    return series


def net_worth(
    db: Database,
    today: date,
    *,
    scope: Scope = "personal",
    date_from: date | None = None,
    date_to: date | None = None,
    granularity: Granularity = "month",
) -> dict:
    where, params = _alias_filter(scope)
    start = date_from or months_ago(today, 12)
    end = min(date_to or today, today)
    if start > end:
        raise ValueError("date_from must not be after date_to")
    dates = _period_ends(start, end, granularity)
    if granularity == "day" and len(dates) > MAX_DAILY_POINTS:
        raise ValueError(
            f"granularity=day allows at most {MAX_DAILY_POINTS} points; narrow date_from/date_to or use week or month"
        )
    foreign: set[str] = set()
    cur = db.read()
    try:
        accounts = _load(
            cur,
            "SELECT a.type, s.account_id, s.day, s.balance, s.currency FROM balance_snapshots s "
            f"JOIN v_accounts a ON a.id = s.account_id WHERE true {where} ORDER BY s.account_id, s.day",
            params,
            foreign,
        )
        investments = _load(
            cur,
            "SELECT 'INV', s.investment_id, s.day, s.balance, s.currency FROM investment_snapshots s "
            f"JOIN v_investments i ON i.id = s.investment_id WHERE true {where} "
            "ORDER BY s.investment_id, s.day",
            params,
            foreign,
        )
        loan_where = where.replace("s.alias", "alias")
        loans_row = cur.execute(
            "SELECT sum(outstanding_balance) FILTER (WHERE currency = 'BRL'), "
            "count(*) FILTER (WHERE currency <> 'BRL' AND outstanding_balance IS NOT NULL) "
            f"FROM v_loans WHERE true {loan_where}",
            params,
        ).fetchone()
        bank, cards = accounts["BANK"], accounts["CREDIT"]
        investment = investments["INV"]
        # Daily amount sums of bank accounts, to walk back from their first snapshot.
        flows: dict[str, tuple[list[date], list[Decimal]]] = {}
        for key in bank.days:
            first = bank.first_day(key)
            if first > dates[0]:
                rows = cur.execute(
                    "SELECT date, sum(amount) FROM v_transactions WHERE account_id = ? "
                    "AND date > ? AND date <= ? GROUP BY date ORDER BY date",
                    [key, dates[0], first],
                ).fetchall()
                flows[key] = ([r[0] for r in rows], [r[1] for r in rows])
    finally:
        cur.close()

    loans_total = loans_row[0] or _ZERO
    loans_foreign = loans_row[1]

    def bank_at(key: str, day: date) -> tuple[Decimal, bool]:
        actual = bank.at(key, day)
        if actual is not None:
            return actual, False
        fdays, fsums = flows.get(key, ([], []))
        i = bisect_right(fdays, day)  # transactions after `day` start at index i
        return bank.balances[key][0] - sum(fsums[i:], _ZERO), True

    points = []
    for day in dates:
        parts = dict.fromkeys(_COMPONENTS, _ZERO)
        seen = dict.fromkeys(_COMPONENTS, False)
        estimated = False
        for key in bank.days:
            got = bank_at(key, day)
            parts["bank"] += got[0]
            estimated = estimated or got[1]
            seen["bank"] = True
        for name, group in (("investment", investment), ("card", cards)):
            for key in group.days:
                balance = group.at(key, day)
                if balance is not None:
                    parts[name] += abs(balance) if name == "card" else balance
                    seen[name] = True
        if day == today and loans_total:
            parts["loan"] = loans_total
            seen["loan"] = True
        if not any(seen.values()):
            continue
        assets = parts["bank"] + parts["investment"]
        liabilities = parts["card"] + parts["loan"]
        points.append({
            "date": day,
            "assets": assets,
            "liabilities": liabilities,
            "net": assets - liabilities,
            **parts,
            "estimated": estimated,
            "components": [c for c in _COMPONENTS if seen[c]],
            "estimated_components": ["bank"] if estimated else [],
        })  # fmt: skip

    def starts(group: _Series) -> date | None:
        firsts = [group.first_day(k) for k in group.days]
        return min(firsts) if firsts else None

    result = {
        "scope": scope,
        "points": [{k: jsonable(v) for k, v in p.items()} for p in points],
        "history_starts": {k: jsonable(v) for k, v in {
            "bank": starts(bank),
            "investment": starts(investment),
            "card": starts(cards),
            "loan": today if loans_total else None,
        }.items()},
    }  # fmt: skip
    if foreign or loans_foreign:
        result["excluded_foreign"] = len(foreign) + loans_foreign
    return result
