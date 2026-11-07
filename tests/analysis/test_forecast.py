from datetime import date, datetime
from decimal import Decimal

import pytest

from cashbook_mcp.analysis.forecast import cash_forecast
from cashbook_mcp.store.db import Database

KEY = "ab" * 32
NOW = datetime(2026, 1, 1)
TODAY = date(2026, 3, 31)


def tx(cur, id, account, d, amount, desc, kind, *, reason="test", merchant=None, counterpart=None,
       bill_id=None, number=None, total=None, purchase=None):  # fmt: skip
    cur.execute(
        "INSERT INTO transactions (id, alias, account_id, date, amount, currency, description, "
        "counterpart_name, merchant_name, bill_id, installment_number, installment_total, "
        "purchase_date, kind, kind_reason, first_seen_at, last_seen_at) "
        "VALUES (?, 'main', ?, ?, ?, 'BRL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [id, account, d, Decimal(amount), desc, counterpart, merchant, bill_id, number, total,
         purchase, kind, reason, NOW, NOW],
    )  # fmt: skip


def bill(cur, id, due, closing, total):
    cur.execute(
        "INSERT INTO bills (id, alias, account_id, due_date, closing_date, total_amount, currency, "
        "first_seen_at, last_seen_at) VALUES (?, 'main', 'card', ?, ?, ?, 'BRL', ?, ?)",
        [id, due, closing, Decimal(total), NOW, NOW],
    )


def account(cur, id, typ, balance):
    cur.execute(
        "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
        "first_seen_at, last_seen_at) VALUES (?, 'main', 'item', ?, 'X', ?, ?, 'BRL', ?, ?)",
        [id, typ, id, Decimal(balance), NOW, NOW],
    )


@pytest.fixture
def empty(tmp_path):
    database = Database.open(tmp_path / "e.duckdb", KEY)
    with database.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) "
            "VALUES ('main', 'item-1', 'Bank A', ?)",
            [NOW],
        )
        account(cur, "bank", "BANK", "1000.00")
    yield database
    database.close()


@pytest.fixture
def fdb(empty):
    """Bank (pays the card) and a richer second bank; a card with two paid bills and one open."""
    with empty.write() as cur:
        account(cur, "big", "BANK", "5000.00")
        account(cur, "card", "CREDIT", "400.00")
        for id, due, closing, total in [
            ("b-feb", date(2026, 2, 10), date(2026, 2, 3), "300.00"),
            ("b-mar", date(2026, 3, 10), date(2026, 3, 3), "350.00"),
            ("b-apr", date(2026, 4, 10), date(2026, 4, 3), "400.00"),
        ]:
            bill(cur, id, due, closing, total)
        tx(cur, "pay-feb", "bank", date(2026, 2, 10), "-300.00", "Pagamento", "card_bill_payment",
           reason="matched_card_bill:b-feb")  # fmt: skip
        tx(cur, "pay-mar", "bank", date(2026, 3, 10), "-350.00", "Pagamento", "card_bill_payment",
           reason="matched_card_bill:b-mar")  # fmt: skip
    return empty


def events(out, source=None):
    return [e for e in out["events"] if source is None or e["source"] == source]


def test_open_bill_in_window(fdb):
    out = cash_forecast(fdb, TODAY)
    assert out["scope"] == "personal"
    assert out["start_balance"] == "6000.00"
    (e,) = events(out, "bill")
    assert (e["date"], e["amount"], e["confidence"]) == ("2026-04-10", "-400.00", "high")
    assert e["account_id"] == "bank"  # the account that paid the card's latest matched bill
    assert e["alias"] == "main"
    assert out["lowest"]["balance"] == "5600.00" and out["negative_on"] is None
    assert len(out["balance"]) == 31


def test_paid_bill_skipped(fdb):
    with fdb.write() as cur:
        tx(cur, "pay-apr", "bank", date(2026, 3, 31), "-400.00", "Pagamento", "card_bill_payment",
           reason="matched_card_bill:b-apr")  # fmt: skip
    assert events(cash_forecast(fdb, TODAY), "bill") == []


def test_no_payer_history_uses_richest_bank_and_notes(fdb):
    with fdb.write() as cur:
        cur.execute("DELETE FROM transactions")
    out = cash_forecast(fdb, TODAY)
    (e,) = events(out, "bill")
    assert e["account_id"] == "big"
    assert any("highest balance" in n for n in out["notes"])


