"""Grouped totals, cash flow and category listings."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from cashbook_mcp.store.db import Database

from .filters import BASE_CURRENCY, INTERNAL_KINDS, TxFilters, jsonable, where_clause

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


def _and(where: str, clause: str) -> str:
    return f"{where} AND {clause}" if where else f"WHERE {clause}"


# Temporary stand-in for the dropped v_spending until the aggregations move to
# v_flows spend_amount: BRL and foreign outflows, no internal movements.
_SPENDING = (
    "(SELECT * FROM v_flows WHERE amount < 0 AND flow IN ('spending', 'foreign') "
    "AND kind NOT IN ('internal_transfer', 'card_bill_payment', 'investment'))"
)


def _count_foreign(cur, base: str, where: str, params: list) -> int:
    sql = f"SELECT count(*) FROM {base} {_and(where, 'currency != ?')}"
    return cur.execute(sql, [*params, BASE_CURRENCY]).fetchone()[0]


def _signed(f: TxFilters) -> bool:
    # Explicit kinds with no direction mix inflows and outflows: keep the sign.
    return bool(f.kinds) and f.direction is None


def _aggregate(db: Database, f: TxFilters, group_by: str) -> tuple[dict[str, dict], int]:
    """Groups for f, and how many matching rows were skipped as foreign currency."""
    # Spending semantics (outflows only, internal movements and investments
    # excluded) unless the caller asks for inflows or specific kinds.
    base = "v_transactions" if (f.direction == "in" or f.kinds) else _SPENDING
    where, params = where_clause(f)
    if base == "v_transactions" and "investment" not in (f.kinds or []):
        # Same exclusion as cash_flow, so income figures agree.
        where = _and(where, "kind != 'investment'")
    signed = _signed(f)
    total = "sum(amount)" if signed else "abs(sum(amount))"
    split = (
        ", coalesce(sum(CASE WHEN amount > 0 THEN amount END), 0), "
        "coalesce(abs(sum(CASE WHEN amount < 0 THEN amount END)), 0)"
        if signed
        else ""
    )
    sql = (
        f"SELECT {_GROUP_EXPR[group_by]} AS k, {total}, count(*), "
        f"min(date), max(date){split} FROM {base} {_and(where, 'currency = ?')} GROUP BY 1"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, BASE_CURRENCY]).fetchall()
        foreign = _count_foreign(cur, base, where, params)
    finally:
        cur.close()
    groups = {}
    for k, amount, count, first, last, *rest in fetched:
        g = {
            "key": k,
            "total": amount,
            "count": count,
            "average": _average(amount, count),
            "first_date": first,
            "last_date": last,
        }
        if signed:
            g["in"], g["out"] = rest
        groups[k] = g
    return groups, foreign


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
    current, foreign = _aggregate(
        db, replace(f, date_from=date_from, date_to=date_to), group_by
    )
    period = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    previous: dict[str, dict] = {}
    if compare_previous:
        length = (date_to - date_from).days + 1
        prev_to = date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=length - 1)
        previous, _ = _aggregate(
            db, replace(f, date_from=prev_from, date_to=prev_to), group_by
        )
        period["previous_date_from"] = prev_from.isoformat()
        period["previous_date_to"] = prev_to.isoformat()
    if group_by in _PERIOD_EXPR:
        ordered = sorted(current.values(), key=lambda g: g["key"])
    else:
        ordered = sorted(current.values(), key=lambda g: (-abs(g["total"]), g["key"]))
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
            fields = ("total", "count", "average") + (("in", "out") if _signed(f) else ())
            block = (
                {k: jsonable(p[k]) for k in fields}
                if p
                else {k: (0 if k == "count" else str(_ZERO)) for k in fields}
            )
            if group_by in _PERIOD_EXPR:
                block["previous_key"] = pkey
            out["previous"] = block
        groups.append(out)
    result = {"groups": groups, "period": period}
    if foreign:
        result["excluded_foreign"] = foreign
    return result


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
    # Income is only kind='income'. Refunds are inflows that undo spending, so
    # they lower spending (and are reported apart) instead of counting as income.
    where = (
        f"WHERE date >= ? AND date <= ? AND kind NOT IN ({marks}, 'investment')"
    )
    params = [date_from, date_to, *INTERNAL_KINDS]
    sql = (
        f"SELECT {_PERIOD_EXPR[granularity]} AS period{account_col}, "
        "coalesce(sum(CASE WHEN kind = 'income' AND amount > 0 THEN amount END), 0), "
        "coalesce(sum(CASE WHEN kind = 'refund' AND amount > 0 THEN amount END), 0), "
        "coalesce(abs(sum(CASE WHEN amount < 0 THEN amount END)), 0) "
        f"FROM v_transactions {where} AND currency = ? "
        f"GROUP BY period{account_col} ORDER BY period{account_col}"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, BASE_CURRENCY]).fetchall()
        foreign = _count_foreign(cur, "v_transactions", where, params)
    finally:
        cur.close()
    periods = []
    total_in = total_refunds = total_out = _ZERO
    for row in fetched:
        income, refunds, outflow = row[-3], row[-2], row[-1]
        spending = outflow - refunds
        total_in += income
        total_refunds += refunds
        total_out += spending
        item = {"period": row[0]}
        if by_account:
            item["account_id"] = row[1]
        item.update(
            income=jsonable(income),
            refunds=jsonable(refunds),
            spending=jsonable(spending),
            net=jsonable(income - spending),
        )
        periods.append(item)
    result = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "granularity": granularity,
        "periods": periods,
        "totals": {
            "income": jsonable(total_in),
            "refunds": jsonable(total_refunds),
            "spending": jsonable(total_out),
            "net": jsonable(total_in - total_out),
        },
    }
    if foreign:
        result["excluded_foreign"] = foreign
    return result


def list_categories(
    db: Database, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    where, params = where_clause(TxFilters(date_from=date_from, date_to=date_to))
    sql = (
        f"SELECT {_GROUP_EXPR['category']} AS k, count(*), abs(sum(amount)) "
        f"FROM {_SPENDING} {_and(where, 'currency = ?')} GROUP BY 1 ORDER BY 3 DESC, 1"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, BASE_CURRENCY]).fetchall()
    finally:
        cur.close()
    return [
        {"category": k, "count": count, "total": jsonable(total)}
        for k, count, total in fetched
    ]
