"""Review queue: live transactions the classifier was unsure about, grouped by counterpart.

A second reason, same_payee_recategorized, surfaces transactions whose counterpart already had a manual
category override, so the agent can apply the same category to the new one (after asking the owner).
"""

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
_NAMES = (
    "id", "date", "amount", "alias", "pluggy_category", "kind", "kind_reason", "counterpart_name",
    "counterpart_tax_id", "counterpart_account_number", "pluggy_category_original", "kind_overridden",
    "category_overridden",
)  # fmt: skip
_COLUMNS = ", ".join(
    "EXISTS (SELECT 1 FROM classification_overrides o WHERE o.transaction_id = v_flows.id)"
    if n == "kind_overridden" else n
    for n in _NAMES
)


def _digits(value: str | None) -> str:
    return _DIGITS.sub("", value or "")


def _is_transfer_like(category: str | None) -> bool:
    """Review-only: the classifier's transfer categories plus Pluggy's "Third party transfers"."""
    return _is_transfer_category(category) or (category or "").strip().lower().startswith(
        "third party transfer"
    )


def _reason(row: dict, min_amount: Decimal) -> str | None:
    kind, why, category = row["kind"], row["kind_reason"], row["pluggy_category_original"]
    if row["kind_overridden"]:
        return None
    if kind in ("income", "purchase") and why == "sign" and _is_transfer_like(category):
        return "unpaired_transfer"
    if kind == "income" and why == "sign" and row["amount"] >= min_amount:
        return "large_unidentified_inflow"
    if kind == "refund" and not (category or "").strip():
        return "uncategorized_card_credit"
    return None


def _same_payee(row: dict, by_tax: dict, by_acct: dict) -> dict[str, tuple[str, bool]]:
    """Live overrides on the row's counterpart, as {transaction id: (value, is inflow)}."""
    matched: dict[str, tuple[str, bool]] = {}  # union by override, so one matching both keys counts once
    if tax := _digits(row["counterpart_tax_id"]):
        matched |= by_tax.get(tax, {})
    if acct := (row["counterpart_account_number"] or "").strip():
        matched |= by_acct.get(acct, {})
    return matched


def _group_key(row: dict) -> tuple[str, str]:
    if tax := _digits(row["counterpart_tax_id"]):
        return "tax", tax
    if acct := (row["counterpart_account_number"] or "").strip():
        return "acct", acct
    if name := _normalize_key(row["counterpart_name"] or ""):
        return "name", name
    return "tx", row["id"]


def _overrides(cur, table: str, column: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Live overrides ({transaction id: (value, is inflow)}) per counterpart tax id and account number.

    `table` and `column` come from fixed call sites, never from callers.
    """
    by_tax: dict[str, dict] = {}
    by_acct: dict[str, dict] = {}
    cur.execute(
        f"SELECT o.transaction_id, (o.{column}, t.amount > 0), t.counterpart_tax_id, "
        "t.counterpart_account_number "
        f"FROM {table} o JOIN v_transactions t ON t.id = o.transaction_id"
    )
    for tx_id, (value, inflow), tax, acct in cur.fetchall():
        if d := _digits(tax):
            by_tax.setdefault(d, {})[tx_id] = (value, inflow)
        if a := (acct or "").strip():
            by_acct.setdefault(a, {})[tx_id] = (value, inflow)
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
    conds = ["currency = 'BRL'"]
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
        rows = [dict(zip(_NAMES, r, strict=True)) for r in cur.execute(sql, params).fetchall()]
        kinds_tax, kinds_acct = _overrides(cur, "classification_overrides", "kind")
        cats_tax, cats_acct = _overrides(cur, "category_overrides", "category")
    finally:
        cur.close()

    buckets: dict[tuple[str, tuple[str, str]], list[dict]] = {}
    for row in rows:
        reason = _reason(row, min_amount)
        # The counterpart was recategorized before (same direction only, so a refund from a payee is
        # not queued with its rent); unless this row already carries one of those categories, the
        # agent may want to apply the same one.
        if reason is None and not row["category_overridden"]:
            cats = {
                c for c, inflow in _same_payee(row, cats_tax, cats_acct).values()
                if inflow == (row["amount"] > 0)
            }
            if cats and row["pluggy_category"] not in cats:
                reason = "same_payee_recategorized"
        if reason:
            buckets.setdefault((reason, _group_key(row)), []).append(row)

    groups = []
    for (reason, (_, _key)), members in buckets.items():
        first = members[0]
        hint = {}
        if kinds := Counter(k for k, _ in _same_payee(first, kinds_tax, kinds_acct).values()):
            hint["kinds"] = dict(kinds)
        if cats := Counter(c for c, _ in _same_payee(first, cats_tax, cats_acct).values()):
            hint["categories"] = dict(cats)
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
            "hint": hint or None,
        })  # fmt: skip
    groups.sort(key=lambda g: (-abs(g["total"]), g["transactions"][0]["id"]))
    groups = groups[: min(MAX_LIMIT, max(1, int(limit)))]
    for g in groups:
        g["total"] = jsonable(g["total"])
    return {"scope": scope, "groups": groups}
