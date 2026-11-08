from decimal import Decimal

import pytest

from cashbook_mcp.store.db import Database

KEY = "ab" * 32

ACCOUNTS = [
    # id, alias, type, owner_tax_id
    ("pa", "pers", "BANK", "111.444.777-35"),
    ("pc", "pers", "CREDIT", "11144477735"),
    ("ba", "biz", "BANK", "12.345.678/0001-95"),
    ("bc", "biz", "CREDIT", "12345678000195"),
    ("sa", "shared", "BANK", "99999999999"),
    ("sb", "shared_biz", "BANK", "99999999999"),
]


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "c.duckdb", KEY)
    with database.write() as cur:
        for alias, role in [("pers", "personal"), ("biz", "business"),
                            ("shared", "personal"), ("shared_biz", "business")]:
            cur.execute(
                "INSERT INTO connections (alias, item_id, connector_name, created_at, role) "
                "VALUES (?, ?, 'Bank', now(), ?)",
                [alias, f"item-{alias}", role],
            )
        for aid, alias, typ, tax in ACCOUNTS:
            cur.execute(
                "INSERT INTO accounts (id, alias, item_id, type, subtype, name, owner_tax_id, "
                "balance, currency, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, 'X', 'Acc', ?, 0, 'BRL', now(), now())",
                [aid, alias, f"item-{alias}", typ, tax],
            )
    yield database
    database.close()


def add(db, tid, account, amount, kind, reason="", currency="BRL", tax=None):
    alias = next(a[1] for a in ACCOUNTS if a[0] == account)
    with db.write() as cur:
        cur.execute(
            "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
            "description, counterpart_tax_id, kind, kind_reason, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, DATE '2026-03-10', ?, ?, 'x', ?, ?, ?, now(), now())",
            [tid, alias, account, amount, currency, tax, kind, reason],
        )


def flows(db):
    cur = db.read()
    try:
        return {
            r[0]: r[1:]
            for r in cur.execute(
                "SELECT id, flow, spend_amount, income_amount, peer_role FROM v_flows"
            ).fetchall()
        }
    finally:
        cur.close()


D = Decimal

CASES = [
    # id, account, amount, kind, reason, currency, expected (flow, spend, income, peer_role)
    ("inc", "pa", "100.00", "income", "sign", "BRL", ("income", D(0), D("100.00"), None)),
    ("pur", "pa", "-40.00", "purchase", "sign", "BRL", ("spending", D("40.00"), D(0), None)),
    ("fee", "pa", "-3.00", "fee", "pluggy_category:x", "BRL", ("spending", D("3.00"), D(0), None)),
    ("loan", "pa", "-8.00", "loan", "pluggy_category:x", "BRL", ("spending", D("8.00"), D(0), None)),
    ("oth", "pa", "-1.00", "other", "sign", "BRL", ("spending", D("1.00"), D(0), None)),
    ("ref", "pc", "15.00", "refund", "card_credit", "BRL", ("refund", D("-15.00"), D(0), None)),
    ("inv", "pa", "-50.00", "investment", "investment_account", "BRL",
     ("investment", D(0), D(0), None)),
    ("cbp", "pa", "-70.00", "card_bill_payment", "matched_card_bill:b1", "BRL",
     ("card_payment", D(0), D(0), None)),
    ("int_same", "pa", "-5.00", "internal_transfer", "paired_with:int_same_peer", "BRL",
     ("internal", D(0), D(0), "personal")),
    ("int_same_peer", "sa", "5.00", "internal_transfer", "paired_with:int_same", "BRL",
     ("internal", D(0), D(0), "personal")),
    ("int_unknown", "pa", "-6.00", "internal_transfer", "manual:1", "BRL",
     ("internal", D(0), D(0), None)),
    # personal account pays the business card: to_business, business side from_personal
    ("pay_biz_card", "pa", "-90.00", "card_bill_payment", "counterpart_is_own_account:biz", "BRL",
     ("to_business", D(0), D(0), "business")),
    ("biz_card_recv", "bc", "90.00", "card_bill_payment", "paired_with:pay_biz_card", "BRL",
     ("from_personal", D(0), D(0), "personal")),
    ("from_biz", "pa", "200.00", "internal_transfer", "counterpart_is_owner_tax_id", "BRL",
     ("from_business", D(0), D(0), "business")),
    ("to_pers", "ba", "-200.00", "internal_transfer", "paired_with:from_biz", "BRL",
     ("to_personal", D(0), D(0), "personal")),
    ("in_pur", "pa", "4.00", "purchase", "sign", "BRL", ("refund", D("-4.00"), D(0), None)),
    ("in_fee", "pa", "2.00", "fee", "sign", "BRL", ("refund", D("-2.00"), D(0), None)),
    ("in_oth", "pa", "1.00", "other", "sign", "BRL", ("refund", D("-1.00"), D(0), None)),
    ("in_loan", "pa", "500.00", "loan", "sign", "BRL", ("internal", D(0), D(0), None)),
    ("out_inc", "pa", "-9.00", "income", "manual:1", "BRL", ("spending", D("9.00"), D(0), None)),
    ("out_ref", "pc", "-7.00", "refund", "manual:1", "BRL", ("spending", D("7.00"), D(0), None)),
    ("oo_out", "pa", "-900.00", "one_off", "manual:1", "BRL", ("one_off", D(0), D(0), None)),
    ("oo_in", "pa", "300.00", "one_off", "manual:1", "BRL", ("one_off", D(0), D(0), None)),
    ("oo_foreign", "pa", "-30.00", "one_off", "manual:1", "USD", ("foreign", D(0), D(0), None)),
    ("foreign_out", "pa", "-30.00", "purchase", "sign", "USD", ("foreign", D(0), D(0), None)),
    ("foreign_xfer", "pa", "-30.00", "internal_transfer", "counterpart_is_own_account:biz",
     "USD", ("foreign", D(0), D(0), "business")),
]


