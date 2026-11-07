import json
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


def test_search_limit_default_and_clamp(db):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
            "description, first_seen_at, last_seen_at) "
            "SELECT 'bulk-' || i, 'main', 'acc-main', DATE '2026-02-01', -1, 'BRL', 'x', "
            "TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-01' FROM range(600) t(i)"
        )
    default = search_transactions(db, TxFilters())
    assert len(default["rows"]) == 50
    assert "limit_clamped" not in default
    page = search_transactions(db, TxFilters(), limit=500)
    assert len(page["rows"]) == 200
    assert page["limit_clamped"] is True
    assert page["next_cursor"] is not None


def test_rows_are_json_safe(db):
    row = search_transactions(db, TxFilters(text="github"))["rows"][0]
    assert row["amount"] == "-52.30"
    assert row["date"] == "2026-03-12"
    full = get_transactions(db, [row["id"]])[0]
    assert full["original_amount"] == "-10.00"
    assert full["suspicion_reasons"] == []


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


def test_requested_internal_kinds_are_not_excluded(db):
    res = search_transactions(db, TxFilters(kinds=["card_bill_payment"]))
    assert [r["id"] for r in res["rows"]] == ["t-bill-pay"]
    both = search_transactions(db, TxFilters(kinds=["internal_transfer", "purchase"]))
    ids = {r["id"] for r in both["rows"]}
    assert {"t-pix-out", "t-pix-in", "t-card-1"} <= ids
    assert "t-bill-pay" not in ids


def test_uncategorized_maps_to_null_category(db, add_row):
    add_row("t-nocat", "main", "acc-main", date(2026, 3, 9), "-7.00", "Banca", "purchase")
    res = search_transactions(db, TxFilters(categories=["uncategorized"]))
    assert [r["id"] for r in res["rows"]] == ["t-nocat"]
    mixed = search_transactions(db, TxFilters(categories=["uncategorized", "Groceries"]))
    assert {r["id"] for r in mixed["rows"]} == {"t-nocat", "t-card-1"}


def test_rows_carry_role_and_flow_and_scope_filters(db, add_row):
    with db.write() as cur:
        cur.execute("UPDATE connections SET role = 'business' WHERE alias = 'second'")
    add_row("b-out", "second", "acc-second", date(2026, 3, 6), "-120.00", "Tool", "purchase")
    biz = search_transactions(db, TxFilters(scope="business"))["rows"]
    assert [(r["id"], r["flow"]) for r in biz] == [("b-out", "spending")]
    assert "b-out" not in {r["id"] for r in search_transactions(db, TxFilters(scope="personal"))["rows"]}
    both = search_transactions(db, TxFilters(scope="consolidated"))["rows"]
    assert "b-out" in {r["id"] for r in both}
    full = get_transactions(db, ["b-out"])[0]
    assert (full["role"], full["flow"]) == ("business", "spending")


def test_scope_keeps_cross_role_transfers_and_drops_same_role_ones(db):
    with db.write() as cur:
        cur.execute("UPDATE connections SET role = 'business' WHERE alias = 'second'")
        cur.execute("UPDATE transactions SET kind_reason = 'paired_with:t-pix-in' WHERE id = 't-pix-out'")
        cur.execute("UPDATE transactions SET kind_reason = 'paired_with:t-pix-out' WHERE id = 't-pix-in'")

    def ids(**kw):
        return {r["id"] for r in search_transactions(db, TxFilters(**kw))["rows"]}

    # A transfer between personal and business is money moving between scopes: it shows up.
    assert "t-pix-out" in ids(scope="personal", direction="out")
    assert "t-pix-in" in ids(scope="business", direction="in")
    # A card bill paid inside the same scope stays internal.
    assert "t-bill-pay" not in ids(scope="personal")
    # Without scope, v0.1 behavior: internal kinds are excluded whatever their flow.
    assert not {"t-pix-out", "t-pix-in"} & ids()
    assert "t-pix-out" in ids(scope="personal", kinds=["internal_transfer"])


