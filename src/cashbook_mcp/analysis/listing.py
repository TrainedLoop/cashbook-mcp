"""Plain listings for the detail tools: connections, accounts, bills, investments and loans."""

import dataclasses
from datetime import date
from typing import Any

from cashbook_mcp.models import Connection
from cashbook_mcp.presenter import iso_utc, problem_of
from cashbook_mcp.store.db import Database

from .filters import jsonable

_ACCOUNT_COLS = (
    "id, alias, item_id, type, subtype, name, number, balance, currency, credit_limit, available_credit"
)
_BILL_COLS = "id, alias, account_id, due_date, closing_date, total_amount, minimum_payment, currency"
_INVESTMENT_COLS = "id, alias, item_id, type, name, balance, currency, due_date, issuer"
_INV_TX_COLS = (
    "id, alias, investment_id, date, trade_date, type, movement_type, quantity, value, amount"
)
_LOAN_COLS = (
    "id, alias, item_id, product_name, contract_amount, outstanding_balance, currency, "
    "installments_total, installments_remaining, next_due_date"
)


def _rows(db: Database, sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = db.read()
    try:
        result = cur.execute(sql, params or [])
        names = [d[0] for d in result.description]
        fetched = result.fetchall()
    finally:
        cur.close()
    return [{n: jsonable(v) for n, v in zip(names, row, strict=True)} for row in fetched]


def _where(conditions: list[tuple[str, Any]]) -> tuple[str, list[Any]]:
    used = [(c, v) for c, v in conditions if v is not None]
    if not used:
        return "", []
    return "WHERE " + " AND ".join(c for c, _ in used), [v for _, v in used]


def list_connections(db: Database) -> list[dict]:
    names = [f.name for f in dataclasses.fields(Connection)]
    cur = db.read()
    try:
        conns = [
            Connection(*r)
            for r in cur.execute(f"SELECT {', '.join(names)} FROM connections ORDER BY alias").fetchall()
        ]
        history = {
            alias: (count, first, last)
            for alias, count, first, last in cur.execute(
                "SELECT alias, count(*), min(date), max(date) FROM v_transactions GROUP BY alias"
            ).fetchall()
        }
    finally:
        cur.close()
    accounts: dict[str, list[dict]] = {}
    for a in _rows(
        db,
        "SELECT a.id, a.alias, a.type, a.subtype, a.name, coalesce(s.purpose, 'regular') AS purpose "
        "FROM v_accounts a LEFT JOIN account_settings s ON s.account_id = a.id ORDER BY a.alias, a.name",
    ):
        accounts.setdefault(a.pop("alias"), []).append(a)
    out = []
    for c in conns:
        count, first, last = history.get(c.alias, (0, None, None))
        problem = problem_of(c)
        out.append(
            {
                "alias": c.alias,
                "bank": c.connector_name,
                "role": c.role,
                "status": c.status,
                "execution_status": c.execution_status,
                "error_code": c.error_code,
                "last_updated_at": iso_utc(c.last_updated_at),
                "next_update_expected": iso_utc(c.next_auto_sync_at),
                "last_synced_at": iso_utc(c.last_synced_at),
                "last_sync_error": c.last_sync_error,
                "consent_expires_at": iso_utc(c.consent_expires_at),
                "accounts": accounts.get(c.alias, []),
                "history": {
                    "transactions": count,
                    "date_from": jsonable(first),
                    "date_to": jsonable(last),
                },
                "hint": problem[1] if problem else None,
            }
        )
    return out


def list_accounts(db: Database, alias: str | None = None) -> list[dict]:
    where, params = _where([("alias = ?", alias)])
    return _rows(db, f"SELECT {_ACCOUNT_COLS} FROM v_accounts {where} ORDER BY alias, type, name", params)


def list_bills(
    db: Database,
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    where, params = _where(
        [("account_id = ?", account_id), ("due_date >= ?", date_from), ("due_date <= ?", date_to)]
    )
    return _rows(db, f"SELECT {_BILL_COLS} FROM v_bills {where} ORDER BY due_date, id", params)


def list_investments(db: Database, alias: str | None = None) -> list[dict]:
    where, params = _where([("alias = ?", alias)])
    return _rows(db, f"SELECT {_INVESTMENT_COLS} FROM v_investments {where} ORDER BY alias, name, id", params)


def get_investment_transactions(
    db: Database, investment_id: str, date_from: date | None = None, date_to: date | None = None
) -> list[dict]:
    where, params = _where(
        [("investment_id = ?", investment_id), ("date >= ?", date_from), ("date <= ?", date_to)]
    )
    return _rows(
        db, f"SELECT {_INV_TX_COLS} FROM v_investment_transactions {where} ORDER BY date DESC, id DESC", params
    )


def list_loans(db: Database, alias: str | None = None) -> list[dict]:
    where, params = _where([("alias = ?", alias)])
    return _rows(db, f"SELECT {_LOAN_COLS} FROM v_loans {where} ORDER BY alias, id", params)
