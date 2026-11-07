from datetime import date, datetime
from decimal import Decimal

from cashbook_mcp.analysis.networth import net_worth
from cashbook_mcp.analysis.overview import finance_overview

NOW = datetime(2026, 1, 1)

TODAY = date(2026, 3, 31)


def snap(cur, table, key, alias, day, balance, currency="BRL"):
    col = "account_id" if table == "balance_snapshots" else "investment_id"
    cur.execute(
        f"INSERT INTO {table} (day, {col}, alias, balance, currency) VALUES (?, ?, ?, ?, ?)",
        [day, key, alias, Decimal(balance), currency],
    )


def test_snapshot_points(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO investments (id, alias, item_id, type, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES ('inv-1', 'main', 'item', 'FIXED', 'CDB', 5000, 'BRL', ?, ?)",
            [NOW, NOW],
        )
        for d, b in [(date(2026, 1, 31), "100.00"), (date(2026, 2, 15), "300.00"), (date(2026, 3, 31), "1200.00")]:
            snap(cur, "balance_snapshots", "acc-main", "main", d, b)
        snap(cur, "investment_snapshots", "inv-1", "main", date(2026, 2, 28), "5000.00")
    out = net_worth(db, TODAY, date_from=date(2026, 1, 1), granularity="month")
    assert out["scope"] == "personal"
    pts = {p["date"]: p for p in out["points"]}
    assert list(pts) == ["2026-01-31", "2026-02-28", "2026-03-31"]
    assert pts["2026-01-31"]["bank"] == "100.00" and pts["2026-01-31"]["investment"] == "0.00"
    assert pts["2026-02-28"]["bank"] == "300.00" and pts["2026-02-28"]["net"] == "5300.00"
    assert pts["2026-03-31"]["assets"] == "6200.00"
    assert not any(p["estimated"] for p in out["points"])
    assert out["history_starts"]["bank"] == "2026-01-31"
    assert out["history_starts"]["investment"] == "2026-02-28"
    assert out["history_starts"]["card"] is None
    weekly = net_worth(db, TODAY, date_from=date(2026, 3, 25), granularity="week")
    assert [p["date"] for p in weekly["points"]] == ["2026-03-29", "2026-03-31"]


def test_estimated_bank_history(db, add_row):
    with db.write() as cur:
        snap(cur, "balance_snapshots", "acc-main", "main", TODAY, "1000.00")
    add_row("e1", "main", "acc-main", date(2026, 3, 28), "-200.00", "x", "purchase")
    add_row("e2", "main", "acc-main", date(2026, 3, 25), "50.00", "y", "income")
    ten = date(2026, 3, 21)
    out = net_worth(db, TODAY, date_from=ten, date_to=TODAY, granularity="day")
    pts = {p["date"]: p for p in out["points"]}
    assert pts["2026-03-21"]["bank"] == "1150.00" and pts["2026-03-21"]["estimated"] is True
    assert pts["2026-03-24"]["bank"] == "1150.00"
    assert pts["2026-03-25"]["bank"] == "1200.00"
    assert pts["2026-03-27"]["bank"] == "1200.00"
    assert pts["2026-03-28"]["bank"] == "1000.00"
    assert pts["2026-03-31"]["estimated"] is False


def test_card_and_loan_are_liabilities(db):
    with db.write() as cur:
        snap(cur, "balance_snapshots", "acc-main", "main", TODAY, "1000.00")
        snap(cur, "balance_snapshots", "card-main", "main", TODAY, "300.00")
        cur.execute(
            "INSERT INTO loans (id, alias, item_id, outstanding_balance, currency, first_seen_at, "
            "last_seen_at) VALUES ('l1', 'main', 'item', 2000, 'BRL', ?, ?)",
            [NOW, NOW],
        )
    out = net_worth(db, TODAY, date_from=date(2026, 3, 30), granularity="day")
    yesterday, now = out["points"]
    assert yesterday["loan"] == "0.00" and yesterday["card"] == "0.00"
    assert now["card"] == "300.00" and now["loan"] == "2000.00"
    assert now["liabilities"] == "2300.00" and now["net"] == "-1300.00"
    assert out["history_starts"]["loan"] == "2026-03-31"


def test_scope_filters_roles(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at, status, role) "
            "VALUES ('biz', 'item-3', 'Bank C', ?, 'UPDATED', 'business')", [NOW],
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES ('acc-biz', 'biz', 'item', 'BANK', 'X', 'Biz', 9, 'BRL', ?, ?)",
            [NOW, NOW],
        )
        snap(cur, "balance_snapshots", "acc-main", "main", TODAY, "1000.00")
        snap(cur, "balance_snapshots", "acc-biz", "biz", TODAY, "7000.00")
    kw = {"date_from": TODAY, "granularity": "day"}
    assert net_worth(db, TODAY, **kw)["points"][0]["net"] == "1000.00"
    assert net_worth(db, TODAY, scope="business", **kw)["points"][0]["net"] == "7000.00"
    assert net_worth(db, TODAY, scope="consolidated", **kw)["points"][0]["net"] == "8000.00"
    try:
        net_worth(db, TODAY, scope="nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_empty_store(db):
    out = net_worth(db, TODAY)
    assert out["points"] == []
    assert out["history_starts"] == {"bank": None, "investment": None, "card": None, "loan": None}


def test_foreign_currency_counted_apart(db):
    with db.write() as cur:
        snap(cur, "balance_snapshots", "acc-main", "main", TODAY, "1000.00")
        snap(cur, "balance_snapshots", "acc-second", "main", TODAY, "50.00", "USD")
    out = net_worth(db, TODAY, date_from=TODAY, granularity="day")
    assert out["points"][0]["net"] == "1000.00" and out["excluded_foreign"] == 1


def test_overview_net_worth_block(db):
    assert finance_overview(db, TODAY)["net_worth"] == {
        "current": None, "change_this_month": None, "change_components": [],
    }
    with db.write() as cur:
        snap(cur, "balance_snapshots", "acc-main", "main", date(2026, 2, 28), "900.00")
        snap(cur, "balance_snapshots", "acc-main", "main", TODAY, "1000.00")
    nw = finance_overview(db, TODAY)["net_worth"]
    assert nw == {"current": "1000.00", "change_this_month": "100.00", "change_components": ["bank"]}


def _bank(cur, d, b):
    snap(cur, "balance_snapshots", "acc-main", "main", d, b)


def test_change_ignores_loans(db):
    with db.write() as cur:
        _bank(cur, date(2026, 2, 28), "900.00")
        _bank(cur, TODAY, "1000.00")
        cur.execute(
            "INSERT INTO loans (id, alias, item_id, outstanding_balance, currency, first_seen_at, "
            "last_seen_at) VALUES ('l1', 'main', 'item', 2000, 'BRL', ?, ?)",
            [NOW, NOW],
        )
    nw = finance_overview(db, TODAY)["net_worth"]
    assert nw["current"] == "-1000.00"
    assert nw["change_this_month"] == "100.00" and nw["change_components"] == ["bank"]


def test_change_excludes_investment_first_seen_this_month(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO investments (id, alias, item_id, type, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES ('inv-1', 'main', 'item', 'FIXED', 'CDB', 5000, 'BRL', ?, ?)",
            [NOW, NOW],
        )
        _bank(cur, date(2026, 2, 28), "900.00")
        _bank(cur, TODAY, "1000.00")
        snap(cur, "investment_snapshots", "inv-1", "main", date(2026, 3, 10), "5000.00")
    nw = finance_overview(db, TODAY)["net_worth"]
    assert nw["change_this_month"] == "100.00" and nw["change_components"] == ["bank"]


def test_change_excludes_estimated_bank(db, add_row):
    with db.write() as cur:
        _bank(cur, date(2026, 3, 5), "1000.00")  # first snapshot after previous month-end
    add_row("e1", "main", "acc-main", date(2026, 3, 3), "-20.00", "x", "purchase")
    nw = finance_overview(db, TODAY)["net_worth"]
    assert nw["current"] == "1000.00"
    assert nw["change_this_month"] is None and nw["change_components"] == []


def test_change_counts_cards_as_liabilities(db):
    with db.write() as cur:
        _bank(cur, date(2026, 2, 28), "900.00")
        _bank(cur, TODAY, "900.00")
        snap(cur, "balance_snapshots", "card-main", "main", date(2026, 2, 28), "100.00")
        snap(cur, "balance_snapshots", "card-main", "main", TODAY, "300.00")
    nw = finance_overview(db, TODAY)["net_worth"]
    assert nw["change_this_month"] == "-200.00" and nw["change_components"] == ["bank", "card"]


def test_date_to_is_clamped_to_today(db):
    with db.write() as cur:
        _bank(cur, TODAY, "1000.00")
    out = net_worth(db, TODAY, date_from=date(2026, 3, 30), date_to=date(2026, 6, 30), granularity="day")
    assert [p["date"] for p in out["points"]] == ["2026-03-30", "2026-03-31"]


def test_daily_granularity_is_capped(db):
    import pytest

    with pytest.raises(ValueError, match="narrow"):
        net_worth(db, TODAY, date_from=date(2025, 1, 1), granularity="day")
    net_worth(db, TODAY, date_from=date(2025, 2, 25), granularity="day")  # 400 points
