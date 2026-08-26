"""VIEW MESSAGE clicks, and deleting an alert five minutes after one.

**Telegram does not report clicks on URL buttons.** A ``KeyboardButtonUrl`` is
resolved entirely inside the client: it opens the link and sends the bot
nothing -- no update, no callback query, no counter. So a single button cannot
both open ``t.me/c/123/456`` and tell us it was pressed.

``answerCallbackQuery`` does take a ``url``, but Telegram restricts it to game
URLs and ``t.me/<bot>?start=`` deep links back to the bot itself. It will not
open an arbitrary chat message, so it cannot close the gap either. (The other
candidate, ``KeyboardButtonUrlAuth`` / Login URL, is an OAuth handshake for a
domain registered with BotFather -- also not applicable to a ``t.me`` link.)

So VIEW MESSAGE is a **callback** button. Pressing it is what MAKIMA observes:
it starts the five-minute timer, then swaps the keyboard for a real URL button
so the very next tap opens the message. Two taps, one visible button at a time,
and no second "seen" button to press -- the interaction that begins viewing is
the interaction that starts the countdown.

Deletion is scheduled *only* by that click. An alert nobody touches stays
forever. Pending deletions are persisted, so a restart mid-countdown resumes
rather than forgetting.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from telethon import Button, events
from telethon.errors import (
    FloodWaitError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    QueryIdInvalidError,
    RPCError,
)

from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("lifecycle")

#: Callback prefix. Data stays tiny -- the URL lives server-side, never in the
#: payload, which keeps us well inside Telegram's 64-byte limit.
CB_VIEW_PREFIX = "v:"

#: How long after the click the alert is removed.
DELETE_AFTER_SECONDS = 300  # 5 minutes

#: How often pending deletions are checked.
SWEEP_SECONDS = 30

#: Unviewed records are pruned after this long, so the file cannot grow forever.
RECORD_TTL_HOURS = 72

VIEW_LABEL = "🔗 VIEW MESSAGE"
OPEN_LABEL = "🔗 OPEN IN TELEGRAM"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class AlertView:
    """One delivered alert copy and its deletion state."""

    token: str
    recipient: int
    chat_id: int
    alert_message_id: int | None = None
    source_url: str = ""
    source_chat_id: str = ""
    source_message_id: int | None = None
    created_at: str = field(default_factory=lambda: _iso(_now()) or "")
    viewed_at: str | None = None
    delete_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class AlertLifecycle:
    """Owns the VIEW callback and the pending-deletion sweep."""

    def __init__(
        self,
        bot_client: Any,
        *,
        is_authorized: Callable[[int | None], bool],
        path: Path,
        delay: int = DELETE_AFTER_SECONDS,
        sweep_seconds: int = SWEEP_SECONDS,
    ) -> None:
        self._bot = bot_client
        self._is_authorized = is_authorized
        self._path = path
        self._delay = delay
        self._sweep_seconds = sweep_seconds
        self._lock = asyncio.Lock()
        self._records: dict[str, AlertView] = {}
        self._task: asyncio.Task[None] | None = None
        self._registered = False

    # -- persistence -------------------------------------------------------- #
    def _read(self) -> dict[str, AlertView]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read %s (%s); starting empty", self._path, exc)
            return {}

        records: dict[str, AlertView] = {}
        for token, payload in (raw.get("pending") or {}).items():
            if not isinstance(payload, dict):
                continue
            fields = {
                key: value
                for key, value in payload.items()
                if key in AlertView.__dataclass_fields__
            }
            fields["token"] = str(token)
            try:
                records[str(token)] = AlertView(**fields)
            except TypeError:
                logger.warning("Skipping malformed alert-view record %s", token)
        return records

    async def _write(self) -> None:
        payload = json.dumps(
            {"pending": {token: rec.to_json() for token, rec in self._records.items()}},
            indent=2,
            ensure_ascii=False,
        )
        await asyncio.to_thread(atomic_write_text, self._path, payload + "\n")

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        async with self._lock:
            self._records = await asyncio.to_thread(self._read)
        logger.info(
            "Alert lifecycle loaded | pending=%d | delete_after=%ds",
            len(self._records),
            self._delay,
        )
        self._register()
        # Anything already due -- clicked before a restart that outlasted the
        # countdown -- is removed on this first sweep, not forgotten.
        await self._sweep()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="alert-lifecycle")

    def _register(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(
            self._on_view, events.CallbackQuery(pattern=CB_VIEW_PREFIX.encode())
        )
        self._registered = True
        logger.info("VIEW MESSAGE handler registered")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    @property
    def pending_count(self) -> int:
        return sum(1 for rec in self._records.values() if rec.delete_at)

    # -- registration from the dispatcher ------------------------------------ #
    async def new_view(
        self,
        *,
        recipient: int,
        source_url: str,
        source_chat_id: Any = "",
        source_message_id: int | None = None,
    ) -> str:
        """Reserve a token before the alert is sent, and return it.

        The token has to exist before sending, because it goes inside the
        button. The alert's own message id is attached afterwards.
        """
        token = secrets.token_hex(4)
        record = AlertView(
            token=token,
            recipient=int(recipient),
            chat_id=int(recipient),
            source_url=source_url,
            source_chat_id=str(source_chat_id or ""),
            source_message_id=source_message_id,
        )
        async with self._lock:
            self._records[token] = record
            self._prune()
            await self._write()
        return token

    async def attach_message(self, token: str, alert_message_id: int) -> None:
        """Record which delivered message a token belongs to."""
        async with self._lock:
            record = self._records.get(token)
            if record is None:
                return
            record.alert_message_id = int(alert_message_id)
            await self._write()

    @staticmethod
    def view_button(token: str) -> Any:
        return Button.inline(VIEW_LABEL, f"{CB_VIEW_PREFIX}{token}".encode())

    def _prune(self) -> None:
        """Drop stale unviewed records. Viewed ones are kept until deleted."""
        cutoff = _now() - timedelta(hours=RECORD_TTL_HOURS)
        for token in [
            token
            for token, rec in self._records.items()
            if not rec.delete_at and (_parse(rec.created_at) or _now()) < cutoff
        ]:
            self._records.pop(token, None)

    # -- the click ------------------------------------------------------------ #
    async def _on_view(self, event: Any) -> None:
        try:
            sender_id = getattr(event, "sender_id", None)
            if not self._is_authorized(sender_id):
                logger.warning("Unauthorised VIEW callback from user id %s", sender_id)
                await self._answer(event, "⛔ Not authorised.", alert=True)
                raise events.StopPropagation

            data = bytes(getattr(event, "data", b"") or b"").decode("utf-8", "ignore")
            token = data[len(CB_VIEW_PREFIX):].strip()
            record = self._records.get(token)

            if record is None:
                await self._answer(event, "This alert is no longer tracked.", alert=True)
                raise events.StopPropagation

            # A token belongs to one recipient's copy. Another admin pressing a
            # forged payload must not touch it.
            if record.recipient != sender_id:
                logger.warning(
                    "VIEW token %s belongs to %s, pressed by %s",
                    token,
                    record.recipient,
                    sender_id,
                )
                await self._answer(event, "⛔ Not your alert.", alert=True)
                raise events.StopPropagation

            already = record.delete_at is not None
            if not already:
                async with self._lock:
                    record.viewed_at = _iso(_now())
                    record.delete_at = _iso(_now() + timedelta(seconds=self._delay))
                    await self._write()
                logger.info(
                    "VIEW MESSAGE clicked | recipient=%s | alert_msg=%s | source_msg=%s",
                    record.recipient,
                    record.alert_message_id,
                    record.source_message_id,
                )
                logger.info(
                    "Alert deletion scheduled | alert_msg=%s | delay=%d",
                    record.alert_message_id,
                    self._delay,
                )

            minutes = max(1, self._delay // 60)
            await self._answer(
                event,
                f"Opening… this alert disappears in {minutes} min"
                if not already
                else "Already counting down",
            )
            # Swap in a real URL button: the next tap opens the message.
            await self._show_open_button(event, record)

        except events.StopPropagation:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to handle a VIEW MESSAGE click")

        raise events.StopPropagation

    async def _show_open_button(self, event: Any, record: AlertView) -> None:
        if not record.source_url:
            return
        try:
            message = await event.get_message()
            text = getattr(message, "raw_text", "") or ""
            await event.edit(
                text,
                buttons=[[Button.url(OPEN_LABEL, record.source_url)]],
                link_preview=False,
                parse_mode=None,
            )
        except RPCError:
            logger.debug("Could not swap in the open-in-Telegram button", exc_info=True)
        except Exception:
            logger.debug("Unexpected error swapping the VIEW button", exc_info=True)

    # -- the sweep ------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Alert lifecycle sweep failed")

    async def _sweep(self) -> None:
        """Delete every alert whose countdown has finished."""
        instant = _now()
        due = [
            record
            for record in list(self._records.values())
            if record.delete_at
            and record.alert_message_id
            and (_parse(record.delete_at) or instant) <= instant
        ]
        if not due:
            return

        for record in due:
            await self._delete(record)

        async with self._lock:
            for record in due:
                self._records.pop(record.token, None)
            await self._write()

    async def _delete(self, record: AlertView) -> None:
        """Remove one alert copy. The source message is never touched."""
        try:
            await self._bot.delete_messages(record.chat_id, [record.alert_message_id])
            logger.info(
                "Alert deleted after view | recipient=%s | alert_msg=%s",
                record.recipient,
                record.alert_message_id,
            )
        except (MessageIdInvalidError, MessageDeleteForbiddenError):
            logger.info("Alert already gone | alert_msg=%s", record.alert_message_id)
        except FloodWaitError as exc:
            logger.warning(
                "Flood wait (%ss) deleting alert %s", exc.seconds, record.alert_message_id
            )
        except RPCError:
            logger.info(
                "Telegram refused to delete alert %s", record.alert_message_id, exc_info=True
            )
        except Exception:
            logger.exception("Unexpected failure deleting alert %s", record.alert_message_id)

    # -- plumbing -------------------------------------------------------------- #
    async def _answer(self, event: Any, message: str = "", *, alert: bool = False) -> None:
        try:
            await event.answer(message or None, alert=alert)
        except (QueryIdInvalidError, MessageIdInvalidError):
            logger.debug("Callback query expired before it could be answered")
        except RPCError:
            logger.debug("Telegram rejected a callback answer", exc_info=True)
