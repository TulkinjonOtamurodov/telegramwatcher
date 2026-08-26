"""Persistent fuel state -- one active cycle per unit.

There is no load history. Each unit keeps exactly one deadline and one status,
and a new load confirmation replaces the previous cycle outright. That is the
whole v1 model, and it is what makes restart recovery simple: the file *is* the
schedule, so rebuilding timers after a restart is a read, not a replay.

Stored in ``data/fuel_state.json`` alongside keywords and group rules, so it
lives on the Docker volume and survives restart, rebuild and reboot.

Processed Telegram messages are recorded here too. A reposted or re-delivered
confirmation is matched on ``chat_id:message_id`` and ignored, which is what
keeps the automation idempotent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("fuel.state")

# -- statuses --------------------------------------------------------------- #
UPCOMING = "UPCOMING"
NEED_TO_ARRANGE = "NEED TO ARRANGE"
ARRANGED = "ARRANGED"
NEED_TO_CHECK = "NEED TO CHECK"

STATUSES = (UPCOMING, NEED_TO_ARRANGE, ARRANGED, NEED_TO_CHECK)

#: Values the existing Fuel Desk board and Google Sheet already understand.
#: UPCOMING is deliberately internal -- writing it to the board would introduce
#: a value the sheet has never had.
BOARD_VALUE = {
    UPCOMING: "Upcoming",
    NEED_TO_ARRANGE: "Need to arrange",
    ARRANGED: "Arranged",
    NEED_TO_CHECK: "Need to check",
}

#: How many processed-message keys to keep before trimming the oldest.
PROCESSED_LIMIT = 2000


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class FuelRecord:
    """One unit's current fuel cycle."""

    unit: str
    status: str = NEED_TO_CHECK
    deadline: str | None = None
    timezone_name: str | None = None
    driver: str | None = None
    chat_id: str | None = None
    group_title: str | None = None
    source_message_id: int | None = None
    source_message_url: str | None = None
    cycle_id: str | None = None
    last_alert_at: str | None = None
    last_reminder_at: str | None = None
    reminder_count: int = 0
    note: str = ""
    updated_at: str = field(default_factory=lambda: iso(now_utc()) or "")

    @property
    def deadline_dt(self) -> datetime | None:
        return parse_iso(self.deadline)

    def overdue_for(self, moment: datetime | None = None) -> timedelta | None:
        deadline = self.deadline_dt
        if deadline is None:
            return None
        delta = (moment or now_utc()) - deadline
        return delta if delta.total_seconds() > 0 else None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class FuelStateStore:
    """Loads, exposes and persists every unit's fuel cycle."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._units: dict[str, FuelRecord] = {}
        self._processed: dict[str, str] = {}

    # -- reading ----------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def get(self, unit: str) -> FuelRecord | None:
        return self._units.get(str(unit).strip())

    def all(self) -> list[FuelRecord]:
        return sorted(self._units.values(), key=lambda record: record.unit)

    def count(self) -> int:
        return len(self._units)

    def counts_by_status(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for record in self._units.values():
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts

    def processed_key(self, chat_id: Any, message_id: Any) -> str:
        return f"{chat_id}:{message_id}"

    def is_processed(self, chat_id: Any, message_id: Any) -> bool:
        return self.processed_key(chat_id, message_id) in self._processed

    # -- loading / saving --------------------------------------------------- #
    def _read_from_disk(self) -> tuple[dict[str, FuelRecord], dict[str, str]]:
        if not self._path.is_file():
            logger.info("Fuel state file %s not found; starting empty", self._path)
            return {}, {}

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            logger.error(
                "%s is not valid JSON (%s); keeping a copy at %s and starting empty",
                self._path,
                exc,
                backup,
            )
            try:
                backup.write_text(self._path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                logger.warning("Could not write the corrupt-state backup", exc_info=True)
            return {}, {}
        except OSError as exc:
            logger.error("Could not read %s (%s); keeping current state", self._path, exc)
            return dict(self._units), dict(self._processed)

        units: dict[str, FuelRecord] = {}
        for unit, payload in (raw.get("units") or {}).items():
            if not isinstance(payload, dict):
                logger.warning("Skipping malformed fuel record for unit %s", unit)
                continue
            fields = {
                key: value
                for key, value in payload.items()
                if key in FuelRecord.__dataclass_fields__
            }
            fields["unit"] = str(unit)
            if fields.get("status") not in STATUSES:
                fields["status"] = NEED_TO_CHECK
            units[str(unit)] = FuelRecord(**fields)

        processed = {
            str(key): str(value)
            for key, value in (raw.get("processed") or {}).items()
        }
        return units, processed

    async def load(self) -> None:
        async with self._lock:
            self._units, self._processed = await asyncio.to_thread(self._read_from_disk)
        logger.info(
            "Fuel state loaded | units=%d | processed=%d",
            len(self._units),
            len(self._processed),
        )

    def _payload(self) -> str:
        data = {
            "units": {unit: record.to_json() for unit, record in sorted(self._units.items())},
            "processed": self._processed,
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    async def _write(self) -> None:
        await asyncio.to_thread(atomic_write_text, self._path, self._payload())

    # -- mutation ----------------------------------------------------------- #
    async def mark_processed(self, chat_id: Any, message_id: Any, unit: str = "") -> None:
        key = self.processed_key(chat_id, message_id)
        async with self._lock:
            self._processed[key] = f"{unit}|{iso(now_utc())}"
            if len(self._processed) > PROCESSED_LIMIT:
                # dicts keep insertion order, so the oldest keys are first.
                excess = len(self._processed) - PROCESSED_LIMIT
                for stale in list(self._processed)[:excess]:
                    self._processed.pop(stale, None)
            await self._write()

    async def start_cycle(
        self,
        unit: str,
        *,
        deadline: datetime,
        timezone_name: str | None = None,
        driver: str | None = None,
        chat_id: Any = None,
        group_title: str | None = None,
        source_message_id: int | None = None,
        source_message_url: str | None = None,
        moment: datetime | None = None,
    ) -> tuple[FuelRecord, str | None]:
        """Replace a unit's cycle with a new deadline.

        Returns ``(record, replaced_deadline)``. A unit already ARRANGED is reset
        to a fresh cycle -- the previous load being handled says nothing about
        this one.
        """
        key = str(unit).strip()
        instant = moment or now_utc()
        status = UPCOMING if deadline > instant else NEED_TO_ARRANGE

        async with self._lock:
            previous = self._units.get(key)
            replaced = previous.deadline if previous else None

            record = FuelRecord(
                unit=key,
                status=status,
                deadline=iso(deadline),
                timezone_name=timezone_name,
                driver=driver or (previous.driver if previous else None),
                chat_id=str(chat_id) if chat_id is not None else (previous.chat_id if previous else None),
                group_title=group_title or (previous.group_title if previous else None),
                source_message_id=source_message_id,
                source_message_url=source_message_url,
                cycle_id=f"{key}-{int(deadline.timestamp())}",
                last_alert_at=None,
                last_reminder_at=None,
                reminder_count=0,
                note="",
                updated_at=iso(instant) or "",
            )
            self._units[key] = record
            await self._write()

        if replaced:
            logger.info(
                "Fuel cycle replaced | unit=%s | old_deadline=%s | new_deadline=%s",
                key,
                replaced,
                record.deadline,
            )
        return record, replaced

    async def set_status(
        self,
        unit: str,
        status: str,
        *,
        note: str = "",
        driver: str | None = None,
        chat_id: Any = None,
        group_title: str | None = None,
        source_message_id: int | None = None,
        source_message_url: str | None = None,
    ) -> FuelRecord:
        """Set a unit's status, creating the record if this is its first event."""
        if status not in STATUSES:
            raise ValueError(f"Unknown fuel status: {status!r}")

        key = str(unit).strip()
        async with self._lock:
            record = self._units.get(key)
            if record is None:
                record = FuelRecord(unit=key)
                self._units[key] = record

            record.status = status
            if note:
                record.note = note
            if driver:
                record.driver = driver
            if chat_id is not None:
                record.chat_id = str(chat_id)
            if group_title:
                record.group_title = group_title
            if source_message_id is not None:
                record.source_message_id = source_message_id
            if source_message_url:
                record.source_message_url = source_message_url

            if status == ARRANGED:
                # Reminders stop here; clearing the counter keeps the next cycle clean.
                record.reminder_count = 0
                record.last_reminder_at = None
            record.updated_at = iso(now_utc()) or ""

            await self._write()

        logger.info("Fuel status set | unit=%s | status=%s", key, status)
        return record

    async def record_alert(self, unit: str, *, reminder: bool = False) -> FuelRecord | None:
        """Stamp that an alert or reminder went out, so restarts do not repeat it."""
        key = str(unit).strip()
        async with self._lock:
            record = self._units.get(key)
            if record is None:
                return None
            stamp = iso(now_utc())
            if record.last_alert_at is None:
                record.last_alert_at = stamp
            if reminder:
                record.last_reminder_at = stamp
                record.reminder_count += 1
            record.updated_at = stamp or ""
            await self._write()
        return record

    async def remove(self, unit: str) -> bool:
        key = str(unit).strip()
        async with self._lock:
            existed = self._units.pop(key, None) is not None
            if existed:
                await self._write()
        return existed

    # -- scheduling queries -------------------------------------------------- #
    def due_now(self, moment: datetime | None = None) -> list[FuelRecord]:
        """UPCOMING records whose deadline has arrived."""
        instant = moment or now_utc()
        return [
            record
            for record in self._units.values()
            if record.status == UPCOMING
            and record.deadline_dt is not None
            and record.deadline_dt <= instant
        ]

    def next_deadline(self, moment: datetime | None = None) -> datetime | None:
        """The soonest future deadline, so the scheduler can sleep until it."""
        instant = moment or now_utc()
        future = [
            record.deadline_dt
            for record in self._units.values()
            if record.status == UPCOMING
            and record.deadline_dt is not None
            and record.deadline_dt > instant
        ]
        return min(future) if future else None

    def reminders_due(
        self, interval: timedelta, moment: datetime | None = None
    ) -> list[FuelRecord]:
        """NEED TO ARRANGE records whose next reminder is owed.

        A record that has never been alerted is due immediately; one already
        reminded is due ``interval`` after the last reminder. ARRANGED records
        are never returned, which is what stops the reminder loop.
        """
        instant = moment or now_utc()
        due: list[FuelRecord] = []
        for record in self._units.values():
            if record.status != NEED_TO_ARRANGE:
                continue
            last = parse_iso(record.last_reminder_at) or parse_iso(record.last_alert_at)
            if last is None or (instant - last) >= interval:
                due.append(record)
        return due

    def units_needing_attention(self) -> Iterable[FuelRecord]:
        return (
            record
            for record in self.all()
            if record.status in (NEED_TO_ARRANGE, NEED_TO_CHECK)
        )
