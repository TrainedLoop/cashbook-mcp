"""Transaction filters and their parameterized SQL translation."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

INTERNAL_KINDS = ("internal_transfer", "card_bill_payment")


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


def today_in(tz: ZoneInfo, now: datetime) -> date:
    """The calendar date of `now` in tz (naive `now` is taken as UTC)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(tz).date()


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
        _in("pluggy_category", f.categories, clauses, params)
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
    if f.exclude_internal:
        clauses.append(f"kind NOT IN ({', '.join('?' * len(INTERNAL_KINDS))})")
        params.extend(INTERNAL_KINDS)
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
