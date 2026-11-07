"""Recurring charge/income candidates inferred from transaction history."""

import re
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import median
from typing import Literal

from cashbook_mcp.content import MARK_CLOSE, MARK_OPEN, wrap_suspicious
from cashbook_mcp.store.db import Database

from .filters import INTERNAL_KINDS, jsonable, months_ago
from .metrics import Scope, scope_clause

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


def is_stopped(expected: date, cadence: int, today: date) -> bool:
    """True when a recurrence is past due by more than half a cadence."""
    return expected < today and (today - expected).days * 2 > cadence


def _fetch_groups(
    db: Database, today: date, months: int, scope: Scope | None = None
) -> tuple[dict[tuple[str, str], list[tuple]], set[tuple[str, str]]]:
    """Rows of the window grouped by (normalized key, direction), plus the groups with flagged text.

    With a scope, rows come from v_flows filtered by connection role, BRL only.
    """
    marks = ", ".join("?" * len(INTERNAL_KINDS))
    view, extra, extra_params = "v_transactions", "", []
    if scope is not None:
        clause, extra_params = scope_clause(scope)
        view = "v_flows"
        extra = " AND currency = 'BRL'" + (f" AND {clause}" if clause else "")
    sql = (
        "SELECT id, date, amount, coalesce(merchant_name, counterpart_name, description), "
        "text_suspicious, account_id, "
        f"(SELECT a.type FROM v_accounts a WHERE a.id = {view}.account_id) "
        f"FROM {view} WHERE date > ? AND date <= ? AND kind NOT IN ({marks}) "
        f"AND amount != 0{extra} ORDER BY date, id"
    )
    cur = db.read()
    try:
        fetched = cur.execute(
            sql, [months_ago(today, months), today, *INTERNAL_KINDS, *extra_params]
        ).fetchall()
    finally:
        cur.close()

    groups: dict[tuple[str, str], list[tuple]] = {}
    # Normalizing strips the untrusted markers, so remember which groups had a
    # flagged row and wrap their key again below.
    flagged: set[tuple[str, str]] = set()
    for id, d, amount, name, suspicious, account_id, account_type in fetched:
        key = _normalize_key(name or "")
        if key:
            group = (key, "in" if amount > 0 else "out")
            groups.setdefault(group, []).append((id, d, amount, account_id, account_type))
            if suspicious:
                flagged.add(group)
    return groups, flagged


def _series(picked: list[tuple]) -> dict:
    """Typical amount, cadence and regularity of date-sorted rows (at least two)."""
    amounts = [abs(r[2]) for r in picked]
    typical = median(amounts)
    intervals = [(b[1] - a[1]).days for a, b in pairwise(picked)]
    mid = median(intervals)
    mad = median(abs(i - mid) for i in intervals)
    return {
        "typical": typical,
        "cadence": round(mid),
        "regularity": max(0.0, min(1.0, 1 - float(mad / mid))) if mid > 0 else 0.0,
        "spread": float((max(amounts) - min(amounts)) / typical),
        "last": picked[-1][1],
    }


def find_recurring(
    db: Database, today: date, months: int = 6, min_occurrences: int = 3
) -> list[dict]:
    if months < 1 or min_occurrences < 1:
        raise ValueError("months and min_occurrences must be >= 1")
    groups, flagged = _fetch_groups(db, today, months)

    items = []
    for (key, direction), rows in groups.items():
        picked = sorted(_cluster(rows), key=lambda r: (r[1], r[0]))
        if len(picked) < max(min_occurrences, 1) or len(picked) < 2:
            continue
        stats = _series(picked)
        typical, cadence, last = stats["typical"], stats["cadence"], stats["last"]
        items.append(
            {
                "key": wrap_suspicious(key) if (key, direction) in flagged else key,
                "direction": direction,
                "typical_amount": jsonable(typical.quantize(_CENT)),
                "cadence_days": cadence,
                "occurrences": len(picked),
                "last_date": last.isoformat(),
                "next_expected": (last + timedelta(days=cadence)).isoformat(),
                "confidence": _confidence(cadence, stats["regularity"], stats["spread"]),
                "evidence_ids": [r[0] for r in picked],
                # The most recent evidence row decides when a candidate spans accounts.
                "account_id": picked[-1][3],
                "account_type": picked[-1][4],
            }
        )
    items.sort(key=lambda i: (_RANK[i["confidence"]], -Decimal(i["typical_amount"]), i["key"]))
    return items


