"""The fuel automation service: message in, deadline out, alerts until arranged.

This is the only module that knows about all the others. The watcher hands it
every group message; everything else -- detection, parsing, mapping, state,
scheduling, FuelHelper -- happens behind this one entry point.

Ordering matters and is deliberate. Detection runs before parsing, parsing
before mapping is consulted for anything but the unit, and a failure at any step
produces NEED TO CHECK with the reason rather than a guessed deadline.

The service is inert until ``FUEL_AUTOMATION_ENABLED`` is true *and* the watcher
hook is attached, so it can ship without touching live behaviour.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Sequence

from telethon import Button, events
from telethon.errors import FloodWaitError, MessageIdInvalidError, QueryIdInvalidError, RPCError

from app.fuel.client import FuelHelperClient
from app.fuel.parser import ParseFailure, looks_like_load_confirmation, parse_pickup
from app.fuel.scheduler import FuelScheduler
from app.fuel.state import (
    ARRANGED,
    NEED_TO_ARRANGE,
    NEED_TO_CHECK,
    UPCOMING,
    FuelRecord,
    FuelStateStore,
    now_utc,
)
from app.fuel.units import UnitMappingStore
from app.logging_config import get_logger
from app.utils import build_message_url, display_name, forum_topic_id, truncate

logger = get_logger("fuel.service")

# -- callback data ----------------------------------------------------------- #
CB_FUEL_ARRANGED = b"FA"
CB_FUEL_CHECK = b"FC"
CB_FUEL_SEP = b"|"

#: Matched only against a short, standalone message in a mapped group. Anything
#: longer is conversation, not a confirmation.
ARRANGED_TEXT = re.compile(
    r"^\s*(?:fuel\s+(?:is\s+)?(?:arranged|done|ok|good)|arranged|fueled|fuelled)\s*[.!]?\s*$",
    re.IGNORECASE,
)
MAX_ARRANGED_TEXT_CHARS = 40

ALERT_TAG = "#FUEL"
CLOSING_LINE = "⚠️ DON'T LEAVE IT UNATTENDED."


def _humanize(seconds: float) -> str:
    seconds = int(abs(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class FuelService:
    """Turns load confirmations into deadlines, then chases them until arranged."""

    def __init__(
        self,
        bot_client: Any,
        *,
        state: FuelStateStore,
        units: UnitMappingStore,
        client: FuelHelperClient,
        recipients: Callable[[], Sequence[int]],
        is_authorized: Callable[[int | None], bool],
        deadline_hours_before: float = 3.0,
        reminder_interval_minutes: int = 60,
        enabled: bool = False,
    ) -> None:
        self._bot = bot_client
        self.state = state
        self.units = units
        self.client = client
        self._recipients = recipients
        self._is_authorized = is_authorized
        self._deadline_hours = deadline_hours_before
        self.enabled = enabled
        self._registered = False

        self.scheduler = FuelScheduler(
            state,
            reminder_interval_minutes=reminder_interval_minutes,
            on_alert=self._on_alert,
            on_transition=self._on_transition,
        )

        self.loads_seen = 0
        self.loads_parsed = 0
        self.parse_failures = 0

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        """Load persisted state and start the scheduler.

        The scheduler's first sweep is the restart-recovery path: anything that
        came due while the process was down is applied immediately.
        """
        if not self.enabled:
            logger.info("Fuel automation is disabled (FUEL_AUTOMATION_ENABLED)")
            return

        await self.state.load()
        await self.units.load()

        if self.client.configured:
            healthy = await self.client.health()
            logger.info(
                "FuelHelper API %s | url=%s",
                "reachable" if healthy else "NOT reachable",
                self.client.base_url,
            )
        else:
            logger.info("FUELHELPER_API_URL is not set; fuel state stays local to MAKIMA")

        self._register_callbacks()
        await self.scheduler.start()

        counts = self.state.counts_by_status()
        logger.info(
            "Fuel automation ready | units=%d | mappings=%d | %s",
            self.state.count(),
            self.units.count(),
            ", ".join(f"{key}={value}" for key, value in counts.items()),
        )

    async def stop(self) -> None:
        await self.scheduler.stop()

    def _register_callbacks(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(
            self._on_callback,
            events.CallbackQuery(pattern=re.compile(rb"^F[AC]\|")),
        )
        self._registered = True
        logger.info("Fuel alert buttons registered")

    # -- watcher hook -------------------------------------------------------- #
    async def handle_message(self, event: Any, chat: Any, text: str) -> bool:
        """Inspect one incoming group message. Returns True if it was a load.

        Called by the watcher for every group message, independently of the
        keyword engine -- a load confirmation still processes in a group whose
        keywords are excluded.
        """
        if not self.enabled or not text:
            return False

        chat_id = getattr(event, "chat_id", None)
        message_id = getattr(getattr(event, "message", None), "id", None)

        detection = looks_like_load_confirmation(text)
        if detection.is_load:
            await self._process_load(event, chat, text, chat_id, message_id, detection)
            return True

        # Not a load: it may still be a manual "fuel arranged" in a mapped group.
        await self._maybe_text_arranged(event, text, chat_id)
        return False

    async def _process_load(
        self,
        event: Any,
        chat: Any,
        text: str,
        chat_id: Any,
        message_id: Any,
        detection: Any,
    ) -> None:
        self.loads_seen += 1
        group_title = display_name(chat, fallback=f"Chat {chat_id}")
        logger.info(
            "Load confirmation detected | chat=%s | msg=%s | %s",
            chat_id,
            message_id,
            detection.reason,
        )

        # Idempotency: the same Telegram message opens at most one cycle.
        if message_id is not None and self.state.is_processed(chat_id, message_id):
            logger.info(
                "Load confirmation already processed | chat=%s | msg=%s", chat_id, message_id
            )
            return

        url = build_message_url(
            chat, message_id, chat_id=chat_id, topic_id=forum_topic_id(getattr(event, "message", None))
        )[0]

        mapping = self.units.get(chat_id)
        if mapping is None:
            logger.warning(
                "Fuel parse failed | chat=%s | msg=%s | reason=group_not_mapped",
                chat_id,
                message_id,
            )
            await self._alert_unmapped(group_title, chat_id, url)
            if message_id is not None:
                await self.state.mark_processed(chat_id, message_id)
            return

        logger.info("Fuel unit resolved | chat=%s | unit=%s", chat_id, mapping.unit)

        parsed = parse_pickup(text, deadline_hours_before=self._deadline_hours)
        if isinstance(parsed, ParseFailure):
            self.parse_failures += 1
            logger.warning(
                "Fuel parse failed | chat=%s | msg=%s | reason=%s%s",
                chat_id,
                message_id,
                parsed.reason,
                f" | detail={parsed.detail}" if parsed.detail else "",
            )
            record = await self.state.set_status(
                mapping.unit,
                NEED_TO_CHECK,
                note=parsed.reason,
                driver=mapping.driver,
                chat_id=chat_id,
                group_title=group_title,
                source_message_id=message_id,
                source_message_url=url,
            )
            await self.client.push_status(record.unit, NEED_TO_CHECK, driver=record.driver)
            await self._send_check_alert(record, parsed.reason, parsed.detail)
            if message_id is not None:
                await self.state.mark_processed(chat_id, message_id, mapping.unit)
            return

        self.loads_parsed += 1
        logger.info(
            "Pickup parsed | date=%s | time=%s | timezone=%s (%s)",
            parsed.pickup_local.date().isoformat(),
            parsed.time_text,
            parsed.timezone,
            parsed.timezone_reason,
        )

        driver = mapping.driver or await self._lookup_driver(mapping.unit)

        record, replaced = await self.state.start_cycle(
            mapping.unit,
            deadline=parsed.deadline_utc,
            timezone_name=parsed.timezone,
            driver=driver,
            chat_id=chat_id,
            group_title=group_title,
            source_message_id=message_id,
            source_message_url=url,
        )
        logger.info(
            "Fuel deadline calculated | unit=%s | deadline=%s | local=%s",
            record.unit,
            record.deadline,
            parsed.deadline_local.isoformat(),
        )

        # Deliberately no FuelHelper write while the deadline is still in the
        # future: UPCOMING is a value the board has never had, and item 7 asks
        # for the least disruptive option. The board is written at the deadline.
        if message_id is not None:
            await self.state.mark_processed(chat_id, message_id, mapping.unit)

    async def _lookup_driver(self, unit: str) -> str:
        """Prefer FuelHelper's driver name over a copy stored in the mapping."""
        payload = await self.client.get_unit(unit)
        if isinstance(payload, dict):
            return str(payload.get("driver") or "").strip()
        return ""

    # -- text-based arranged -------------------------------------------------- #
    async def _maybe_text_arranged(self, event: Any, text: str, chat_id: Any) -> None:
        """Accept 'fuel arranged' only where the unit is unambiguous."""
        if len(text) > MAX_ARRANGED_TEXT_CHARS or not ARRANGED_TEXT.match(text):
            return

        mapping = self.units.get(chat_id)
        if mapping is None:
            return

        record = self.state.get(mapping.unit)
        if record is None or record.status == ARRANGED:
            return

        logger.info(
            "Fuel arranged | unit=%s | source=telegram_text | chat=%s", mapping.unit, chat_id
        )
        await self.mark_arranged(mapping.unit, source="telegram_text")

    # -- status changes -------------------------------------------------------- #
    async def mark_arranged(self, unit: str, *, source: str = "telegram_button") -> FuelRecord:
        record = await self.state.set_status(unit, ARRANGED)
        await self.client.push_status(unit, ARRANGED, driver=record.driver)
        logger.info("Fuel arranged | unit=%s | source=%s", unit, source)
        return record

    async def mark_need_check(self, unit: str, *, source: str = "telegram_button") -> FuelRecord:
        record = await self.state.set_status(unit, NEED_TO_CHECK)
        await self.client.push_status(unit, NEED_TO_CHECK, driver=record.driver)
        logger.info("Fuel needs check | unit=%s | source=%s", unit, source)
        return record

    # -- scheduler handlers ----------------------------------------------------- #
    async def _on_transition(self, record: FuelRecord) -> None:
        """A deadline arrived: mirror NEED TO ARRANGE to FuelHelper."""
        await self.client.push_status(record.unit, NEED_TO_ARRANGE, driver=record.driver)

    async def _on_alert(self, record: FuelRecord, is_reminder: bool) -> None:
        await self._send_fuel_alert(record, is_reminder)

    # -- alerts ------------------------------------------------------------------ #
    def _buttons(self, unit: str, url: str | None) -> list[list[Any]]:
        encoded = unit.encode()
        rows = [
            [
                Button.inline("✅ ARRANGED", CB_FUEL_ARRANGED + CB_FUEL_SEP + encoded),
                Button.inline("⚠️ NEED TO CHECK", CB_FUEL_CHECK + CB_FUEL_SEP + encoded),
            ]
        ]
        if url:
            rows.append([Button.url("🔗 OPEN MESSAGE", url)])
        return rows

    async def _send_fuel_alert(self, record: FuelRecord, is_reminder: bool) -> None:
        overdue = record.overdue_for()
        if overdue is None:
            when = "Deadline reached."
        else:
            when = f"Deadline passed {_humanize(overdue.total_seconds())} ago."

        lines = [ALERT_TAG, "", f"🔴 UNIT {record.unit} NEEDS FUEL.", ""]
        if record.driver:
            lines.append(record.driver)
        lines.append(when)
        if is_reminder:
            lines.append(f"Reminder {record.reminder_count + 1}.")
        if record.group_title:
            lines += ["", f"🏢 {record.group_title}"]
        lines += ["", CLOSING_LINE]

        await self._deliver("\n".join(lines), self._buttons(record.unit, record.source_message_url))

    async def _send_check_alert(self, record: FuelRecord, reason: str, detail: str) -> None:
        lines = [
            ALERT_TAG,
            "",
            "⚠️ NEED TO CHECK",
            "",
            f"Unit {record.unit}",
        ]
        if record.driver:
            lines.append(record.driver)
        lines += [
            "",
            "I found a load confirmation, but could not determine the pickup deadline.",
            f"Reason: {reason}",
        ]
        if detail:
            lines.append(truncate(detail, 120))
        if record.group_title:
            lines += ["", f"🏢 {record.group_title}"]

        await self._deliver("\n".join(lines), self._buttons(record.unit, record.source_message_url))

    async def _alert_unmapped(self, group_title: str, chat_id: Any, url: str | None) -> None:
        """A load arrived in a group with no truck mapped to it."""
        text = "\n".join(
            [
                ALERT_TAG,
                "",
                "⚠️ NEED TO CHECK",
                "",
                "A load confirmation arrived in a group with no truck mapped to it.",
                "",
                f"🏢 {group_title}",
                f"Chat ID: {chat_id}",
                "",
                "Map it by sending this inside that group:",
                "/mapfuelunit <unit>",
            ]
        )
        buttons = [[Button.url("🔗 OPEN MESSAGE", url)]] if url else None
        await self._deliver(text, buttons)

    async def _deliver(self, text: str, buttons: Any = None) -> None:
        """Send to every authorized admin, tolerating one bad recipient."""
        for recipient in self._recipients():
            try:
                await self._bot.send_message(
                    recipient, text, buttons=buttons, link_preview=False, parse_mode=None
                )
            except FloodWaitError as exc:
                logger.warning("Flood wait (%ss) sending a fuel alert", exc.seconds)
                await asyncio.sleep(min(int(exc.seconds) + 2, 60))
            except RPCError:
                logger.warning("Could not send a fuel alert to %s", recipient, exc_info=True)

    # -- buttons ------------------------------------------------------------------ #
    async def _on_callback(self, event: Any) -> None:
        try:
            sender_id = getattr(event, "sender_id", None)
            if not self._is_authorized(sender_id):
                logger.warning("Unauthorised fuel action from user id %s", sender_id)
                await self._answer(event, "⛔ Not authorised.", alert=True)
                raise events.StopPropagation

            data = bytes(getattr(event, "data", b"") or b"")
            code, _, raw_unit = data.partition(CB_FUEL_SEP)
            unit = raw_unit.decode("utf-8", "ignore").strip()
            if not unit:
                await self._answer(event, "⚠️ Unknown unit.", alert=True)
                raise events.StopPropagation

            if code == CB_FUEL_ARRANGED:
                record = await self.mark_arranged(unit)
                await self._answer(event, f"Unit {unit}: arranged")
                note = f"✅ UNIT {unit} — fuel arranged."
            else:
                record = await self.mark_need_check(unit)
                await self._answer(event, f"Unit {unit}: needs check")
                note = f"⚠️ UNIT {unit} — marked NEED TO CHECK."

            if record.driver:
                note += f"\n{record.driver}"
            await self._edit(event, note)

        except events.StopPropagation:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to handle a fuel button")

        raise events.StopPropagation

    async def _edit(self, event: Any, text: str) -> None:
        """Replace the alert with its outcome, dropping the now-stale buttons."""
        try:
            await event.edit(text, buttons=None, link_preview=False, parse_mode=None)
        except (MessageIdInvalidError, QueryIdInvalidError):
            logger.debug("Fuel alert message is gone; skipping the edit")
        except RPCError:
            logger.debug("Telegram rejected a fuel alert edit", exc_info=True)

    async def _answer(self, event: Any, message: str = "", *, alert: bool = False) -> None:
        try:
            await event.answer(message or None, alert=alert)
        except (QueryIdInvalidError, MessageIdInvalidError):
            logger.debug("Fuel callback expired before it could be answered")
        except RPCError:
            logger.debug("Telegram rejected a fuel callback answer", exc_info=True)

    # -- views -------------------------------------------------------------------- #
    def status_line(self) -> str:
        counts = self.state.counts_by_status()
        return (
            f"Fuel units: {self.state.count()} "
            f"(need {counts.get(NEED_TO_ARRANGE, 0)}, "
            f"upcoming {counts.get(UPCOMING, 0)}, "
            f"arranged {counts.get(ARRANGED, 0)}, "
            f"check {counts.get(NEED_TO_CHECK, 0)})"
        )

    def desk_text(self) -> str:
        counts = self.state.counts_by_status()
        lines = [
            "🚛 FUEL DESK",
            "",
            f"NEED TO ARRANGE: {counts.get(NEED_TO_ARRANGE, 0)}",
            f"ARRANGED: {counts.get(ARRANGED, 0)}",
            f"UPCOMING: {counts.get(UPCOMING, 0)}",
            f"NEED TO CHECK: {counts.get(NEED_TO_CHECK, 0)}",
        ]
        active = [r for r in self.state.all() if r.status != ARRANGED]
        if active:
            lines += ["", "Active:"]
            moment = now_utc()
            for record in active[:20]:
                detail = record.status
                if record.status == NEED_TO_ARRANGE:
                    overdue = record.overdue_for(moment)
                    if overdue:
                        detail += f" · overdue {_humanize(overdue.total_seconds())}"
                elif record.status == UPCOMING and record.deadline_dt:
                    left = (record.deadline_dt - moment).total_seconds()
                    detail += f" · in {_humanize(left)}"
                lines.append(f"- UNIT {record.unit}: {detail}")
        return "\n".join(lines)
