"""Scopes over v_flows and the totals every aggregation shares."""

from datetime import date
from decimal import Decimal
from typing import Literal

from cashbook_mcp.store.db import Database

Scope = Literal["personal", "business", "consolidated"]
SCOPES: tuple[Scope, ...] = ("personal", "business", "consolidated")

# Flows reported next to the totals, not as spending.
APART_FLOWS: dict[Scope, tuple[str, ...]] = {
    "personal": ("from_business", "to_business"),
    "business": ("to_personal", "from_personal"),
    "consolidated": (),
}
# Result key of each apart flow (owner draws and contributions read better).
APART_KEYS = {
    "from_business": "from_business",
    "to_business": "to_business",
    "to_personal": "owner_draws",
    "from_personal": "owner_contributions",
}
_ZERO = Decimal("0.00")
# Foreign rows are counted apart, except movements that never were spending or income.
_NOT_COUNTED_FOREIGN = ("internal_transfer", "card_bill_payment", "investment")


def scope_clause(scope: Scope) -> tuple[str, list]:
    """Row filter for a scope: ("role = ?", [role]) or ("", []) for consolidated."""
    if scope in ("personal", "business"):
        return "role = ?", [scope]
    if scope == "consolidated":
        return "", []
    raise ValueError(f"unsupported scope: {scope}")


def metric_exprs(scope: Scope) -> dict[str, str]:
    """Aggregate SQL per result key, over the scope's v_flows rows (fixed fragments)."""
    scope_clause(scope)  # validates
    if scope == "personal":
        # Owner draws received count as income, and are also reported apart.
        income = (
            "sum(CASE WHEN flow = 'income' THEN income_amount "
            "WHEN flow = 'from_business' THEN amount ELSE 0 END)"
        )
    else:
        income = "sum(income_amount)"
    exprs = {
        "income": income,
        "refunds": "sum(CASE WHEN flow = 'refund' THEN -spend_amount ELSE 0 END)",
        "spending": "sum(spend_amount)",
    }
    for flow in APART_FLOWS[scope]:
        exprs[APART_KEYS[flow]] = f"sum(CASE WHEN flow = '{flow}' THEN abs(amount) ELSE 0 END)"
    return exprs


def metric_flows(scope: Scope) -> tuple[str, ...]:
    """Flows that contribute to the scope's metrics (others only add empty periods)."""
    scope_clause(scope)  # validates
    return ("income", "spending", "refund", *APART_FLOWS[scope])


def count_foreign(
    cur, where: str, params: list, side: Literal["in", "out"] | None = None
) -> int:
    """Foreign-currency rows matching `where` (scope included) left out of the sums; `side` keeps only outflows or inflows."""
    marks = ", ".join("?" * len(_NOT_COUNTED_FOREIGN))
    joiner = f"{where} AND" if where else "WHERE"
    sql = f"SELECT count(*) FROM v_flows {joiner} flow = 'foreign' AND kind NOT IN ({marks})"
    if side == "out":
        sql += " AND amount < 0"
    elif side == "in":
        sql += " AND amount > 0"
    return cur.execute(sql, [*params, *_NOT_COUNTED_FOREIGN]).fetchone()[0]


def totals(db: Database, date_from: date, date_to: date, scope: Scope = "personal") -> dict:
    exprs = metric_exprs(scope)
    clause, cparams = scope_clause(scope)
    where = "WHERE date >= ? AND date <= ?" + (f" AND {clause}" if clause else "")
    params = [date_from, date_to, *cparams]
    sql = f"SELECT {', '.join(f'coalesce({e}, 0)' for e in exprs.values())} FROM v_flows {where}"
    cur = db.read()
    try:
        row = cur.execute(sql, params).fetchone()
        foreign = count_foreign(cur, where, params)
    finally:
        cur.close()
    result = dict(zip(exprs, row, strict=True))
    result["net"] = result["income"] - result["spending"]
    result = {k: Decimal(v).quantize(_ZERO) for k, v in result.items()}
    result["excluded_foreign"] = foreign
    return result
