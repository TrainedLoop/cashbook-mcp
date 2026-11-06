"""One-call snapshot of the user's finances."""

from datetime import date, timedelta

from cashbook_mcp.store.db import Database

from .filters import jsonable
from .summarize import cash_flow, list_categories


def _dicts(cur, sql: str, params: list | None = None) -> list[dict]:
    result = cur.execute(sql, params or [])
    names = [d[0] for d in result.description]
    return [
        {n: jsonable(v) for n, v in zip(names, row, strict=True)}
        for row in result.fetchall()
    ]


def finance_overview(db: Database, today: date, days_ahead: int = 10) -> dict:
    cur = db.read()
    try:
        accounts = _dicts(
            cur,
            "SELECT id, alias, name, subtype, balance, currency FROM v_accounts "
            "WHERE type = 'BANK' ORDER BY alias, name",
        )
        cards = _dicts(
            cur,
            "SELECT id, alias, name, balance, credit_limit, available_credit, currency "
            "FROM v_accounts WHERE type = 'CREDIT' ORDER BY alias, name",
        )
        bills_due = _dicts(
            cur,
            "SELECT id, alias, account_id, due_date, closing_date, total_amount, "
            "minimum_payment, currency FROM v_bills "
            "WHERE due_date BETWEEN ? AND ? ORDER BY due_date, id",
            [today, today + timedelta(days=days_ahead)],
        )
        problems = _dicts(
            cur,
            "SELECT alias, status, last_sync_error AS error FROM connections "
            "WHERE status IS DISTINCT FROM 'UPDATED' OR last_sync_error IS NOT NULL "
            "ORDER BY alias",
        )
    finally:
        cur.close()
    month_start = today.replace(day=1)
    return {
        "accounts": accounts,
        "cards": cards,
        "bills_due": bills_due,
        "month_to_date": cash_flow(db, month_start, today)["totals"],
        "top_categories": list_categories(db, month_start, today)[:5],
        "connections_with_problems": problems,
    }
