"""Load Pluggy snapshots and merge them into the store, one connection at a time.

Fetching happens without any lock held. The per-alias asyncio.Lock guards the
merge and the disconnect, so a disconnect never waits on network I/O, and a
load that finishes after its connection was removed (or replaced) writes
nothing and reports "discarded".

All timestamps written to the database are naive UTC.
"""

import asyncio
import calendar
import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import duckdb

from cashbook_mcp.analysis.filters import today_in
from cashbook_mcp.classify import classify
from cashbook_mcp.content import prepare_text
from cashbook_mcp.models import (
    PURPOSES,
    ROLES,
    Account,
    Bill,
    Connection,
    Purpose,
    Role,
    Snapshot,
    Transaction,
)
from cashbook_mcp.pluggy.client import PluggyClient, PluggyError
from cashbook_mcp.pluggy.normalize import _doc, normalize_snapshot
from cashbook_mcp.store.db import Database

MEUPLUGGY_CONNECTOR_ID = 200
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ERROR_CHARS = 500

# Date-based tables: removal applies only inside the fetched date range.
_DATE_TABLES = {"transactions": "date", "investment_transactions": "date"}
_ALIAS_TABLES = (
    "transactions",
    "investment_transactions",
    "bills",
    "accounts",
    "investments",
    "loans",
    "sync_runs",
    "balance_snapshots",
    "investment_snapshots",
    "account_settings",
    "classification_overrides",  # the classification_changes journal stays
)
# Columns that the merge never overwrites on conflict; classification owns them.
_KEEP_ON_CONFLICT = {"id", "kind", "kind_reason"}


@dataclass
class SyncResult:
    alias: str
    outcome: Literal["ok", "error", "discarded"]
    upserted: int = 0
    removed: int = 0
    error: str | None = None


@dataclass
class ItemPreview:
    item_id: str
    bank: str
    status: str | None
    accounts: list[dict]
    suggested_role: Role


@dataclass
class DisconnectImpact:
    alias: str
    total_transactions: int
    before_window: int
    oldest_date: date | None
    window_start: date


