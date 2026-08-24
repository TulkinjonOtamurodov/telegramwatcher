"""The message watcher: detects mentions, replies and keyword hits.

Only incoming group and channel messages are inspected. Private chats, your own
outgoing messages, and anything that matches no rule are ignored without ever
touching the network.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from telethon import events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import MessageEntityMentionName

from app.ai_classifier import classify_message
from app.alerts import (
    REASON_MENTION,
    REASON_REPLY,
    AlertDispatcher,
    build_alert,
    keyword_reason,
)
from app.keywords import KeywordStore
from app.logging_config import get_logger
from app.settings import SettingsStore
from app.utils import display_name

logger = get_logger("watcher")


class Watcher:
    """Registers the ``NewMessage`` handler and evaluates every group message."""

    def __init__(
        self,
        user_client: Any,
        *,
        settings: SettingsStore,
        keywords: KeywordStore,
        dispatcher: AlertDispatcher,
    ) -> None:
        self._client = user_client
        self._settings = settings
        self._keywords = keywords
        self._dispatcher = dispatcher

        self._me_id: int | None = None
        self._me_username: str | None = None
        self._mention_pattern: re.Pattern[str] | None = None
        self._registered = False

        self.messages_seen = 0
        self.alerts_raised = 0

    # -- lifecycle --------------------------------------------------------- #
    def bind_identity(self, me: Any) -> None:
        """Remember who 'I' am so mentions and replies can be recognised."""
        self._me_id = getattr(me, "id", None)
        username = getattr(me, "username", None)
        self._me_username = str(username) if username else None
        if self._me_username:
            self._mention_pattern = re.compile(
                rf"(?<!\w)@{re.escape(self._me_username)}(?!\w)", re.IGNORECASE
            )
        else:
            self._mention_pattern = None
            logger.warning(
                "Your account has no @username; text mentions cannot be matched by name "
                "(Telegram's own mention flag still works)."
            )

    async def start(self) -> None:
        if self._me_id is None:
            raise RuntimeError("bind_identity() must be called before start()")
        # add_event_handler returns None, so track registration ourselves.
        self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        self._registered = True
        logger.info("Message watcher registered on the user client")

    async def stop(self) -> None:
        if self._registered:
            try:
                self._client.remove_event_handler(self._on_new_message)
            except Exception:  # pragma: no cover - client may already be gone
                logger.debug("Could not remove the event handler", exc_info=True)
            self._registered = False

    # -- detection --------------------------------------------------------- #
    def _is_mention(self, event: Any, text: str) -> bool:
        message = event.message

        # Telegram's own flag: set when you are @-mentioned or text-mentioned.
        if getattr(message, "mentioned", False):
            return True

        # Explicit "@username" in the text.
        if self._mention_pattern and self._mention_pattern.search(text):
            return True

        # Text mentions of accounts without a username carry the id in an entity.
        for entity in getattr(message, "entities", None) or []:
            if isinstance(entity, MessageEntityMentionName) and entity.user_id == self._me_id:
                return True

        return False

    async def _is_reply_to_me(self, event: Any) -> bool:
        if not event.is_reply:
            return False
        try:
            replied = await event.get_reply_message()
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while resolving a replied message", exc.seconds)
            return False
        except (RPCError, ConnectionError, OSError, ValueError) as exc:
            logger.warning("Failed to resolve replied message: %s", exc)
            return False
        if replied is None:
            logger.warning("Failed to resolve replied message: it is no longer available")
            return False
        return getattr(replied, "sender_id", None) == self._me_id

    # -- event handling ---------------------------------------------------- #
    async def _on_new_message(self, event: Any) -> None:
        try:
            await self._process(event)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while processing a message", exc.seconds)
        except Exception:
            logger.exception("Failed to process Telegram event")

    async def _process(self, event: Any) -> None:
        # Groups and channels only.
        if event.is_private or not (event.is_group or event.is_channel):
            return
        # Never react to my own messages.
        if getattr(event, "out", False) or event.sender_id == self._me_id:
            return

        self.messages_seen += 1
        text = event.raw_text or ""
        settings = self._settings

        reasons: list[str] = []

        if settings.watch_mentions and self._is_mention(event, text):
            reasons.append(REASON_MENTION)

        if settings.watch_replies and await self._is_reply_to_me(event):
            reasons.append(REASON_REPLY)

        keyword_hits: list[str] = []
        if settings.watch_keywords and text:
            keyword_hits = self._keywords.find_hits(text)
            # The full hit list still drives the "+N more" counter in the alert;
            # only the reason line is capped, so it cannot run away.
            reasons.extend(
                keyword_reason(hit) for hit in keyword_hits[: settings.max_keyword_preview]
            )

        if not reasons:
            return

        await self._raise_alert(event, reasons, keyword_hits)

    async def _raise_alert(self, event: Any, reasons: list[str], keyword_hits: list[str]) -> None:
        try:
            chat = await event.get_chat()
        except (RPCError, ConnectionError, OSError, ValueError) as exc:
            logger.warning("Could not resolve the chat for an alert: %s", exc)
            chat = getattr(event, "chat", None)

        try:
            sender = await event.get_sender()
        except (RPCError, ConnectionError, OSError, ValueError) as exc:
            logger.warning("Could not resolve the sender for an alert: %s", exc)
            sender = None

        sender_name = display_name(sender)
        sender_username = getattr(sender, "username", None)

        classification = await classify_message(
            event.raw_text or "",
            {
                "enabled": self._settings.ai_enabled,
                "reasons": reasons,
                "keyword_hits": keyword_hits,
                "group": display_name(chat, fallback=""),
                "sender": sender_name,
                "chat_id": getattr(event, "chat_id", None),
                "message_id": getattr(event.message, "id", None),
            },
        )

        if classification.get("enabled") and not classification.get("important", True):
            logger.info(
                "AI classified message %s in chat %s as not important; alert suppressed",
                getattr(event.message, "id", "?"),
                getattr(event, "chat_id", "?"),
            )
            return

        text = build_alert(
            event,
            reasons=reasons,
            keyword_hits=keyword_hits,
            sender_name=sender_name,
            sender_username=sender_username,
            chat=chat,
            settings=self._settings,
            classification=classification,
        )

        await self._dispatcher.enqueue(text)
        self.alerts_raised += 1

        summary = ", ".join(reasons[:4]) + ("..." if len(reasons) > 4 else "")
        logger.info(
            "%s alert sent | group=%s | msg=%s | %s",
            "Keyword" if keyword_hits else "Mention/Reply",
            display_name(chat, fallback="?"),
            getattr(event.message, "id", "?"),
            summary,
        )
