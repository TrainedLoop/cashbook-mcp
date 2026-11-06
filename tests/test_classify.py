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
