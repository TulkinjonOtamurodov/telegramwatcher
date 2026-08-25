"""The message watcher: detects mentions, replies and keyword hits.

Only incoming group and channel messages are inspected. Private chats, your own
outgoing messages, and anything that matches no rule are ignored without ever
touching the network.
"""

from __future__ import annotations

import asyncio
import logging
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
from app.keywords import KeywordStore, find_hits_in
from app.logging_config import get_logger
from app.settings import SettingsStore
from app.utils import display_name

logger = get_logger("watcher")

#: Commands typed inside a watched group by my own account. Recognised on the
#: user client because that is the only client actually in those groups -- the
#: bot never joins one, and that separation is deliberate.
GROUP_COMMAND_RE = re.compile(
    r"^/(excludekeywords|allowkeywords|grouprules)(?:@[\w_]+)?\s*$", re.IGNORECASE
)


class Watcher:
    """Registers the ``NewMessage`` handler and evaluates every group message."""

    def __init__(
        self,
        user_client: Any,
        *,
        settings: SettingsStore,
        keywords: KeywordStore,
        dispatcher: AlertDispatcher,
        watched: Any = None,
        group_rules: Any = None,
    ) -> None:
        self._client = user_client
        self._settings = settings
        self._keywords = keywords
        self._dispatcher = dispatcher
        self._watched = watched
        self._group_rules = group_rules

        self._me_id: int | None = None
        self._me_username: str | None = None
        self._mention_pattern: re.Pattern[str] | None = None
        self._registered = False
        self._exclusion_handler: Any = None

        self.messages_seen = 0
        self.alerts_raised = 0

    def set_exclusion_handler(self, handler: Any) -> None:
        """Install the coroutine that applies ``/excludekeywords`` in a group.

        Injected rather than imported so the watcher stays independent of the
        bot-side command objects, which are built after it.
        """
        self._exclusion_handler = handler

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
        # Exclusion commands are typed by my own account inside the target
        # group, so the chat id comes straight off the event -- no guessing.
        self._client.add_event_handler(
            self._on_group_command,
            events.NewMessage(outgoing=True, pattern=GROUP_COMMAND_RE),
        )
        self._registered = True
        logger.info("Message watcher registered on the user client")

    async def stop(self) -> None:
        if self._registered:
            try:
                self._client.remove_event_handler(self._on_new_message)
                self._client.remove_event_handler(self._on_group_command)
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

    # -- keyword matching ---------------------------------------------------- #
    def _match_keywords(self, text: str, chat_id: Any) -> list[str]:
        """Global keywords minus this group's ignores, plus its own keywords.

        With no rule for the chat, this is exactly the previous behaviour: the
        full global list, matched by the same engine.
        """
        if self._group_rules is None:
            return self._keywords.find_hits(text)

        patterns, rule = self._group_rules.effective_patterns(
            chat_id, self._keywords.patterns_excluding
        )

        if rule is not None and not rule.keywords_enabled:
            logger.debug("Keyword matching disabled for group | chat=%s", chat_id)
            return []

        hits = find_hits_in(text, patterns)

        if rule is not None:
            for hit in hits:
                if hit in rule.extra_patterns:
                    logger.info(
                        "Group keyword matched | chat=%s | keyword=%s", chat_id, hit
                    )
            # Only worth the second pass when someone is actually reading DEBUG.
            if rule.ignored_keywords and logger.isEnabledFor(logging.DEBUG):
                ignored = {
                    word: pattern
                    for word, pattern in self._keywords.patterns.items()
                    if word in set(rule.ignored_keywords)
                }
                for word in find_hits_in(text, ignored):
                    logger.debug(
                        "Keyword ignored by group rule | chat=%s | keyword=%s",
                        chat_id,
                        word,
                    )

        return hits

    # -- in-group exclusion commands ---------------------------------------- #
    async def _on_group_command(self, event: Any) -> None:
        """Apply ``/excludekeywords`` or ``/allowkeywords`` typed in a group.

        Only fires on messages my own account sent (Telegram guarantees the
        ``outgoing`` flag), so the person issuing it is the account owner. The
        chat id is taken from the event itself, which is why this is the most
        reliable of the possible flows -- no forwarding, no typing ids.
        """
        try:
            if event.is_private or not (event.is_group or event.is_channel):
                return
            if self._exclusion_handler is None:
                logger.warning("Exclusion command received before the handler was wired")
                return

            command = (event.pattern_match.group(1) or "").lower()
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                logger.warning("Exclusion command with no resolvable chat id; ignored")
                return

            try:
                chat = await event.get_chat()
            except (RPCError, ConnectionError, OSError, ValueError):
                chat = getattr(event, "chat", None)
            title = display_name(chat, fallback=f"Chat {chat_id}")

            logger.info(
                "Group command /%s | chat=%s | group=%s", command, chat_id, title
            )
            await self._exclusion_handler(command, int(chat_id), title, event.sender_id)

        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) on a group command", exc.seconds)
        except Exception:
            logger.exception("Failed to handle an in-group exclusion command")

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

        # A mention of a configured watched member tags the alert with that
        # person; a mention of my own account falls back to the default tag.
        watched_tags: list[str] = []
        if settings.watch_mentions:
            entities = getattr(event.message, "entities", None)
            if self._watched is not None:
                watched_tags = [
                    member.tag for member in self._watched.find_mentions(text, entities)
                ]
            if watched_tags or self._is_mention(event, text):
                reasons.append(REASON_MENTION)

        if settings.watch_replies and await self._is_reply_to_me(event):
            reasons.append(REASON_REPLY)

        # Keyword matching is the only thing group rules touch. The mention and
        # reply checks above already ran, so those still alert normally.
        keyword_hits: list[str] = []
        chat_id = getattr(event, "chat_id", None)
        if settings.watch_keywords and text:
            keyword_hits = self._match_keywords(text, chat_id)
        if keyword_hits:
            # The full hit list still drives the "+N more" counter in the alert;
            # only the reason line is capped, so it cannot run away.
            reasons.extend(
                keyword_reason(hit) for hit in keyword_hits[: settings.max_keyword_preview]
            )

        if not reasons:
            return

        await self._raise_alert(event, reasons, keyword_hits, watched_tags)

    async def _raise_alert(
        self,
        event: Any,
        reasons: list[str],
        keyword_hits: list[str],
        tags: list[str] | None = None,
    ) -> None:
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

        alert = build_alert(
            event,
            reasons=reasons,
            keyword_hits=keyword_hits,
            sender_name=sender_name,
            sender_username=sender_username,
            chat=chat,
            settings=self._settings,
            classification=classification,
            tags=tags,
        )

        await self._dispatcher.enqueue(alert)
        self.alerts_raised += 1

        summary = ", ".join(reasons[:4]) + ("..." if len(reasons) > 4 else "")
        logger.info(
            "%s alert sent | group=%s | msg=%s | %s",
            "Keyword" if keyword_hits else "Mention/Reply",
            display_name(chat, fallback="?"),
            getattr(event.message, "id", "?"),
            summary,
        )
