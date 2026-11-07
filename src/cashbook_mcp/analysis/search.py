"""Transaction search with keyset pagination, and lookup by id."""

import base64
import json
from datetime import date

from cashbook_mcp.store.db import Database

from .filters import TxFilters, jsonable, where_clause

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
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
    "role",
    "flow",
)
_SELECT = ", ".join(COLUMNS)


def _row(values: tuple) -> dict:
    return {c: jsonable(v) for c, v in zip(COLUMNS, values, strict=True)}


_ALWAYS = ("id", "date", "amount", "alias", "description", "kind", "flow")


def compact_row(row: dict) -> dict:
    """The few fields an agent needs per hit; empty or default fields are left out."""
    out = {k: row[k] for k in _ALWAYS}
    for key, src in (
        ("category", "pluggy_category"),
        ("merchant_name", "merchant_name"),
        ("counterpart_name", "counterpart_name"),
    ):
        if row[src] is not None:
            out[key] = row[src]
    if row["currency"] != "BRL":
        out["currency"] = row["currency"]
    if row["text_suspicious"]:
        out["text_suspicious"] = True
    return out


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
    db: Database, f: TxFilters, *, limit: int = DEFAULT_LIMIT, cursor: str | None = None
) -> dict:
    """One page of compact rows; `limit_clamped` is set when limit exceeded MAX_LIMIT."""
    clamped = int(limit) > MAX_LIMIT
    limit = max(1, min(int(limit), MAX_LIMIT))
    where, params = where_clause(f)
    if cursor:
        d, i = decode_cursor(cursor)
        keyset = "(date < ? OR (date = ? AND id < ?))"
        where = f"{where} AND {keyset}" if where else f"WHERE {keyset}"
        params = [*params, d, d, i]
    sql = (
        f"SELECT {_SELECT} FROM v_flows {where} "
        "ORDER BY date DESC, id DESC LIMIT ?"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [*params, limit + 1]).fetchall()
    finally:
        cur.close()
    page = fetched[:limit]
    rows = [compact_row(_row(r)) for r in page]
    next_cursor = None
    if len(fetched) > limit:
        last = page[-1]
        next_cursor = encode_cursor(last[COLUMNS.index("date")], last[0])
    result = {"rows": rows, "next_cursor": next_cursor}
    if clamped:
        result["limit_clamped"] = True
    return result


def get_transactions(db: Database, ids: list[str]) -> list[dict]:
    if len(ids) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} ids per call")
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    cur = db.read()
    try:
        fetched = cur.execute(
            f"SELECT {_SELECT} FROM v_flows WHERE id IN ({marks}) "
            "ORDER BY date DESC, id DESC",
            list(ids),
        ).fetchall()
    finally:
        cur.close()
    return [_row(r) for r in fetched]
