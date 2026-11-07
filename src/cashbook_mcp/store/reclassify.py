"""Agent-driven reclassification: a journal of batches plus one override row per transaction.

The override table holds the live state; the journal makes every batch auditable and undoable. A newer
batch replaces an older override of the same transaction, and undoing a batch only removes overrides that
still point to it. Timestamps in the store are naive UTC.
"""

import secrets
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cashbook_mcp.content import prepare_text, suspicion_reasons, wrap_suspicious
from cashbook_mcp.models import KINDS
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.sync import Syncer

MAX_IDS_PER_CALL = 200
MAX_NOTE_CHARS = 200
MAX_ID_CHARS = 64
MAX_UNKNOWN_LISTED = 20
MAX_LIST_LIMIT = 100


class ReclassifyError(Exception):
    """A rejected call; `code` is machine-readable and `details` carries the facts the agent needs."""

    def __init__(self, code: str, message: str, **details) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The local calendar day containing `now`, as naive-UTC [start, end)."""
    local = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(tz).date()
    start = datetime.combine(local, time.min, tzinfo=tz)
    end = datetime.combine(local + timedelta(days=1), time.min, tzinfo=tz)
    return _naive_utc(start), _naive_utc(end)


def _prepare_note(note: str | None, tool_names: frozenset[str]) -> str | None:
    """Agent-written text is stored and echoed later, so it gets the same treatment as bank text."""
    text = (prepare_text(note) or "")[:MAX_NOTE_CHARS].strip()
    if not text:
        return None
    return wrap_suspicious(text) if suspicion_reasons([text], tool_names) else text


def reclassify(
    db: Database,
    ids: list[str],
    kind: str,
    note: str | None,
    *,
    now: datetime,
    tz: ZoneInfo,
    daily_cap: int,
    tool_names: frozenset[str] = frozenset(),
) -> dict:
    if not ids:
        raise ReclassifyError("empty", "ids must list at least one transaction id")
    if len(ids) > MAX_IDS_PER_CALL:
        raise ReclassifyError(
            "too_many", f"at most {MAX_IDS_PER_CALL} ids per call, got {len(ids)}", limit=MAX_IDS_PER_CALL
        )
    if kind not in KINDS:
        raise ReclassifyError("bad_kind", f"kind must be one of: {', '.join(KINDS)}", kinds=list(KINDS))
    ids = list(dict.fromkeys(ids))
    note = _prepare_note(note, tool_names)
    change_id = secrets.token_urlsafe(9)
    created = _naive_utc(now)

    with db.write() as cur:
        marks = ", ".join("?" for _ in ids)
        live = {
            tid: (alias, current)
            for tid, alias, current in cur.execute(
                f"SELECT id, alias, kind FROM transactions WHERE removed_at IS NULL AND id IN ({marks})", ids
            ).fetchall()
        }
        unknown = [i for i in ids if len(i) > MAX_ID_CHARS or i not in live]
        if unknown:
            raise ReclassifyError(
                "unknown_ids",
                "some ids are not live transactions; nothing was changed",
                unknown_ids=[i[:MAX_ID_CHARS] for i in unknown[:MAX_UNKNOWN_LISTED]],
                unknown_count=len(unknown),
            )
        existing = dict(
            cur.execute(
                f"SELECT transaction_id, kind FROM classification_overrides WHERE transaction_id IN ({marks})",
                ids,
            ).fetchall()
        )
        todo = [i for i in ids if existing.get(i) != kind]
        if not todo:
            return {"change_id": None, "count": 0, "changes": []}

        start, end = _day_bounds(now, tz)
        used = cur.execute(
            "SELECT COALESCE(sum(count), 0) FROM classification_changes WHERE created_at >= ? AND created_at < ?",
            [start, end],
        ).fetchone()[0]
        if used + len(todo) > daily_cap:
            raise ReclassifyError(
                "daily_cap",
                f"daily limit of {daily_cap} reclassified transactions would be exceeded; nothing was changed",
                cap=daily_cap,
                used=int(used),
                requested=len(todo),
                remaining=max(daily_cap - int(used), 0),
            )

        cur.execute(
            "INSERT INTO classification_changes (change_id, created_at, kind, note, count) VALUES (?, ?, ?, ?, ?)",
            [change_id, created, kind, note, len(todo)],
        )
        cur.executemany(
            "INSERT INTO classification_overrides (transaction_id, alias, kind, change_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (transaction_id) DO UPDATE SET alias = excluded.alias, kind = excluded.kind, "
            "change_id = excluded.change_id",
            [[i, live[i][0], kind, change_id] for i in todo],
        )
        Syncer._reclassify(cur)
        return {
            "change_id": change_id,
            "count": len(todo),
            "changes": [{"id": i, "old_kind": live[i][1], "new_kind": kind} for i in todo],
        }


def undo(db: Database, change_id: str, *, now: datetime) -> dict:
    with db.write() as cur:
        row = cur.execute(
            "SELECT undone_at FROM classification_changes WHERE change_id = ?", [change_id]
        ).fetchone()
        if row is None:
            raise ReclassifyError("unknown_change", "no such change_id", change_id=change_id)
        if row[0] is not None:
            raise ReclassifyError("already_undone", "this change was already undone", change_id=change_id)
        reverted = cur.execute(
            "SELECT count(*) FROM classification_overrides WHERE change_id = ?", [change_id]
        ).fetchone()[0]
        cur.execute("DELETE FROM classification_overrides WHERE change_id = ?", [change_id])
        cur.execute(
            "UPDATE classification_changes SET undone_at = ? WHERE change_id = ?", [_naive_utc(now), change_id]
        )
        Syncer._reclassify(cur)
    return {"change_id": change_id, "reverted": int(reverted)}


def list_changes(db: Database, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    cur = db.read()
    try:
        rows = cur.execute(
            "SELECT change_id, created_at, kind, note, count, undone_at FROM classification_changes "
            "ORDER BY created_at DESC, change_id LIMIT ?",
            [limit],
        ).fetchall()
    finally:
        cur.close()
    names = ("change_id", "created_at", "kind", "note", "count", "undone_at")
    return [dict(zip(names, r, strict=True)) for r in rows]
