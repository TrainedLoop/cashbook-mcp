from dataclasses import replace
from datetime import date
from decimal import Decimal

from cashbook_mcp.classify import CATEGORY_KINDS, classify
from cashbook_mcp.models import Account, Bill, Transaction

OWN_TAX = "123.456.789-09"


def acct(id="a1", alias="main", type="BANK", number="0001-1", tax=OWN_TAX):
    return Account(id, alias, "item", type, "CHECKING", "Acc", number, tax,
                   Decimal(0), "BRL", None, None)


def tx(id="t1", amount="-10.00", d=date(2026, 3, 10), account_id="a1", **kw):
    base = {
        "id": id, "alias": "main", "account_id": account_id, "date": d,
        "amount": Decimal(amount), "currency": "BRL", "original_amount": None,
        "original_currency": None, "description": "x", "counterpart_name": None,
        "counterpart_tax_id": None, "counterpart_account_number": None,
        "merchant_name": None, "pluggy_category": None, "bill_id": None,
    }
    base.update(kw)
    return Transaction(**base)


def bill(id="b1", due=date(2026, 3, 12), total="1234.56"):
    return Bill(id, "main", "c1", due, None, Decimal(total), None, "BRL")


ACCOUNTS = [acct(), acct("c1", "card", "CREDIT", number=None, tax=OWN_TAX)]


def one(t, bills=()):
    return classify([t], ACCOUNTS, list(bills))[0]


def test_bill_payment_matched():
    r = one(tx(amount="-1234.56", d=date(2026, 3, 10)), [bill()])
    assert (r.kind, r.kind_reason) == ("card_bill_payment", "matched_card_bill:b1")


def test_bill_payment_too_far_off():
    r = one(tx(amount="-1234.56", d=date(2026, 2, 20)), [bill()])
    assert r.kind == "purchase"


def test_bill_payment_window_edges():
    assert one(tx(amount="-1234.56", d=date(2026, 3, 2)), [bill()]).kind == "card_bill_payment"
    assert one(tx(amount="-1234.56", d=date(2026, 3, 17)), [bill()]).kind == "card_bill_payment"
    assert one(tx(amount="-1234.56", d=date(2026, 3, 1)), [bill()]).kind == "purchase"
    assert one(tx(amount="-1234.56", d=date(2026, 3, 18)), [bill()]).kind == "purchase"


def test_bill_payment_requires_bank_outflow():
    t = tx(amount="-1234.56", d=date(2026, 3, 10), account_id="c1")
    assert one(t, [bill()]).kind == "purchase"


def test_card_side_payment_received():
    t = tx(amount="500.00", account_id="c1", pluggy_category="Credit card payment")
    r = one(t)
    assert (r.kind, r.kind_reason) == ("card_bill_payment", "card_payment_received")


def test_internal_transfer_by_tax_id():
    r = one(tx(counterpart_tax_id="12345678909"))
    assert (r.kind, r.kind_reason) == ("internal_transfer", "counterpart_is_owner_tax_id")


def test_other_tax_id_is_purchase():
    assert one(tx(counterpart_tax_id="999.999.999-99")).kind == "purchase"


def test_internal_transfer_by_account_number():
    r = one(tx(counterpart_account_number="0001-1"))
    assert (r.kind, r.kind_reason) == ("internal_transfer", "counterpart_is_own_account:main")


def test_refund_on_credit_inflow():
    t = tx(amount="20.00", account_id="c1")
    r = one(t)
    assert (r.kind, r.kind_reason) == ("refund", "card_credit")


def test_bank_inflow_not_refund():
    assert one(tx(amount="20.00")).kind == "income"


def test_category_map():
    r = one(tx(pluggy_category="Bank FEES"))
    assert (r.kind, r.kind_reason) == ("fee", "pluggy_category:Bank FEES")
    assert one(tx(pluggy_category="Loans")).kind == "loan"
    assert one(tx(pluggy_category="Fixed income")).kind == "investment"
    assert "credit card payment" in CATEGORY_KINDS


def test_sign_fallback():
    r = one(tx(amount="5.00"))
    assert (r.kind, r.kind_reason) == ("income", "sign")
    assert one(tx(amount="-5.00")).kind == "purchase"


def test_rule_order_bill_beats_transfer():
    t = tx(amount="-1234.56", d=date(2026, 3, 10), counterpart_tax_id=OWN_TAX)
    assert one(t, [bill()]).kind == "card_bill_payment"


def test_pure_and_ordered():
    a, b = tx("t1", "-1.00"), tx("t2", "2.00")
    out = classify([a, b], ACCOUNTS, [])
    assert [t.id for t in out] == ["t1", "t2"]
    assert a == replace(a) and a.kind == "other"


