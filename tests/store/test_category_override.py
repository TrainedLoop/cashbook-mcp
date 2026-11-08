from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from cashbook_mcp.analysis.filters import TxFilters
from cashbook_mcp.analysis.summarize import (
    cash_flow,
    list_categories,
    summarize_transactions,
)
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.reclassify import (
    EXTRA_CATEGORIES,
    ReclassifyError,
    list_changes,
    reclassify,
    undo,
)

KEY = "ef" * 32
TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)
SEEN = datetime(2026, 1, 1)
ROWS = [  # id, category, amount
    ("t-1", "Entrepreneurial activities", "-1500.00"),
    ("t-2", "Entrepreneurial activities", "-1500.00"),
    ("t-3", "Groceries", "-40.00"),
    ("t-4", None, "-5.00"),
    ("t-5", "", "-7.00"),
]


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "s.duckdb", KEY)
    with d.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) VALUES ('main', 'i', 'Bank', ?)",
            [SEEN],
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, first_seen_at, "
            "last_seen_at) VALUES ('acc', 'main', 'i', 'BANK', 'CHECKING_ACCOUNT', 'Checking', 0, 'BRL', ?, ?)",
            [SEEN, SEEN],
        )
        for tid, category, amount in ROWS:
            cur.execute(
                "INSERT INTO transactions (id, alias, account_id, date, amount, currency, description, "
                "pluggy_category, kind, kind_reason, first_seen_at, last_seen_at) VALUES "
                "(?, 'main', 'acc', ?, ?, 'BRL', 'ACME Imóveis', ?, 'purchase', 'auto', ?, ?)",
                [tid, date(2026, 3, 5), Decimal(amount), category, SEEN, SEEN],
            )
    yield d
    d.close()


def run(db, ids, kind=None, category=None, note=None, cap=1000):
    return reclassify(db, ids, kind, note, category=category, now=NOW, tz=TZ, daily_cap=cap)


def one(db, sql, *params):
    cur = db.read()
    try:
        return cur.execute(sql, list(params)).fetchall()
    finally:
        cur.close()


def flow_row(db, tid):
    return one(
        db,
        "SELECT pluggy_category, pluggy_category_original, category_overridden, kind FROM v_flows WHERE id = ?",
        tid,
    )[0]


def test_category_only_batch(db):
    out = run(db, ["t-1", "t-2"], category="rent")
    assert out["count"] == 2
    assert out["changes"] == [
        {"id": i, "old_category": "Entrepreneurial activities", "new_category": "Rent"} for i in ("t-1", "t-2")
    ]
    assert flow_row(db, "t-1") == ("Rent", "Entrepreneurial activities", True, "purchase")
    assert flow_row(db, "t-4") == (None, None, False, "purchase")
    assert one(db, "SELECT kind_reason FROM transactions WHERE id = 't-1'") == [("sign",)]
    (change,) = list_changes(db)
    assert change["kind"] is None and change["category"] == "Rent" and change["count"] == 2
    assert run(db, ["t-1"], category="Rent")["count"] == 0  # already the effective category


def test_kind_and_category_batch_counts_once(db):
    out = run(db, ["t-1"], kind="refund", category="Housing", cap=1)
    assert out["count"] == 1
    assert out["changes"] == [{
        "id": "t-1", "old_kind": "purchase", "new_kind": "refund",
        "old_category": "Entrepreneurial activities", "new_category": "Housing",
    }]
    assert one(db, "SELECT kind FROM v_flows WHERE id = 't-1'") == [("refund",)]
    assert flow_row(db, "t-1")[0] == "Housing"
    (change,) = list_changes(db)
    assert (change["kind"], change["category"], change["count"]) == ("refund", "Housing", 1)
    with pytest.raises(ReclassifyError) as e:
        run(db, ["t-2"], category="Rent", cap=1)
    assert e.value.code == "daily_cap" and e.value.details["remaining"] == 0


def test_batches_do_not_erase_each_other(db):
    k = run(db, ["t-1"], kind="refund")
    c = run(db, ["t-1"], category="Rent")
    assert one(db, "SELECT kind FROM v_flows WHERE id = 't-1'") == [("refund",)]
    k2 = run(db, ["t-1"], kind="income")
    assert flow_row(db, "t-1")[0] == "Rent"
    assert undo(db, k2["change_id"], now=NOW)["reverted"] == 1
    assert flow_row(db, "t-1")[0] == "Rent"
    assert undo(db, c["change_id"], now=NOW)["reverted"] == 1
    assert flow_row(db, "t-1")[:3] == ("Entrepreneurial activities", "Entrepreneurial activities", False)
    assert one(db, "SELECT kind FROM v_flows WHERE id = 't-1'") == [("purchase",)]  # k2 replaced k
    assert undo(db, k["change_id"], now=NOW)["reverted"] == 0


