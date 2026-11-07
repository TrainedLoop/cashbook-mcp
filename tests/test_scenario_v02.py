"""End-to-end v0.2 scenario with synthetic data, built through Syncer and a fake Pluggy client."""

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cashbook_mcp.analysis.filters import TxFilters
from cashbook_mcp.analysis.forecast import cash_forecast
from cashbook_mcp.analysis.networth import net_worth
from cashbook_mcp.analysis.overview import finance_overview
from cashbook_mcp.analysis.summarize import (
    cash_flow,
    list_categories,
    summarize_transactions,
)
from cashbook_mcp.schema_doc import PERSONAL_INCOME_SQL
from cashbook_mcp.sql import run_query
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.reclassify import list_changes, reclassify, undo
from cashbook_mcp.store.sync import Syncer

# The sync tests own the fake Pluggy client; load it by path (tests are not a package).
_spec = importlib.util.spec_from_file_location(
    "sync_fakes", Path(__file__).parent / "store" / "test_sync.py"
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)
FakeClient, Clock, iso, KEY, TZ = _fakes.FakeClient, _fakes.Clock, _fakes.iso, _fakes.KEY, _fakes.TZ

CNPJ = "12345678000190"
CPF = "11122233344"
OCT = (date(2026, 10, 1), date(2026, 10, 31))
TODAY = date(2026, 11, 6)
D = Decimal


def put(fake, account_id, tx_id, day, amount, description, category=None):
    tx = {"id": tx_id, "date": iso(day), "amount": amount, "description": description,
          "currencyCode": "BRL"}  # fmt: skip
    if category:
        tx["category"] = category
    fake.transactions[account_id].append(tx)


@pytest.fixture
def world(tmp_path):
    db = Database.open(tmp_path / "scenario.duckdb", KEY)
    fake = FakeClient()
    for item in ("biz", "bro", "per"):
        fake.add_item(f"item-{item}")
        for acc in fake.accounts[f"item-{item}"]:
            acc["taxNumber"] = CNPJ if item == "biz" else CPF
    # Card amounts are positive for spending in Pluggy and negative for credits.
    put(fake, "item-biz-bank", "biz-pay-1", "2026-10-05", 8000.0, "Client A invoice")
    put(fake, "item-biz-bank", "biz-pay-2", "2026-10-12", 4000.0, "Client B invoice")
    put(fake, "item-biz-bank", "biz-tax", "2026-10-07", -500.0, "Tax slip", "Taxes")
    put(fake, "item-biz-bank", "biz-to-bro", "2026-10-10", -3000.0, "Transfer out", "Transfers")
    put(fake, "item-biz-bank", "biz-to-per", "2026-10-15", -2000.0, "Transfer out", "Transfers")
    put(fake, "item-biz-card", "biz-card-1", "2026-10-09", 200.0, "Cloud hosting", "Software")
    put(fake, "item-bro-bank", "bro-in", "2026-10-10", 3000.0, "Transfer in")
    put(fake, "item-bro-bank", "bro-to-per", "2026-10-12", -3000.0, "Transfer out", "Transfers")
    put(fake, "item-bro-bank", "bro-shop", "2026-10-14", -700.0, "Stock purchase", "Shopping")
    put(fake, "item-per-bank", "per-in-bro", "2026-10-12", 3000.0, "Transfer in")
    put(fake, "item-per-bank", "per-in-biz", "2026-10-15", 2000.0, "Transfer in")
    put(fake, "item-per-bank", "per-pix", "2026-10-20", -300.0, "PIX card payment")
    put(fake, "item-per-card", "per-card-1", "2026-10-08", 200.0, "Mercado", "Groceries")
    put(fake, "item-per-card", "per-card-2", "2026-10-09", 100.0, "Cinema", "Leisure")
    put(fake, "item-per-card", "per-card-refund", "2026-10-11", -40.0, "Refund Cinema")
    put(fake, "item-per-card", "per-card-paid", "2026-10-20", -300.0, "Payment received")
    clock = Clock()
    clock.value = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    syncer = Syncer(db, fake, history_months=12, tz=TZ, now=clock, tool_names=frozenset())
    yield db, syncer, fake
    db.close()


