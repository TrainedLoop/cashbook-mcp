import asyncio
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from cashbook_mcp.pluggy.client import PluggyError
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.sync import (
    MEUPLUGGY_CONNECTOR_ID,
    Syncer,
    ValidationError,
)

KEY = "ab" * 32
TZ = ZoneInfo("America/Sao_Paulo")
TABLES = (
    "accounts",
    "transactions",
    "bills",
    "investments",
    "investment_transactions",
    "loans",
    "sync_runs",
    "connections",
)


def iso(d: str) -> str:
    return f"{d}T00:00:00.000Z"


class FakeClient:
    """In-memory stand-in with the PluggyClient method names."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.accounts: dict[str, list[dict]] = {}
        self.transactions: dict[str, list[dict]] = {}
        self.bills: dict[str, list[dict]] = {}
        self.investments: dict[str, list[dict]] = {}
        self.inv_txs: dict[str, list[dict]] = {}
        self.loans: dict[str, list[dict]] = {}
        self.fail: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    def add_item(self, item_id: str, *, connector_id: int = MEUPLUGGY_CONNECTOR_ID) -> None:
        self.items[item_id] = {
            "id": item_id,
            "connector": {"id": connector_id, "name": f"Bank {item_id}"},
            "status": "UPDATED",
            "executionStatus": "SUCCESS",
            "error": None,
            "lastUpdatedAt": "2026-11-05T09:00:00.000Z",
            "consentExpiresAt": "2027-01-10T12:00:00.000Z",
        }
        bank, card = f"{item_id}-bank", f"{item_id}-card"
        self.accounts[item_id] = [
            {
                "id": bank,
                "itemId": item_id,
                "type": "BANK",
                "subtype": "CHECKING_ACCOUNT",
                "name": "Checking",
                "number": "0001-12345",
                "balance": 100.0,
                "currencyCode": "BRL",
                "taxNumber": "11111111111",
            },
            {
                "id": card,
                "itemId": item_id,
                "type": "CREDIT",
                "subtype": "CREDIT_CARD",
                "name": "Card",
                "number": "4321",
                "balance": -50.0,
                "currencyCode": "BRL",
                "creditData": {"creditLimit": 1000.0, "availableCreditLimit": 950.0},
            },
        ]
        self.transactions[bank] = []
        self.transactions[card] = []
        self.bills[card] = [
            {"id": f"{item_id}-bill-1", "dueDate": "2026-11-12", "totalAmount": 50.0}
        ]
        self.investments[item_id] = [
            {"id": f"{item_id}-inv-1", "itemId": item_id, "type": "FIXED_INCOME",
             "name": "CDB", "balance": 1000.0}
        ]
        self.inv_txs[f"{item_id}-inv-1"] = []
        self.loans[item_id] = [
            {"id": f"{item_id}-loan-1", "itemId": item_id, "productName": "Loan"}
        ]

    def add_tx(self, account_id: str, tx_id: str, day: str, amount: float = -10.0) -> None:
        self.transactions[account_id].append(
            {"id": tx_id, "date": iso(day), "amount": amount,
             "description": f"Purchase {tx_id}", "currencyCode": "BRL"}
        )

    async def get_item(self, item_id: str) -> dict:
        self.calls.append(("get_item", item_id))
        if self.gate is not None:
            self.entered.set()
            await self.gate.wait()
        if item_id not in self.items:
            raise PluggyError("not found", status=404)
        return dict(self.items[item_id])

    async def list_accounts(self, item_id: str) -> list[dict]:
        self.calls.append(("list_accounts", item_id))
        if item_id in self.fail:
            raise PluggyError("boom", status=500)
        return list(self.accounts[item_id])

    async def list_transactions(self, account_id, date_from: date, date_to: date) -> list[dict]:
        return [
            t for t in self.transactions[account_id]
            if date_from <= date.fromisoformat(t["date"][:10]) <= date_to
        ]

    async def list_bills(self, account_id: str) -> list[dict]:
        return list(self.bills.get(account_id, []))

    async def list_investments(self, item_id: str) -> list[dict]:
        return list(self.investments[item_id])

    async def list_investment_transactions(self, investment_id: str) -> list[dict]:
        return list(self.inv_txs[investment_id])

    async def list_loans(self, item_id: str) -> list[dict]:
        return list(self.loans[item_id])


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "cashbook.duckdb", KEY)
    yield database
    database.close()


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def syncer(db, fake, clock):
    return Syncer(
        db,
        fake,
        history_months=12,
        tz=TZ,
        now=clock,
        tool_names=frozenset({"search_transactions"}),
    )


def rows(db, sql, params=()):
    cur = db.read()
    try:
        return cur.execute(sql, list(params)).fetchall()
    finally:
        cur.close()


def alias_counts(db, alias):
    return {t: rows(db, f"SELECT count(*) FROM {t} WHERE alias = ?", [alias])[0][0] for t in TABLES}


async def test_first_load_inserts(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-1", "2026-10-01")
    fake.add_tx("item-a-card", "tx-2", "2026-10-02", amount=25.0)
    fake.inv_txs["item-a-inv-1"] = [
        {"id": "itx-in", "date": iso("2026-09-01"), "type": "BUY", "amount": 100.0},
        {"id": "itx-old", "date": iso("2020-01-01"), "type": "BUY", "amount": 5.0},
    ]

    conn = await syncer.add_connection("a", "item-a")

    assert conn.alias == "a" and conn.item_id == "item-a"
    assert conn.connector_name == "Checking"
    assert conn.status == "UPDATED"
    assert conn.last_updated_at == datetime(2026, 11, 5, 9, 0)
    assert conn.last_synced_at == datetime(2026, 11, 6, 15, 0)
    assert conn.last_sync_error is None
    counts = alias_counts(db, "a")
    assert counts == {
        "accounts": 2,
        "transactions": 2,
        "bills": 1,
        "investments": 1,
        "investment_transactions": 1,  # the 2020 row is outside the window
        "loans": 1,
        "sync_runs": 1,
        "connections": 1,
    }
    tx = rows(db, "SELECT amount, kind, first_seen_at, removed_at FROM transactions "
                  "WHERE id = 'tx-2'")[0]
    assert float(tx[0]) == -25.0  # card sign flipped by normalize
    assert tx[1] == "purchase"
    assert tx[2] == datetime(2026, 11, 6, 15, 0)
    assert tx[3] is None
    run = rows(db, "SELECT outcome, rows_upserted, rows_removed FROM sync_runs")[0]
    assert run == ("ok", 8, 0)
    assert syncer.has_completed_load()


async def test_reload_marks_removed_inside_window_only(syncer, fake, db, clock):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-old", "2025-12-01")
    fake.add_tx("item-a-bank", "tx-gone", "2026-10-01")
    fake.add_tx("item-a-bank", "tx-kept", "2026-10-02")
    await syncer.add_connection("a", "item-a")

    clock.value = datetime(2027, 1, 10, 15, 0, tzinfo=UTC)  # window starts 2026-01-10
    fake.transactions["item-a-bank"] = [
        t for t in fake.transactions["item-a-bank"] if t["id"] != "tx-gone"
    ]
    fake.loans["item-a"] = []
    result = await syncer.load("a")

    assert result.outcome == "ok"
    assert result.removed == 2  # tx-gone and the loan
    got = dict(rows(db, "SELECT id, removed_at FROM transactions"))
    assert got["tx-old"] is None
    assert got["tx-kept"] is None
    assert got["tx-gone"] == datetime(2027, 1, 10, 15, 0)
    kept = rows(db, "SELECT first_seen_at, last_seen_at FROM transactions WHERE id='tx-kept'")
    assert kept == [(datetime(2026, 11, 6, 15, 0), datetime(2027, 1, 10, 15, 0))]
    assert rows(db, "SELECT removed_at FROM loans") == [(datetime(2027, 1, 10, 15, 0),)]

    # A row that comes back is revived.
    fake.add_tx("item-a-bank", "tx-gone", "2026-10-01")
    await syncer.load("a")
    assert rows(db, "SELECT removed_at FROM transactions WHERE id='tx-gone'") == [(None,)]


async def test_duplicate_ids_single_row(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-dup", "2026-10-01", amount=-1.0)
    fake.add_tx("item-a-bank", "tx-dup", "2026-10-01", amount=-2.0)
    await syncer.add_connection("a", "item-a")

    assert rows(db, "SELECT id, amount FROM transactions") == [("tx-dup", pytest.approx(-2.0))]


async def test_failed_fetch_keeps_previous_data(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-1", "2026-10-01")
    await syncer.add_connection("a", "item-a")
    before = alias_counts(db, "a")

    fake.fail.add("item-a")
    fake.transactions["item-a-bank"] = []
    result = await syncer.load("a")

    assert result.outcome == "error"
    assert result.error and "boom" in result.error
    assert rows(db, "SELECT removed_at FROM transactions") == [(None,)]
    after = alias_counts(db, "a")
    assert after["sync_runs"] == before["sync_runs"] + 1
    assert {k: v for k, v in after.items() if k != "sync_runs"} == {
        k: v for k, v in before.items() if k != "sync_runs"
    }
    (conn,) = syncer.connections()
    assert conn.last_sync_error and "boom" in conn.last_sync_error
    assert conn.last_synced_at == datetime(2026, 11, 6, 15, 0)
    last = rows(db, "SELECT outcome, error FROM sync_runs ORDER BY id DESC LIMIT 1")[0]
    assert last[0] == "error" and "boom" in last[1]


async def test_one_alias_failure_isolated(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_item("item-b")
    fake.add_tx("item-a-bank", "tx-a", "2026-10-01")
    fake.add_tx("item-b-bank", "tx-b", "2026-10-01")
    fake.fail.add("item-b")
    await syncer.add_connection("a", "item-a")
    await syncer.add_connection("b", "item-b")

    results = {r.alias: r for r in await syncer.load_all()}

    assert results["a"].outcome == "ok"
    assert results["b"].outcome == "error"
    assert alias_counts(db, "a")["transactions"] == 1
    assert alias_counts(db, "b")["transactions"] == 0


async def test_disconnect_deletes_all_alias_rows(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_item("item-b")
    fake.add_tx("item-a-bank", "tx-a", "2026-10-01")
    fake.add_tx("item-b-bank", "tx-b", "2026-10-01")
    fake.inv_txs["item-a-inv-1"] = [
        {"id": "itx-a", "date": iso("2026-09-01"), "type": "BUY", "amount": 1.0}
    ]
    await syncer.add_connection("a", "item-a")
    await syncer.add_connection("b", "item-b")

    await syncer.disconnect("a")

    assert set(alias_counts(db, "a").values()) == {0}
    assert alias_counts(db, "b")["transactions"] == 1
    assert [c.alias for c in syncer.connections()] == ["b"]


async def test_disconnect_during_load_discards(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-a", "2026-10-01")
    await syncer.add_connection("a", "item-a")

    fake.gate = asyncio.Event()
    task = asyncio.create_task(syncer.load("a"))
    await asyncio.wait_for(fake.entered.wait(), 1)
    await asyncio.wait_for(syncer.disconnect("a"), 1)
    fake.gate.set()
    result = await asyncio.wait_for(task, 1)

    assert result.outcome == "discarded"
    assert set(alias_counts(db, "a").values()) == {0}


async def test_add_connection_validation(syncer, fake):
    fake.add_item("item-a")
    fake.add_item("item-other", connector_id=201)
    fake.add_item("item-c")

    async def code(alias, item_id):
        with pytest.raises(ValidationError) as exc:
            await syncer.add_connection(alias, item_id)
        return exc.value.code

    assert await code("Bad Alias", "item-a") == "alias_invalid"
    assert await code("-a", "item-a") == "alias_invalid"
    assert await code("a" * 33, "item-a") == "alias_invalid"
    assert await code("x", "item-missing") == "item_unknown"
    assert await code("x", "item-other") == "item_not_meupluggy"

    await syncer.add_connection("a", "item-a")
    assert await code("a", "item-c") == "alias_taken"
    assert await code("z", "item-a") == "item_taken"
    assert [c.alias for c in syncer.connections()] == ["a"]


async def test_add_connection_forbidden_item_is_unknown(syncer, fake):
    async def forbidden(item_id):
        raise PluggyError("forbidden", status=403)

    fake.get_item = forbidden
    with pytest.raises(ValidationError) as exc:
        await syncer.add_connection("x", "item-a")
    assert exc.value.code == "item_unknown"


async def test_disconnect_impact_counts_before_window(syncer, fake, clock):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-old", "2025-12-01")
    fake.add_tx("item-a-bank", "tx-new", "2026-10-01")
    await syncer.add_connection("a", "item-a")

    clock.value = datetime(2027, 1, 10, 15, 0, tzinfo=UTC)
    impact = syncer.disconnect_impact("a")

    assert impact.alias == "a"
    assert impact.total_transactions == 2
    assert impact.before_window == 1
    assert impact.oldest_date == date(2025, 12, 1)
    assert impact.window_start == date(2026, 1, 10)


async def test_check_updates_reloads_only_changed(syncer, fake):
    fake.add_item("item-a")
    fake.add_item("item-b")
    await syncer.add_connection("a", "item-a")
    await syncer.add_connection("b", "item-b")
    fake.calls.clear()

    fake.items["item-a"]["status"] = "LOGIN_ERROR"
    fake.items["item-a"]["error"] = {"code": "INVALID_CREDENTIALS", "message": "bad"}
    fake.items["item-b"]["lastUpdatedAt"] = "2026-11-06T10:00:00.000Z"

    reloaded = await syncer.check_updates()

    assert reloaded == ["b"]
    assert ("list_accounts", "item-a") not in fake.calls
    assert ("list_accounts", "item-b") in fake.calls
    conns = {c.alias: c for c in syncer.connections()}
    assert conns["a"].status == "LOGIN_ERROR"
    assert conns["a"].error_code == "INVALID_CREDENTIALS"
    assert conns["b"].last_updated_at == datetime(2026, 11, 6, 10, 0)


async def test_preview_masks_numbers(syncer, fake):
    fake.add_item("item-a")

    p = await syncer.preview("item-a")

    assert p.item_id == "item-a"
    assert p.bank == "Checking"
    assert p.status == "UPDATED"
    assert p.accounts == [
        {"name": "Checking", "type": "BANK", "number": "****2345"},
        {"name": "Card", "type": "CREDIT", "number": "****4321"},
    ]


async def test_bank_name_comes_from_the_accounts(syncer, fake):
    fake.add_item("item-a")
    fake.accounts["item-a"].reverse()  # the card first: the checking account still names the bank
    assert (await syncer.preview("item-a")).bank == "Checking"
    fake.accounts["item-a"] = [a for a in fake.accounts["item-a"] if a["type"] == "CREDIT"]
    assert (await syncer.preview("item-a")).bank == "Card"
    fake.accounts["item-a"] = []
    assert (await syncer.preview("item-a")).bank == "Bank item-a"


async def test_load_refreshes_the_bank_name_but_keeps_it_without_accounts(syncer, fake, db):
    fake.add_item("item-a")
    await syncer.add_connection("a", "item-a")
    fake.accounts["item-a"][0]["name"] = "Renamed Bank"
    await syncer.load("a")
    assert syncer.connections()[0].connector_name == "Renamed Bank"
    fake.accounts["item-a"] = []
    await syncer.load("a")
    assert syncer.connections()[0].connector_name == "Renamed Bank"


async def test_add_connection_without_load_stores_only_the_connection(syncer, fake, db):
    fake.add_item("item-a")
    fake.add_tx("item-a-bank", "tx-1", "2026-10-01")
    conn = await syncer.add_connection("a", "item-a", load=False)

    assert conn.alias == "a" and conn.last_synced_at is None
    assert fake.calls == [("get_item", "item-a"), ("list_accounts", "item-a")]  # no data fetched yet
    assert conn.connector_name == "Checking"
    assert alias_counts(db, "a")["transactions"] == 0
    assert alias_counts(db, "a")["connections"] == 1
    assert (await syncer.load("a")).outcome == "ok"
    assert alias_counts(db, "a")["transactions"] == 1
