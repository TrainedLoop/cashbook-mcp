"""Normalize raw Pluggy JSON into domain models."""

from datetime import date
from decimal import Decimal

from cashbook_mcp.content import apply_text_policy
from cashbook_mcp.models import (
    Account,
    Bill,
    Investment,
    InvestmentTransaction,
    Loan,
    Snapshot,
    Transaction,
)

# Pluggy credit card amounts are positive for purchases; the domain model wants
# money out to be negative. Verified against real data in Task 17.
CARD_AMOUNT_SIGN: int = -1


def _dec(v: object) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v))


def _req_dec(v: object) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def _date(v: str | None) -> date | None:
    """Date part of an ISO string, with no timezone conversion."""
    if not v:
        return None
    return date.fromisoformat(v[:10])


def _doc(v: object) -> str | None:
    """Document number as a plain string or as {"type": ..., "value": ...}."""
    if isinstance(v, dict):
        v = v.get("value")
    return str(v) if v else None


def _account(alias: str, a: dict) -> Account:
    credit = a.get("creditData") or {}
    return Account(
        id=a["id"],
        alias=alias,
        item_id=a.get("itemId") or "",
        type=a["type"],
        subtype=a.get("subtype") or "",
        name=a.get("name") or "",
        number=a.get("number"),
        owner_tax_id=_doc(a.get("taxNumber")),
        balance=_req_dec(a.get("balance")),
        currency=a.get("currencyCode") or "BRL",
        credit_limit=_dec(credit.get("creditLimit")),
        available_credit=_dec(credit.get("availableCreditLimit")),
    )


def _transaction(alias: str, account: Account, t: dict) -> Transaction:
    sign = CARD_AMOUNT_SIGN if account.type == "CREDIT" else 1
    tx_currency = t.get("currencyCode") or account.currency
    foreign = tx_currency != account.currency
    in_account = t.get("amountInAccountCurrency")
    if foreign and in_account is None:
        # No conversion given: keep the amount in its own currency and label it
        # so, rather than passing a foreign figure off as the account currency.
        amount = _req_dec(t.get("amount")) * sign
        currency = tx_currency
    else:
        amount = _req_dec(in_account if in_account is not None else t.get("amount")) * sign
        currency = account.currency
    # Same sign convention as amount (negative = money out).
    original = _dec(t.get("amount")) if foreign else None
    original_amount = original * sign if original is not None else None

    payment = t.get("paymentData") or {}
    party = payment.get("receiver" if amount < 0 else "payer") or {}
    merchant = t.get("merchant") or {}
    card_meta = t.get("creditCardMetadata") or {}
    return Transaction(
        id=t["id"],
        alias=alias,
        account_id=account.id,
        date=_date(t["date"]),  # type: ignore[arg-type]
        amount=amount,
        currency=currency,
        original_amount=original_amount,
        original_currency=tx_currency if foreign else None,
        description=t.get("description") or t.get("descriptionRaw") or "",
        counterpart_name=party.get("name"),
        counterpart_tax_id=_doc(party.get("documentNumber")),
        counterpart_account_number=party.get("accountNumber"),
        merchant_name=merchant.get("businessName") or merchant.get("name"),
        pluggy_category=t.get("category"),
        bill_id=card_meta.get("billId"),
    )


def normalize_snapshot(
    alias: str,
    item: dict,
    accounts: list[dict],
    transactions: dict[str, list[dict]],
    bills: dict[str, list[dict]],
    investments: list[dict],
    investment_transactions: dict[str, list[dict]],
    loans: list[dict],
    fetched_from: date,
    fetched_to: date,
    tool_names: frozenset[str],
) -> Snapshot:
    accs = [_account(alias, a) for a in accounts]
    by_id = {a.id: a for a in accs}

    txs = [
        apply_text_policy(_transaction(alias, by_id[acc_id], t), tool_names)
        for acc_id, rows in transactions.items()
        if acc_id in by_id
        for t in rows
    ]
    bill_models = [
        Bill(
            id=b["id"],
            alias=alias,
            account_id=acc_id,
            due_date=_date(b["dueDate"]),  # type: ignore[arg-type]
            closing_date=_date(b.get("closeDate") or b.get("closingDate")),
            total_amount=_req_dec(b.get("totalAmount")),
            minimum_payment=_dec(b.get("minimumPaymentAmount")),
            currency=b.get("totalAmountCurrencyCode") or "BRL",
        )
        for acc_id, rows in bills.items()
        for b in rows
    ]
    invs = [
        Investment(
            id=i["id"],
            alias=alias,
            item_id=i.get("itemId") or "",
            type=i.get("type") or "",
            name=i.get("name") or "",
            balance=_req_dec(i.get("balance")),
            currency=i.get("currencyCode") or "BRL",
            due_date=_date(i.get("dueDate")),
            issuer=i.get("issuer"),
        )
        for i in investments
    ]
    inv_txs = [
        InvestmentTransaction(
            id=r["id"],
            alias=alias,
            investment_id=inv_id,
            date=_date(r["date"]),  # type: ignore[arg-type]
            trade_date=_date(r.get("tradeDate")),
            type=r.get("type") or "",
            movement_type=r.get("movementType"),
            quantity=_dec(r.get("quantity")),
            value=_dec(r.get("value")),
            amount=_req_dec(r.get("amount")),
        )
        for inv_id, rows in investment_transactions.items()
        for r in rows
    ]
    loan_models = []
    for ln in loans:
        inst = ln.get("installments") or {}
        loan_models.append(
            Loan(
                id=ln["id"],
                alias=alias,
                item_id=ln.get("itemId") or "",
                product_name=ln.get("productName"),
                contract_amount=_dec(ln.get("contractAmount")),
                outstanding_balance=_dec(ln.get("outstandingBalance")),
                currency=ln.get("currencyCode") or "BRL",
                installments_total=inst.get("totalInstallments"),
                installments_remaining=inst.get("dueInstallments"),
                next_due_date=_date(ln.get("nextDueDate")),
            )
        )
    return Snapshot(
        alias=alias,
        item=item,
        accounts=accs,
        transactions=txs,
        bills=bill_models,
        investments=invs,
        investment_transactions=inv_txs,
        loans=loan_models,
        fetched_from=fetched_from,
        fetched_to=fetched_to,
    )