def card_bill(id="cb1", due=date(2026, 3, 12), total="500.00", closing=None, account_id="c1"):
    return Bill(id, "main", account_id, due, closing, Decimal(total), None, "BRL")


def test_uncategorized_card_payment_matches_bill():
    t = tx(amount="500.00", account_id="c1", d=date(2026, 3, 10))
    r = one(t, [card_bill()])
    assert (r.kind, r.kind_reason) == ("card_bill_payment", "matched_card_bill:cb1")


def test_card_payment_window_uses_closing_date():
    b = card_bill(closing=date(2026, 3, 5))
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 5)), [b]).kind == "card_bill_payment"
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 17)), [b]).kind == "card_bill_payment"
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 4)), [b]).kind == "refund"
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 18)), [b]).kind == "refund"


def test_card_payment_needs_bill_of_same_account():
    other = card_bill(account_id="c2")
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 10)), [other]).kind == "refund"
    near = card_bill(total="500.02")
    assert one(tx(amount="500.00", account_id="c1", d=date(2026, 3, 10)), [near]).kind == "refund"


# -- category map additions ---------------------------------------------------


def test_category_map_covers_pluggy_categories_seen_in_real_data():
    assert one(tx(amount="0.12", pluggy_category="Automatic investment")).kind == "investment"
    assert one(tx(amount="-50.00", pluggy_category="Automatic investment")).kind == "investment"
    assert one(tx(amount="-80.00", pluggy_category="Income taxes")).kind == "fee"
    assert one(tx(amount="-3.10", pluggy_category="Tax on financial operations")).kind == "fee"
    assert one(tx(amount="900.00", pluggy_category="Same person transfer")).kind == "internal_transfer"
    assert one(tx(amount="1557.12", pluggy_category="Blocked balances")).kind == "internal_transfer"
    assert one(tx(amount="-1557.12", pluggy_category="Blocked balances")).kind == "internal_transfer"


# -- pairing across own accounts ------------------------------------------------

BANKS = [
    acct("a1", "main", number="0001-1"),
    acct("a2", "other", number="0002-2"),
    acct("c1", "card", "CREDIT", number=None),
]


def run(*txs, bills=()):
    out = classify(list(txs), BANKS, list(bills))
    return {t.id: (t.kind, t.kind_reason) for t in out}


def test_transfer_with_one_side_recognized_pairs_the_other():
    # The sending bank names the owner; the receiving bank names nobody.
    out = tx("o", "-10000.00", date(2026, 3, 10), "a1", counterpart_tax_id=OWN_TAX,
             pluggy_category="Transfer - PIX")
    inn = tx("i", "10000.00", date(2026, 3, 11), "a2", pluggy_category="Transfer - PIX")
    got = run(out, inn)
    assert got["o"] == ("internal_transfer", "counterpart_is_owner_tax_id")
    assert got["i"] == ("internal_transfer", "paired_with:o")


