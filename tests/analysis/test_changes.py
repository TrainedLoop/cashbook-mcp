from datetime import datetime, timedelta, timezone

import pytest

from cashbook_mcp.analysis.changes import decode_cursor, encode_cursor, list_changes

NOW = datetime(2026, 4, 10, 12, 0)


@pytest.fixture
def changed(db):
    """Set changed_at per transaction (everything else is first seen on 2026-01-01)."""

    def _set(tx_id, when, *, removed=False):
        with db.write() as cur:
            cur.execute(
                "UPDATE transactions SET changed_at = ?, removed_at = ? WHERE id = ?",
                [when, when if removed else None, tx_id],
            )

    return _set


def ids(out):
    return [c["id"] for c in out["changes"]]


def test_default_window_is_seven_days_back_and_rows_are_oldest_first(db, changed):
    changed("t-salary", NOW - timedelta(days=8))  # outside the default window
    changed("t-card-1", NOW - timedelta(days=3))
    changed("t-card-2", NOW - timedelta(days=1))
    out = list_changes(db, NOW)
    assert ids(out) == ["t-card-1", "t-card-2"]
    assert out["has_more"] is False
    assert {"id", "date", "amount", "alias", "description", "kind", "flow", "change"} <= set(out["changes"][0])


def test_change_labels(db, changed):
    with db.write() as cur:
        cur.execute("UPDATE transactions SET first_seen_at = ? WHERE id = 't-card-1'", [NOW - timedelta(hours=5)])
    changed("t-card-1", NOW - timedelta(hours=5))  # first_seen_at == changed_at
    changed("t-card-2", NOW - timedelta(hours=4))  # seen long ago, changed since
    changed("t-salary", NOW - timedelta(hours=3), removed=True)
    out = {c["id"]: c for c in list_changes(db, NOW)["changes"]}
    assert out["t-card-1"]["change"] == "new"
    assert out["t-card-2"]["change"] == "updated"
    removed = out["t-salary"]
    assert removed["change"] == "removed"
    assert removed["amount"] == "5000.00" and removed["description"] == "Salario"
    assert removed["flow"] is None


def test_cursor_round_trip_resumes_after_last_row(db, changed):
    changed("t-card-1", NOW - timedelta(hours=3))
    changed("t-card-2", NOW - timedelta(hours=2))
    first = list_changes(db, NOW)
    assert ids(first) == ["t-card-1", "t-card-2"]
    again = list_changes(db, NOW, cursor=first["next_cursor"])
    assert again["changes"] == []
    assert again["next_cursor"] == first["next_cursor"]  # same position, so a routine can keep it
    changed("t-salary", NOW - timedelta(hours=1))
    assert ids(list_changes(db, NOW, cursor=first["next_cursor"])) == ["t-salary"]


def test_limit_has_more_and_paging_is_gapless_for_equal_timestamps(db, changed):
    same = NOW - timedelta(hours=2)
    for tx in ("t-card-1", "t-card-2", "t-salary", "t-usd"):
        changed(tx, same)
    seen, cursor = [], None
    while True:
        page = list_changes(db, NOW, cursor=cursor, limit=3)
        seen += ids(page)
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
        assert len(page["changes"]) == 3
    assert seen == sorted(["t-card-1", "t-card-2", "t-salary", "t-usd"])
    assert len(list_changes(db, NOW, limit=0)["changes"]) <= 1  # clamped to 1
    assert len(list_changes(db, NOW, limit=10_000)["changes"]) <= 200


def test_empty_result_still_returns_next_cursor(db):
    out = list_changes(db, NOW)
    assert out["changes"] == [] and out["has_more"] is False
    stamp, tx_id = decode_cursor(out["next_cursor"])
    assert stamp == NOW - timedelta(days=7) and tx_id == ""
    assert list_changes(db, NOW, cursor=out["next_cursor"])["next_cursor"] == out["next_cursor"]