class ValidationError(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _utc_naive(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _months_back(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _mask(number: str | None) -> str | None:
    if not number:
        return None
    return "****" + re.sub(r"\W", "", number)[-4:]


def suggest_role(accounts: list[dict]) -> Role:
    """business when a checking account belongs to a 14-digit (CNPJ) tax number."""
    for a in accounts:
        digits = re.sub(r"\D", "", _doc(a.get("taxNumber")) or "")
        if a.get("type") == "BANK" and len(digits) == 14:
            return "business"
    return "personal"


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:_ERROR_CHARS]


def _bank_name(item: dict, accounts: list[tuple[Any, Any]]) -> str:
    """The bank behind an item, from (type, name) pairs of its accounts. Every MeuPluggy item has
    the MeuPluggy connector, so the connector name says nothing: the first checking account names
    the bank, else the first account, else the connector."""
    names = [(kind, prepare_text(name)) for kind, name in accounts]
    for kind, name in names:
        if name and kind == "BANK":
            return name
    for _, name in names:
        if name:
            return name
    return prepare_text((item.get("connector") or {}).get("name")) or ""


def _status_fields(item: dict) -> dict[str, Any]:
    error = item.get("error") or {}
    return {
        "status": item.get("status"),
        "execution_status": item.get("executionStatus"),
        "error_code": error.get("code"),
        "error_message": prepare_text(error.get("message")),
        "consent_expires_at": _parse_ts(item.get("consentExpiresAt")),
    }


def _dedup(models: list) -> list:
    """Keep the last occurrence of each id, in first-seen order."""
    by_id: dict[str, Any] = {}
    for m in models:
        by_id.pop(m.id, None)
        by_id[m.id] = m
    return list(by_id.values())


def _from_rows(cur: duckdb.DuckDBPyConnection, model: type, sql: str) -> list:
    names = [f.name for f in dataclasses.fields(model)]
    out = []
    for row in cur.execute(sql.format(cols=", ".join(names))).fetchall():
        values = dict(zip(names, row, strict=True))
        if "suspicion_reasons" in values:
            values["suspicion_reasons"] = tuple(values["suspicion_reasons"] or ())
        out.append(model(**values))
    return out


class Syncer:
    def __init__(
        self,
        db: Database,
        client: PluggyClient,
        *,
        history_months: int,
        tz: ZoneInfo,
        now: Callable[[], datetime],
        tool_names: frozenset[str],
    ) -> None:
        self._db = db
        self._client = client
        self._history_months = history_months
        self._tz = tz
        self._now = now
        self._tool_names = tool_names
        self._locks: dict[str, asyncio.Lock] = {}
        self._started: dict[str, int] = {}
        self._merged: dict[str, int] = {}
        self._completed = False

    # -- helpers -----------------------------------------------------------

    def _lock(self, alias: str) -> asyncio.Lock:
        return self._locks.setdefault(alias, asyncio.Lock())

    def _now_utc(self) -> datetime:
        return _utc_naive(self._now())

    def _window(self) -> tuple[date, date]:
        now = self._now()
        today = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(self._tz).date()
        return _months_back(today, self._history_months), today

    def _connection(self, alias: str) -> Connection | None:
        names = [f.name for f in dataclasses.fields(Connection)]
        cur = self._db.read()
        try:
            row = cur.execute(
                f"SELECT {', '.join(names)} FROM connections WHERE alias = ?", [alias]
            ).fetchone()
        finally:
            cur.close()
        return Connection(*row) if row else None

    async def _fetch_item(self, item_id: str) -> dict:
        """get_item for validation: unknown or foreign items raise ValidationError."""
        try:
            item = await self._client.get_item(item_id)
        except PluggyError as e:
            if e.status in (403, 404):
                raise ValidationError("item_unknown") from None
            raise
        if (item.get("connector") or {}).get("id") != MEUPLUGGY_CONNECTOR_ID:
            raise ValidationError("item_not_meupluggy")
        return item

    async def _fetch(self, alias: str, item_id: str, start: date, end: date) -> Snapshot:
        c = self._client
        item = await c.get_item(item_id)
        accounts = await c.list_accounts(item_id)
        transactions: dict[str, list[dict]] = {}
        bills: dict[str, list[dict]] = {}
        for a in accounts:
            transactions[a["id"]] = await c.list_transactions(a["id"], start, end)
            if a.get("type") == "CREDIT":
                bills[a["id"]] = await c.list_bills(a["id"])
        investments = await c.list_investments(item_id)
        inv_txs: dict[str, list[dict]] = {}
        for inv in investments:
            rows = await c.list_investment_transactions(inv["id"])
            # The API has no date filter here; keep the window only.
            inv_txs[inv["id"]] = [
                r for r in rows if start <= date.fromisoformat(str(r["date"])[:10]) <= end
            ]
        loans = await c.list_loans(item_id)
        return normalize_snapshot(
            alias, item, accounts, transactions, bills, investments, inv_txs, loans,
            start, end, self._tool_names,
        )

    # -- load --------------------------------------------------------------

    async def load(self, alias: str) -> SyncResult:
        conn = self._connection(alias)
        if conn is None:
            return SyncResult(alias, "discarded")
        seq = self._started[alias] = self._started.get(alias, 0) + 1
        started_at = self._now_utc()
        start, end = self._window()

        snapshot: Snapshot | None = None
        error: str | None = None
        try:
            snapshot = await self._fetch(alias, conn.item_id, start, end)
        except Exception as e:  # noqa: BLE001 - any fetch failure is recorded, never raised
            error = _error_text(e)

        async with self._lock(alias):
            current = self._connection(alias)
            if (
                current is None
                or current.item_id != conn.item_id
                or self._merged.get(alias, 0) > seq
            ):
                return SyncResult(alias, "discarded")
            if snapshot is not None:
                try:
                    with self._db.write() as cur:
                        upserted, removed = self._merge(cur, snapshot, started_at)
                    self._merged[alias] = seq
                    self._completed = True
                    return SyncResult(alias, "ok", upserted, removed)
                except Exception as e:  # noqa: BLE001 - rolled back; recorded on the connection
                    error = _error_text(e)
            assert error is not None
            self._record_error(alias, started_at, error)
            return SyncResult(alias, "error", error=error)

    def _record_error(self, alias: str, started_at: datetime, error: str) -> None:
        finished = self._now_utc()
        with self._db.write() as cur:
            cur.execute(
                "UPDATE connections SET last_sync_error = ? WHERE alias = ?", [error, alias]
            )
            cur.execute(
                "INSERT INTO sync_runs (alias, started_at, finished_at, outcome, error) "
                "VALUES (?, ?, ?, 'error', ?)",
                [alias, started_at, finished, error],
            )

    def _merge(
        self, cur: duckdb.DuckDBPyConnection, snap: Snapshot, started_at: datetime
    ) -> tuple[int, int]:
        now = self._now_utc()
        tables: list[tuple[str, list]] = [
            ("accounts", snap.accounts),
            ("transactions", snap.transactions),
            ("bills", snap.bills),
            ("investments", snap.investments),
            ("investment_transactions", snap.investment_transactions),
            ("loans", snap.loans),
        ]
        upserted = removed = 0
        for table, models in tables:
            models = _dedup(models)
            upserted += len(models)
            self._upsert(cur, table, models, now)
            removed += self._mark_removed(cur, table, snap, [m.id for m in models], now)

        self._reclassify(cur)
        self._write_snapshots(cur, snap)

        status = _status_fields(snap.item)
        cur.execute(
            "UPDATE connections SET status = ?, execution_status = ?, error_code = ?, "
            "error_message = ?, consent_expires_at = ?, last_updated_at = ?, "
            "last_synced_at = ?, last_sync_error = NULL WHERE alias = ?",
            [*status.values(), _parse_ts(snap.item.get("lastUpdatedAt")), now, snap.alias],
        )
        if snap.accounts:  # without accounts the stored name is better than the fallback
            bank = _bank_name(snap.item, [(a.type, a.name) for a in snap.accounts])
            cur.execute("UPDATE connections SET connector_name = ? WHERE alias = ?", [bank, snap.alias])
        cur.execute(
            "INSERT INTO sync_runs (alias, started_at, finished_at, outcome, rows_upserted, "
            "rows_removed) VALUES (?, ?, ?, 'ok', ?, ?)",
            [snap.alias, started_at, now, upserted, removed],
        )
        return upserted, removed

    def _write_snapshots(self, cur: duckdb.DuckDBPyConnection, snap: Snapshot) -> None:
        """One balance row per account and investment for today; a later load replaces it."""
        day = today_in(self._tz, self._now())
        for table, key, models in (
            ("balance_snapshots", "account_id", snap.accounts),
            ("investment_snapshots", "investment_id", snap.investments),
        ):
            models = _dedup(models)
            if models:
                cur.executemany(
                    f"INSERT OR REPLACE INTO {table} (day, {key}, alias, balance, currency) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [[day, m.id, m.alias, m.balance, m.currency] for m in models],
                )

    @staticmethod
    def _upsert(cur: duckdb.DuckDBPyConnection, table: str, models: list, now: datetime) -> None:
        if not models:
            return
        names = [f.name for f in dataclasses.fields(models[0])]
        cols = [*names, "first_seen_at", "last_seen_at", "removed_at"]
        updates = [f"{n} = excluded.{n}" for n in names if n not in _KEEP_ON_CONFLICT]
        updates += ["last_seen_at = excluded.last_seen_at", "removed_at = NULL"]
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT (id) DO UPDATE SET {', '.join(updates)}"
        )
        params = []
        for m in models:
            values = [getattr(m, n) for n in names]
            if isinstance(m, Transaction):
                values[names.index("suspicion_reasons")] = list(m.suspicion_reasons)
            params.append([*values, now, now, None])
        cur.executemany(sql, params)

    @staticmethod
    def _mark_removed(
        cur: duckdb.DuckDBPyConnection,
        table: str,
        snap: Snapshot,
        ids: list[str],
        now: datetime,
    ) -> int:
        sql = (
            f"UPDATE {table} SET removed_at = ? WHERE alias = ? AND removed_at IS NULL "
            "AND NOT list_contains(?::VARCHAR[], id)"
        )
        params: list[Any] = [now, snap.alias, ids]
        date_col = _DATE_TABLES.get(table)
        if date_col:
            sql += f" AND {date_col} BETWEEN ? AND ?"
            params += [snap.fetched_from, snap.fetched_to]
        return cur.execute(sql, params).fetchone()[0]

    @staticmethod
    def _reclassify(cur: duckdb.DuckDBPyConnection) -> None:
        txs = _from_rows(cur, Transaction, "SELECT {cols} FROM transactions WHERE removed_at IS NULL")
        accounts = _from_rows(cur, Account, "SELECT {cols} FROM accounts WHERE removed_at IS NULL")
        bills = _from_rows(cur, Bill, "SELECT {cols} FROM bills WHERE removed_at IS NULL")
        purposes = dict(cur.execute("SELECT account_id, purpose FROM account_settings").fetchall())
        overrides = {
            tid: (kind, change_id)
            for tid, kind, change_id in cur.execute(
                "SELECT o.transaction_id, o.kind, o.change_id FROM classification_overrides o "
                "JOIN transactions t ON t.id = o.transaction_id WHERE t.removed_at IS NULL"
            ).fetchall()
        }
        classified = classify(txs, accounts, bills, purposes=purposes, overrides=overrides)
        changed = [
            [new.kind, new.kind_reason, new.id]
            for old, new in zip(txs, classified, strict=True)
            if (old.kind, old.kind_reason) != (new.kind, new.kind_reason)
        ]
        if changed:
            cur.executemany(
                "UPDATE transactions SET kind = ?, kind_reason = ? WHERE id = ?", changed
            )

    async def load_all(self) -> list[SyncResult]:
        aliases = [c.alias for c in self.connections()]
        results = await asyncio.gather(
            *(self.load(a) for a in aliases), return_exceptions=True
        )
        self._completed = True
        return [
            r if isinstance(r, SyncResult) else SyncResult(a, "error", error=_error_text(r))
            for a, r in zip(aliases, results, strict=True)
        ]

    async def check_updates(self) -> list[str]:
        """Refresh status fields; reload connections whose lastUpdatedAt advanced.

        last_updated_at itself is only written by a successful load, so a reload
        that fails is retried on the next check.
        """
        changed: list[str] = []
        for conn in self.connections():
            try:
                item = await self._client.get_item(conn.item_id)
            except Exception as e:  # noqa: BLE001 - one connection never blocks the others
                with self._db.write() as cur:
                    cur.execute(
                        "UPDATE connections SET last_sync_error = ? "
                        "WHERE alias = ? AND item_id = ?",
                        [_error_text(e), conn.alias, conn.item_id],
                    )
                continue
            with self._db.write() as cur:
                cur.execute(
                    "UPDATE connections SET status = ?, execution_status = ?, error_code = ?, "
                    "error_message = ?, consent_expires_at = ? WHERE alias = ? AND item_id = ?",
                    [*_status_fields(item).values(), conn.alias, conn.item_id],
                )
            updated = _parse_ts(item.get("lastUpdatedAt"))
            if updated is not None and (
                conn.last_updated_at is None or updated > conn.last_updated_at
            ):
                changed.append(conn.alias)
        await asyncio.gather(*(self.load(a) for a in changed))
        return changed

    # -- connections -------------------------------------------------------

    async def preview(self, item_id: str) -> ItemPreview:
        item = await self._fetch_item(item_id)
        accounts = await self._client.list_accounts(item_id)
        return ItemPreview(
            item_id=item_id,
            bank=_bank_name(item, [(a.get("type"), a.get("name")) for a in accounts]),
            status=item.get("status"),
            accounts=[
                {
                    "name": prepare_text(a.get("name")) or "",
                    "type": a.get("type"),
                    "number": _mask(a.get("number")),
                }
                for a in accounts
            ],
            suggested_role=suggest_role(accounts),
        )

    async def add_connection(
        self, alias: str, item_id: str, *, role: Role | None = None, load: bool = True
    ) -> Connection:
        """Validate and store a connection, then run its first load (unless load=False, for a
        caller that starts the load in the background itself). role=None suggests one from the
        accounts."""
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            raise ValidationError("alias_invalid")
        if role is not None and role not in ROLES:
            raise ValidationError("role_invalid")
        self._check_free(alias, item_id)
        item = await self._fetch_item(item_id)
        try:
            accounts = await self._client.list_accounts(item_id)
        except PluggyError:  # only the bank name needs them, and the first load sets it again
            accounts = []
        if role is None:
            role = suggest_role(accounts)
        now = self._now_utc()
        with self._db.write() as cur:
            # Re-checked inside the write: another add may have won meanwhile.
            self._check_free(alias, item_id, cur)
            status = _status_fields(item)
            cur.execute(
                "INSERT INTO connections (alias, item_id, connector_name, created_at, status, "
                "execution_status, error_code, error_message, consent_expires_at, role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    alias,
                    item_id,
                    _bank_name(item, [(a.get("type"), a.get("name")) for a in accounts]),
                    now,
                    *status.values(),
                    role,
                ],
            )
        if load:
            await self.load(alias)
        conn = self._connection(alias)
        if conn is None:  # disconnected while loading
            raise LookupError(f"connection {alias!r} was removed during its first load")
        return conn

    def _check_free(
        self, alias: str, item_id: str, cur: duckdb.DuckDBPyConnection | None = None
    ) -> None:
        own = cur is None
        cur = cur or self._db.read()
        try:
            if cur.execute("SELECT 1 FROM connections WHERE alias = ?", [alias]).fetchone():
                raise ValidationError("alias_taken")
            if cur.execute("SELECT 1 FROM connections WHERE item_id = ?", [item_id]).fetchone():
                raise ValidationError("item_taken")
        finally:
            if own:
                cur.close()

    def set_role(self, alias: str, role: Role) -> None:
        if role not in ROLES:
            raise ValidationError("role_invalid")
        with self._db.write() as cur:
            if not cur.execute("SELECT 1 FROM connections WHERE alias = ?", [alias]).fetchone():
                raise LookupError(f"unknown connection {alias!r}")
            cur.execute("UPDATE connections SET role = ? WHERE alias = ?", [role, alias])

    def set_purpose(self, account_id: str, purpose: Purpose) -> None:
        """Mark a bank account as regular or investment, then reclassify in the same write."""
        if purpose not in PURPOSES:
            raise ValidationError("purpose_invalid")
        with self._db.write() as cur:
            row = cur.execute(
                "SELECT alias, type FROM accounts WHERE id = ? AND removed_at IS NULL", [account_id]
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown account {account_id!r}")
            if row[1] != "BANK":
                raise ValidationError("purpose_invalid")  # only bank accounts have a purpose
            if purpose == "regular":
                cur.execute("DELETE FROM account_settings WHERE account_id = ?", [account_id])
            else:
                cur.execute(
                    "INSERT OR REPLACE INTO account_settings (account_id, alias, purpose) "
                    "VALUES (?, ?, ?)",
                    [account_id, row[0], purpose],
                )
            self._reclassify(cur)

    async def disconnect(self, alias: str) -> None:
        async with self._lock(alias):
            with self._db.write() as cur:
                for table in _ALIAS_TABLES:
                    cur.execute(f"DELETE FROM {table} WHERE alias = ?", [alias])
                cur.execute("DELETE FROM connections WHERE alias = ?", [alias])
                # Rows of other connections may point at deleted ones (paired_with, matched bills).
                self._reclassify(cur)

    def disconnect_impact(self, alias: str) -> DisconnectImpact:
        start, _ = self._window()
        cur = self._db.read()
        try:
            total, before, oldest = cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE date < ?), min(date) "
                "FROM v_transactions WHERE alias = ?",
                [start, alias],
            ).fetchone()
        finally:
            cur.close()
        return DisconnectImpact(alias, total, before, oldest, start)

    def connections(self) -> list[Connection]:
        names = [f.name for f in dataclasses.fields(Connection)]
        cur = self._db.read()
        try:
            rows = cur.execute(
                f"SELECT {', '.join(names)} FROM connections ORDER BY alias"
            ).fetchall()
        finally:
            cur.close()
        return [Connection(*r) for r in rows]

    def has_completed_load(self) -> bool:
        if self._completed:
            return True
        cur = self._db.read()
        try:
            row = cur.execute("SELECT 1 FROM sync_runs WHERE outcome = 'ok' LIMIT 1").fetchone()
        finally:
            cur.close()
        return row is not None

