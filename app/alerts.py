"""Alert formatting and delivery.

:func:`build_alert` turns a matched message into the text an admin sees plus the
deep link that becomes the "OPEN MESSAGE" button. :class:`AlertDispatcher` owns
a small queue and a worker task, so a flood wait while sending can never block
the message watcher.

Alerts are sent with ``parse_mode=None``. Nothing in a group message is ever
parsed as Markdown or HTML, so a sender name, group title or message body cannot
break the rendering or inject formatting -- the strongest form of escaping is
not parsing at all. The button is separate from the text for the same reason.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from telethon import Button
from telethon.errors import (
    ButtonUrlInvalidError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    RPCError,
    UserIsBlockedError,
)

from app.alert_lifecycle import CB_ALERT_SEEN, OPEN_MESSAGE_LABEL, SEEN_LABEL
from app.logging_config import get_logger
from app.settings import SettingsStore
from app.utils import (
    TELEGRAM_MAX_MESSAGE,
    build_group_link,
    build_message_link,
    build_message_url,
    display_name,
    render_template,
    split_heading,
    truncate,
    utc_timestamp,
)

logger = get_logger("alerts")

REASON_MENTION = "mention"
REASON_REPLY = "reply"
REASON_KEYWORD_PREFIX = "keyword:"

#: Tag used when no watched member was mentioned.
DEFAULT_TAG = "SAFETY"

#: Separates the trigger words on the second line of an alert.
TRIGGER_SEPARATOR = " • "

#: Marks a detected heading line in the message block.
HEADING_PREFIX = "\U0001f4c4 "

_MENTION_TRIGGER = "MENTION"
_REPLY_TRIGGER = "REPLY"

# Legacy display labels, still used by the {{reasons}} placeholder so an older
# custom template keeps rendering the way its author expects.
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


@dataclass(frozen=True)
class Alert:
    """One rendered alert: the text, an optional link, and its controls.

    ``dismissible`` is False for system notices such as the startup message --
    those get no buttons at all.
    """

    text: str
    url: str | None = None
    dismissible: bool = True


def keyword_reason(keyword: str) -> str:
    """Build the raw reason code the watcher emits for a keyword hit."""
    return f"{REASON_KEYWORD_PREFIX}{keyword}"


def format_reason(raw: str) -> str:
    """Turn a raw reason code into its legacy display form.

    Retained for the ``{{reasons}}`` placeholder, which older custom templates
    may still use. The current default uses ``{{triggers}}`` instead.
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


def format_tags(tags: Sequence[str] | None) -> str:
    """``#RAYN #THOMAS``, or ``#SAFETY`` when no watched member was mentioned."""
    cleaned = [str(tag).strip().lstrip("#").upper() for tag in (tags or [])]
    cleaned = [tag for tag in cleaned if tag]
    if not cleaned:
        return f"#{DEFAULT_TAG}"
    seen: list[str] = []
    for tag in cleaned:
        if tag not in seen:
            seen.append(tag)
    return " ".join(f"#{tag}" for tag in seen)