@pytest.mark.parametrize("cursor", ["not base64!", "bm9waXBl", "", "Zm9vfGJhcg=="])
def test_bad_cursor_is_a_value_error(db, cursor):
    if cursor == "":
        assert list_changes(db, NOW, cursor=cursor)["changes"] == []  # empty means no cursor
        return
    with pytest.raises(ValueError, match="invalid cursor"):
        list_changes(db, NOW, cursor=cursor)


def test_cursor_encoding_round_trips():
    stamp = datetime(2026, 4, 10, 12, 0, 1, 250)
    assert decode_cursor(encode_cursor(stamp, "tx|with|bars")) == (stamp, "tx|with|bars")


def test_scope_filters_by_connection_role_including_removed_rows(db, changed):
    with db.write() as cur:
        cur.execute("UPDATE connections SET role = 'business' WHERE alias = 'second'")
    changed("t-card-1", NOW - timedelta(hours=3))  # main, personal
    changed("t-pix-in", NOW - timedelta(hours=2))  # second, business
    changed("t-card-2", NOW - timedelta(hours=1), removed=True)  # main, removed
    assert ids(list_changes(db, NOW, scope="personal")) == ["t-card-1", "t-card-2"]
    assert ids(list_changes(db, NOW, scope="business")) == ["t-pix-in"]
    assert ids(list_changes(db, NOW, scope="consolidated")) == ["t-card-1", "t-pix-in", "t-card-2"]
    assert ids(list_changes(db, NOW)) == ["t-card-1", "t-pix-in", "t-card-2"]
    with pytest.raises(ValueError):
        list_changes(db, NOW, scope="nope")


def test_payment_method_rides_along_in_rows(db, changed):
    with db.write() as cur:
        cur.execute("UPDATE transactions SET payment_method = 'BOLETO' WHERE id = 't-card-1'")
    changed("t-card-1", NOW - timedelta(hours=1))
    assert list_changes(db, NOW)["changes"][0]["payment_method"] == "BOLETO"


def test_removed_rows_report_the_overridden_category(db, changed):
    with db.write() as cur:
        cur.execute("INSERT INTO category_overrides VALUES ('t-card-1', 'main', 'Housing', 'c1')")
    changed("t-card-1", NOW - timedelta(hours=2), removed=True)
    changed("t-card-2", NOW - timedelta(hours=1), removed=True)
    out = {c["id"]: c for c in list_changes(db, NOW)["changes"]}
    assert out["t-card-1"]["category"] == "Housing" and out["t-card-1"]["category_overridden"] is True
    assert out["t-card-2"]["category"] == "Groceries" or out["t-card-2"]["category"] == "Eating out"
    assert "category_overridden" not in out["t-card-2"]


def test_since_starts_from_that_instant_and_a_cursor_wins(db, changed):
    changed("t-card-1", NOW - timedelta(days=3))
    changed("t-card-2", NOW - timedelta(hours=2))
    out = list_changes(db, NOW, since=NOW - timedelta(days=1))
    assert ids(out) == ["t-card-2"]
    first = list_changes(db, NOW)
    assert ids(list_changes(db, NOW, cursor=first["next_cursor"], since=NOW - timedelta(days=30))) == []
    # aware instants are converted, not dropped
    aware = (NOW - timedelta(days=1)).replace(tzinfo=timezone(timedelta(hours=-3)))
    assert ids(list_changes(db, NOW, since=aware + timedelta(hours=3))) == ["t-card-2"]


def test_since_in_the_future_returns_nothing_and_resumes_at_now(db, changed):
    changed("t-card-1", NOW - timedelta(hours=1))
    out = list_changes(db, NOW, since=NOW + timedelta(hours=1))
    assert out == {"changes": [], "next_cursor": encode_cursor(NOW, ""), "has_more": False}
    changed("t-card-2", NOW + timedelta(minutes=5))
    assert ids(list_changes(db, NOW + timedelta(minutes=10), cursor=out["next_cursor"])) == ["t-card-2"]


def test_cursor_with_offset_is_converted_to_utc():
    cursor = encode_cursor(datetime(2026, 4, 10, 9, 0, tzinfo=timezone(timedelta(hours=-3))), "x")
    assert decode_cursor(cursor) == (datetime(2026, 4, 10, 12, 0), "x")
