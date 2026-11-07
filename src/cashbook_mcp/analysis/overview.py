"""One-call snapshot of the user's finances."""

from datetime import date, timedelta
from decimal import Decimal

from cashbook_mcp.store.db import Database

from .filters import jsonable
from .forecast import cash_forecast
from .metrics import totals
from .networth import net_worth
from .summarize import cash_flow, list_categories

# Whose money each listed row is: the role of its connection.
_ROLE = "(SELECT c.role FROM connections c WHERE c.alias = a.alias) AS role"


def _dicts(cur, sql: str, params: list | None = None) -> list[dict]:
    result = cur.execute(sql, params or [])
    names = [d[0] for d in result.description]
    return [
        {n: jsonable(v) for n, v in zip(names, row, strict=True)}
        for row in result.fetchall()
    ]


def _net_worth_block(db: Database, today: date) -> dict:
    """Current personal net worth and its change since the last day of the previous month.

    The change sums (today - previous month-end) over bank, investment and card, counting only
    components with a real (not estimated) value on both days; loans never count. Null when none qualify.
    """

    def point_on(day: date) -> dict | None:
        points = net_worth(db, today, date_from=day, date_to=day, granularity="day")["points"]
        return points[0] if points else None

    now = point_on(today)
    before = point_on(today.replace(day=1) - timedelta(days=1))
    used: list[str] = []
    change = Decimal("0.00")
    if now and before:
        for name in ("bank", "investment", "card"):
            if (
                name in now["components"]
                and name in before["components"]
                and name not in now["estimated_components"]
                and name not in before["estimated_components"]
            ):
                sign = -1 if name == "card" else 1
                change += sign * (Decimal(now[name]) - Decimal(before[name]))
                used.append(name)
    return {
        "current": now["net"] if now else None,
        "change_this_month": jsonable(change) if used else None,
        "change_components": used,
    }


def finance_overview(db: Database, today: date, days_ahead: int = 10) -> dict:
    cur = db.read()
    try:
        accounts = _dicts(
            cur,
            f"SELECT id, alias, {_ROLE}, name, subtype, balance, currency FROM v_accounts a "
            "WHERE type = 'BANK' ORDER BY alias, name",
        )
        cards = _dicts(
            cur,
            f"SELECT id, alias, {_ROLE}, name, balance, credit_limit, available_credit, currency "
            "FROM v_accounts a WHERE type = 'CREDIT' ORDER BY alias, name",
        )
        bills_due = _dicts(
            cur,
            f"SELECT id, alias, {_ROLE}, account_id, due_date, closing_date, total_amount, "
            "minimum_payment, currency FROM v_bills a "
            "WHERE due_date BETWEEN ? AND ? ORDER BY due_date, id",
            [today, today + timedelta(days=days_ahead)],
        )
        has_business = cur.execute("SELECT count(*) FROM connections WHERE role = 'business'").fetchone()[0] > 0
        problems = _dicts(
            cur,
            "SELECT alias, status, last_sync_error AS error FROM connections "
            "WHERE status IS DISTINCT FROM 'UPDATED' OR last_sync_error IS NOT NULL "
            "ORDER BY alias",
        )
    finally:
        cur.close()
    month_start = today.replace(day=1)
    flow = cash_flow(db, month_start, today)
    month_to_date = {
        k: v for k, v in flow["totals"].items() if k not in ("one_off_out", "one_off_in") or Decimal(v)
    }
    if "excluded_foreign" in flow:
        month_to_date["excluded_foreign"] = flow["excluded_foreign"]
    overview = {
        "accounts": accounts,
        "cards": cards,
        "bills_due": bills_due,
        "month_to_date": month_to_date,
        "top_categories": list_categories(db, month_start, today)[:5],
        "connections_with_problems": problems,
    }
    overview["net_worth"] = _net_worth_block(db, today)
    projection = cash_forecast(db, today, scope="personal", days=30)
    overview["forecast"] = {
        "lowest": projection["lowest"],
        "negative_on": projection["negative_on"],
        "low_confidence_events": sum(e["confidence"] == "low" for e in projection["events"]),
    }
    if has_business:
        business = totals(db, month_start, today, "business")
        overview["business"] = {k: jsonable(business[k]) for k in ("income", "spending", "owner_draws")}
    return overview