_MIN_RISE = Decimal("1.00")
_RISE = Decimal("0.05")
_NEW_DAYS = 60
_DUPLICATE_DAYS = 3
_CHANGE_ORDER = {"price_increase": 0, "new": 1, "stopped": 2, "duplicate": 3}


def recurring_changes(
    db: Database, today: date, *, scope: Scope = "personal", months: int = 6
) -> dict:
    """Price increases, new and stopped recurrences, and duplicate charges in the scope."""
    if months < 1:
        raise ValueError("months must be >= 1")
    groups, flagged = _fetch_groups(db, today, months, scope)
    changes: list[dict] = []
    for (key, direction), rows in groups.items():
        rows = sorted(rows, key=lambda r: (r[1], r[0]))
        shown = wrap_suspicious(key) if (key, direction) in flagged else key
        base = {"key": shown, "direction": direction}

        for prev, row in pairwise(rows):
            if row[2] == prev[2] and (row[1] - prev[1]).days <= _DUPLICATE_DAYS:
                changes.append(
                    {
                        "type": "duplicate", **base, "date": row[1].isoformat(),
                        "amount": jsonable(abs(row[2])), "evidence_ids": [prev[0], row[0]],
                    }
                )  # fmt: skip

        picked = sorted(_cluster(rows), key=lambda r: (r[1], r[0]))
        if len(picked) >= 3:
            stats = _series(picked)
            cadence = stats["cadence"]
            confident = _confidence(cadence, stats["regularity"], stats["spread"]) != "low"
            expected = stats["last"] + timedelta(days=cadence)
            if confident and cadence >= 1 and is_stopped(expected, cadence, today):
                changes.append(
                    {
                        "type": "stopped", **base, "expected_date": expected.isoformat(),
                        "last_date": stats["last"].isoformat(), "evidence_ids": [r[0] for r in picked],
                    }
                )  # fmt: skip

        # Price rise: the latest charge against the regular series before it.
        prior = sorted(_cluster(rows[:-1]), key=lambda r: (r[1], r[0])) if len(rows) > 2 else []
        if len(prior) >= 2:
            stats = _series(prior)
            cadence, last = stats["cadence"], rows[-1]
            gap = (last[1] - prior[-1][1]).days
            typical, latest = stats["typical"], abs(last[2])
            regular = _confidence(cadence, stats["regularity"], stats["spread"]) != "low"
            if (
                regular and cadence >= 1 and last not in prior
                and cadence / 2 <= gap <= cadence * 1.5
                and latest > typical * (1 + _RISE) and latest - typical > _MIN_RISE
            ):  # fmt: skip
                changes.append(
                    {
                        "type": "price_increase", **base,
                        "typical_amount": jsonable(typical.quantize(_CENT)),
                        "last_amount": jsonable(latest), "last_date": last[1].isoformat(),
                        "evidence_ids": [r[0] for r in prior] + [last[0]],
                    }
                )  # fmt: skip

        first = rows[0][1]
        if len(picked) >= 2 and first >= today - timedelta(days=_NEW_DAYS):
            stats = _series(picked)
            if _confidence(stats["cadence"], stats["regularity"], stats["spread"]) == "high":
                changes.append(
                    {
                        "type": "new", **base, "first_date": first.isoformat(),
                        "occurrences": len(picked), "evidence_ids": [r[0] for r in picked],
                    }
                )  # fmt: skip
    changes.sort(key=lambda c: (_CHANGE_ORDER[c["type"]], c["key"], c.get("date", "")))
    return {"scope": scope, "changes": changes}
