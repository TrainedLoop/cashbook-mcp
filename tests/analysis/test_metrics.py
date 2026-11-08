from datetime import date
from decimal import Decimal

import pytest

from cashbook_mcp.analysis.filters import TxFilters
from cashbook_mcp.analysis.metrics import SCOPES, scope_clause, totals
from cashbook_mcp.analysis.summarize import (
    cash_flow,
    list_categories,
    summarize_transactions,
)
from cashbook_mcp.store.db import Database

KEY = "ab" * 32
CNPJ = "12.345.678/0001-95"
ACCOUNTS = [
    ("pa", "pers", "BANK", "111.444.777-35"),
    ("pc", "pers", "CREDIT", "11144477735"),
    ("ba", "biz", "BANK", CNPJ),
]
MARCH = (date(2026, 3, 1), date(2026, 3, 31))
TODAY = date(2026, 3, 31)


def make_db(tmp_path, with_business=True):
    database = Database.open(tmp_path / "m.duckdb", KEY)
    with database.write() as cur:
        conns = [("pers", "personal")] + ([("biz", "business")] if with_business else [])
        for alias, role in conns:
            cur.execute(
                "INSERT INTO connections (alias, item_id, connector_name, created_at, role) "
                "VALUES (?, ?, 'Bank', now(), ?)",
                [alias, f"item-{alias}", role],
            )
        for aid, alias, typ, tax in ACCOUNTS:
            if alias == "biz" and not with_business:
                continue
            cur.execute(
                "INSERT INTO accounts (id, alias, item_id, type, subtype, name, owner_tax_id, "
                "balance, currency, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, 'X', 'Acc', ?, 0, 'BRL', now(), now())",
                [aid, alias, f"item-{alias}", typ, tax],
            )
    return database


def add(db, tid, account, amount, kind, *, d=date(2026, 3, 10), category=None,
        reason="sign", tax=None):
    alias = next(a[1] for a in ACCOUNTS if a[0] == account)
    with db.write() as cur:
        cur.execute(
            "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
            "description, counterpart_tax_id, pluggy_category, kind, kind_reason, "
            "first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, 'BRL', 'x', ?, ?, ?, ?, now(), now())",
            [tid, alias, account, d, Decimal(amount), tax, category, kind, reason],
        )


@pytest.fixture
def db(tmp_path):
    database = make_db(tmp_path)
    yield database
    database.close()


@pytest.fixture
def scoped(db):
    add(db, "client", "ba", "1000.00", "income")
    add(db, "tax", "ba", "-150.00", "purchase", category="Taxes")
    add(db, "draw", "ba", "-200.00", "internal_transfer", reason="paired_with:from_biz")
    add(db, "from_biz", "pa", "200.00", "internal_transfer",
        reason="counterpart_is_owner_tax_id", tax=CNPJ)
    add(db, "groc", "pa", "-80.00", "purchase", category="Groceries")
    # Owner contribution: personal account pays the business card.
    add(db, "contrib", "pa", "-90.00", "card_bill_payment",
        reason="counterpart_is_own_account:biz")
    add(db, "contrib_in", "ba", "90.00", "card_bill_payment", reason="paired_with:contrib")
    return db


def test_scope_totals(scoped):
    personal = totals(scoped, *MARCH, "personal")
    assert personal == {
        "income": Decimal("200.00"), "refunds": Decimal("0.00"),
        "spending": Decimal("80.00"), "net": Decimal("120.00"),
        "from_business": Decimal("200.00"), "to_business": Decimal("90.00"),
        "one_off_out": Decimal("0.00"), "one_off_in": Decimal("0.00"), "excluded_foreign": 0,
    }
    business = totals(scoped, *MARCH, "business")
    assert business == {
        "income": Decimal("1000.00"), "refunds": Decimal("0.00"),
        "spending": Decimal("150.00"), "net": Decimal("850.00"),
        "owner_draws": Decimal("200.00"), "owner_contributions": Decimal("90.00"),
        "one_off_out": Decimal("0.00"), "one_off_in": Decimal("0.00"), "excluded_foreign": 0,
    }
    cons = totals(scoped, *MARCH, "consolidated")
    assert cons == {
        "income": Decimal("1000.00"), "refunds": Decimal("0.00"),
        "spending": Decimal("230.00"), "net": Decimal("770.00"),
        "one_off_out": Decimal("0.00"), "one_off_in": Decimal("0.00"), "excluded_foreign": 0,
    }


def test_categories_add_up_to_cash_flow_spending(db):
    add(db, "a", "pa", "-100.00", "purchase", category="Groceries")
    add(db, "b", "pc", "-60.00", "purchase", category="Eating out")
    add(db, "r", "pc", "25.00", "refund", category="Eating out")
    add(db, "r2", "pc", "5.00", "purchase", category="Groceries")  # inflow purchase = refund
    add(db, "biz", "ba", "-70.00", "purchase", category="Taxes")
    f = TxFilters(date_from=MARCH[0], date_to=MARCH[1])
    for scope in SCOPES:
        spending = Decimal(cash_flow(db, *MARCH, scope=scope)["totals"]["spending"])
        cats = list_categories(db, *MARCH, scope=scope)
        assert sum(Decimal(c["total"]) for c in cats) == spending
        groups = summarize_transactions(db, f, "category", today=TODAY, scope=scope)["groups"]
        assert sum(Decimal(g["total"]) for g in groups) == spending
    personal = {c["category"]: c["total"] for c in list_categories(db, *MARCH)}
    assert personal == {"Groceries": "95.00", "Eating out": "35.00"}


