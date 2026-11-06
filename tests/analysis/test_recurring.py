from datetime import date
from decimal import Decimal

from cashbook_mcp.analysis.recurring import find_recurring


def _by_key(items, needle):
    return [i for i in items if needle in i["key"]]


def test_monthly_subscription_high(db, today):
    (item,) = _by_key(find_recurring(db, today), "netflix")
    assert item["direction"] == "out"
    assert 27 <= item["cadence_days"] <= 33
    assert item["confidence"] == "high"
    assert item["occurrences"] == 6
    assert sorted(item["evidence_ids"]) == [f"t-sub-{n}" for n in range(6)]
    assert item["typical_amount"] == "55.90"
    assert item["last_date"] == "2026-03-05"
    assert item["next_expected"] == date(2026, 3, 5).fromordinal(
        date(2026, 3, 5).toordinal() + item["cadence_days"]
    ).isoformat()


def test_salary_detected_as_income(db, today, add_row):
    for n, d in enumerate([date(2026, 1, 5), date(2026, 2, 5)]):
        add_row(f"t-sal-{n}", "main", "acc-main", d, "5000.00", "Salario", "income",
                counterpart="Employer Ltda")  # fmt: skip
    (item,) = _by_key(find_recurring(db, today), "employer")
    assert item["direction"] == "in"
    assert item["occurrences"] == 3
    assert Decimal(item["typical_amount"]) == Decimal("5000.00")
    assert item["confidence"] == "high"


def test_irregular_merchant_not_high(db, today, add_row):
    for n, (d, amt) in enumerate(
        [(date(2026, 1, 3), "-20.00"), (date(2026, 1, 5), "-21.00"),
         (date(2026, 2, 20), "-19.50"), (date(2026, 3, 28), "-20.50")]
    ):  # fmt: skip
        add_row(f"t-pad-{n}", "main", "acc-main", d, amt, f"PADARIA {n}", "purchase",
                merchant="Padaria *123")  # fmt: skip
    (item,) = _by_key(find_recurring(db, today), "padaria")
    assert item["confidence"] != "high"
    assert "*" not in item["key"] and not any(c.isdigit() for c in item["key"])


def test_internal_transfers_ignored(db, today, add_row):
    for n, d in enumerate([date(2026, 1, 10), date(2026, 2, 10)]):
        add_row(f"t-ip-{n}", "main", "acc-main", d, "-1000.00", "PIX enviado",
                "internal_transfer", counterpart="Owner Name")  # fmt: skip
        add_row(f"t-bp-{n}", "main", "acc-main", d, "-300.00", "Pagamento fatura",
                "card_bill_payment")  # fmt: skip
    keys = [i["key"] for i in find_recurring(db, today)]
    assert not any("pix" in k or "owner" in k or "fatura" in k for k in keys)


def test_min_occurrences_and_window(db, today):
    assert _by_key(find_recurring(db, today, months=2), "netflix") == []
    assert _by_key(find_recurring(db, today, min_occurrences=7), "netflix") == []