def test_undo_removes_only_its_category_overrides(db):
    a = run(db, ["t-1", "t-2"], category="Rent")
    b = run(db, ["t-2"], category="Housing")
    assert undo(db, a["change_id"], now=NOW)["reverted"] == 1  # t-2 belongs to B now
    assert flow_row(db, "t-1")[2] is False
    assert flow_row(db, "t-2")[0] == "Housing"
    both = run(db, ["t-3"], kind="refund", category="Rent")
    assert undo(db, both["change_id"], now=NOW)["reverted"] == 1  # one transaction, two overrides
    assert one(db, "SELECT count(*) FROM category_overrides WHERE change_id = ?", both["change_id"]) == [(0,)]
    assert undo(db, b["change_id"], now=NOW)["reverted"] == 1


def test_bad_and_empty_category(db):
    for bad in ("Nonsense", "", "   "):
        with pytest.raises(ReclassifyError) as e:
            run(db, ["t-1"], category=bad)
        assert e.value.code == "bad_category"
        valid = e.value.details["valid_categories"]
        assert valid == sorted(valid)
        assert {"Groceries", "Entrepreneurial activities", *EXTRA_CATEGORIES} == set(valid)  # no "" from t-5
    assert one(db, "SELECT count(*) FROM classification_changes") == [(0,)]


def test_nothing_to_change(db):
    with pytest.raises(ReclassifyError) as e:
        run(db, ["t-1"])
    assert e.value.code == "nothing_to_change"


@pytest.mark.parametrize("name", EXTRA_CATEGORIES)
def test_extra_categories_accepted(db, name):
    assert run(db, ["t-3"], category=name)["changes"][0]["new_category"] == name


def test_case_insensitive_canonicalization(db):
    out = run(db, ["t-3"], category="ENTREPRENEURIAL ACTIVITIES")
    assert out["changes"][0]["new_category"] == "Entrepreneurial activities"
    assert one(db, "SELECT category FROM category_overrides") == [("Entrepreneurial activities",)]


def test_uncategorized_old_category_is_null(db):
    assert run(db, ["t-4"], category="Rent")["changes"][0]["old_category"] is None


def test_summaries_use_effective_category_and_add_up(db):
    run(db, ["t-1"], category="Rent")
    f = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
    cats = {c["category"]: c["total"] for c in list_categories(db, f.date_from, f.date_to)}
    assert cats == {
        "Rent": "1500.00", "Entrepreneurial activities": "1500.00", "Groceries": "40.00",
        "uncategorized": "5.00", "": "7.00",
    }
    flow = cash_flow(db, f.date_from, f.date_to, today=date(2026, 3, 31))
    assert sum(Decimal(v) for v in cats.values()) == Decimal(flow["totals"]["spending"])
    by_cat = summarize_transactions(db, f, "category", today=date(2026, 3, 31))
    assert {g["key"] for g in by_cat["groups"]} == set(cats)
    only_rent = summarize_transactions(
        db, TxFilters(date_from=f.date_from, date_to=f.date_to, categories=["Rent"]), "kind",
        today=date(2026, 3, 31),
    )
    assert only_rent["groups"][0]["count"] == 1


def test_kind_is_never_changed_by_category(db):
    from cashbook_mcp.store.sync import Syncer

    run(db, ["t-1"], category="Rent")
    with db.write() as cur:
        Syncer._reclassify(cur, NOW.replace(tzinfo=None))
    assert one(db, "SELECT kind, kind_reason FROM transactions WHERE id = 't-1'") == [("purchase", "sign")]
    # Classification reads the provider's category: a transfer-like category still decides the kind.
    with db.write() as cur:
        cur.execute("UPDATE transactions SET pluggy_category = 'Credit card payment' WHERE id = 't-2'")
        cur.execute("INSERT INTO category_overrides VALUES ('t-2', 'main', 'Rent', 'x')")
        Syncer._reclassify(cur, NOW.replace(tzinfo=None))
    assert one(db, "SELECT kind FROM transactions WHERE id = 't-2'") == [("card_bill_payment",)]


def test_override_on_removed_row_is_harmless(db):
    out = run(db, ["t-1"], category="Rent")
    with db.write() as cur:
        cur.execute("UPDATE transactions SET removed_at = ? WHERE id = 't-1'", [SEEN])
    assert len(list_changes(db)) == 1
    assert undo(db, out["change_id"], now=NOW)["reverted"] == 1


def test_back_to_provider_category_deletes_the_override(db):
    a = run(db, ["t-1"], category="Rent")
    out = run(db, ["t-1"], category="entrepreneurial activities")
    assert out["count"] == 1  # still a journaled change
    assert out["changes"] == [
        {"id": "t-1", "old_category": "Rent", "new_category": "Entrepreneurial activities"}
    ]
    assert one(db, "SELECT count(*) FROM category_overrides") == [(0,)]
    assert flow_row(db, "t-1")[2] is False
    assert len(list_changes(db)) == 2
    assert undo(db, a["change_id"], now=NOW)["reverted"] == 0
