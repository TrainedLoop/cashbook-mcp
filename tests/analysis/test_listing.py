from datetime import date, datetime
from decimal import Decimal

from cashbook_mcp.analysis.listing import (
    get_investment_transactions,
    list_accounts,
    list_bills,
    list_connections,
    list_investments,
    list_loans,
)

NOW = datetime(2026, 1, 1)


def _add_investments(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO investments (id, alias, item_id, type, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES "
            "('inv-1', 'main', 'item-1', 'FIXED_INCOME', 'CDB A', 1000.00, 'BRL', ?, ?), "
            "('inv-2', 'second', 'item-2', 'MUTUAL_FUND', 'Fund B', 500.00, 'BRL', ?, ?)",
            [NOW] * 4,
        )
        for id, d in [("it-1", date(2026, 1, 10)), ("it-2", date(2026, 3, 10))]:
            cur.execute(
                "INSERT INTO investment_transactions (id, alias, investment_id, date, type, "
                "quantity, value, amount, first_seen_at, last_seen_at) "
                "VALUES (?, 'main', 'inv-1', ?, 'BUY', 1.5, 100.12345678, 150.19, ?, ?)",
                [id, d, NOW, NOW],
            )
        cur.execute(
            "INSERT INTO loans (id, alias, item_id, product_name, outstanding_balance, currency, "
            "installments_total, installments_remaining, next_due_date, first_seen_at, last_seen_at) "
            "VALUES ('loan-1', 'second', 'item-2', 'Personal', 2000.00, 'BRL', 12, 4, ?, ?, ?)",
            [date(2026, 4, 15), NOW, NOW],
        )


def test_list_connections_has_accounts_history_and_hints(db):
    out = {c["alias"]: c for c in list_connections(db)}
    main = out["main"]
    assert main["bank"] == "Bank A"
    assert main["status"] == "UPDATED"
    assert main["hint"] is None
    assert {a["id"] for a in main["accounts"]} == {"acc-main", "card-main"}
    assert main["history"]["transactions"] == 12
    assert main["history"]["date_from"] == "2025-10-05"
    assert main["history"]["date_to"] == "2026-03-20"
    second = out["second"]
    assert second["status"] == "LOGIN_ERROR"
    assert "MeuPluggy" in second["hint"]
    assert second["last_sync_error"] == "boom"


def test_list_accounts_filters_alias_and_hides_tax_id(db):
    rows = list_accounts(db, "second")
    assert [r["id"] for r in rows] == ["acc-second"]
    assert rows[0]["balance"] == "800.00"
    assert "owner_tax_id" not in rows[0]
    assert len(list_accounts(db)) == 3


def test_list_bills_filters_by_due_date(db):
    assert [b["id"] for b in list_bills(db)] == ["bill-past", "bill-soon", "bill-edge", "bill-far"]
    got = list_bills(db, account_id="card-main", date_from=date(2026, 4, 1), date_to=date(2026, 4, 10))
    assert [b["id"] for b in got] == ["bill-soon", "bill-edge"]
    assert list_bills(db, account_id="nope") == []


def test_investments_and_loans(db):
    _add_investments(db)
    assert [i["id"] for i in list_investments(db)] == ["inv-1", "inv-2"]
    assert [i["id"] for i in list_investments(db, "second")] == ["inv-2"]
    txs = get_investment_transactions(db, "inv-1", date_from=date(2026, 2, 1))
    assert [t["id"] for t in txs] == ["it-2"]
    assert Decimal(txs[0]["value"]) == Decimal("100.12345678")
    loans = list_loans(db)
    assert loans[0]["id"] == "loan-1"
    assert loans[0]["next_due_date"] == "2026-04-15"
    assert list_loans(db, "main") == []


def test_list_connections_timestamps_carry_utc_offset(db):
    with db.write() as cur:
        cur.execute(
            "UPDATE connections SET last_synced_at = ?, last_updated_at = ?, consent_expires_at = ? "
            "WHERE alias = 'main'",
            [datetime(2026, 3, 31, 9, 0), datetime(2026, 3, 31, 8, 0), datetime(2026, 9, 1)],
        )
    main = {c["alias"]: c for c in list_connections(db)}["main"]
    assert main["last_synced_at"] == "2026-03-31T09:00:00+00:00"
    assert main["last_updated_at"] == "2026-03-31T08:00:00+00:00"
    assert main["consent_expires_at"] == "2026-09-01T00:00:00+00:00"
