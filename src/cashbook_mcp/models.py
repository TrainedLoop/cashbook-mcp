"""Domain models: frozen, slotted dataclasses. Money is Decimal, dates are date."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Kind = Literal[
    "purchase",
    "income",
    "internal_transfer",
    "card_bill_payment",
    "refund",
    "investment",
    "fee",
    "loan",
    "other",
]
KINDS: tuple[str, ...] = (
    "purchase",
    "income",
    "internal_transfer",
    "card_bill_payment",
    "refund",
    "investment",
    "fee",
    "loan",
    "other",
)

Role = Literal["personal", "business"]
ROLES: tuple[str, ...] = ("personal", "business")

Purpose = Literal["regular", "investment"]
PURPOSES: tuple[str, ...] = ("regular", "investment")


@dataclass(frozen=True, slots=True)
class Connection:
    alias: str
    item_id: str
    connector_name: str
    created_at: datetime
    status: str | None
    execution_status: str | None
    error_code: str | None
    error_message: str | None
    last_updated_at: datetime | None
    consent_expires_at: datetime | None
    last_synced_at: datetime | None
    last_sync_error: str | None
    role: Role = "personal"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    alias: str
    item_id: str
    type: Literal["BANK", "CREDIT"]
    subtype: str
    name: str
    number: str | None
    owner_tax_id: str | None
    balance: Decimal
    currency: str
    credit_limit: Decimal | None
    available_credit: Decimal | None


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    alias: str
    account_id: str
    date: date
    amount: Decimal  # signed: negative is money out, in account currency
    currency: str
    original_amount: Decimal | None
    original_currency: str | None
    description: str
    counterpart_name: str | None
    counterpart_tax_id: str | None
    counterpart_account_number: str | None
    merchant_name: str | None
    pluggy_category: str | None
    bill_id: str | None
    installment_number: int | None = None
    installment_total: int | None = None
    purchase_date: date | None = None
    kind: Kind = "other"
    kind_reason: str = ""
    text_suspicious: bool = False
    suspicion_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Bill:
    id: str
    alias: str
    account_id: str
    due_date: date
    closing_date: date | None
    total_amount: Decimal
    minimum_payment: Decimal | None
    currency: str


@dataclass(frozen=True, slots=True)
class Investment:
    id: str
    alias: str
    item_id: str
    type: str
    name: str
    balance: Decimal
    currency: str
    due_date: date | None
    issuer: str | None


@dataclass(frozen=True, slots=True)
class InvestmentTransaction:
    id: str
    alias: str
    investment_id: str
    date: date
    trade_date: date | None
    type: str
    movement_type: str | None
    quantity: Decimal | None
    value: Decimal | None
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Loan:
    id: str
    alias: str
    item_id: str
    product_name: str | None
    contract_amount: Decimal | None
    outstanding_balance: Decimal | None
    currency: str
    installments_total: int | None
    installments_remaining: int | None
    next_due_date: date | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    alias: str
    item: dict
    accounts: list[Account]
    transactions: list[Transaction]
    bills: list[Bill]
    investments: list[Investment]
    investment_transactions: list[InvestmentTransaction]
    loans: list[Loan]
    fetched_from: date
    fetched_to: date
