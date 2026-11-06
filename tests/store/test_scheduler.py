import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from cashbook_mcp.store.scheduler import Scheduler, next_daily_run

TZ = ZoneInfo("America/Sao_Paulo")
SIX = time(6, 0)


def at(h, m=0, s=0, day=6):
    return datetime(2026, 11, day, h, m, s, tzinfo=TZ)


def test_next_daily_run_same_day_and_next_day():
    assert next_daily_run(at(5), SIX, TZ) == at(6)
    assert next_daily_run(at(6, 0, 1), SIX, TZ) == at(6, day=7)


class FakeSyncer:
    def __init__(self, fail_check=False):
        self.calls = []
        self.fail_check = fail_check

    async def load_all(self):
        self.calls.append("load_all")
        return []

    async def check_updates(self):
        self.calls.append("check_updates")
        if self.fail_check:
            raise RuntimeError("boom")
        return []


class Harness:
    def __init__(self, start, syncer, stop_after):
        self.clock = start
        self.syncer = syncer
        self.stop_after = stop_after
        self.sleeps = []

    def now(self):
        return self.clock

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.syncer.calls) >= self.stop_after:
            raise asyncio.CancelledError
        self.clock += timedelta(seconds=seconds)

    def scheduler(self):
        return Scheduler(
            self.syncer, refresh_at=SIX, tz=TZ, now=self.now, sleep=self.sleep
        )


def run(h):
    async def go():
        with pytest.raises(asyncio.CancelledError):
            await h.scheduler().run()

    asyncio.run(go())


def test_run_sequence():
    h = Harness(at(3), FakeSyncer(), stop_after=3)
    run(h)
    assert h.syncer.calls == ["load_all", "check_updates", "check_updates"]
    assert h.sleeps[:2] == [3600, 3600]


def test_daily_and_hour_tick_coincide_runs_only_load_all():
    h = Harness(at(4), FakeSyncer(), stop_after=3)
    run(h)
    assert h.syncer.calls == ["load_all", "check_updates", "load_all"]
    assert h.clock == at(6)


def test_exception_does_not_end_loop():
    h = Harness(at(3), FakeSyncer(fail_check=True), stop_after=3)
    run(h)
    assert h.syncer.calls == ["load_all", "check_updates", "check_updates"]


def test_cancellation_propagates():
    h = Harness(at(4, 30), FakeSyncer(), stop_after=1)
    run(h)
    assert h.syncer.calls == ["load_all"]


def test_last_and_next_full_refresh():
    h = Harness(at(4, 30), FakeSyncer(), stop_after=1)
    s = h.scheduler()
    assert s.last_full_refresh is None
    assert s.next_full_refresh == at(6)

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await s.run()

    asyncio.run(go())
    assert s.last_full_refresh == at(4, 30)