def test_two_dimension_grouping(db):
    add(db, "a", "pa", "-10.00", "purchase", category="Groceries", d=date(2026, 2, 10))
    add(db, "b", "pa", "-20.00", "purchase", category="Groceries")
    add(db, "c", "pa", "-30.00", "purchase", category="Fun")
    f = TxFilters(date_from=date(2026, 2, 1), date_to=MARCH[1])
    groups = summarize_transactions(db, f, ["category", "month"], today=TODAY)["groups"]
    assert {(tuple(g["key"]), g["total"]) for g in groups} == {
        (("Groceries", "2026-02"), "10.00"),
        (("Groceries", "2026-03"), "20.00"),
        (("Fun", "2026-03"), "30.00"),
    }
    with pytest.raises(ValueError):
        summarize_transactions(db, f, ["category", "month", "kind"], today=TODAY)


def test_unknown_scope_raises(db):
    with pytest.raises(ValueError, match="unsupported scope: nope"):
        scope_clause("nope")
    with pytest.raises(ValueError, match="unsupported scope"):
        totals(db, *MARCH, "nope")
    with pytest.raises(ValueError, match="unsupported scope"):
        cash_flow(db, *MARCH, scope="nope")


def test_business_scope_with_no_business_connection_is_empty(tmp_path):
    database = make_db(tmp_path, with_business=False)
    try:
        add(database, "a", "pa", "-10.00", "purchase", category="Groceries")
        t = totals(database, *MARCH, "business")
        assert t["income"] == t["spending"] == t["net"] == Decimal("0.00")
        assert t["owner_draws"] == t["owner_contributions"] == Decimal("0.00")
        res = cash_flow(database, *MARCH, scope="business")
        assert res["periods"] == [] and res["scope"] == "business"
        assert list_categories(database, *MARCH, scope="business") == []
        f = TxFilters(date_from=MARCH[0], date_to=MARCH[1])
        out = summarize_transactions(database, f, "category", today=TODAY, scope="business")
        assert out["groups"] == [] and out["scope"] == "business"
    finally:
        database.close()


def test_cash_flow_carries_apart_keys(scoped):
    res = cash_flow(scoped, *MARCH, scope="business")
    assert res["scope"] == "business"
    assert res["periods"][0]["owner_draws"] == "200.00"
    assert res["totals"]["owner_draws"] == "200.00"
    assert res["totals"]["owner_contributions"] == "90.00"
    assert res["totals"]["income"] == "1000.00"  # the contribution is not income
    res = cash_flow(scoped, *MARCH)
    assert res["periods"][0]["from_business"] == "200.00"
    assert res["totals"]["income"] == "200.00"


def test_filters_honor_scope():
    from cashbook_mcp.analysis.filters import where_clause

    assert where_clause(TxFilters(scope="business", exclude_internal=False))[0] == "WHERE role = ?"
    assert where_clause(TxFilters(scope="consolidated", exclude_internal=False)) == ("", [])


def test_excluded_foreign_counts_only_the_requested_side(db):
    def foreign(tid, amount, kind):
        with db.write() as cur:
            cur.execute(
                "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
                "description, kind, kind_reason, first_seen_at, last_seen_at) "
                "VALUES (?, 'pers', 'pa', DATE '2026-03-10', ?, 'USD', 'x', ?, 'sign', "
                "now(), now())",
                [tid, Decimal(amount), kind],
            )

    add(db, "pay", "pa", "100.00", "income")
    foreign("fp", "-9.00", "purchase")
    f = TxFilters(date_from=MARCH[0], date_to=MARCH[1], direction="in")
    res = summarize_transactions(db, f, "kind", today=TODAY)
    assert "excluded_foreign" not in res
    foreign("fi", "9.00", "income")
    res = summarize_transactions(db, f, "kind", today=TODAY)
    assert res["excluded_foreign"] == 1
    out = summarize_transactions(
        db, TxFilters(date_from=MARCH[0], date_to=MARCH[1]), "kind", today=TODAY
    )
    assert out["excluded_foreign"] == 1
    foreign("fo", "-500.00", "one_off")  # not skipped spending
    out = summarize_transactions(
        db, TxFilters(date_from=MARCH[0], date_to=MARCH[1]), "kind", today=TODAY
    )
    assert out["excluded_foreign"] == 1
    assert totals(db, *MARCH)["excluded_foreign"] == 2  # fp and fi, not fo


def test_one_off_is_reported_apart_and_categories_still_add_up(db):
    add(db, "tx-spend", "pa", "-80.00", "purchase", category="Groceries")
    add(db, "tx-pay", "pa", "-900.00", "one_off", category="Housing", reason="manual:1")
    add(db, "tx-back", "pa", "100.00", "one_off", category="Housing", reason="manual:1")
    add(db, "tx-biz", "ba", "-50.00", "one_off", reason="manual:1")
    res = cash_flow(db, *MARCH, today=TODAY)
    assert res["totals"]["spending"] == "80.00"
    assert res["totals"]["income"] == "0.00"
    assert (res["totals"]["one_off_out"], res["totals"]["one_off_in"]) == ("900.00", "100.00")
    assert res["periods"][0]["one_off_out"] == "900.00"
    assert sum(Decimal(c["total"]) for c in list_categories(db, *MARCH)) == Decimal("80.00")
    t = totals(db, *MARCH)
    assert (t["spending"], t["one_off_out"], t["one_off_in"]) == (
        Decimal("80.00"), Decimal("900.00"), Decimal("100.00"))
    assert totals(db, *MARCH, "business")["one_off_out"] == Decimal("50.00")
    assert totals(db, *MARCH, "consolidated")["one_off_out"] == Decimal("950.00")
