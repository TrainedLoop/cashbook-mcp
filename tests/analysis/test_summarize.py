from datetime import date
from decimal import Decimal

import pytest

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
        {"period": "2026-03", "income": "5000.00", "refunds": "0.00", "spending": "408.20",
         "net": "4591.80", "one_off_out": "0.00", "one_off_in": "0.00",
         "from_business": "0.00", "to_business": "0.00"}
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


def test_in_summary_matches_cash_flow_income(db, add_row, today):
    add_row("t-redeem", "main", "acc-main", date(2026, 3, 25), "700.00", "Resgate",
            "investment")  # fmt: skip
    f_in = TxFilters(date_from=MARCH.date_from, date_to=MARCH.date_to, direction="in")
    groups = summarize_transactions(db, f_in, "kind", today=today)["groups"]
    total = sum(Decimal(g["total"]) for g in groups)
    flow = cash_flow(db, date(2026, 3, 1), date(2026, 3, 31))
    assert str(total) == flow["totals"]["income"] == "5000.00"


def test_compare_previous_months_match_by_offset(db, add_row, today):
    add_row("t-dec-extra", "main", "acc-main", date(2025, 12, 10), "-10.00", "Extra",
            "purchase")  # fmt: skip
    f = TxFilters(date_from=date(2026, 2, 1), date_to=date(2026, 3, 31))
    groups = summarize_transactions(db, f, "month", compare_previous=True, today=today)[
        "groups"
    ]
    assert [g["key"] for g in groups] == ["2026-02", "2026-03"]
    assert groups[0]["previous"] == {
        "total": "65.90", "count": 2, "average": "32.95", "previous_key": "2025-12",
    }  # fmt: skip
    assert groups[1]["previous"]["total"] == "55.90"
    assert groups[1]["previous"]["previous_key"] == "2026-01"


def test_compare_previous_gap_pairs_by_calendar_offset(db, today):
    # No February rows: March must still pair with January, not December.
    with db.write() as cur:
        cur.execute("DELETE FROM transactions WHERE id = 't-sub-4'")
    f = TxFilters(date_from=date(2026, 2, 1), date_to=date(2026, 3, 31))
    groups = summarize_transactions(db, f, "month", compare_previous=True, today=today)[
        "groups"
    ]
    assert [g["key"] for g in groups] == ["2026-03"]
    assert groups[0]["previous"]["previous_key"] == "2026-01"
    assert groups[0]["previous"]["total"] == "55.90"


def test_compare_previous_weeks_by_calendar_offset(db, today):
    f = TxFilters(date_from=date(2026, 3, 9), date_to=date(2026, 3, 22))
    groups = summarize_transactions(db, f, "week", compare_previous=True, today=today)[
        "groups"
    ]
    by_key = {g["key"]: g for g in groups}
    # 2026-W11 (Mar 9-15) pairs with W09 (Feb 23-Mar 1), which is empty.
    assert by_key["2026-W11"]["previous"]["previous_key"] == "2026-W09"
    assert by_key["2026-W11"]["previous"]["total"] == "0.00"


def test_requested_internal_kinds_are_summarized(db, today):
    f = TxFilters(date_from=MARCH.date_from, date_to=MARCH.date_to, kinds=["card_bill_payment"])
    groups = summarize_transactions(db, f, "kind", today=today)["groups"]
    assert [(g["key"], g["count"]) for g in groups] == [("card_bill_payment", 1)]


def test_uncategorized_category_filter(db, add_row, today):
    add_row("t-nocat", "main", "acc-main", date(2026, 3, 9), "-7.00", "Banca", "purchase")
    f = TxFilters(date_from=MARCH.date_from, date_to=MARCH.date_to, categories=["uncategorized"])
    groups = summarize_transactions(db, f, "category", today=today)["groups"]
    assert [(g["key"], g["total"]) for g in groups] == [("uncategorized", "7.00")]


def test_kinds_without_direction_are_signed(db, add_row, today):
    # The internal PIX pair nets to zero; each side is reported in the split.
    add_row("t-pix-out2", "main", "acc-main", date(2026, 3, 11), "-50.00", "PIX", "internal_transfer")
    f = TxFilters(date_from=MARCH.date_from, date_to=MARCH.date_to, kinds=["internal_transfer"])
    g = summarize_transactions(db, f, "kind", today=today)["groups"][0]
    assert g["key"] == "internal_transfer"
    assert (g["total"], g["in"], g["out"], g["count"]) == ("-50.00", "1000.00", "1050.00", 3)
    assert g["average"] == "-16.67"


def test_cash_flow_card_payment_and_refund(db, add_row, today):
    # An uncategorized card-side payment, classified card_bill_payment, is not income;
    # a refund lowers spending and is reported separately.
    add_row("t-card-pay", "main", "card-main", date(2026, 3, 20), "300.00", "Pagamento recebido",
            "card_bill_payment")  # fmt: skip
    add_row("t-refund", "main", "card-main", date(2026, 3, 18), "20.00", "Estorno", "refund")
    res = cash_flow(db, date(2026, 3, 1), date(2026, 3, 31), today=today)
    assert res["totals"] == {
        "income": "5000.00", "refunds": "20.00", "spending": "388.20", "net": "4611.80",
        "one_off_out": "0.00", "one_off_in": "0.00", "from_business": "0.00", "to_business": "0.00",
    }  # fmt: skip
    assert res["periods"][0]["refunds"] == "20.00"
    assert res["periods"][0]["income"] == "5000.00"


