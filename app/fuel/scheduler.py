"""The fuel scheduler: one periodic check, no per-unit jobs.

Every tick asks the persisted state what is owed *now*. That is the whole
design, and it is what makes restart recovery free: nothing is scheduled in
advance, so there is nothing to rebuild. A unit that went overdue while the
container was down is simply overdue on the next tick.

It also means duplicate reminders are impossible by construction. There are no
timer objects to leak -- one loop, one question, asked repeatedly.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable

from app.fuel.state import NEED_TO_ARRANGE, FuelRecord, FuelStateStore, now_utc
from app.logging_config import get_logger

logger = get_logger("fuel.scheduler")

#: How often to inspect active units.
TICK_SECONDS = 60

#: A handler gets this long before the loop moves on to the next unit.
HANDLER_TIMEOUT_SECONDS = 60

#: ``(record, is_reminder) -> None``
AlertHandler = Callable[[FuelRecord, bool], Awaitable[None]]

#: ``(record) -> None`` -- a deadline just flipped a unit to NEED TO ARRANGE.
TransitionHandler = Callable[[FuelRecord], Awaitable[None]]


class FuelScheduler:
    """UPCOMING -> NEED TO ARRANGE at the deadline, then remind until ARRANGED."""

    def __init__(
        self,
        state: FuelStateStore,
        *,
        reminder_interval_minutes: int = 60,
        tick_seconds: int = TICK_SECONDS,
        on_alert: AlertHandler | None = None,
        on_transition: TransitionHandler | None = None,
    ) -> None:
        self._state = state
        self._interval = timedelta(minutes=max(1, int(reminder_interval_minutes)))
        self._tick = max(5, int(tick_seconds))
        self._on_alert = on_alert
        self._on_transition = on_transition
        self._task: asyncio.Task[None] | None = None

        self.ticks = 0
        self.transitions = 0
        self.alerts_sent = 0

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="fuel-scheduler")
        logger.info(
            "Fuel scheduler started | tick=%ds | reminder_interval=%dm",
            self._tick,
            int(self._interval.total_seconds() // 60),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Fuel scheduler stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- the loop ------------------------------------------------------------ #
    async def _run(self) -> None:
        # The first sweep is the restart-recovery path: anything that fell due
        # while the process was down is handled before the first sleep.
        first = True
        while True:
            try:
                await self._sweep(first=first)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Fuel scheduler tick failed")
            await asyncio.sleep(self._tick)

    async def _sweep(self, *, first: bool = False) -> None:
        self.ticks += 1
        instant = now_utc()

        # 1. Deadlines that have arrived.
        for record in self._state.due_now(instant):
            logger.info(
                "Fuel deadline reached | unit=%s | deadline=%s%s",
                record.unit,
                record.deadline,
                " | recovered_after_restart" if first else "",
            )
            updated = await self._state.set_status(record.unit, NEED_TO_ARRANGE)
            self.transitions += 1
            if self._on_transition is not None:
                await self._call(self._on_transition(updated), record.unit, "transition")

        # 2. The first alert and every hourly reminder after it. ARRANGED and
        #    NEED TO CHECK are never returned here, which is what stops the loop.
        for record in self._state.reminders_due(self._interval, instant):
            is_reminder = record.reminder_count > 0
            if self._on_alert is not None:
                await self._call(self._on_alert(record, is_reminder), record.unit, "alert")
            await self._state.record_alert(record.unit, reminder=True)
            self.alerts_sent += 1
            logger.info(
                "Fuel %s sent | unit=%s | reminder=%d",
                "reminder" if is_reminder else "alert",
                record.unit,
                record.reminder_count + 1,
            )

    async def _call(self, coro: Awaitable[Any], unit: str, label: str) -> None:
        """One unit's failure must not stall the rest of the sweep."""
        try:
            await asyncio.wait_for(coro, timeout=HANDLER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Fuel %s handler timed out | unit=%s", label, unit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fuel %s handler failed | unit=%s", label, unit)
