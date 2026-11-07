"""Grouped totals, cash flow and category listings."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from cashbook_mcp.store.db import Database

from .filters import BASE_CURRENCY, TxFilters, jsonable, where_clause
from .metrics import Scope, count_foreign, metric_exprs, metric_flows, scope_clause

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




def _signed(f: TxFilters) -> bool:
    # Explicit kinds with no direction mix inflows and outflows: keep the sign.
    return bool(f.kinds) and f.direction is None


def _aggregate(
    db: Database, f: TxFilters, dims: list[str], scope: Scope
) -> tuple[dict[tuple, dict], int]:
    """Groups for f, and how many matching rows were skipped as foreign currency."""
    exprs = ", ".join(_GROUP_EXPR[d] for d in dims)
    signed = _signed(f)
    raw = bool(f.kinds)
    income = f.direction == "in" and not raw
    scoped = replace(f, scope=scope)
    if raw:
        # Rows of the kinds asked for, as stored: signed or by magnitude.
        where, wparams = where_clause(scoped)
        total = "sum(amount)" if signed else "abs(sum(amount))"
        extra = _and(where, "currency = ?")
        params = [*wparams, BASE_CURRENCY]
    else:
        # Spending (net of refunds) or income, as v_flows defines them. Internal
        # movements are excluded by their flow, not by kind, so groups add up to
        # cash_flow.
        where, wparams = where_clause(replace(scoped, direction=None, exclude_internal=False))
        if income:
            flows = "('income', 'from_business')" if scope == "personal" else "('income')"
            total = metric_exprs(scope)["income"]
        else:
            flows = "('spending', 'refund')"
            total = "sum(spend_amount)"
        extra = _and(where, f"flow IN {flows}")
        params = wparams
    split = (
        ", coalesce(sum(CASE WHEN amount > 0 THEN amount END), 0), "
        "coalesce(abs(sum(CASE WHEN amount < 0 THEN amount END)), 0)"
        if signed
        else ""
    )
    sql = (
        f"SELECT {exprs}, {total}, count(*), min(date), max(date){split} "
        f"FROM v_flows {extra} GROUP BY {', '.join(str(i + 1) for i in range(len(dims)))}"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, params).fetchall()
        if raw:
            foreign = cur.execute(
                f"SELECT count(*) FROM v_flows {_and(where, 'currency != ?')}",
                [*wparams, BASE_CURRENCY],
            ).fetchone()[0]
        else:
            foreign = count_foreign(cur, where, wparams, side="in" if income else "out")
    finally:
        cur.close()
    groups = {}
    n = len(dims)
    for row in fetched:
        keys, (amount, count, first, last, *rest) = tuple(row[:n]), row[n:]
        g = {
            "key": keys[0] if n == 1 else list(keys),
            "total": amount,
            "count": count,
            "average": _average(amount, count),
            "first_date": first,
            "last_date": last,
        }
        if signed:
            g["in"], g["out"] = rest
        groups[keys] = g
    return groups, foreign


def summarize_transactions(
    db: Database,
    f: TxFilters,
    group_by: GroupBy | list[GroupBy],
    compare_previous: bool = False,
    *,
    today: date,
    scope: Scope = "personal",
) -> dict:
    dims = [group_by] if isinstance(group_by, str) else list(group_by)
    if not 1 <= len(dims) <= 2:
        raise ValueError("group_by takes one or two dimensions")
    for d in dims:
        if d not in _GROUP_EXPR:
            raise ValueError(f"unsupported group_by: {d}")
    scope_clause(scope)  # validates
    date_to = f.date_to or today
    date_from = f.date_from or (date_to - timedelta(days=29))
    current, foreign = _aggregate(
        db, replace(f, date_from=date_from, date_to=date_to), dims, scope
    )
    period = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    previous: dict[tuple, dict] = {}
    if compare_previous:
        length = (date_to - date_from).days + 1
        prev_to = date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=length - 1)
        previous, _ = _aggregate(
            db, replace(f, date_from=prev_from, date_to=prev_to), dims, scope
        )
        period["previous_date_from"] = prev_from.isoformat()
        period["previous_date_to"] = prev_to.isoformat()
    if dims[0] in _PERIOD_EXPR:
        ordered = sorted(current.items(), key=lambda kv: kv[0])
    else:
        ordered = sorted(current.items(), key=lambda kv: (-abs(kv[1]["total"]), kv[0]))

    window_days = (date_to - date_from).days + 1
    months_in_window = (date_to.year * 12 + date_to.month) - (
        date_from.year * 12 + date_from.month
    ) + 1

    # Time groupings pair periods by calendar offset (computed from the key,
    # so empty periods do not shift the pairing); others match by key.
    def previous_key_of(dim: str, key: str) -> str:
        if dim == "month":
            y, m = int(key[:4]), int(key[5:7])
            idx = y * 12 + (m - 1) - months_in_window
            return f"{idx // 12:04d}-{idx % 12 + 1:02d}"
        if dim == "week":
            y, w = int(key[:4]), int(key[6:8])
            monday = date.fromisocalendar(y, w, 1) - timedelta(days=window_days)
            iso = monday.isocalendar()
            return f"{iso.year:04d}-W{iso.week:02d}"
        return key

    has_period = any(d in _PERIOD_EXPR for d in dims)
    groups = []
    for keys, g in ordered:
        out = {k: jsonable(v) for k, v in g.items()}
        if compare_previous:
            pkeys = tuple(previous_key_of(d, k) for d, k in zip(dims, keys, strict=True))
            p = previous.get(pkeys)
            fields = ("total", "count", "average") + (("in", "out") if _signed(f) else ())
            block = (
                {k: jsonable(p[k]) for k in fields}
                if p
                else {k: (0 if k == "count" else str(_ZERO)) for k in fields}
            )
            if has_period:
                block["previous_key"] = pkeys[0] if len(dims) == 1 else list(pkeys)
            out["previous"] = block
        groups.append(out)
    result = {"groups": groups, "period": period, "scope": scope}
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
    scope: Scope = "personal",
) -> dict:
    """Income and spending per period; `today` is accepted for call symmetry."""
    if granularity not in _PERIOD_EXPR:
        raise ValueError(f"unsupported granularity: {granularity}")
    exprs = metric_exprs(scope)
    clause, cparams = scope_clause(scope)
    account_col = ", account_id" if by_account else ""
    flows = metric_flows(scope)
    where = "WHERE date >= ? AND date <= ?" + (f" AND {clause}" if clause else "")
    where_flows = f"{where} AND flow IN ({', '.join('?' * len(flows))})"
    params = [date_from, date_to, *cparams]
    selects = ", ".join(f"coalesce({e}, 0)" for e in exprs.values())
    sql = (
        f"SELECT {_PERIOD_EXPR[granularity]} AS period{account_col}, {selects} "
        f"FROM v_flows {where_flows} GROUP BY period{account_col} ORDER BY period{account_col}"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, *flows]).fetchall()
        foreign = count_foreign(cur, where, params)
    finally:
        cur.close()
    names = list(exprs)
    sums = dict.fromkeys(names, _ZERO)
    periods = []
    for row in fetched:
        values = dict(zip(names, row[-len(names):], strict=True))
        for k, v in values.items():
            sums[k] += v
        item = {"period": row[0]}
        if by_account:
            item["account_id"] = row[1]
        item.update(_report(values))
        periods.append(item)
    result = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "granularity": granularity,
        "scope": scope,
        "periods": periods,
        "totals": _report(sums),
    }
    if foreign:
        result["excluded_foreign"] = foreign
    return result


def _report(values: dict) -> dict:
    """Metric values with net added, as JSON-ready strings (income first, net after spending)."""
    out = {k: jsonable(v) for k, v in values.items() if k in ("income", "refunds", "spending")}
    out["net"] = jsonable(values["income"] - values["spending"])
    out.update({k: jsonable(v) for k, v in values.items() if k not in out})
    return out


def list_categories(
    db: Database,
    date_from: date | None = None,
    date_to: date | None = None,
    scope: Scope = "personal",
) -> list[dict]:
    """Spending per category, net of refunds; the totals add up to cash_flow spending."""
    scope_clause(scope)  # validates
    f = TxFilters(date_from=date_from, date_to=date_to)
    groups, _ = _aggregate(db, f, ["category"], scope)
    rows = sorted(groups.values(), key=lambda g: (-g["total"], g["key"]))
    return [
        {"category": g["key"], "count": g["count"], "total": jsonable(g["total"])}
        for g in rows
    ]
