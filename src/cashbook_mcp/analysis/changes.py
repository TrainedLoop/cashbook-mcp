"""Transactions that changed since a position, for routines that resume where they stopped.

A row counts as changed when it was inserted, its content changed, it was marked removed, or a
reclassification touched it (transactions.changed_at). Timestamps in the store are naive UTC.
"""

import base64
from datetime import datetime, timedelta

from cashbook_mcp.store.db import Database

from .filters import jsonable
from .metrics import Scope, scope_clause
from .search import COLUMNS, compact_row

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
DEFAULT_DAYS_BACK = 7

# Removed rows are read from `transactions`: no flow, and the role comes from the connection.
_REMOVED_EXPR = {
    "category_overridden": "false",
    "role": "c.role",
    "flow": "NULL",
}


def encode_cursor(changed_at: datetime, tx_id: str) -> str:
    return base64.urlsafe_b64encode(f"{changed_at.isoformat()}|{tx_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        stamp, tx_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(stamp).replace(tzinfo=None), tx_id
    except (ValueError, TypeError) as e:  # UnicodeDecodeError and binascii.Error are ValueErrors
        raise ValueError("invalid cursor") from e


def _change(first_seen_at: datetime, changed_at: datetime, removed_at: datetime | None) -> str:
    if removed_at is not None:
        return "removed"
    return "new" if first_seen_at == changed_at else "updated"


def list_changes(
    db: Database,
    now: datetime,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    scope: Scope | None = None,
) -> dict:
    """Changed rows after the cursor (default: 7 days back), oldest change first.

    `next_cursor` is always returned: after the last row, or the starting position when nothing
    changed, so a routine can store it and resume.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    if cursor:
        start = decode_cursor(cursor)
    else:
        start = (now.replace(tzinfo=None) - timedelta(days=DEFAULT_DAYS_BACK), "")
    stamp = "coalesce(t.changed_at, t.first_seen_at)"
    clauses = [f"({stamp} > ? OR ({stamp} = ? AND t.id > ?))"]
    params: list = [start[0], start[0], start[1]]
    if scope is not None:
        clause, cparams = scope_clause(scope)
        if clause:
            clauses.append(f"c.{clause}")
            params.extend(cparams)
    keys_sql = (
        "SELECT t.id, coalesce(t.changed_at, t.first_seen_at), t.first_seen_at, t.removed_at "
        "FROM transactions t JOIN connections c ON c.alias = t.alias "
        f"WHERE {' AND '.join(clauses)} ORDER BY 2, t.id LIMIT ?"
    )
    cur = db.read()
    try:
        keys = cur.execute(keys_sql, [*params, limit + 1]).fetchall()
        page = keys[:limit]
        by_id: dict[str, dict] = {}
        live = [k[0] for k in page if k[3] is None]
        removed = [k[0] for k in page if k[3] is not None]
        if live:
            marks = ", ".join("?" * len(live))
            for row in cur.execute(
                f"SELECT {', '.join(COLUMNS)} FROM v_flows WHERE id IN ({marks})", live
            ).fetchall():
                by_id[row[0]] = dict(zip(COLUMNS, row, strict=True))
        if removed:
            marks = ", ".join("?" * len(removed))
            select = ", ".join(
                _REMOVED_EXPR.get(col, f"t.{col}") + f" AS {col}" for col in COLUMNS
            )
            for row in cur.execute(
                f"SELECT {select} FROM transactions t JOIN connections c ON c.alias = t.alias "
                f"WHERE t.id IN ({marks})",
                removed,
            ).fetchall():
                by_id[row[0]] = dict(zip(COLUMNS, row, strict=True))
    finally:
        cur.close()
    changes = []
    for tx_id, changed_at, first_seen_at, removed_at in page:
        row = by_id.get(tx_id)
        if row is None:  # gone between the two reads
            continue
        item = compact_row({c: jsonable(v) for c, v in row.items()})
        item["change"] = _change(first_seen_at, changed_at, removed_at)
        changes.append(item)
    if page:
        next_cursor = encode_cursor(page[-1][1], page[-1][0])
    else:
        next_cursor = encode_cursor(*start)
    return {"changes": changes, "next_cursor": next_cursor, "has_more": len(keys) > limit}
