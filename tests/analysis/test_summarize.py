from datetime import date

from cashbook_mcp.analysis.filters import TxFilters
from cashbook_mcp.analysis.summarize import (
    cash_flow,
    list_categories,
    summarize_transactions,
)

MARCH = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))


def test_spending_counts_purchases_once(db, today):
    # 300.00 card purchases + 55.90 subscription + 52.30 USD charge in BRL;
    # the 300.00 bill payment and the 1,000.00 internal PIX are excluded.
    res = summarize_transactions(db, MARCH, "kind", today=today)
    assert res["groups"] == [
        {
            "key": "purchase",
            "total": "408.20",
            "count": 4,
            "average": "102.05",
            "first_date": "2026-03-05",
            "last_date": "2026-03-15",
        }
    ]


def test_cash_flow_excludes_internal(db, today):
    res = cash_flow(db, date(2026, 3, 1), date(2026, 3, 31), today=today)
    assert res["periods"] == [
        {"period": "2026-03", "income": "5000.00", "spending": "408.20", "net": "4591.80"}
    ]
    assert res["totals"]["net"] == "4591.80"


def test_cash_flow_by_account_and_week(db, today):
    res = cash_flow(db, date(2026, 3, 1), date(2026, 3, 31), "week", True)
    assert {p["account_id"] for p in res["periods"]} == {"acc-main", "card-main"}
    assert all(p["period"].startswith("2026-W") for p in res["periods"])


def test_compare_previous(db, today):
    f = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
    res = summarize_transactions(db, f, "merchant", compare_previous=True, today=today)
    netflix = next(g for g in res["groups"] if g["key"] == "Netflix")
    assert netflix["total"] == "55.90"
    assert netflix["previous"]["total"] == "55.90"  # Jan 29 .. Feb 28 holds Feb 5
    github = next(g for g in res["groups"] if g["key"] == "GitHub")
    assert github["previous"] == {"total": "0.00", "count": 0, "average": "0.00"}
    assert res["period"]["previous_date_from"] == "2026-01-29"
    assert res["period"]["previous_date_to"] == "2026-02-28"


def test_default_period_is_last_30_days(db, today):
    res = summarize_transactions(db, TxFilters(), "month", today=today)
    assert res["period"] == {"date_from": "2026-03-02", "date_to": "2026-03-31"}


def test_group_by_month_and_category(db, today):
    f = TxFilters(date_from=date(2025, 10, 1), date_to=date(2026, 3, 31))
    months = summarize_transactions(db, f, "month", today=today)["groups"]
    assert [g["key"] for g in months] == [
        "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
    ]  # fmt: skip
    cats = summarize_transactions(db, f, "category", today=today)["groups"]
    assert cats[0]["key"] == "Streaming"
    assert cats[0]["total"] == "335.40"


def test_income_direction(db, today):
    res = summarize_transactions(db, TxFilters(direction="in"), "kind", today=today)
    assert res["groups"][0]["key"] == "income"
    assert res["groups"][0]["total"] == "5000.00"


def test_list_categories(db):
    cats = list_categories(db, date(2026, 3, 1), date(2026, 3, 31))
    assert cats[0] == {"category": "Groceries", "count": 1, "total": "200.00"}
    assert {c["category"] for c in cats} == {
        "Groceries", "Eating out", "Streaming", "Software",
    }  # fmt: skip
