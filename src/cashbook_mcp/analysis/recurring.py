"""Recurring charge/income candidates inferred from transaction history."""

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import median
from typing import Literal

from cashbook_mcp.content import MARK_CLOSE, MARK_OPEN, wrap_suspicious
from cashbook_mcp.store.db import Database

from .filters import INTERNAL_KINDS, jsonable

Confidence = Literal["high", "medium", "low"]

_CENT = Decimal("0.01")
_TOLERANCE = Decimal("0.15")
_RANK = {"high": 0, "medium": 1, "low": 2}
_HIGH_CADENCES = ((6, 8), (27, 33), (360, 370))
_MARKERS = re.compile(
    "|".join(re.escape(m) for m in (MARK_OPEN, MARK_CLOSE)), re.IGNORECASE
)
_NOISE = re.compile(r"[\d*]+")


def _normalize_key(text: str) -> str:
    text = _MARKERS.sub(" ", text).lower()
    return " ".join(_NOISE.sub(" ", text).split())


def _months_ago(today: date, months: int) -> date:
    idx = today.year * 12 + (today.month - 1) - months
    year, month = idx // 12, idx % 12 + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _cluster(rows: list[tuple]) -> list[tuple]:
    """Largest set of rows whose amounts sit within +-15% of the set median."""
    best: list[tuple] = []
    for anchor in {abs(r[2]) for r in rows}:
        picked = [r for r in rows if abs(abs(r[2]) - anchor) <= anchor * _TOLERANCE]
        mid = median(abs(r[2]) for r in picked)
        picked = [r for r in picked if abs(abs(r[2]) - mid) <= mid * _TOLERANCE]
        if len(picked) > len(best):
            best = picked
    return best


def _confidence(cadence: int, regularity: float, spread: float) -> Confidence:
    if (
        any(lo <= cadence <= hi for lo, hi in _HIGH_CADENCES)
        and regularity >= 0.7
        and spread <= 0.10
    ):
        return "high"
    return "medium" if regularity >= 0.5 else "low"


def find_recurring(
    db: Database, today: date, months: int = 6, min_occurrences: int = 3
) -> list[dict]:
    if months < 1 or min_occurrences < 1:
        raise ValueError("months and min_occurrences must be >= 1")
    marks = ", ".join("?" * len(INTERNAL_KINDS))
    sql = (
        "SELECT id, date, amount, coalesce(merchant_name, counterpart_name, description), "
        "text_suspicious "
        f"FROM v_transactions WHERE date > ? AND date <= ? AND kind NOT IN ({marks}) "
        "AND amount != 0 ORDER BY date, id"
    )
    cur = db.read()
    try:
        fetched = cur.execute(sql, [_months_ago(today, months), today, *INTERNAL_KINDS]).fetchall()
    finally:
        cur.close()

    groups: dict[tuple[str, str], list[tuple]] = {}
    # Normalizing strips the untrusted markers, so remember which groups had a
    # flagged row and wrap their key again below.
    flagged: set[tuple[str, str]] = set()
    for id, d, amount, name, suspicious in fetched:
        key = _normalize_key(name or "")
        if key:
            group = (key, "in" if amount > 0 else "out")
            groups.setdefault(group, []).append((id, d, amount))
            if suspicious:
                flagged.add(group)

    items = []
    for (key, direction), rows in groups.items():
        picked = sorted(_cluster(rows), key=lambda r: (r[1], r[0]))
        if len(picked) < max(min_occurrences, 1) or len(picked) < 2:
            continue
        amounts = [abs(r[2]) for r in picked]
        typical = median(amounts)
        intervals = [(b[1] - a[1]).days for a, b in pairwise(picked)]
        cadence = round(median(intervals))
        mid = median(intervals)
        mad = median(abs(i - mid) for i in intervals)
        regularity = max(0.0, min(1.0, 1 - float(mad / mid))) if mid > 0 else 0.0
        spread = float((max(amounts) - min(amounts)) / typical)
        last = picked[-1][1]
        items.append(
            {
                "key": wrap_suspicious(key) if (key, direction) in flagged else key,
                "direction": direction,
                "typical_amount": jsonable(typical.quantize(_CENT)),
                "cadence_days": cadence,
                "occurrences": len(picked),
                "last_date": last.isoformat(),
                "next_expected": (last + timedelta(days=cadence)).isoformat(),
                "confidence": _confidence(cadence, regularity, spread),
                "evidence_ids": [r[0] for r in picked],
            }
        )
    items.sort(key=lambda i: (_RANK[i["confidence"]], -Decimal(i["typical_amount"]), i["key"]))
    return items