def test_flow_values(db):
    # "from_biz" pairs by tax id (the CNPJ), so it needs the counterpart tax id.
    for tid, account, amount, kind, reason, currency, _ in CASES:
        add(db, tid, account, amount, kind, reason, currency,
            tax="12.345.678/0001-95" if tid == "from_biz" else None)
    got = flows(db)
    for tid, *_, expected in CASES:
        assert got[tid] == expected, tid
    assert set(got) == {c[0] for c in CASES}
    cur = db.read()
    assert cur.execute("SELECT count(*) FROM v_flows WHERE flow IS NULL").fetchone() == (0,)


def test_peer_role_by_reason(db):
    add(db, "x1", "pa", "-1.00", "internal_transfer", "paired_with:x2")
    add(db, "x2", "ba", "1.00", "internal_transfer", "paired_with:x1")
    add(db, "o1", "pa", "-1.00", "internal_transfer", "counterpart_is_own_account:biz")
    add(db, "t1", "pa", "-1.00", "internal_transfer", "counterpart_is_owner_tax_id",
        tax="12345678000195")
    add(db, "m1", "pa", "-1.00", "internal_transfer", "manual:3")
    cur = db.read()
    got = {
        r[0]: r[1:]
        for r in cur.execute("SELECT id, peer_role, peer_alias FROM v_flows").fetchall()
    }
    assert got["x1"] == ("business", "biz")
    assert got["x2"] == ("personal", "pers")
    assert got["o1"] == ("business", "biz")
    assert got["t1"] == ("business", "biz")
    assert got["m1"] == (None, None)


@pytest.mark.parametrize(
    ("digits", "tax", "role"),
    [
        ("12345678000195", "12.345.678/0001-95", "business"),
        ("99999999999", "999.999.999-99", "personal"),
    ],
)
def test_tax_id_matching_both_roles_prefers_by_length(db, digits, tax, role):
    # The same digits on a personal and a business bank account: a CNPJ
    # (14 digits) prefers business, a CPF (11 digits) personal.
    with db.write() as cur:
        cur.execute("UPDATE accounts SET owner_tax_id = ? WHERE id IN ('sa', 'sb')", [digits])
    add(db, "t", "pa", "-1.00", "internal_transfer", "counterpart_is_owner_tax_id", tax=tax)
    assert flows(db)["t"][3] == role


def test_tax_id_ignores_card_accounts(db):
    with db.write() as cur:
        cur.execute("UPDATE accounts SET owner_tax_id = '55555555555' WHERE id = 'bc'")
    add(db, "t", "pa", "-1.00", "internal_transfer", "counterpart_is_owner_tax_id",
        tax="55555555555")
    assert flows(db)["t"][3] is None


def add_bill(db, bid, account):
    with db.write() as cur:
        cur.execute(
            "INSERT INTO bills (id, alias, account_id, due_date, total_amount, currency, "
            "first_seen_at, last_seen_at) VALUES (?, 'x', ?, DATE '2026-03-15', 1, 'BRL', "
            "now(), now())",
            [bid, account],
        )


def test_matched_card_bill_peer(db):
    add_bill(db, "b1", "bc")
    add_bill(db, "b2", "pc")
    add(db, "bank1", "pa", "-90.00", "card_bill_payment", "matched_card_bill:b1")
    add(db, "card1", "bc", "90.00", "card_bill_payment", "matched_card_bill:b1")
    add(db, "bank2", "pa", "-20.00", "card_bill_payment", "matched_card_bill:b2")
    add(db, "card2", "pc", "20.00", "card_bill_payment", "matched_card_bill:b2")
    add_bill(db, "b3", "pc")
    add(db, "card3", "pc", "5.00", "card_bill_payment", "matched_card_bill:b3")
    got = flows(db)
    assert got["bank1"] == ("to_business", D(0), D(0), "business")
    assert got["card1"] == ("from_personal", D(0), D(0), "personal")
    assert got["bank2"] == ("card_payment", D(0), D(0), "personal")
    assert got["card2"] == ("card_payment", D(0), D(0), "personal")
    assert got["card3"] == ("card_payment", D(0), D(0), None)


def test_removed_rows_are_not_in_v_flows(db):
    add(db, "gone", "pa", "-1.00", "purchase")
    with db.write() as cur:
        cur.execute("UPDATE transactions SET removed_at = now() WHERE id = 'gone'")
    assert flows(db) == {}

