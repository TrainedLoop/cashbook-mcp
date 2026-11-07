"""Agent-driven reclassification: a journal of batches plus override rows per transaction.

A batch sets a kind, a category or both. Kinds and categories live in separate override tables, so a batch
that changes one never erases the other. The override tables hold the live state; the journal makes every
batch auditable and undoable. A newer batch replaces an older override of the same transaction and field,
and undoing a batch only removes overrides that still point to it. A category override only relabels the
transaction in groupings and filters (v_flows.pluggy_category); it never changes the kind or the totals.
Timestamps in the store are naive UTC.
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
# Categories the provider rarely sends but an override may use, besides those already in the store.
EXTRA_CATEGORIES = ("Rent", "Housing", "Utilities", "Education", "Childcare")


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


def _valid_categories(cur) -> dict[str, str]:
    """{lowercase: canonical spelling}: provider categories in the store, then EXTRA_CATEGORIES."""
    valid: dict[str, str] = {}
    stored = cur.execute(
        "SELECT DISTINCT pluggy_category FROM transactions "
        "WHERE removed_at IS NULL AND trim(pluggy_category) <> '' ORDER BY 1"
    ).fetchall()
    for name in [r[0] for r in stored] + list(EXTRA_CATEGORIES):
        valid.setdefault(name.lower(), name)
    return valid


def reclassify(
    db: Database,
    ids: list[str],
    kind: str | None,
    note: str | None,
    *,
    category: str | None = None,
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
    if kind is None and category is None:
        raise ReclassifyError("nothing_to_change", "give a kind, a category or both")
    if kind is not None and kind not in KINDS:
        raise ReclassifyError("bad_kind", f"kind must be one of: {', '.join(KINDS)}", kinds=list(KINDS))
    ids = list(dict.fromkeys(ids))
    note = _prepare_note(note, tool_names)
    change_id = secrets.token_urlsafe(9)
    created = _naive_utc(now)

    with db.write() as cur:
        marks = ", ".join("?" for _ in ids)
        if category is not None:
            valid = _valid_categories(cur)
            if not category.strip() or category.lower() not in valid:
                raise ReclassifyError(
                    "bad_category",
                    "category must be one of valid_categories; nothing was changed",
                    valid_categories=sorted(valid.values()),
                )
            category = valid[category.lower()]
        live = {
            tid: (alias, current, original)
            for tid, alias, current, original in cur.execute(
                "SELECT id, alias, kind, pluggy_category FROM transactions "
                f"WHERE removed_at IS NULL AND id IN ({marks})",
                ids,
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
        kinds = dict(
            cur.execute(
                f"SELECT transaction_id, kind FROM classification_overrides WHERE transaction_id IN ({marks})",
                ids,
            ).fetchall()
        )
        categories = dict(
            cur.execute(
                f"SELECT transaction_id, category FROM category_overrides WHERE transaction_id IN ({marks})",
                ids,
            ).fetchall()
        )
        # Only the fields that actually change are written, and a transaction counts once.
        kind_todo = [i for i in ids if kind is not None and kinds.get(i) != kind]
        cat_todo = [
            i for i in ids if category is not None and categories.get(i, live[i][2]) != category
        ]
        todo = [i for i in ids if i in kind_todo or i in cat_todo]
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
            "INSERT INTO classification_changes (change_id, created_at, kind, category, note, count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [change_id, created, kind, category, note, len(todo)],
        )
        if kind_todo:
            cur.executemany(
                "INSERT INTO classification_overrides (transaction_id, alias, kind, change_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (transaction_id) DO UPDATE SET alias = excluded.alias, kind = excluded.kind, "
                "change_id = excluded.change_id",
                [[i, live[i][0], kind, change_id] for i in kind_todo],
            )
        # Back to the provider's category: drop the override instead of storing one equal to it.
        restored = [i for i in cat_todo if live[i][2] == category]
        cat_todo = [i for i in cat_todo if i not in restored]
        if restored:
            cur.executemany("DELETE FROM category_overrides WHERE transaction_id = ?", [[i] for i in restored])
        if cat_todo:
            cur.executemany(
                "INSERT INTO category_overrides (transaction_id, alias, category, change_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (transaction_id) DO UPDATE SET alias = excluded.alias, "
                "category = excluded.category, change_id = excluded.change_id",
                [[i, live[i][0], category, change_id] for i in cat_todo],
            )
        Syncer._reclassify(cur)
        changes = []
        for i in todo:
            change: dict = {"id": i}
            if kind is not None:
                change |= {"old_kind": live[i][1], "new_kind": kind}
            if category is not None:
                change |= {"old_category": categories.get(i, live[i][2]), "new_category": category}
            changes.append(change)
        return {"change_id": change_id, "count": len(todo), "changes": changes}


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
            "SELECT count(DISTINCT transaction_id) FROM ("
            "SELECT transaction_id FROM classification_overrides WHERE change_id = ? "
            "UNION ALL SELECT transaction_id FROM category_overrides WHERE change_id = ?)",
            [change_id, change_id],
        ).fetchone()[0]
        cur.execute("DELETE FROM classification_overrides WHERE change_id = ?", [change_id])
        cur.execute("DELETE FROM category_overrides WHERE change_id = ?", [change_id])
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
            "SELECT change_id, created_at, kind, category, note, count, undone_at FROM classification_changes "
            "ORDER BY created_at DESC, change_id LIMIT ?",
            [limit],
        ).fetchall()
    finally:
        cur.close()
    names = ("change_id", "created_at", "kind", "category", "note", "count", "undone_at")
    return [dict(zip(names, r, strict=True)) for r in rows]