async def load(syncer):
    await syncer.add_connection("biz", "item-biz")
    await syncer.add_connection("bro", "item-bro")
    await syncer.add_connection("per", "item-per")


def flow(db, scope, **kw):
    return cash_flow(db, *OCT, "month", False, today=TODAY, scope=scope, **kw)["periods"][0]


def spending_of_categories(db, scope):
    return sum((D(str(c["total"])) for c in list_categories(db, *OCT, scope=scope)), D(0))


async def test_scenario(world):
    db, syncer, _ = world
    await load(syncer)
    roles = {c.alias: c.role for c in syncer.connections()}
    assert roles == {"biz": "business", "bro": "personal", "per": "personal"}

    personal, business, consolidated = (flow(db, s) for s in ("personal", "business", "consolidated"))

    # Personal income is the two draws; the card paid by PIX and the refund are not income.
    assert D(str(personal["income"])) == D("5000")
    assert D(str(personal["from_business"])) == D("5000")
    # 700 shopping + 200 + 100 card purchases - 40 refund.
    assert D(str(personal["refunds"])) == D("40")
    assert D(str(personal["spending"])) == D("960")

    assert D(str(business["income"])) == D("12000")
    assert D(str(business["spending"])) == D("700")  # tax + hosting
    assert D(str(business["owner_draws"])) == D("5000")

    assert D(str(consolidated["income"])) == D("12000")
    assert D(str(consolidated["spending"])) == D("1660")

    for scope, period in (("personal", personal), ("business", business), ("consolidated", consolidated)):
        assert spending_of_categories(db, scope) == D(str(period["spending"])), scope
    summary = summarize_transactions(
        db, TxFilters(date_from=OCT[0], date_to=OCT[1]), ["category", "month"], today=TODAY
    )
    assert sum((D(str(g["total"])) for g in summary["groups"]), D(0)) == D(str(personal["spending"]))

    # Personal income agrees across cash_flow, summarize_transactions and the documented SQL recipe
    # (whose example month is this scenario's October).
    income_in = summarize_transactions(
        db, TxFilters(date_from=OCT[0], date_to=OCT[1], direction="in"), "month", today=TODAY
    )
    (recipe,) = run_query(db, PERSONAL_INCOME_SQL).rows
    assert (
        D(str(personal["income"]))
        == sum((D(str(g["total"])) for g in income_in["groups"]), D(0))
        == D(str(recipe[0]))
    )

    # Marking the broker cash account as investment moves the unidentified purchase out.
    syncer.set_purpose("item-bro-bank", "investment")
    after = flow(db, "personal")
    assert D(str(after["spending"])) == D("260")
    assert D(str(after["income"])) == D("5000")
    assert spending_of_categories(db, "personal") == D("260")
    syncer.set_purpose("item-bro-bank", "regular")
    assert D(str(flow(db, "personal")["spending"])) == D("960")

    # Reclassify and undo round-trip.
    def kind(tx_id):
        cur = db.read()
        try:
            return cur.execute("SELECT kind FROM transactions WHERE id = ?", [tx_id]).fetchone()[0]
        finally:
            cur.close()

    before = kind("bro-shop")
    out = reclassify(db, ["bro-shop"], "investment", "owner confirmed", now=datetime(2026, 11, 6, 15, tzinfo=UTC),
                     tz=TZ, daily_cap=1000)  # fmt: skip
    assert kind("bro-shop") == "investment"
    assert D(str(flow(db, "personal")["spending"])) == D("260")
    assert list_changes(db)[0]["change_id"] == out["change_id"]
    assert undo(db, out["change_id"], now=datetime(2026, 11, 6, 16, tzinfo=UTC))["reverted"] == 1
    assert kind("bro-shop") == before
    assert D(str(flow(db, "personal")["spending"])) == D("960")

    # Net worth, forecast and the overview run on the loaded store.
    nw = net_worth(db, TODAY, scope="personal")
    assert D(str(nw["points"][-1]["assets"])) > 0
    fc = cash_forecast(db, TODAY, scope="personal", days=30)
    assert fc["start_balance"] is not None and "lowest" in fc and "negative_on" in fc
    assert "business" in finance_overview(db, TODAY)