ALWAYS = {"id", "date", "amount", "alias", "description", "kind", "flow"}


def test_compact_rows_keys(db, add_row):
    add_row("k-plain", "main", "acc-main", date(2026, 3, 20), "-5.00", "Plain", "purchase")
    add_row(
        "k-full", "main", "acc-main", date(2026, 3, 21), "-9.00", "Full", "purchase",
        category="Food", merchant="Shop", counterpart="Someone", currency="USD",
    )  # fmt: skip
    with db.write() as cur:
        cur.execute("UPDATE transactions SET text_suspicious = true WHERE id = 'k-full'")
    rows = {r["id"]: r for r in search_transactions(db, TxFilters())["rows"]}
    assert set(rows["k-plain"]) == ALWAYS
    assert set(rows["k-full"]) == ALWAYS | {
        "category", "merchant_name", "counterpart_name", "currency", "text_suspicious",
    }
    assert rows["k-full"]["text_suspicious"] is True


def test_compact_row_is_small(db, add_row):
    add_row("0123456789abcdef0123456789abcdef0123", "main", "acc-main", date(2026, 3, 22),
            "-120.50", "Supermarket purchase", "purchase", category="Groceries")  # fmt: skip
    rows = search_transactions(db, TxFilters(text="Supermarket purchase"))["rows"]
    assert len(json.dumps(rows[0])) < 300


def test_category_overridden_flag_only_when_true(db, add_row):
    add_row("t-ovr", "main", "acc-main", date(2026, 3, 20), "-9.00", "ACME Imóveis", "purchase", category="Other")
    with db.write() as cur:
        cur.execute("INSERT INTO category_overrides VALUES ('t-ovr', 'main', 'Rent', 'c1')")
    rows = {r["id"]: r for r in search_transactions(db, TxFilters(exclude_internal=False), limit=500)["rows"]}
    assert rows["t-ovr"]["category"] == "Rent" and rows["t-ovr"]["category_overridden"] is True
    assert all("category_overridden" not in r for i, r in rows.items() if i != "t-ovr")
    assert get_transactions(db, ["t-ovr"])[0]["category_overridden"] is True


def _set_method(db, tx_id, method):
    with db.write() as cur:
        cur.execute("UPDATE transactions SET payment_method = ? WHERE id = ?", [method, tx_id])


def test_compact_row_shows_payment_method_unless_null_or_other(db):
    _set_method(db, "t-pix-out", "PIX")
    _set_method(db, "t-salary", "OTHER")
    f = TxFilters(exclude_internal=False)
    rows = {r["id"]: r for r in search_transactions(db, f, limit=200)["rows"]}
    assert rows["t-pix-out"]["payment_method"] == "PIX"
    assert "payment_method" not in rows["t-salary"]
    assert "payment_method" not in rows["t-usd"]  # null
    # full rows always carry it
    assert get_transactions(db, ["t-salary"])[0]["payment_method"] == "OTHER"
    assert get_transactions(db, ["t-usd"])[0]["payment_method"] is None


def test_payment_methods_filter_is_case_insensitive_and_bound(db):
    _set_method(db, "t-pix-out", "PIX")
    _set_method(db, "t-pix-in", "PIX")
    _set_method(db, "t-bill-pay", "BOLETO")
    ids = lambda f: {r["id"] for r in search_transactions(db, f, limit=200)["rows"]}
    assert ids(TxFilters(payment_methods=["pix"], exclude_internal=False)) == {"t-pix-out", "t-pix-in"}
    assert ids(TxFilters(payment_methods=["Boleto", "pix"], exclude_internal=False)) == {
        "t-pix-out", "t-pix-in", "t-bill-pay",
    }  # fmt: skip
    assert ids(TxFilters(payment_methods=["x' OR '1'='1"], exclude_internal=False)) == set()
    where, params = where_clause(TxFilters(payment_methods=["pix"]))
    assert "pix" not in where.lower().replace("payment_method", "") and "PIX" in params
