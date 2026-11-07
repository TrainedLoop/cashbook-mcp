"""Review queue: live transactions the classifier was unsure about, grouped by counterpart."""

import re
from collections import Counter
from datetime import date
from decimal import Decimal

from cashbook_mcp.classify import _is_transfer_category
from cashbook_mcp.store.db import Database

from .filters import jsonable
from .metrics import Scope, scope_clause
from .recurring import _normalize_key

_DIGITS = re.compile(r"\D+")
MAX_LIMIT = 100  # groups per call
_COLUMNS = (
    "id, date, amount, alias, pluggy_category, kind, kind_reason, "
    "counterpart_name, counterpart_tax_id, counterpart_account_number"
)


def _digits(value: str | None) -> str:
    return _DIGITS.sub("", value or "")


def _reason(row: dict, min_amount: Decimal) -> str | None:
    kind, why, category = row["kind"], row["kind_reason"], row["pluggy_category"]
    if kind in ("income", "purchase") and why == "sign" and _is_transfer_category(category):
        return "unpaired_transfer"
    if kind == "income" and why == "sign" and row["amount"] >= min_amount:
        return "large_unidentified_inflow"
    if kind == "refund" and not (category or "").strip():
        return "uncategorized_card_credit"
    return None


def _group_key(row: dict) -> tuple[str, str]:
    if tax := _digits(row["counterpart_tax_id"]):
        return "tax", tax
    if acct := (row["counterpart_account_number"] or "").strip():
        return "acct", acct
    if name := _normalize_key(row["counterpart_name"] or ""):
        return "name", name
    return "tx", row["id"]


def _override_kinds(cur) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Kinds of live overrides ({transaction id: kind}) per counterpart tax id digits and per account number."""
    by_tax: dict[str, dict[str, str]] = {}
    by_acct: dict[str, dict[str, str]] = {}
    cur.execute(
        "SELECT o.transaction_id, o.kind, t.counterpart_tax_id, t.counterpart_account_number "
        "FROM classification_overrides o JOIN v_transactions t ON t.id = o.transaction_id"
    )
    for tx_id, kind, tax, acct in cur.fetchall():
        if d := _digits(tax):
            by_tax.setdefault(d, {})[tx_id] = kind
        if a := (acct or "").strip():
            by_acct.setdefault(a, {})[tx_id] = kind
    return by_tax, by_acct


def review_classification(
    db: Database,
    *,
    scope: Scope = "personal",
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 20,
    min_amount: Decimal,
) -> dict:
    where, params = scope_clause(scope)
    conds = [
        "currency = 'BRL'",
        "NOT EXISTS (SELECT 1 FROM classification_overrides o WHERE o.transaction_id = v_flows.id)",
    ]
    if where:
        conds.append(where)
    if date_from:
        conds.append("date >= ?")
        params.append(date_from)
    if date_to:
        conds.append("date <= ?")
        params.append(date_to)
    sql = (
        f"SELECT {_COLUMNS} FROM v_flows WHERE {' AND '.join(conds)} "
        "AND kind IN ('income', 'purchase', 'refund') ORDER BY date, id"
    )
    cur = db.read()
    try:
        names = [c for c in _COLUMNS.replace(" ", "").split(",")]
        rows = [dict(zip(names, r, strict=True)) for r in cur.execute(sql, params).fetchall()]
        by_tax, by_acct = _override_kinds(cur)
    finally:
        cur.close()

    buckets: dict[tuple[str, tuple[str, str]], list[dict]] = {}
    for row in rows:
        if reason := _reason(row, min_amount):
            buckets.setdefault((reason, _group_key(row)), []).append(row)

    groups = []
    for (reason, (_, _key)), members in buckets.items():
        first = members[0]
        matched: dict[str, str] = {}  # union by override, so one matching both keys counts once
        if tax := _digits(first["counterpart_tax_id"]):
            matched |= by_tax.get(tax, {})
        if acct := (first["counterpart_account_number"] or "").strip():
            matched |= by_acct.get(acct, {})
        counts = Counter(matched.values())
        named = next((m for m in members if m["counterpart_name"]), first)
        groups.append({
            "reason": reason,
            "counterpart": {
                "name": named["counterpart_name"],
                "tax_id": first["counterpart_tax_id"],
                "account_number": first["counterpart_account_number"],
            },
            "transactions": [
                {
                    "id": m["id"], "date": jsonable(m["date"]), "amount": jsonable(m["amount"]),
                    "alias": m["alias"], "category": m["pluggy_category"],
                }
                for m in members
            ],
            "total": sum((m["amount"] for m in members), Decimal(0)),  # stringified after ordering
            "hint": {"kinds": dict(counts)} if counts else None,
        })  # fmt: skip
    groups.sort(key=lambda g: (-abs(g["total"]), g["transactions"][0]["id"]))
    groups = groups[: min(MAX_LIMIT, max(1, int(limit)))]
    for g in groups:
        g["total"] = jsonable(g["total"])
    return {"scope": scope, "groups": groups}