def test_future_installments(fdb):
    with fdb.write() as cur:
        # Installment 2 of 4 sits in the open April bill; 3 and 4 are still to come.
        tx(cur, "inst-2", "card", date(2026, 3, 20), "-100.00", "Loja 2/4", "purchase",
           bill_id="b-apr", number=2, total=4, purchase=date(2026, 2, 20))  # fmt: skip
        tx(cur, "inst-1", "card", date(2026, 2, 20), "-100.00", "Loja 1/4", "purchase",
           bill_id="b-mar", number=1, total=4, purchase=date(2026, 2, 20))  # fmt: skip
    out = cash_forecast(fdb, TODAY, days=90)
    inst = events(out, "installment")
    assert [(e["date"], e["amount"]) for e in inst] == [("2026-05-10", "-100.00"), ("2026-06-10", "-100.00")]
    assert {e["account_id"] for e in inst} == {"bank"}
    assert [e["date"] for e in events(out, "bill")] == ["2026-04-10"]  # no duplicate of the open bill
    assert "installment" not in {e["source"] for e in events(cash_forecast(fdb, TODAY, days=30))}


def test_installments_seen_at_3_of_10_remaining_seven(fdb):
    with fdb.write() as cur:
        tx(cur, "i3", "card", date(2026, 3, 12), "-50.00", "Loja 3/10", "purchase",
           number=3, total=10, purchase=date(2026, 1, 12))  # fmt: skip
    out = cash_forecast(fdb, TODAY, days=90)
    # No bill_id: the first remaining one falls on the next usual due day (day 10).
    assert [e["date"] for e in events(out, "installment")] == ["2026-04-10", "2026-05-10", "2026-06-10"]


def test_bank_recurring_and_income(fdb):
    with fdb.write() as cur:
        for n, d in enumerate([date(2025, 12, 5), date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]):
            tx(cur, f"sal-{n}", "bank", d, "3000.00", "Salario", "income", counterpart="Employer Ltda")
            tx(cur, f"rent-{n}", "bank", d, "-800.00", "Aluguel", "purchase", merchant="Landlord")
    out = cash_forecast(fdb, TODAY)
    rec = events(out, "recurring")
    assert sorted((e["amount"], e["account_id"]) for e in rec) == [("-800.00", "bank"), ("3000.00", "bank")]
    assert all("2026-04-01" <= e["date"] <= "2026-04-30" for e in rec)
    assert out["typical_monthly_inflow"] == "3000.00"
    assert any("typical_monthly_inflow" in n and "not" in n for n in out["notes"])


def test_card_recurring_goes_into_future_bill(fdb):
    with fdb.write() as cur:
        for n, d in enumerate([date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]):
            tx(cur, f"nf-{n}", "card", d, "-50.00", "NETFLIX", "purchase", merchant="Netflix")
    out = cash_forecast(fdb, TODAY, days=60)
    rec = events(out, "recurring")
    # Charge ~4/4 closes 5/3 and is due 5/10 (this card's bills fall due a week after closing).
    assert [(e["date"], e["amount"], e["account_id"]) for e in rec[:1]] == [("2026-05-10", "-50.00", "bank")]
    assert not any(e["date"] <= "2026-04-30" for e in rec)
    assert any("estimated future bill" in n for n in out["notes"])


def test_negative_on_and_lowest(fdb):
    with fdb.write() as cur:
        cur.execute("UPDATE accounts SET balance = 100 WHERE id = 'bank'")
        cur.execute("UPDATE accounts SET balance = 0 WHERE id = 'big'")
    out = cash_forecast(fdb, TODAY, by_account=True)
    assert out["negative_on"] == "2026-04-10"
    assert out["lowest"] == {"date": "2026-04-10", "balance": "-300.00"}
    by_id = {a["account_id"]: a for a in out["accounts"]}
    assert by_id["bank"]["negative_on"] == "2026-04-10" and by_id["big"]["negative_on"] is None
    assert by_id["big"]["events"] == [] and by_id["bank"]["events"][0]["amount"] == "-400.00"


def test_nothing_scheduled_is_flat(empty):
    out = cash_forecast(empty, TODAY)
    assert out["events"] == []
    assert {p["balance"] for p in out["balance"]} == {"1000.00"}
    assert out["lowest"] == {"date": "2026-03-31", "balance": "1000.00"}
    assert out["negative_on"] is None
    assert out["typical_monthly_inflow"] == "0.00"


def test_days_bounds(empty):
    for bad in (0, 91):
        with pytest.raises(ValueError):
            cash_forecast(empty, TODAY, days=bad)
    with pytest.raises(ValueError):
        cash_forecast(empty, TODAY, scope="nope")  # type: ignore[arg-type]
    weekly = cash_forecast(empty, TODAY, days=90)["balance"]
    assert [p["date"] for p in weekly][:3] == ["2026-03-31", "2026-04-07", "2026-04-14"]
    assert weekly[-1]["date"] == "2026-06-29"
    assert len(cash_forecast(empty, TODAY, days=1)["balance"]) == 2


def test_business_scope_without_connection(empty):
    out = cash_forecast(empty, TODAY, scope="business")
    assert out["start_balance"] == "0.00" and out["events"] == [] and out["negative_on"] is None
