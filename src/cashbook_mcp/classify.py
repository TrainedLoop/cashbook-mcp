"""Rule-based transaction kind classification. Pure; first matching rule wins."""

import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from cashbook_mcp.models import Account, Bill, Kind, Transaction

# Keys are lowercase Pluggy category descriptions (English). Matching is
# case-insensitive on the exact string.
CATEGORY_KINDS: dict[str, Kind] = {
    "investments": "investment",
    "fixed income": "investment",
    "variable income": "investment",
    "taxes": "fee",
    "bank fees": "fee",
    "account fees": "fee",
    "credit card fees": "fee",
    "interests charged": "fee",
    "loans": "loan",
    "financing": "loan",
    "loans and financing": "loan",
    "credit card payment": "card_bill_payment",
}

_TOLERANCE = Decimal("0.01")
_BEFORE_DUE = timedelta(days=10)
_AFTER_DUE = timedelta(days=5)


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _category_kind(category: str | None) -> Kind | None:
    return CATEGORY_KINDS.get(category.strip().lower()) if category else None


def classify(
    transactions: list[Transaction], accounts: list[Account], bills: list[Bill]
) -> list[Transaction]:
    by_id = {a.id: a for a in accounts}
    tax_ids = {d for a in accounts if (d := _digits(a.owner_tax_id))}
    numbers = {a.number: a.alias for a in accounts if a.number is not None}
    return [_classify_one(t, by_id, tax_ids, numbers, bills) for t in transactions]


def _classify_one(
    t: Transaction,
    by_id: dict[str, Account],
    tax_ids: set[str],
    numbers: dict[str, str],
    bills: list[Bill],
) -> Transaction:
    account = by_id.get(t.account_id)
    kind = _category_kind(t.pluggy_category)

    if account is not None and account.type == "BANK" and t.amount < 0:
        for b in bills:
            if (
                abs(abs(t.amount) - b.total_amount) <= _TOLERANCE
                and b.due_date - _BEFORE_DUE <= t.date <= b.due_date + _AFTER_DUE
            ):
                return replace(
                    t, kind="card_bill_payment", kind_reason=f"matched_card_bill:{b.id}"
                )

    credit_inflow = account is not None and account.type == "CREDIT" and t.amount > 0
    if credit_inflow and kind == "card_bill_payment":
        return replace(t, kind="card_bill_payment", kind_reason="card_payment_received")

    if t.counterpart_account_number in numbers:
        alias = numbers[t.counterpart_account_number]
        return replace(
            t, kind="internal_transfer", kind_reason=f"counterpart_is_own_account:{alias}"
        )
    if (tid := _digits(t.counterpart_tax_id)) and tid in tax_ids:
        return replace(
            t, kind="internal_transfer", kind_reason="counterpart_is_owner_tax_id"
        )

    if credit_inflow:
        return replace(t, kind="refund", kind_reason="card_credit")

    if kind is not None:
        return replace(t, kind=kind, kind_reason=f"pluggy_category:{t.pluggy_category}")

    if t.amount > 0:
        return replace(t, kind="income", kind_reason="sign")
    return replace(t, kind="purchase", kind_reason="sign")
