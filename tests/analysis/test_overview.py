from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from cashbook_mcp.analysis.filters import today_in
from cashbook_mcp.analysis.overview import finance_overview

SP = ZoneInfo("America/Sao_Paulo")


def test_month_boundary_tz(db, add_row):
    assert today_in(SP, datetime(2026, 4, 1, 3, 30, tzinfo=UTC)) == date(2026, 4, 1)
    assert today_in(SP, datetime(2026, 4, 1, 2, 30, tzinfo=UTC)) == date(2026, 3, 31)
    add_row("t-apr", "main", "acc-main", date(2026, 4, 1), "-10.00", "Padaria", "purchase",
            category="Groceries")  # fmt: skip
    today = today_in(SP, datetime(2026, 4, 1, 3, 30, tzinfo=UTC))
    mtd = finance_overview(db, today)["month_to_date"]
    assert mtd == {"income": "0.00", "refunds": "0.00", "spending": "10.00", "net": "-10.00",
                   "from_business": "0.00", "to_business": "0.00"}
    march = finance_overview(db, date(2026, 3, 31))["month_to_date"]
    assert march["spending"] == "408.20"


def test_overview_bills_due_window(db):
    ov = finance_overview(db, date(2026, 4, 1), days_ahead=9)
    assert [(b["id"], b["role"]) for b in ov["bills_due"]] == [("bill-soon", "personal"), ("bill-edge", "personal")]
    assert ov["bills_due"][0]["total_amount"] == "300.00"
    narrow = finance_overview(db, date(2026, 4, 1), days_ahead=3)
    assert narrow["bills_due"] == []


def test_overview_shape(db, today):
    ov = finance_overview(db, today)
    assert [a["id"] for a in ov["accounts"]] == ["acc-main", "acc-second"]
    assert [c["id"] for c in ov["cards"]] == ["card-main"]
    assert ov["month_to_date"] == {
        "income": "5000.00", "refunds": "0.00", "spending": "408.20", "net": "4591.80",
        "from_business": "0.00", "to_business": "0.00",
    }  # fmt: skip
    assert len(ov["top_categories"]) <= 5
    assert ov["connections_with_problems"] == [
        {"alias": "second", "status": "LOGIN_ERROR", "error": "boom"}
    ]


def test_overview_reports_skipped_foreign_rows(db, add_row, today):
    add_row("t-eur", "main", "card-main", date(2026, 3, 13), "-9.99", "SHOP EU", "purchase",
            currency="EUR")  # fmt: skip
    mtd = finance_overview(db, today)["month_to_date"]
    assert mtd["spending"] == "408.20"
    assert mtd["excluded_foreign"] == 1


def test_business_block_only_with_a_business_connection(db, add_row, today):
    assert "business" not in finance_overview(db, today)
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at, status, role) "
            "VALUES ('biz', 'item-3', 'Bank C', TIMESTAMP '2026-01-01', 'UPDATED', 'business')"
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES ('acc-biz', 'biz', 'item-3', 'BANK', 'X', 'Biz', "
            "10.00, 'BRL', TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-01')"
        )
    add_row("b-in", "biz", "acc-biz", date(2026, 3, 4), "700.00", "Client", "income")
    add_row("b-out", "biz", "acc-biz", date(2026, 3, 6), "-120.00", "Tool", "purchase")
    ov = finance_overview(db, today)
    # Every account, card and bill says whose money it is.
    assert {a["id"]: a["role"] for a in ov["accounts"]} == {
        "acc-biz": "business", "acc-main": "personal", "acc-second": "personal"}  # fmt: skip
    assert {c["role"] for c in ov["cards"]} == {"personal"}
    assert {b["role"] for b in ov["bills_due"]} <= {"personal"}
    block = ov["business"]
    assert block["income"] == "700.00"
    assert block["spending"] == "120.00"
    assert block["owner_draws"] == "0.00"
    # personal figures stay out of the business block and the business out of the personal one
    assert finance_overview(db, today)["month_to_date"]["spending"] == "408.20"


def test_overview_has_forecast_block(db, today):
    from cashbook_mcp.analysis.overview import finance_overview

    block = finance_overview(db, today)["forecast"]
    assert set(block) == {"lowest", "negative_on", "low_confidence_events"}
    from cashbook_mcp.analysis.forecast import cash_forecast

    events = cash_forecast(db, today, days=30)["events"]
    assert block["low_confidence_events"] == sum(e["confidence"] == "low" for e in events) > 0
    assert set(block["lowest"]) == {"date", "balance"}


def test_overview_shows_one_off_only_when_non_zero(db, add_row, today):
    assert "one_off_out" not in finance_overview(db, today)["month_to_date"]
    add_row("t-house", "main", "acc-main", date(2026, 3, 14), "-900.00", "Down payment", "one_off")
    mtd = finance_overview(db, today)["month_to_date"]
    assert mtd["spending"] == "408.20" and mtd["one_off_out"] == "900.00"
    assert "one_off_in" not in mtd