def format_triggers(reasons: Sequence[str], keyword_hits: Sequence[str], limit: int) -> str:
    """``MENTION • INSURANCE``: what fired, uppercase, in a fixed order."""
    parts: list[str] = []
    if REASON_MENTION in reasons:
        parts.append(_MENTION_TRIGGER)
    if REASON_REPLY in reasons:
        parts.append(_REPLY_TRIGGER)
    for keyword in list(keyword_hits)[: max(1, limit)]:
        upper = str(keyword).strip().upper()
        if upper and upper not in parts:
            parts.append(upper)
    return TRIGGER_SEPARATOR.join(parts) if parts else "ALERT"


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
    tags: Sequence[str] | None = None,
) -> Alert:
    """Render one matched message into the alert an admin receives."""
    message = getattr(event, "message", None)
    chat = chat if chat is not None else getattr(event, "chat", None)
    message_id = getattr(message, "id", 0) or 0

    group_name = display_name(chat, fallback="Unknown group")

    # One central helper decides the link; here we only report what it decided.
    url, kind = build_message_url(
        chat, message_id, chat_id=getattr(event, "chat_id", None)
    )
    if url:
        logger.info(
            "Message URL built | group=%s | msg=%s | url_type=%s",
            group_name,
            message_id,
            kind,
        )
    else:
        logger.warning(
            "Message URL unavailable | group=%s | msg=%s | reason=%s",
            group_name,
            message_id,
            kind,
        )

    sender_display = sender_name or "Unknown"
    if sender_username:
        sender_display = f"{sender_display} (@{sender_username})"

    raw_text = (
        getattr(message, "raw_text", None) or getattr(event, "raw_text", "") or ""
    ).strip()

    heading: str | None = None
    if not settings.include_message_text:
        body = "(message text hidden by settings)"
    elif raw_text:
        heading, body = split_heading(raw_text)
        body = truncate(body, settings.max_message_chars)
    else:
        media = _describe_media(message)
        body = f"(no text - {media})" if media else "(no text)"

    heading_line = f"{HEADING_PREFIX}{heading}" if heading else ""
    message_block = f"{heading_line}\n{body}" if heading_line else body

    preview_limit = settings.max_keyword_preview
    shown_hits = list(keyword_hits)[:preview_limit]
    extra = len(keyword_hits) - len(shown_hits)
    hits_text = ", ".join(shown_hits) if shown_hits else "-"
    if extra > 0:
        hits_text = f"{hits_text} (+{extra} more)"

    classification = classification or {}
    variables: dict[str, Any] = {
        # -- the current format --
        "tags": format_tags(tags),
        "triggers": format_triggers(reasons, keyword_hits, preview_limit),
        "sender": sender_display,
        "group": group_name,
        "heading": heading_line,
        "body": body,
        "message_block": message_block,
        # -- kept working for older custom templates --
        "timestamp": utc_timestamp(
            settings.timestamp_format, getattr(message, "date", None)
        ),
        "reasons": format_reasons(reasons),
        "group_link": build_group_link(chat),
        "sender_name": sender_name,
        "sender_username": f"@{sender_username}" if sender_username else "-",
        "sender_id": getattr(event, "sender_id", None) or "-",
        "chat_id": getattr(event, "chat_id", None) or "-",
        "message_id": message_id,
        "keyword_hits": hits_text,
        "message_text": body,
        "message_link": build_message_link(chat, message_id),
        # -- populated only once the AI layer is switched on --
        "category": classification.get("category", "-"),
        "severity": classification.get("severity", "-"),
        "summary": classification.get("summary", "") or "-",
        "requires_action": "YES" if classification.get("requires_action") else "NO",
        "unit": classification.get("unit") or "-",
    }

    rendered = render_template(settings.template, variables)
    return Alert(text=truncate(rendered, TELEGRAM_MAX_MESSAGE, suffix="\n[...]"), url=url)


class AlertDispatcher:
    """Queues alerts and delivers them through the bot client."""

    def __init__(self, bot_client: Any, recipients: Sequence[int] | None = None) -> None:
        self._bot = bot_client
        self._recipients: list[int] = list(recipients or [])
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
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
    async def enqueue(self, alert: Alert | str) -> None:
        """Hand an alert to the worker without ever blocking the caller."""
        item = alert if isinstance(alert, Alert) else Alert(text=str(alert))
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                logger.warning(
                    "Alert queue full (%d); dropped the oldest alert (%d chars)",
                    QUEUE_MAX_SIZE,
                    len(dropped.text),
                )
            except asyncio.QueueEmpty:  # pragma: no cover - race, harmless
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - race, harmless
                self._failed += 1
                logger.error("Alert dropped: queue still full")

    async def send_now(self, alert: Alert | str) -> bool:
        """Send immediately, bypassing the queue (used for the startup notice).

        A plain string is a system notice, so it carries no buttons.
        """
        item = (
            alert
            if isinstance(alert, Alert)
            else Alert(text=str(alert), dismissible=False)
        )
        return await self._deliver(item)

    # -- worker ------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            alert = await self._queue.get()
            try:
                await self._deliver(alert)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected failure while delivering an alert")
            finally:
                self._queue.task_done()

    async def _deliver(self, alert: Alert) -> bool:
        if not self._recipients:
            logger.error("No alert recipients configured; alert not delivered")
            self._failed += 1
            return False

        delivered_any = False
        for recipient in self._recipients:
            if await self._send_to(recipient, alert):
                delivered_any = True

        if delivered_any:
            self._sent += 1
        else:
            self._failed += 1
        return delivered_any

    @staticmethod
    def _buttons(alert: Alert, *, with_url: bool = True) -> list[list[Any]] | None:
        """The deep-link button when there is a link, plus the dismiss button.

        Telegram never reports a URL-button press, so the second (callback)
        button is what tells us the admin is done with the alert.
        """
        if not alert.dismissible:
            return None
        rows: list[list[Any]] = []
        if alert.url and with_url:
            rows.append([Button.url(OPEN_MESSAGE_LABEL, alert.url)])
        rows.append([Button.inline(SEEN_LABEL, CB_ALERT_SEEN)])
        return rows

    async def _send_to(self, recipient: int, alert: Alert) -> bool:
        buttons = self._buttons(alert)

        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                await self._bot.send_message(
                    recipient,
                    alert.text,
                    buttons=buttons,
                    link_preview=False,
                    parse_mode=None,  # group content is sent verbatim, never parsed
                )
                return True

            except ButtonUrlInvalidError:
                # A link Telegram will not accept must never cost us the alert.
                # Drop only the link button; the dismiss control stays.
                logger.warning(
                    "Message URL rejected by Telegram | msg_url=%r | sending without it",
                    alert.url,
                )
                retry = self._buttons(alert, with_url=False)
                if buttons == retry:
                    return False
                buttons = retry

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
