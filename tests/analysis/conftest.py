from datetime import date, datetime
from decimal import Decimal

import pytest

from cashbook_mcp.store.db import Database

KEY = "ab" * 32
NOW = datetime(2026, 1, 1)
TODAY = date(2026, 3, 31)

TX_COLS = (
    "id, alias, account_id, date, amount, currency, original_amount, original_currency, "
    "description, counterpart_name, merchant_name, pluggy_category, bill_id, kind, "
    "kind_reason, first_seen_at, last_seen_at"
)


def add_tx(
    cur,
    id,
    alias,
    account_id,
    d,
    amount,
    description,
    kind,
    *,
    category=None,
    merchant=None,
    counterpart=None,
    original=None,
    original_currency=None,
    bill_id=None,
    currency="BRL",
):
    cur.execute(
        f"INSERT INTO transactions ({TX_COLS}) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', ?, ?)",
        [
            id, alias, account_id, d, Decimal(amount), currency, original, original_currency,
            description, counterpart, merchant, category, bill_id, kind, NOW, NOW,
        ],
    )  # fmt: skip


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "t.duckdb", KEY)
    with database.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at, "
            "status, last_sync_error) VALUES "
            "('main', 'item-1', 'Bank A', ?, 'UPDATED', NULL), "
            "('second', 'item-2', 'Bank B', ?, 'LOGIN_ERROR', 'boom')",
            [NOW, NOW],
        )
        for id, alias, typ, name, bal, limit, avail in [
            ("acc-main", "main", "BANK", "Checking A", "1200.00", None, None),
            ("card-main", "main", "CREDIT", "Card A", "300.00", "5000.00", "4700.00"),
            ("acc-second", "second", "BANK", "Checking B", "800.00", None, None),
        ]:
            cur.execute(
                "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, "
                "currency, credit_limit, available_credit, first_seen_at, last_seen_at) "
                "VALUES (?, ?, 'item', ?, 'X', ?, ?, 'BRL', ?, ?, ?, ?)",
                [id, alias, typ, name, Decimal(bal),
                 Decimal(limit) if limit else None,
                 Decimal(avail) if avail else None, NOW, NOW],
            )  # fmt: skip
        for id, due, total in [
            ("bill-soon", date(2026, 4, 5), "300.00"),
            ("bill-edge", date(2026, 4, 10), "120.00"),
            ("bill-far", date(2026, 4, 11), "99.00"),
            ("bill-past", date(2026, 3, 30), "50.00"),
        ]:
            cur.execute(
                "INSERT INTO bills (id, alias, account_id, due_date, total_amount, "
                "currency, first_seen_at, last_seen_at) "
                "VALUES (?, 'main', 'card-main', ?, ?, 'BRL', ?, ?)",
                [id, due, Decimal(total), NOW, NOW],
            )
        # March card purchases (300.00 in total) and the matching bank payment.
        add_tx(cur, "t-card-1", "main", "card-main", date(2026, 3, 8), "-200.00",
               "Mercado Central", "purchase", category="Groceries", merchant="Mercado Central")  # fmt: skip
        add_tx(cur, "t-card-2", "main", "card-main", date(2026, 3, 15), "-100.00",
               "Restaurante Sol", "purchase", category="Eating out", merchant="Restaurante Sol")  # fmt: skip
        add_tx(cur, "t-bill-pay", "main", "acc-main", date(2026, 3, 20), "-300.00",
               "Pagamento fatura", "card_bill_payment", category="Credit card payment")  # fmt: skip
        # Internal PIX of 1,000.00 between the two aliases' own accounts.
        add_tx(cur, "t-pix-out", "main", "acc-main", date(2026, 3, 10), "-1000.00",
               "PIX enviado", "internal_transfer", counterpart="Owner Name")  # fmt: skip
        add_tx(cur, "t-pix-in", "second", "acc-second", date(2026, 3, 10), "1000.00",
               "PIX recebido", "internal_transfer", counterpart="Owner Name")  # fmt: skip
        add_tx(cur, "t-salary", "main", "acc-main", date(2026, 3, 5), "5000.00",
               "Salario", "income", category="Salary", counterpart="Employer Ltda")  # fmt: skip
        # Subscription: Oct 2025 .. Mar 2026.
        for n, d in enumerate(
            [date(2025, 10, 5), date(2025, 11, 5), date(2025, 12, 5),
             date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]
        ):  # fmt: skip
            add_tx(cur, f"t-sub-{n}", "main", "card-main", d, "-55.90", "NETFLIX.COM",
                   "purchase", category="Streaming", merchant="Netflix")  # fmt: skip
        # USD purchase, already converted to BRL.
        add_tx(cur, "t-usd", "main", "card-main", date(2026, 3, 12), "-52.30",
               "GITHUB INC", "purchase", category="Software", merchant="GitHub",
               original="-10.00", original_currency="USD")  # fmt: skip
    yield database
    database.close()


@pytest.fixture
def today():
    return TODAY


@pytest.fixture
def add_row(db):
    def _add(*args, **kwargs):
        with db.write() as cur:
            add_tx(cur, *args, **kwargs)

    return _add
