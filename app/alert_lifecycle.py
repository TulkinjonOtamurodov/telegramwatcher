"""Alert lifecycle: marking an alert seen, then deleting it on a timer.

**Telegram does not tell a bot when a URL button is pressed.** A
``KeyboardButtonUrl`` is handled entirely by the client: it opens the link and
sends nothing back to the server. There is no update, no callback query, no
counter -- the bot genuinely cannot know. (The one exception, ``urlAuth`` /
Login URL buttons, is an OAuth handshake for external websites registered with
BotFather; it does not apply to ``t.me`` deep links.)

So the alert carries two buttons: the real URL button that opens the message,
and a callback button next to it that the admin taps to dismiss. Tapping it
schedules deletion of *that recipient's copy* five minutes later.

Timers are in-memory ``asyncio`` tasks. **A container restart cancels every
pending deletion** -- the alert simply stays in the chat. That is a deliberate
trade for a five-minute timer: persisting it would mean a file write per alert
and a rehydration pass at startup, for a failure window most restarts never hit.
"""

from __future__ import annotations

import asyncio
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

logger = get_logger("lifecycle")

#: Callback data for the dismiss button. Short, and distinct from every
#: control-panel code so the two handlers never collide.
CB_ALERT_SEEN = b"ALSEEN"

#: How long after the tap the alert is removed.
DELETE_AFTER_SECONDS = 300  # 5 minutes

OPEN_MESSAGE_LABEL = "\U0001f517 OPEN MESSAGE"
SEEN_LABEL = "✅ SEEN — DELETE IN 5 MIN"
PENDING_LABEL = "🕒 Deleting in 5 min…"


class AlertLifecycle:
    """Owns the dismiss callback and the pending-deletion timers."""

    def __init__(
        self,
        bot_client: Any,
        *,
        is_authorized: Callable[[int | None], bool],
        delay: int = DELETE_AFTER_SECONDS,
    ) -> None:
        self._bot = bot_client
        self._is_authorized = is_authorized
        self._delay = delay
        # Keyed by (chat_id, message_id): one timer per delivered copy, so one
        # recipient dismissing never touches another recipient's alert.
        self._pending: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._registered = False

    # -- lifecycle --------------------------------------------------------- #
    def register(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(
            self._on_seen, events.CallbackQuery(pattern=CB_ALERT_SEEN)
        )
        self._registered = True
        logger.info(
            "Alert lifecycle registered (dismiss then delete after %ds)", self._delay
        )

    async def stop(self) -> None:
        """Cancel every pending deletion; alerts are left in place."""
        tasks = list(self._pending.values())
        self._pending.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                pass
        if tasks:
            logger.info("Cancelled %d pending alert deletion(s) on shutdown", len(tasks))

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -- callback ---------------------------------------------------------- #
    async def _on_seen(self, event: Any) -> None:
        """Handle a tap on the dismiss button."""
        try:
            sender_id = getattr(event, "sender_id", None)
            if not self._is_authorized(sender_id):
                logger.warning("Unauthorised alert dismissal from user id %s", sender_id)
                await self._answer(event, "⛔ Not authorised.", alert=True)
                raise events.StopPropagation

            chat_id = getattr(event, "chat_id", None)
            message_id = getattr(event, "message_id", None)
            if message_id is None:
                message_id = getattr(getattr(event, "query", None), "msg_id", None)

            if chat_id is None or message_id is None:
                await self._answer(event, "⚠️ Could not identify this alert.", alert=True)
                raise events.StopPropagation

            minutes = max(1, self._delay // 60)
            await self._answer(event, f"Will disappear in {minutes} min")
            await self._mark_pending(event)
            self.schedule_delete(int(chat_id), int(message_id), recipient=sender_id)

            logger.info(
                "Alert marked seen | recipient=%s | alert_msg=%s", sender_id, message_id
            )

        except events.StopPropagation:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to handle an alert dismissal")

        # This callback is ours; the control panel must not also react to it.
        raise events.StopPropagation

    async def _mark_pending(self, event: Any) -> None:
        """Grey the dismiss button out, keeping the link button usable."""
        try:
            message = await event.get_message()
        except Exception:
            logger.debug("Could not load the alert message to update it", exc_info=True)
            return
        if message is None:
            return

        url = self._existing_url(message)
        buttons: list[list[Any]] = []
        if url:
            buttons.append([Button.url(OPEN_MESSAGE_LABEL, url)])
        buttons.append([Button.inline(PENDING_LABEL, CB_ALERT_SEEN)])

        try:
            # The text is passed back explicitly: Telethon parses whatever it is
            # given, and handing it None would raise rather than keep the body.
            await event.edit(
                message.raw_text or "",
                buttons=buttons,
                link_preview=False,
                parse_mode=None,
            )
        except RPCError:
            logger.debug("Could not update the alert buttons", exc_info=True)
        except Exception:
            logger.debug("Unexpected error updating the alert buttons", exc_info=True)

    @staticmethod
    def _existing_url(message: Any) -> str | None:
        """Recover the OPEN MESSAGE url already attached to the alert."""
        markup = getattr(message, "reply_markup", None)
        for row in getattr(markup, "rows", None) or []:
            for button in getattr(row, "buttons", None) or []:
                url = getattr(button, "url", None)
                if url:
                    return str(url)
        return None

    # -- deletion ---------------------------------------------------------- #
    def schedule_delete(
        self, chat_id: int, message_id: int, *, recipient: int | None = None
    ) -> None:
        """Queue one alert copy for deletion, without blocking anything."""
        key = (int(chat_id), int(message_id))
        if key in self._pending:
            logger.debug("Deletion already scheduled for %s", key)
            return
        task = asyncio.create_task(
            self._delete_later(key, recipient), name=f"alert-delete-{message_id}"
        )
        self._pending[key] = task
        task.add_done_callback(lambda _t, k=key: self._pending.pop(k, None))

    async def _delete_later(
        self, key: tuple[int, int], recipient: int | None
    ) -> None:
        chat_id, message_id = key
        try:
            await asyncio.sleep(self._delay)
            await self._bot.delete_messages(chat_id, [message_id])
            logger.info(
                "Alert deleted | recipient=%s | alert_msg=%s", recipient or chat_id, message_id
            )
        except asyncio.CancelledError:
            raise
        except (MessageIdInvalidError, MessageDeleteForbiddenError) as exc:
            # Already gone, or too old to delete. Neither is worth an error.
            logger.info(
                "Alert %s not deleted (%s); nothing to do", message_id, type(exc).__name__
            )
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while deleting alert %s", exc.seconds, message_id)
        except RPCError:
            logger.info("Telegram refused to delete alert %s", message_id, exc_info=True)
        except Exception:
            logger.exception("Unexpected failure deleting alert %s", message_id)

    # -- plumbing ---------------------------------------------------------- #
    async def _answer(self, event: Any, message: str = "", *, alert: bool = False) -> None:
        try:
            await event.answer(message or None, alert=alert)
        except (QueryIdInvalidError, MessageIdInvalidError):
            logger.debug("Callback query expired before it could be answered")
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while answering a dismissal", exc.seconds)
        except RPCError:
            logger.debug("Telegram rejected a callback answer", exc_info=True)