def test_foreign_currency_rows_are_not_summed(db, add_row, today):
    add_row("t-eur", "main", "card-main", date(2026, 3, 13), "-9.99", "SHOP EU", "purchase",
            category="Software", currency="EUR", original="-9.99", original_currency="EUR")  # fmt: skip
    flow = cash_flow(db, date(2026, 3, 1), date(2026, 3, 31), today=today)
    assert flow["totals"]["spending"] == "408.20"
    assert flow["excluded_foreign"] == 1
    summary = summarize_transactions(db, MARCH, "kind", today=today)
    assert summary["groups"][0]["total"] == "408.20"
    assert summary["excluded_foreign"] == 1
    cats = {c["category"]: c["total"] for c in list_categories(db, MARCH.date_from, MARCH.date_to)}
    assert cats["Software"] == "52.30"
    clean = cash_flow(db, date(2025, 10, 1), date(2025, 10, 31), today=today)
    assert "excluded_foreign" not in clean


def test_signed_compare_previous_carries_split(db, today):
    f = TxFilters(date_from=MARCH.date_from, date_to=MARCH.date_to, kinds=["internal_transfer"])
    g = summarize_transactions(db, f, "kind", compare_previous=True, today=today)["groups"][0]
    assert g["previous"] == {"total": "0.00", "count": 0, "average": "0.00", "in": "0.00", "out": "0.00"}


def _many_merchants(add_row, n=30):
    for i in range(n):
        add_row(
            f"m-{i}", "main", "acc-main", date(2026, 3, 1 + i % 20), f"-{10 + i}.00",
            f"Shop {i}", "purchase", merchant=f"Merchant {i:02d}",
        )  # fmt: skip


def test_summarize_top_n_keeps_total(db, add_row, today):
    _many_merchants(add_row)
    f = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31), text="Shop")
    full = summarize_transactions(db, f, "merchant", today=today, limit=200)["groups"]
    top = summarize_transactions(db, f, "merchant", today=today, limit=5)["groups"]
    assert len(full) == 30
    assert len(top) == 6
    assert top[-1]["key"] == "others"
    assert top[-1]["groups"] == 25
    assert sum(Decimal(g["total"]) for g in top) == sum(Decimal(g["total"]) for g in full)
    assert top[-1]["count"] == 25


def test_others_carries_previous(db, add_row, today):
    _many_merchants(add_row)
    add_row("p-1", "main", "acc-main", date(2026, 2, 10), "-7.00", "Shop old", "purchase",
            merchant="Merchant 00")  # fmt: skip
    f = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31), text="Shop")
    res = summarize_transactions(db, f, "merchant", True, today=today, limit=1)["groups"]
    assert res[-1]["key"] == "others"
    assert res[-1]["previous"]["total"] == "7.00"


def test_time_grouping_not_limited(db, today):
    f = TxFilters(date_from=date(2025, 10, 1), date_to=date(2026, 3, 31))
    res = summarize_transactions(db, f, "month", today=today, limit=2)["groups"]
    assert len(res) == 6
    assert all(g["key"] != "others" for g in res)


def test_two_dimension_top_n(db, add_row, today):
    _many_merchants(add_row)
    f = TxFilters(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31), text="Shop")
    full = summarize_transactions(db, f, ["merchant", "category"], today=today, limit=200)["groups"]
    top = summarize_transactions(db, f, ["merchant", "category"], today=today, limit=4)["groups"]
    assert len(top) == 5
    assert top[-1]["key"] == "others"
    assert top[-1]["groups"] == len(full) - 4
    assert sum(Decimal(g["total"]) for g in top) == sum(Decimal(g["total"]) for g in full)


def test_limit_bounds(db, today):
    for bad in (0, 201, -1):
        with pytest.raises(ValueError):
            summarize_transactions(db, TxFilters(), "category", today=today, limit=bad)


def test_one_off_kind_is_outside_spending_and_summarized_raw(db, add_row, today):
    add_row("t-oo1", "main", "acc-main", date(2026, 3, 12), "-900.00", "Down payment", "one_off")
    add_row("t-oo2", "main", "acc-main", date(2026, 3, 13), "100.00", "Partial return", "one_off")
    base = {"date_from": MARCH.date_from, "date_to": MARCH.date_to}
    spend = summarize_transactions(db, TxFilters(**base), "kind", today=today)["groups"]
    assert "one_off" not in {g["key"] for g in spend}
    income = summarize_transactions(db, TxFilters(**base, direction="in"), "kind", today=today)["groups"]
    assert "one_off" not in {g["key"] for g in income}
    g = summarize_transactions(db, TxFilters(**base, kinds=["one_off"]), "kind", today=today)["groups"][0]
    assert (g["key"], g["total"], g["in"], g["out"], g["count"]) == ("one_off", "-800.00", "100.00", "900.00", 2)
