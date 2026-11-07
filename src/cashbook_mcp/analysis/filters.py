"""Transaction filters and their parameterized SQL translation."""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .metrics import Scope, scope_clause

INTERNAL_KINDS = ("internal_transfer", "card_bill_payment")
# With a scope, internal movements are excluded by flow, so transfers between personal
# and business (owner draws, contributions) stay visible as money in or out of the scope.
_INTERNAL_FLOWS = {"internal_transfer": "internal", "card_bill_payment": "card_payment"}
UNCATEGORIZED = "uncategorized"
# Money is only summed in this currency; rows in any other are counted apart.
BASE_CURRENCY = "BRL"


@dataclass
class TxFilters:
    date_from: date | None = None
    date_to: date | None = None
    aliases: list[str] | None = None
    account_ids: list[str] | None = None
    categories: list[str] | None = None
    kinds: list[str] | None = None
    text: str | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    direction: Literal["in", "out"] | None = None
    exclude_internal: bool = True
    scope: Scope | None = None


def today_in(tz: ZoneInfo, now: datetime) -> date:
    """The calendar date of `now` in tz (naive `now` is taken as UTC)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(tz).date()


def months_ago(today: date, months: int) -> date:
    """Same day `months` calendar months earlier, clamped to the month's last day."""
    idx = today.year * 12 + (today.month - 1) - months
    year, month = idx // 12, idx % 12 + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _in(column: str, values: list[str], clauses: list[str], params: list[Any]) -> None:
    clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
    params.extend(values)


def where_clause(f: TxFilters) -> tuple[str, list]:
    """Return ("WHERE ..." or "", params). Values are always bound, never inlined."""
    clauses: list[str] = []
    params: list[Any] = []
    if f.date_from is not None:
        clauses.append("date >= ?")
        params.append(f.date_from)
    if f.date_to is not None:
        clauses.append("date <= ?")
        params.append(f.date_to)
    if f.aliases:
        _in("alias", f.aliases, clauses, params)
    if f.account_ids:
        _in("account_id", f.account_ids, clauses, params)
    if f.categories:
        # "uncategorized" is how groupings label a missing category.
        named = [c for c in f.categories if c.lower() != UNCATEGORIZED]
        parts = []
        if named:
            parts.append(f"pluggy_category IN ({', '.join('?' * len(named))})")
            params.extend(named)
        if len(named) < len(f.categories):
            parts.append("pluggy_category IS NULL")
        clauses.append("(" + " OR ".join(parts) + ")")
    if f.kinds:
        _in("kind", f.kinds, clauses, params)
    if f.text:
        escaped = f.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(description ILIKE ? ESCAPE '\\' OR counterpart_name ILIKE ? ESCAPE '\\' "
            "OR merchant_name ILIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 3)
    if f.min_amount is not None:
        clauses.append("abs(amount) >= ?")
        params.append(f.min_amount)
    if f.max_amount is not None:
        clauses.append("abs(amount) <= ?")
        params.append(f.max_amount)
    if f.direction == "in":
        clauses.append("amount > 0")
    elif f.direction == "out":
        clauses.append("amount < 0")
    if f.scope is not None:
        clause, cparams = scope_clause(f.scope)
        if clause:
            clauses.append(clause)
            params.extend(cparams)
    # Kinds the caller asked for by name are never excluded.
    excluded = [k for k in INTERNAL_KINDS if k not in (f.kinds or ())]
    if f.exclude_internal and excluded:
        marks = ", ".join("?" * len(excluded))
        if f.scope is None:
            clauses.append(f"kind NOT IN ({marks})")
            params.extend(excluded)
        else:
            # Foreign rows keep flow 'foreign': their internal kinds stay excluded by kind.
            clauses.append(f"NOT (flow IN ({marks}) OR (flow = 'foreign' AND kind IN ({marks})))")
            params.extend([_INTERNAL_FLOWS[k] for k in excluded] + excluded)
    return (("WHERE " + " AND ".join(clauses)) if clauses else ""), params


def jsonable(value: Any) -> Any:
    """Decimal -> str, date/datetime -> ISO string; everything else unchanged."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value
