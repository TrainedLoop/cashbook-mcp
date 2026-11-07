from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from cashbook_mcp.models import KINDS, Transaction


def _tx() -> Transaction:
    return Transaction(
        id="t1",
        alias="main",
        account_id="a1",
        date=date(2026, 1, 2),
        amount=Decimal("-10.50"),
        currency="BRL",
        original_amount=None,
        original_currency=None,
        description="PIX",
        counterpart_name=None,
        counterpart_tax_id=None,
        counterpart_account_number=None,
        merchant_name=None,
        pluggy_category=None,
        bill_id=None,
    )


def test_transaction_defaults_and_immutability():
    t = _tx()
    assert t.kind == "other"
    assert t.kind_reason == ""
    assert t.text_suspicious is False
    assert t.suspicion_reasons == ()
    with pytest.raises(FrozenInstanceError):
        t.amount = Decimal(1)


def test_kinds_order():
    assert KINDS[0] == "purchase"
    assert len(KINDS) == 10
