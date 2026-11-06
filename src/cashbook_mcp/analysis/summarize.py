"""Grouped totals, cash flow and category listings."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from cashbook_mcp.store.db import Database

from .filters import INTERNAL_KINDS, TxFilters, jsonable, where_clause

GroupBy = Literal["category", "merchant", "month", "week", "account", "kind"]

# Fixed expressions chosen from this whitelist; never built from caller input.
_GROUP_EXPR: dict[str, str] = {
    "category": "coalesce(pluggy_category, 'uncategorized')",
    "merchant": "coalesce(merchant_name, counterpart_name, description)",
    "month": "strftime(date, '%Y-%m')",
    "week": "strftime(date, '%G-W%V')",
    "account": "account_id",
    "kind": "kind",
}
_PERIOD_EXPR = {"month": _GROUP_EXPR["month"], "week": _GROUP_EXPR["week"]}
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def _average(total: Decimal, count: int) -> Decimal:
    return (total / count).quantize(_CENT) if count else _ZERO


def _aggregate(db: Database, f: TxFilters, group_by: str) -> dict[str, dict]:
    # Spending semantics (outflows only, internal movements and investments
    # excluded) unless the caller asks for inflows or specific kinds.
    base = "v_transactions" if (f.direction == "in" or f.kinds) else "v_spending"
    where, params = where_clause(f)
    if base == "v_transactions" and "investment" not in (f.kinds or []):
        # Same exclusion as cash_flow, so income figures agree.
        where = f"{where} AND kind != 'investment'" if where else "WHERE kind != 'investment'"
    sql = (
        f"SELECT {_GROUP_EXPR[group_by]} AS k, abs(sum(amount)), count(*), "
        f"min(date), max(date) FROM {base} {where} GROUP BY 1"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, params).fetchall()
    finally:
        cur.close()
    return {
        k: {
            "key": k,
            "total": total,
            "count": count,
            "average": _average(total, count),
            "first_date": first,
            "last_date": last,
        }
        for k, total, count, first, last in fetched
    }


def summarize_transactions(
    db: Database,
    f: TxFilters,
    group_by: GroupBy,
    compare_previous: bool = False,
    *,
    today: date,
) -> dict:
    if group_by not in _GROUP_EXPR:
        raise ValueError(f"unsupported group_by: {group_by}")
    date_to = f.date_to or today
    date_from = f.date_from or (date_to - timedelta(days=29))
    current = _aggregate(db, replace(f, date_from=date_from, date_to=date_to), group_by)
    period = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    previous: dict[str, dict] = {}
    if compare_previous:
        length = (date_to - date_from).days + 1
        prev_to = date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=length - 1)
        previous = _aggregate(
            db, replace(f, date_from=prev_from, date_to=prev_to), group_by
        )
        period["previous_date_from"] = prev_from.isoformat()
        period["previous_date_to"] = prev_to.isoformat()
    if group_by in _PERIOD_EXPR:
        ordered = sorted(current.values(), key=lambda g: g["key"])
    else:
        ordered = sorted(current.values(), key=lambda g: (-g["total"], g["key"]))
    # Time groupings pair periods by calendar offset (computed from the key,
    # so empty periods do not shift the pairing); others match by key.
    def previous_key_of(key: str) -> str:
        if group_by == "month":
            y, m = int(key[:4]), int(key[5:7])
            idx = y * 12 + (m - 1) - months_in_window
            return f"{idx // 12:04d}-{idx % 12 + 1:02d}"
        if group_by == "week":
            y, w = int(key[:4]), int(key[6:8])
            monday = date.fromisocalendar(y, w, 1) - timedelta(days=window_days)
            iso = monday.isocalendar()
            return f"{iso.year:04d}-W{iso.week:02d}"
        return key

    window_days = (date_to - date_from).days + 1
    months_in_window = (date_to.year * 12 + date_to.month) - (
        date_from.year * 12 + date_from.month
    ) + 1
    groups = []
    for g in ordered:
        out = {k: jsonable(v) for k, v in g.items()}
        if compare_previous:
            pkey = previous_key_of(g["key"])
            p = previous.get(pkey)
            block = (
                {k: jsonable(p[k]) for k in ("total", "count", "average")}
                if p
                else {"total": str(_ZERO), "count": 0, "average": str(_ZERO)}
            )
            if group_by in _PERIOD_EXPR:
                block["previous_key"] = pkey
            out["previous"] = block
        groups.append(out)
    return {"groups": groups, "period": period}


def cash_flow(
    db: Database,
    date_from: date,
    date_to: date,
    granularity: Literal["month", "week"] = "month",
    by_account: bool = False,
    *,
    today: date | None = None,
) -> dict:
    """Income and spending per period; `today` is accepted for call symmetry."""
    if granularity not in _PERIOD_EXPR:
        raise ValueError(f"unsupported granularity: {granularity}")
    marks = ", ".join("?" * len(INTERNAL_KINDS))
    account_col = ", account_id" if by_account else ""
    sql = (
        f"SELECT {_PERIOD_EXPR[granularity]} AS period{account_col}, "
        "coalesce(sum(CASE WHEN amount > 0 THEN amount END), 0), "
        "coalesce(abs(sum(CASE WHEN amount < 0 THEN amount END)), 0) "
        "FROM v_transactions "
        f"WHERE date >= ? AND date <= ? AND kind NOT IN ({marks}, 'investment') "
        f"GROUP BY period{account_col} ORDER BY period{account_col}"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [date_from, date_to, *INTERNAL_KINDS]).fetchall()
    finally:
        cur.close()
    periods = []
    total_in = total_out = _ZERO
    for row in fetched:
        income, spending = row[-2], row[-1]
        total_in += income
        total_out += spending
        item = {"period": row[0]}
        if by_account:
            item["account_id"] = row[1]
        item.update(
            income=jsonable(income),
            spending=jsonable(spending),
            net=jsonable(income - spending),
        )
        periods.append(item)
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "granularity": granularity,
        "periods": periods,
        "totals": {
            "income": jsonable(total_in),
            "spending": jsonable(total_out),
            "net": jsonable(total_in - total_out),
        },
    }


def list_categories(
    db: Database, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    where, params = where_clause(TxFilters(date_from=date_from, date_to=date_to))
    sql = (
        f"SELECT {_GROUP_EXPR['category']} AS k, count(*), abs(sum(amount)) "
        f"FROM v_spending {where} GROUP BY 1 ORDER BY 3 DESC, 1"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, params).fetchall()
    finally:
        cur.close()
    return [
        {"category": k, "count": count, "total": jsonable(total)}
        for k, count, total in fetched
    ]
