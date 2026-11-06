from datetime import date
from decimal import Decimal

import pytest

from cashbook_mcp.analysis.filters import TxFilters, where_clause
from cashbook_mcp.analysis.search import get_transactions, search_transactions


def test_search_keyset_survives_reload(db, add_row):
    f = TxFilters(exclude_internal=False)
    everything = search_transactions(db, f, limit=500)["rows"]
    original_ids = [r["id"] for r in everything]
    page1 = search_transactions(db, f, limit=3)
    assert [r["id"] for r in page1["rows"]] == original_ids[:3]
    add_row("t-new", "main", "acc-main", date(2026, 3, 30), "-1.00", "Newer", "purchase")
    seen = [r["id"] for r in page1["rows"]]
    cursor = page1["next_cursor"]
    while cursor:
        page = search_transactions(db, f, limit=3, cursor=cursor)
        seen += [r["id"] for r in page["rows"]]
        cursor = page["next_cursor"]
    assert seen == original_ids
    assert "t-new" not in seen


def test_limit_capped_at_500(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
            "description, first_seen_at, last_seen_at) "
            "SELECT 'bulk-' || i, 'main', 'acc-main', DATE '2026-02-01', -1, 'BRL', 'x', "
            "TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-01' FROM range(600) t(i)"
        )
    page = search_transactions(db, TxFilters(), limit=10_000)
    assert len(page["rows"]) == 500
    assert page["next_cursor"] is not None


def test_rows_are_json_safe(db):
    row = search_transactions(db, TxFilters(text="github"))["rows"][0]
    assert row["amount"] == "-52.30"
    assert row["date"] == "2026-03-12"
    assert row["original_amount"] == "10.00"
    assert row["suspicion_reasons"] == []


def test_filters(db):
    ids = lambda f: {r["id"] for r in search_transactions(db, f)["rows"]}
    assert ids(TxFilters(text="NETFLIX", date_from=date(2026, 1, 1))) == {
        "t-sub-3", "t-sub-4", "t-sub-5",
    }  # fmt: skip
    assert ids(TxFilters(min_amount=Decimal(1000), exclude_internal=False)) == {
        "t-salary", "t-pix-out", "t-pix-in",
    }  # fmt: skip
    assert ids(TxFilters(min_amount=Decimal(1000))) == {"t-salary"}
    assert ids(TxFilters(direction="in", aliases=["second"], exclude_internal=False)) == {
        "t-pix-in"
    }
    assert ids(TxFilters(categories=["Software"])) == {"t-usd"}
    assert ids(TxFilters(kinds=["card_bill_payment"], exclude_internal=False)) == {
        "t-bill-pay"
    }


def test_text_wildcards_are_literal(db):
    assert search_transactions(db, TxFilters(text="%"))["rows"] == []


def test_where_clause_binds_values():
    sql, params = where_clause(TxFilters(text="x'; DROP TABLE t; --", aliases=["a"]))
    assert "DROP" not in sql
    assert any("DROP" in str(p) for p in params)


def test_invalid_cursor(db):
    with pytest.raises(ValueError):
        search_transactions(db, TxFilters(), cursor="not-a-cursor")


def test_get_transactions(db):
    rows = get_transactions(db, ["t-usd", "t-salary", "missing"])
    assert {r["id"] for r in rows} == {"t-usd", "t-salary"}
    with pytest.raises(ValueError):
        get_transactions(db, [f"id-{i}" for i in range(101)])
