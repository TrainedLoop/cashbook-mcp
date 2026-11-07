"""Rule-based transaction kind classification. Pure.

Each transaction is classified on its own (first matching rule wins), then a
second pass pairs transfers between the owner's accounts whose sides were
classified apart (one bank names the owner, the other names nobody).
"""

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from cashbook_mcp.models import Account, Bill, Kind, Purpose, Transaction

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
    "automatic investment": "investment",
    "income taxes": "fee",
    # Yields stay income even on investment accounts.
    "proceeds interests and dividends": "income",
    "tax on financial operations": "fee",
    "same person transfer": "internal_transfer",
    # Money held and later released inside the same account.
    "blocked balances": "internal_transfer",
}

# Kinds set only by a fallback (the sign, or any credit on a card): pairing may
# replace them. Everything else (bill matches, owner matches, category kinds)
# is firmer and kept.
_GUESSED_KINDS = frozenset({"income", "purchase", "refund"})
_PAIR_WINDOW = timedelta(days=3)
# Manual kinds that may be one side of a pair; any other manual kind rules the pair out.
_MANUAL_PAIR_KINDS = frozenset({"internal_transfer", "card_bill_payment", "investment"})

_TOLERANCE = Decimal("0.01")
_BEFORE_DUE = timedelta(days=10)
_AFTER_DUE = timedelta(days=5)


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _category_kind(category: str | None) -> Kind | None:
    return CATEGORY_KINDS.get(category.strip().lower()) if category else None


def classify(
    transactions: list[Transaction],
    accounts: list[Account],
    bills: list[Bill],
    *,
    purposes: Mapping[str, Purpose] | None = None,
    overrides: Mapping[str, tuple[Kind, str]] | None = None,
) -> list[Transaction]:
    """Per-transaction rules, then manual overrides (transaction id -> (kind,
    change id)), then own-transfer pairing. purposes maps account id to its purpose."""
    by_id = {a.id: a for a in accounts}
    tax_ids = {d for a in accounts if (d := _digits(a.owner_tax_id))}
    numbers = {a.number: a.alias for a in accounts if a.number is not None}
    investment = {a for a, p in (purposes or {}).items() if p == "investment"}
    classified = [
        _classify_one(t, by_id, tax_ids, numbers, bills, investment) for t in transactions
    ]
    if overrides:
        classified = [
            replace(t, kind=o[0], kind_reason=f"manual:{o[1]}") if (o := overrides.get(t.id)) else t
            for t in classified
        ]
    return _pair_own_transfers(classified, by_id)


def _is_transfer_category(category: str | None) -> bool:
    c = (category or "").strip().lower()
    return c.startswith(("transfer", "same person transfer"))


def _guessed(t: Transaction) -> bool:
    """A fallback kind that pairing may replace; manual kinds never are."""
    if t.kind_reason == "investment_account":
        return True  # the investment-account fallback is a sign guess like any other
    return t.kind in _GUESSED_KINDS and not (t.kind_reason or "").startswith("manual:")


def _manual_non_transfer(t: Transaction) -> bool:
    """The owner set a kind that is not a movement between own accounts: never a pair side."""
    return (t.kind_reason or "").startswith("manual:") and t.kind not in _MANUAL_PAIR_KINDS


def _pair_kind(out: Transaction, inn: Transaction, by_id: dict[str, Account]) -> Kind | None:
    """The kind both sides of a candidate pair take, or None if they must not pair."""
    if _manual_non_transfer(out) or _manual_non_transfer(inn):
        return None
    out_type, in_type = by_id[out.account_id].type, by_id[inn.account_id].type
    if not _guessed(out) and not _guessed(inn):
        return None  # nothing to fix
    if out_type == "BANK" and in_type == "CREDIT":
        return "card_bill_payment"  # an exact credit on the card is the payment
    if out_type == "BANK" and in_type == "BANK":
        # Two accounts can share an amount and a day by chance: one side must
        # already look like a transfer.
        evidence = (
            "internal_transfer" in (out.kind, inn.kind)
            or _is_transfer_category(out.pluggy_category)
            or _is_transfer_category(inn.pluggy_category)
        )
        return "internal_transfer" if evidence else None
    return None  # card outflows are purchases, never the sending side


def _pair_own_transfers(
    transactions: list[Transaction], by_id: dict[str, Account]
) -> list[Transaction]:
    """Pair an outflow of one own account with the same inflow in another, within
    _PAIR_WINDOW, closest dates first; each transaction pairs at most once. Only
    guessed sides are rewritten, with kind_reason paired_with:<other id>."""
    own = [t for t in transactions if t.account_id in by_id]
    outs = [t for t in own if t.amount < 0]
    ins_by_key: dict[tuple, list[Transaction]] = {}
    for t in own:
        if t.amount > 0:
            ins_by_key.setdefault((t.amount, t.currency), []).append(t)
    candidates = []
    for o in outs:
        for i in ins_by_key.get((-o.amount, o.currency), ()):
            gap = abs(i.date - o.date)
            if i.account_id == o.account_id or gap > _PAIR_WINDOW:
                continue
            if (kind := _pair_kind(o, i, by_id)) is not None:
                candidates.append((gap, o.date, o.id, i.id, kind, o, i))
    candidates.sort(key=lambda c: c[:4])
    taken: set[str] = set()
    rewritten: dict[str, Transaction] = {}
    for _, _, oid, iid, kind, o, i in candidates:
        if oid in taken or iid in taken:
            continue
        taken.update((oid, iid))
        for side, other in ((o, iid), (i, oid)):
            if _guessed(side):
                rewritten[side.id] = replace(side, kind=kind, kind_reason=f"paired_with:{other}")
    return [rewritten.get(t.id, t) for t in transactions]


def _classify_one(
    t: Transaction,
    by_id: dict[str, Account],
    tax_ids: set[str],
    numbers: dict[str, str],
    bills: list[Bill],
    investment: set[str],
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
    if credit_inflow:
        # The card side of a bill payment often carries no Pluggy category;
        # match it against this card's own bills so it is not taken as income.
        for b in bills:
            start = b.closing_date or (b.due_date - _BEFORE_DUE)
            if (
                b.account_id == t.account_id
                and abs(t.amount - b.total_amount) <= _TOLERANCE
                and start <= t.date <= b.due_date + _AFTER_DUE
            ):
                return replace(
                    t, kind="card_bill_payment", kind_reason=f"matched_card_bill:{b.id}"
                )

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

    if t.account_id in investment:
        return replace(t, kind="investment", kind_reason="investment_account")

    if t.amount > 0:
        return replace(t, kind="income", kind_reason="sign")
    return replace(t, kind="purchase", kind_reason="sign")
