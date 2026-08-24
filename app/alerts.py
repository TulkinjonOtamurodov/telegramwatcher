"""Alert formatting and delivery.

:func:`build_alert` turns a Telegram event plus the reasons it matched into the
final text. :class:`AlertDispatcher` owns a small queue and a worker task, so a
flood wait while sending can never block the message watcher.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Sequence

from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    RPCError,
    UserIsBlockedError,
)

from app.logging_config import get_logger
from app.settings import SettingsStore
from app.utils import (
    TELEGRAM_MAX_MESSAGE,
    build_group_link,
    build_message_link,
    display_name,
    render_template,
    truncate,
    utc_timestamp,
)

logger = get_logger("alerts")

REASON_MENTION = "mention"
REASON_REPLY = "reply"
REASON_KEYWORD_PREFIX = "keyword:"

_MENTION_LABEL = "\U0001f7e5 Mention"
_REPLY_LABEL = "\U0001f9f7 Reply"
_KEYWORD_LABEL = "\U0001f50d Keyword"

#: Cap on queued alerts. Beyond this the oldest is dropped, which is preferable
#: to unbounded memory growth during a long Telegram outage.
QUEUE_MAX_SIZE = 500

#: Sending is retried this many times before an alert is abandoned.
MAX_SEND_ATTEMPTS = 3

#: Never sleep longer than this for a flood wait; log and give up instead.
MAX_FLOOD_WAIT_SECONDS = 900


def keyword_reason(keyword: str) -> str:
    """Build the raw reason code the watcher emits for a keyword hit."""
    return f"{REASON_KEYWORD_PREFIX}{keyword}"


def format_reason(raw: str) -> str:
    """Turn a raw reason code into its display form.

    ``"mention"`` -> ``"\U0001f7e5 Mention"``,
    ``"keyword:fuel"`` -> ``"\U0001f50d Keyword: fuel"``.
    The legacy ``"Keyword match: fuel"`` phrasing is understood too.
    """
    text = (raw or "").strip()
    lowered = text.lower()
    if lowered == REASON_MENTION:
        return _MENTION_LABEL
    if lowered == REASON_REPLY:
        return _REPLY_LABEL
    if lowered.startswith(REASON_KEYWORD_PREFIX):
        return f"{_KEYWORD_LABEL}: {text[len(REASON_KEYWORD_PREFIX):].strip()}"
    if lowered.startswith("keyword match:"):
        return f"{_KEYWORD_LABEL}: {text.split(':', 1)[1].strip()}"
    return text


def format_reasons(reasons: Iterable[str]) -> str:
    formatted = [format_reason(reason) for reason in reasons]
    return " | ".join(part for part in formatted if part) or "-"


def _describe_media(message: Any) -> str:
    """A short label for a message that carries media."""
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None):
        return "voice message"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "media", None):
        return "media"
    return ""


def build_alert(
    event: Any,
    *,
    reasons: Sequence[str],
    keyword_hits: Sequence[str],
    sender_name: str,
    sender_username: str | None = None,
    chat: Any = None,
    settings: SettingsStore,
    classification: dict[str, Any] | None = None,
) -> str:
    """Render the alert text for one matched message."""
    message = getattr(event, "message", None)
    chat = chat if chat is not None else getattr(event, "chat", None)
    message_id = getattr(message, "id", 0) or 0

    group_name = display_name(chat, fallback="Unknown group")
    group_link = build_group_link(chat)
    message_link = build_message_link(chat, message_id)

    sender_display = sender_name or "Unknown"
    if sender_username:
        sender_display = f"{sender_display} (@{sender_username})"

    raw_text = (getattr(message, "raw_text", None) or getattr(event, "raw_text", "") or "").strip()
    if not settings.include_message_text:
        body = "(message text hidden by settings)"
    elif raw_text:
        body = truncate(raw_text, settings.max_message_chars)
    else:
        media = _describe_media(message)
        body = f"(no text - {media})" if media else "(no text)"

    preview_limit = settings.max_keyword_preview
    shown_hits = list(keyword_hits)[:preview_limit]
    extra = len(keyword_hits) - len(shown_hits)
    hits_text = ", ".join(shown_hits) if shown_hits else "-"
    if extra > 0:
        hits_text = f"{hits_text} (+{extra} more)"

    classification = classification or {}
    variables: dict[str, Any] = {
        "timestamp": utc_timestamp(settings.timestamp_format, getattr(message, "date", None)),
        "reasons": format_reasons(reasons),
        "group": group_name,
        "group_link": group_link,
        "sender": sender_display,
        "sender_name": sender_name,
        "sender_username": f"@{sender_username}" if sender_username else "-",
        "sender_id": getattr(event, "sender_id", None) or "-",
        "chat_id": getattr(event, "chat_id", None) or "-",
        "message_id": message_id,
        "keyword_hits": hits_text,
        "message_text": body,
        "message_link": message_link,
        # Populated once the AI layer is switched on; harmless placeholders now.
        "category": classification.get("category", "-"),
        "severity": classification.get("severity", "-"),
        "summary": classification.get("summary", "") or "-",
        "requires_action": "YES" if classification.get("requires_action") else "NO",
        "unit": classification.get("unit") or "-",
    }

    rendered = render_template(settings.template, variables)
    return truncate(rendered, TELEGRAM_MAX_MESSAGE, suffix="\n[...]")


class AlertDispatcher:
    """Queues alerts and delivers them through the bot client."""

    def __init__(self, bot_client: Any, recipients: Sequence[int] | None = None) -> None:
        self._bot = bot_client
        self._recipients: list[int] = list(recipients or [])
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._worker: asyncio.Task[None] | None = None
        self._sent = 0
        self._failed = 0

    # -- lifecycle --------------------------------------------------------- #
    def set_recipients(self, recipients: Iterable[int]) -> None:
        self._recipients = list(dict.fromkeys(recipients))
        logger.info("Alert recipients: %s", self._recipients or "(none configured)")

    @property
    def recipients(self) -> list[int]:
        return list(self._recipients)

    @property
    def stats(self) -> dict[str, int]:
        return {"sent": self._sent, "failed": self._failed, "queued": self._queue.qsize()}

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="alert-dispatcher")
            logger.info("Alert dispatcher started")

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """Give queued alerts a moment to flush, then cancel the worker."""
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            logger.warning("Shutting down with %d alert(s) still queued", self._queue.qsize())
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None
        logger.info("Alert dispatcher stopped")

    # -- queueing ---------------------------------------------------------- #
    async def enqueue(self, text: str) -> None:
        """Hand an alert to the worker without ever blocking the caller."""
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                logger.warning(
                    "Alert queue full (%d); dropped the oldest alert (%d chars)",
                    QUEUE_MAX_SIZE,
                    len(dropped),
                )
            except asyncio.QueueEmpty:  # pragma: no cover - race, harmless
                pass
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:  # pragma: no cover - race, harmless
                self._failed += 1
                logger.error("Alert dropped: queue still full")

    async def send_now(self, text: str) -> bool:
        """Send immediately, bypassing the queue (used for the startup notice)."""
        return await self._deliver(text)

    # -- worker ------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._deliver(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected failure while delivering an alert")
            finally:
                self._queue.task_done()

    async def _deliver(self, text: str) -> bool:
        if not self._recipients:
            logger.error("No alert recipients configured; alert not delivered")
            self._failed += 1
            return False

        delivered_any = False
        for recipient in self._recipients:
            if await self._send_to(recipient, text):
                delivered_any = True

        if delivered_any:
            self._sent += 1
        else:
            self._failed += 1
        return delivered_any

    async def _send_to(self, recipient: int, text: str) -> bool:
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                await self._bot.send_message(
                    recipient,
                    text,
                    link_preview=False,
                    parse_mode=None,  # user content is sent verbatim, never parsed
                )
                return True

            except FloodWaitError as exc:
                wait_for = int(getattr(exc, "seconds", 0)) + 2
                if wait_for > MAX_FLOOD_WAIT_SECONDS:
                    logger.error(
                        "Flood wait of %ss exceeds the %ss cap; dropping alert to %s",
                        wait_for,
                        MAX_FLOOD_WAIT_SECONDS,
                        recipient,
                    )
                    return False
                logger.warning("Flood wait: sleeping %ss before retrying %s", wait_for, recipient)
                await asyncio.sleep(wait_for)

            except (UserIsBlockedError, InputUserDeactivatedError) as exc:
                logger.error(
                    "Cannot message %s (%s). Open a private chat with the bot and press Start.",
                    recipient,
                    type(exc).__name__,
                )
                return False

            except (PeerIdInvalidError, ChatWriteForbiddenError) as exc:
                logger.error(
                    "Recipient %s is unreachable (%s). Check ADMIN_USER_IDS and that "
                    "the account has sent /start to the bot.",
                    recipient,
                    type(exc).__name__,
                )
                return False

            except (ConnectionError, TimeoutError, OSError) as exc:
                logger.warning(
                    "Network problem sending to %s (attempt %d/%d): %s",
                    recipient,
                    attempt,
                    MAX_SEND_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(min(2 ** attempt, 15))

            except RPCError:
                logger.exception("Telegram rejected the alert for %s", recipient)
                return False

            except asyncio.CancelledError:
                raise

        logger.error("Giving up on alert to %s after %d attempts", recipient, MAX_SEND_ATTEMPTS)
        return False