def test_transfer_with_neither_side_recognized_pairs_on_transfer_category():
    out = tx("o", "-7000.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    inn = tx("i", "7000.00", date(2026, 3, 10), "a2", pluggy_category="Transfer - TED")
    assert run(out, inn) == {
        "o": ("internal_transfer", "paired_with:i"),
        "i": ("internal_transfer", "paired_with:o"),
    }


def test_same_amount_without_transfer_evidence_is_not_paired():
    # A purchase and a payment received that merely share an amount and a day.
    out = tx("o", "-100.00", date(2026, 3, 10), "a1", pluggy_category="Groceries")
    inn = tx("i", "100.00", date(2026, 3, 10), "a2")
    assert run(out, inn) == {"o": ("purchase", "sign"), "i": ("income", "sign")}


def test_bank_payment_into_card_pairs_as_bill_payment():
    out = tx("o", "-154.75", date(2026, 11, 3), "a1", pluggy_category="Transfers")
    inn = tx("i", "154.75", date(2026, 11, 4), "c1")
    assert run(out, inn) == {
        "o": ("card_bill_payment", "paired_with:i"),
        "i": ("card_bill_payment", "paired_with:o"),
    }


def test_card_purchase_never_pairs_with_bank_inflow():
    out = tx("o", "-50.00", date(2026, 3, 10), "c1", pluggy_category="Transfers")
    inn = tx("i", "50.00", date(2026, 3, 10), "a1", pluggy_category="Transfer - PIX")
    assert run(out, inn) == {"o": ("purchase", "sign"), "i": ("income", "sign")}


def test_pairing_window_and_same_account():
    out = tx("o", "-300.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    late = tx("late", "300.00", date(2026, 3, 14), "a2", pluggy_category="Transfers")
    same = tx("same", "300.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    got = run(out, late, same)
    assert got["late"] == ("income", "sign")
    assert got["same"] == ("income", "sign")
    assert got["o"] == ("purchase", "sign")
    edge = tx("edge", "300.00", date(2026, 3, 13), "a2", pluggy_category="Transfers")
    assert run(out, edge)["edge"] == ("internal_transfer", "paired_with:o")


def test_each_transaction_pairs_once_closest_date_first():
    out = tx("o", "-500.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    far = tx("far", "500.00", date(2026, 3, 12), "a2", pluggy_category="Transfers")
    near = tx("near", "500.00", date(2026, 3, 10), "a2", pluggy_category="Transfers")
    got = run(out, far, near)
    assert got["o"] == ("internal_transfer", "paired_with:near")
    assert got["near"] == ("internal_transfer", "paired_with:o")
    assert got["far"] == ("income", "sign")


def test_bill_match_and_category_kinds_are_kept():
    # Sides with a firmer classification keep it; only the guessed side changes.
    out = tx("o", "-1234.56", date(2026, 3, 10), "a1")
    inn = tx("i", "1234.56", date(2026, 3, 10), "c1")
    got = run(out, inn, bills=[bill()])
    assert got["o"] == ("card_bill_payment", "matched_card_bill:b1")
    assert got["i"][0] == "card_bill_payment"
    inv = tx("inv", "-200.00", date(2026, 3, 10), "a1", pluggy_category="Investments")
    back = tx("back", "200.00", date(2026, 3, 10), "a2", pluggy_category="Transfers")
    got = run(inv, back)
    assert got["inv"] == ("investment", "pluggy_category:Investments")
    # Money that left another own account is not income, whatever that side was.
    assert got["back"] == ("internal_transfer", "paired_with:inv")


def test_pairing_requires_same_currency():
    out = tx("o", "-10.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    inn = tx("i", "10.00", date(2026, 3, 10), "a2", pluggy_category="Transfers", currency="USD")
    assert run(out, inn)["i"] == ("income", "sign")


def test_investment_account_fallback():
    accounts = [*ACCOUNTS, acct("i1", "invest", number="0009-9")]
    purposes = {"i1": "investment"}

    def run(t):
        r = classify([t], accounts, [], purposes=purposes)[0]
        return r.kind, r.kind_reason

    assert run(tx(account_id="i1")) == ("investment", "investment_account")
    assert run(tx(amount="10.00", account_id="i1")) == ("investment", "investment_account")
    cat = "Proceeds interests and dividends"
    assert run(tx(amount="5.00", account_id="i1", pluggy_category=cat)) == (
        "income", f"pluggy_category:{cat}")
    assert run(tx(account_id="i1", pluggy_category="Income taxes"))[0] == "fee"


def test_transfer_into_investment_account_still_pairs():
    accounts = [*BANKS, acct("i1", "invest", number="0009-9")]
    out = tx("o", "-3000.00", date(2026, 3, 10), "a1", pluggy_category="Transfers")
    inn = tx("i", "3000.00", date(2026, 3, 10), "i1")
    got = {t.id: (t.kind, t.kind_reason) for t in
           classify([out, inn], accounts, [], purposes={"i1": "investment"})}
    assert got["i"] == ("internal_transfer", "paired_with:o")
    # A firm category kind on the investment account is not a guess.
    firm = tx("f", "3000.00", date(2026, 3, 10), "i1", pluggy_category="Fixed income")
    got = classify([out, firm], accounts, [], purposes={"i1": "investment"})
    assert got[1].kind == "investment" and got[1].kind_reason.startswith("pluggy_category")


def test_override_wins_and_is_reasoned():
    r = classify([tx()], ACCOUNTS, [], overrides={"t1": ("internal_transfer", "c1")})[0]
    assert (r.kind, r.kind_reason) == ("internal_transfer", "manual:c1")


def test_pairing_never_rewrites_manual_side_but_uses_it():
    accounts = [acct(), acct("a2", "other", number="0002-2")]
    inn = tx("in", amount="50.00", account_id="a2")
    out = tx("out", amount="-50.00", account_id="a1")
    res = classify([out, inn], accounts, [], overrides={"in": ("internal_transfer", "c1")})
    by = {t.id: (t.kind, t.kind_reason) for t in res}
    assert by["in"] == ("internal_transfer", "manual:c1")
    assert by["out"] == ("internal_transfer", "paired_with:in")


def test_override_for_missing_transaction_is_ignored():
    r = classify([tx()], ACCOUNTS, [], overrides={"gone": ("income", "c1")})[0]
    assert (r.kind, r.kind_reason) == ("purchase", "sign")
