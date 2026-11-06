"""Transaction search with keyset pagination, and lookup by id."""

import base64
import json
from datetime import date

from cashbook_mcp.store.db import Database

from .filters import TxFilters, jsonable, where_clause

MAX_LIMIT = 500
MAX_IDS = 100

COLUMNS = (
    "id",
    "alias",
    "account_id",
    "date",
    "amount",
    "currency",
    "original_amount",
    "original_currency",
    "description",
    "counterpart_name",
    "counterpart_tax_id",
    "counterpart_account_number",
    "merchant_name",
    "pluggy_category",
    "bill_id",
    "kind",
    "kind_reason",
    "text_suspicious",
    "suspicion_reasons",
)
_SELECT = ", ".join(COLUMNS)


def _row(values: tuple) -> dict:
    return {c: jsonable(v) for c, v in zip(COLUMNS, values, strict=True)}


def encode_cursor(d: date, tx_id: str) -> str:
    raw = json.dumps({"d": d.isoformat(), "i": tx_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[date, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return date.fromisoformat(data["d"]), str(data["i"])
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("invalid cursor") from e


def search_transactions(
    db: Database, f: TxFilters, *, limit: int = 100, cursor: str | None = None
) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    where, params = where_clause(f)
    if cursor:
        d, i = decode_cursor(cursor)
        keyset = "(date < ? OR (date = ? AND id < ?))"
        where = f"{where} AND {keyset}" if where else f"WHERE {keyset}"
        params = [*params, d, d, i]
    sql = (
        f"SELECT {_SELECT} FROM v_transactions {where} "
        "ORDER BY date DESC, id DESC LIMIT ?"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, limit + 1]).fetchall()
    finally:
        cur.close()
    page = fetched[:limit]
    rows = [_row(r) for r in page]
    next_cursor = None
    if len(fetched) > limit:
        last = page[-1]
        next_cursor = encode_cursor(last[COLUMNS.index("date")], last[0])
    return {"rows": rows, "next_cursor": next_cursor}


def get_transactions(db: Database, ids: list[str]) -> list[dict]:
    if len(ids) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} ids per call")
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    cur = db.read()
    try:
        fetched = cur.execute(
            f"SELECT {_SELECT} FROM v_transactions WHERE id IN ({marks}) "
            "ORDER BY date DESC, id DESC",
            list(ids),
        ).fetchall()
    finally:
        cur.close()
    return [_row(r) for r in fetched]
