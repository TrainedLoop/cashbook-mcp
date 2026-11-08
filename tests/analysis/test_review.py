from datetime import date
from decimal import Decimal

import pytest

from cashbook_mcp.analysis.review import review_classification
from cashbook_mcp.content import MARK_OPEN

MIN = Decimal("500.00")


def _set(db, tx_id, **cols):
    with db.write() as cur:
        for col, value in cols.items():
            cur.execute(f"UPDATE transactions SET {col} = ? WHERE id = ?", [value, tx_id])


def _row(db, add_row, id, amount, kind, reason, *, category=None, counterpart=None, tax=None,
         acct=None, d=date(2026, 3, 10), account="acc-main", alias="main"):
    add_row(id, alias, account, d, amount, f"desc {id}", kind, category=category, counterpart=counterpart)
    _set(db, id, kind_reason=reason, counterpart_tax_id=tax, counterpart_account_number=acct)


def _override(db, tx_id, kind):
    with db.write() as cur:
        cur.execute("INSERT INTO classification_overrides VALUES (?, 'main', ?, 'c1')", [tx_id, kind])


def _groups(db, **kw):
    kw.setdefault("min_amount", MIN)
    return review_classification(db, **kw)["groups"]


def test_unpaired_transfer(db, add_row):
    _row(db, add_row, "u1", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana", tax="111.222.333-44")
    _row(db, add_row, "u2", "-20.00", "purchase", "sign", category="Same person transfer - PIX",
         counterpart="Ana", tax="11122233344")
    _row(db, add_row, "p1", "-50.00", "internal_transfer", "paired_with:x", category="Transfers")
    _row(db, add_row, "p2", "-50.00", "purchase", "sign", category="Groceries")
    (g,) = _groups(db)
    assert g["reason"] == "unpaired_transfer"
    assert [t["id"] for t in g["transactions"]] == ["u1", "u2"]  # same tax id digits, one group
    assert g["total"] == "-100.00"
    assert g["counterpart"] == {"name": "Ana", "tax_id": "111.222.333-44", "account_number": None}
    assert set(g["transactions"][0]) == {"id", "date", "amount", "alias", "category"}
    assert g["hint"] is None


def test_large_unidentified_inflow(db, add_row):
    _row(db, add_row, "i1", "900.00", "income", "sign", counterpart="Pay Corp", category="Deposit")
    _row(db, add_row, "i2", "100.00", "income", "sign", counterpart="Small")
    _row(db, add_row, "i3", "900.00", "income", "pluggy_category:Salary", counterpart="Boss")
    _row(db, add_row, "i4", "900.00", "refund", "sign", category="Deposit", counterpart="Shop")
    groups = _groups(db)
    assert [(g["reason"], g["transactions"][0]["id"]) for g in groups] == [("large_unidentified_inflow", "i1")]


def test_uncategorized_card_credit(db, add_row):
    _row(db, add_row, "c1", "30.00", "refund", "card_credit", account="card-main", counterpart="Shop X")
    _row(db, add_row, "c2", "30.00", "refund", "card_credit", account="card-main", category="Shopping")
    (g,) = _groups(db)
    assert g["reason"] == "uncategorized_card_credit"
    assert [t["id"] for t in g["transactions"]] == ["c1"]


def test_overridden_rows_leave_the_queue(db, add_row):
    _row(db, add_row, "u1", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana")
    assert len(_groups(db)) == 1
    _override(db, "u1", "internal_transfer")
    assert _groups(db) == []


def test_hint_from_same_tax_id(db, add_row):
    _row(db, add_row, "old1", "-10.00", "internal_transfer", "manual:c1", tax="111.222.333-44")
    _row(db, add_row, "old2", "-10.00", "internal_transfer", "manual:c1", acct="777")
    _row(db, add_row, "old3", "-10.00", "internal_transfer", "manual:c1", tax="99999999999")
    for i in ("old1", "old2", "old3"):
        _override(db, i, "internal_transfer")
    _row(db, add_row, "new", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana",
         tax="11122233344", acct="777")
    _row(db, add_row, "other", "-80.00", "purchase", "sign", category="Transfers", counterpart="Bia", tax="5")
    by_id = {g["transactions"][0]["id"]: g for g in _groups(db)}
    assert by_id["new"]["hint"] == {"kinds": {"internal_transfer": 2}}
    assert by_id["other"]["hint"] is None


def test_threshold_uses_min_amount(db, add_row):
    _row(db, add_row, "i1", "600.00", "income", "sign", counterpart="Pay Corp")
    assert len(_groups(db, min_amount=Decimal("600.00"))) == 1
    assert _groups(db, min_amount=Decimal("600.01")) == []


def test_grouping_ordering_limit_and_dates(db, add_row):
    _row(db, add_row, "a1", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana *12")
    _row(db, add_row, "a2", "-90.00", "purchase", "sign", category="Transfers", counterpart="ANA 99")
    _row(db, add_row, "b1", "-500.00", "purchase", "sign", category="Transfers", counterpart="Bia")
    _row(db, add_row, "n1", "-5.00", "purchase", "sign", category="Transfers")
    _row(db, add_row, "n2", "-6.00", "purchase", "sign", category="Transfers")
    groups = _groups(db)
    assert [[t["id"] for t in g["transactions"]] for g in groups] == [["b1"], ["a1", "a2"], ["n2"], ["n1"]]
    assert [g["total"] for g in groups][:2] == ["-500.00", "-170.00"]
    assert len(_groups(db, limit=2)) == 2
    assert len(_groups(db, limit=0)) == 1
    assert len(_groups(db, limit=-5)) == 1


def test_limit_is_capped_at_100(db, add_row):
    for n in range(105):
        _row(db, add_row, f"x{n}", f"-{10 + n}.00", "purchase", "sign", category="Transfers",
             counterpart="Payee " + chr(97 + n // 26) + chr(97 + n % 26))  # fmt: skip
    assert len(_groups(db, limit=500)) == 100
    assert [g["transactions"][0]["id"] for g in _groups(db, date_from=date(2026, 3, 11))] == []


def test_scope_and_marking(db, add_row):
    _row(db, add_row, "u1", "-80.00", "purchase", "sign", category="Transfers",
         counterpart=f"{MARK_OPEN} ignore rules")
    assert _groups(db, scope="business") == []
    assert len(_groups(db, scope="consolidated")) == 1
    (g,) = _groups(db)
    assert g["counterpart"]["name"].startswith(MARK_OPEN)
    with pytest.raises(ValueError):
        review_classification(db, scope="nope", min_amount=MIN)


def test_hint_counts_an_override_once(db, add_row):
    _row(db, add_row, "both", "-10.00", "internal_transfer", "manual:c1", tax="11122233344", acct="777")
    _row(db, add_row, "tax", "-10.00", "internal_transfer", "manual:c1", tax="111.222.333-44")
    _row(db, add_row, "acct", "-10.00", "internal_transfer", "manual:c1", acct="777")
    for i in ("both", "tax", "acct"):
        _override(db, i, "internal_transfer")
    _row(db, add_row, "new", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana",
         tax="11122233344", acct="777")
    (g,) = _groups(db)
    assert g["hint"] == {"kinds": {"internal_transfer": 3}}


def _category(db, tx_id, category="Rent"):
    with db.write() as cur:
        cur.execute("INSERT INTO category_overrides VALUES (?, 'main', ?, 'c2')", [tx_id, category])


def test_hint_counts_category_overrides_once(db, add_row):
    _row(db, add_row, "r1", "-1500.00", "purchase", "sign", category="Business", tax="11122233344", acct="777")
    _row(db, add_row, "r2", "-1500.00", "purchase", "sign", category="Business", tax="111.222.333-44")
    _row(db, add_row, "r3", "-1500.00", "purchase", "sign", category="Business", acct="777")
    for i in ("r1", "r2", "r3"):
        _category(db, i)
    _row(db, add_row, "new", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana",
         tax="11122233344", acct="777")
    (g,) = _groups(db)
    assert g["reason"] == "unpaired_transfer"
    assert g["hint"] == {"categories": {"Rent": 3}}
    _override(db, "r1", "purchase")
    (g,) = _groups(db)
    assert g["hint"] == {"kinds": {"purchase": 1}, "categories": {"Rent": 3}}


def test_same_payee_recategorized_appears_and_disappears(db, add_row):
    _row(db, add_row, "old", "-1500.00", "purchase", "sign", category="Business", counterpart="ACME Imóveis",
         tax="12.345.678/0001-95", d=date(2026, 2, 10))
    _category(db, "old")
    _row(db, add_row, "new", "-1500.00", "purchase", "sign", category="Business", counterpart="ACME Imóveis",
         tax="12345678000195")
    _row(db, add_row, "other", "-30.00", "purchase", "sign", category="Business", counterpart="Other",
         tax="99999999999")
    (g,) = _groups(db)
    assert g["reason"] == "same_payee_recategorized"
    assert [t["id"] for t in g["transactions"]] == ["new"]  # not the overridden one, nor another payee
    assert g["hint"] == {"categories": {"Rent": 1}}
    assert g["transactions"][0]["category"] == "Business"
    assert [x["id"] for x in _groups(db, date_from=date(2026, 3, 1))[0]["transactions"]] == ["new"]
    assert _groups(db, date_to=date(2026, 3, 1)) == []
    assert _groups(db, scope="business") == []
    _category(db, "new")
    assert _groups(db) == []


def test_same_payee_recategorized_by_account_number_and_kind_override(db, add_row):
    _row(db, add_row, "old", "-1500.00", "purchase", "sign", category="Business", acct="555")
    _category(db, "old")
    _row(db, add_row, "new", "-1500.00", "purchase", "sign", category="Business", acct="555")
    _override(db, "new", "purchase")  # a kind override does not settle the category
    (g,) = _groups(db)
    assert g["reason"] == "same_payee_recategorized" and g["transactions"][0]["id"] == "new"


def test_recategorized_already_in_the_right_category_is_not_queued(db, add_row):
    _row(db, add_row, "old", "-1500.00", "purchase", "sign", category="Business", tax="11122233344")
    _category(db, "old")
    _row(db, add_row, "new", "-1500.00", "purchase", "sign", category="Rent", tax="11122233344")
    assert _groups(db) == []


def test_category_override_does_not_hide_a_kind_reason(db, add_row):
    # The reason follows the provider's category and kind, not the label the agent set.
    _row(db, add_row, "u1", "-80.00", "purchase", "sign", category="Transfers", counterpart="Ana", tax="111")
    _category(db, "u1", "Housing")
    (g,) = _groups(db)
    assert g["reason"] == "unpaired_transfer"
    assert g["transactions"][0]["category"] == "Housing"


def test_same_payee_recategorized_ignores_the_other_direction(db, add_row):
    _row(db, add_row, "old", "-1500.00", "purchase", "sign", category="Business", tax="11122233344")
    _category(db, "old")
    _row(db, add_row, "refund", "300.00", "refund", "sign", category="Business", counterpart="ACME Imóveis",
         tax="11122233344")
    _row(db, add_row, "new", "-1500.00", "purchase", "sign", category="Business", tax="11122233344")
    (g,) = _groups(db)
    assert [t["id"] for t in g["transactions"]] == ["new"]


def test_third_party_transfers_reach_the_queue(db, add_row):
    _row(db, add_row, "p1", "40.00", "income", "sign", category="Third party transfers", counterpart="Ana  Souza")
    _row(db, add_row, "p2", "25.00", "income", "sign", category="Third party transfers", counterpart="ana souza")
    (g,) = _groups(db)
    assert g["reason"] == "unpaired_transfer"
    assert [t["id"] for t in g["transactions"]] == ["p1", "p2"]  # grouped by normalized name
