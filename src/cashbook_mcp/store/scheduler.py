"""Background scheduler: daily full refresh plus hourly update checks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from cashbook_mcp.store.sync import Syncer

log = logging.getLogger("cashbook-mcp")


def next_daily_run(now: datetime, refresh_at: time, tz: ZoneInfo) -> datetime:
    """Next wall-clock occurrence of refresh_at in tz, strictly after now."""
    local = now.astimezone(tz)
    candidate = datetime.combine(local.date(), refresh_at, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), refresh_at, tzinfo=tz)
    return candidate


def _next_hour(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return (local + timedelta(hours=1)).astimezone(tz)


class Scheduler:
    def __init__(
        self,
        syncer: Syncer,
        *,
        refresh_at: time,
        tz: ZoneInfo,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._syncer = syncer
        self._refresh_at = refresh_at
        self._tz = tz
        self._now = now
        self._sleep = sleep
        self.last_full_refresh: datetime | None = None

    @property
    def next_full_refresh(self) -> datetime:
        return next_daily_run(self._now(), self._refresh_at, self._tz)

    async def _full(self) -> None:
        try:
            await self._syncer.load_all()
        except Exception:
            log.exception("full refresh failed")
        self.last_full_refresh = self._now()

    async def _check(self) -> None:
        try:
            await self._syncer.check_updates()
        except Exception:
            log.exception("update check failed")

    async def run(self) -> None:
        await self._full()
        while True:
            now = self._now()
            hour = _next_hour(now, self._tz)
            daily = next_daily_run(now, self._refresh_at, self._tz)
            target = min(hour, daily)
            await self._sleep((target - now).total_seconds())
            # A coinciding daily run supersedes the hourly check.
            if target == daily:
                await self._full()
            else:
                await self._check()
