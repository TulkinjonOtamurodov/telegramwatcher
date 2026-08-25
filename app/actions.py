"""Shared operations behind both the slash commands and the button panel.

Nothing in here knows about Telegram events, keyboards or callbacks. Every
mutation goes through the existing :class:`SettingsStore` / :class:`KeywordStore`
methods, so a change made from a button persists exactly like one made from a
command -- same files, same locks, same atomic writes.

:class:`BotCommands` and :class:`ControlPanel` both hold one of these and call
the same methods, which is what keeps the two interfaces from drifting apart.
"""

from __future__ import annotations

import time
from typing import Any

from app.ai_classifier import has_backend
from app.group_rules import describe_all, describe_rule
from app.keywords import KeywordError, KeywordStore
from app.logging_config import get_logger
from app.settings import (
    DEFAULT_TEMPLATE,
    MAX_MESSAGE_CHARS,
    MIN_MESSAGE_CHARS,
    SettingsStore,
)
from app.utils import template_placeholders

logger = get_logger("actions")

#: How many keywords are listed before the rest are summarised.
KEYWORD_LIST_LIMIT = 120

#: Longest alert template accepted.
MAX_TEMPLATE_LENGTH = 2000

#: The three watch modes, mapped to their settings path and display label.
WATCH_MODES: dict[str, tuple[str, str]] = {
    "mentions": ("watching.mentions", "Mentions"),
    "replies": ("watching.replies", "Replies"),
    "keywords": ("watching.keywords", "Keywords"),
}

KNOWN_PLACEHOLDERS = {
    # -- used by the current default template --
    "tags",
    "triggers",
    "heading",
    "body",
    "message_block",
    # -- still supported, for older custom templates --
    "timestamp",
    "reasons",
    "group",
    "group_link",
    "sender",
    "sender_name",
    "sender_username",
    "sender_id",
    "chat_id",
    "message_id",
    "keyword_hits",
    "message_text",
    "message_link",
    "category",
    "severity",
    "summary",
    "requires_action",
    "unit",
}


class ActionError(ValueError):
    """A failure whose message is safe to show the admin verbatim."""


def on_off(value: bool) -> str:
    return "ON" if value else "OFF"


class MakimaActions:
    """Every state change and every rendered view, in one place."""

    def __init__(
        self,
        *,
        settings: SettingsStore,
        keywords: KeywordStore,
        watcher: Any = None,
        dispatcher: Any = None,
        watched: Any = None,
        group_rules: Any = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.keywords = keywords
        self.watcher = watcher
        self.dispatcher = dispatcher
        self.watched = watched
        self.group_rules = group_rules
        self.identity = identity or {}
        self._started_at = time.monotonic()

    # -- state readers ----------------------------------------------------- #
    def watch_enabled(self, mode: str) -> bool:
        path, _ = WATCH_MODES[mode]
        return self.settings.get_bool(path, True)

    @property
    def max_chars(self) -> int:
        return self.settings.max_message_chars

    @property
    def template(self) -> str:
        return self.settings.template

    def uptime_text(self) -> str:
        total = int(time.monotonic() - self._started_at)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

    # -- mutations --------------------------------------------------------- #
    async def set_watch(self, mode: str, enabled: bool) -> bool:
        """Turn one watch mode on or off and persist it."""
        if mode not in WATCH_MODES:
            raise ActionError(f"Unknown mode '{mode}'.")
        path, label = WATCH_MODES[mode]
        await self.settings.set(path, bool(enabled))
        logger.info("%s alerts set to %s", label, on_off(enabled))
        return bool(enabled)

    async def toggle_watch(self, mode: str) -> bool:
        """Flip one watch mode. Returns the new state."""
        return await self.set_watch(mode, not self.watch_enabled(mode))

    async def add_keyword(self, raw: str) -> str:
        try:
            return await self.keywords.add(raw)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def remove_keyword(self, raw: str) -> str:
        try:
            return await self.keywords.remove(raw)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def set_max_chars(self, raw: Any) -> int:
        """Validate against the 20-4000 range and persist."""
        text = str(raw).strip()
        if not text.lstrip("+-").isdigit():
            raise ActionError(
                f"That is not a number. Send a value between "
                f"{MIN_MESSAGE_CHARS} and {MAX_MESSAGE_CHARS}."
            )
        value = int(text)
        if not MIN_MESSAGE_CHARS <= value <= MAX_MESSAGE_CHARS:
            raise ActionError(
                f"Value must be between {MIN_MESSAGE_CHARS} and {MAX_MESSAGE_CHARS}."
            )
        await self.settings.set("alerts.max_message_chars", value)
        logger.info("Alert preview length set to %d characters", value)
        return value

    async def set_template(self, raw: str) -> list[str]:
        """Persist a new template. Returns any unrecognised placeholders.

        A literal ``\\n`` is converted to a line break, so a template can be
        pasted on one line as well as sent multi-line.
        """
        if not raw or not raw.strip():
            raise ActionError("The template cannot be empty.")
        template = raw.replace("\\n", "\n")
        if len(template) > MAX_TEMPLATE_LENGTH:
            raise ActionError(
                f"Template is too long ({len(template)} chars, "
                f"limit {MAX_TEMPLATE_LENGTH})."
            )
        unknown = sorted(template_placeholders(template) - KNOWN_PLACEHOLDERS)
        await self.settings.set("alerts.template", template)
        logger.info("Alert template updated (%d chars)", len(template))
        return unknown

    async def reset_template(self) -> None:
        await self.settings.set("alerts.template", DEFAULT_TEMPLATE)
        logger.info("Alert template reset to the default")

    # -- group rules --------------------------------------------------------- #
    #
    # A group rule only ever affects *keyword* matching in that one chat.
    # Mentions and replies are evaluated before any of this and are untouched.

    def rules_count(self) -> int:
        return self.group_rules.count() if self.group_rules else 0

    def keyword_disabled_count(self) -> int:
        return self.group_rules.disabled_count() if self.group_rules else 0

    def get_rule(self, chat_id: Any) -> Any:
        return self.group_rules.get(chat_id) if self.group_rules else None

    def _require_store(self) -> Any:
        if self.group_rules is None:  # pragma: no cover - always wired in main
            raise ActionError("Group rules are not available.")
        return self.group_rules

    async def ensure_group(self, chat_id: Any, title: str = "") -> Any:
        try:
            return await self._require_store().ensure(chat_id, title)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def remove_group(self, chat_id: Any) -> str:
        try:
            return await self._require_store().remove(chat_id)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def toggle_group_keywords(self, chat_id: Any) -> bool:
        try:
            return await self._require_store().toggle_keywords(chat_id)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def add_ignored(self, chat_id: Any, word: str) -> tuple[str, bool]:
        """Ignore a global keyword here. Also reports whether it is global."""
        try:
            keyword = await self._require_store().add_ignored(chat_id, word)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc
        return keyword, keyword in self.keywords

    async def remove_ignored(self, chat_id: Any, word: str) -> str:
        try:
            return await self._require_store().remove_ignored(chat_id, word)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def add_group_keyword(self, chat_id: Any, word: str) -> str:
        try:
            return await self._require_store().add_extra(chat_id, word)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def remove_group_keyword(self, chat_id: Any, word: str) -> str:
        try:
            return await self._require_store().remove_extra(chat_id, word)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def clear_group_list(self, chat_id: Any, attr: str) -> int:
        try:
            return await self._require_store().clear_list(chat_id, attr)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    async def set_group_description(
        self, chat_id: Any, text: str, admin: Any = None
    ) -> str:
        try:
            return await self._require_store().set_description(chat_id, text, admin)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc

    def group_rules_text(self) -> str:
        return describe_all(self.group_rules.all()) if self.group_rules else describe_all([])

    def group_detail_text(self, chat_id: Any) -> str:
        return describe_rule(self.get_rule(chat_id), chat_id)

    # -- backward-compatible whole-group exclusion --------------------------- #
    # /excludekeywords and /allowkeywords predate group rules; they now set the
    # keywords_enabled flag so there is only one source of truth.

    def excluded_count(self) -> int:
        return self.keyword_disabled_count()

    def is_chat_excluded(self, chat_id: Any) -> bool:
        rule = self.get_rule(chat_id)
        return rule is not None and not rule.keywords_enabled

    async def exclude_chat(self, chat_id: Any, title: str = "") -> str:
        store = self._require_store()
        try:
            rule = await store.ensure(chat_id, title)
            if not rule.keywords_enabled:
                raise ActionError("Keyword alerts are already disabled for that group.")
            await store.set_keywords_enabled(chat_id, False)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc
        return rule.display_title

    async def allow_chat(self, chat_id: Any) -> str:
        store = self._require_store()
        try:
            rule = store.get(chat_id)
            if rule is None or rule.keywords_enabled:
                raise ActionError("Keyword alerts are not disabled for that group.")
            await store.set_keywords_enabled(chat_id, True)
        except KeywordError as exc:
            raise ActionError(str(exc)) from exc
        return rule.display_title

    def exclusions_text(self) -> str:
        return self.group_rules_text()

    async def reload(self) -> None:
        """Re-read every data file from disk without restarting the process."""
        await self.settings.load()
        await self.keywords.load()
        if self.watched is not None:
            await self.watched.load()
        if self.group_rules is not None:
            await self.group_rules.load()
        logger.info("Reloaded settings and %d keywords from disk", self.keywords.count())

    # -- rendered views ---------------------------------------------------- #
    def status_text(self) -> str:
        settings = self.settings
        lines = [
            "⚙️ MAKIMA STATUS",
            "",
            f"Mentions: {on_off(settings.watch_mentions)}",
            f"Replies: {on_off(settings.watch_replies)}",
            f"Keywords: {on_off(settings.watch_keywords)}",
            f"Keywords loaded: {self.keywords.count()}",
            f"Watched members: {self.watched.count() if self.watched else 0}",
            f"Group rules: {self.rules_count()} configured",
            f"Keyword-disabled groups: {self.keyword_disabled_count()}",
            f"Max preview chars: {settings.max_message_chars}",
            f"AI classification: {on_off(settings.ai_enabled)}"
            + (
                ""
                if not settings.ai_enabled
                else f" ({'backend' if has_backend() else 'rule-based'})"
            ),
            "",
            f"Uptime: {self.uptime_text()}",
        ]

        if self.watcher is not None:
            lines.append(f"Messages inspected: {self.watcher.messages_seen}")
            lines.append(f"Alerts raised: {self.watcher.alerts_raised}")
        if self.dispatcher is not None:
            stats = self.dispatcher.stats
            lines.append(
                f"Alerts delivered: {stats['sent']} (failed {stats['failed']}, "
                f"queued {stats['queued']})"
            )

        user_display = self.identity.get("user_display")
        bot_username = self.identity.get("bot_username")
        if user_display:
            lines.append(f"Account: {user_display}")
        if bot_username:
            lines.append(f"Bot: @{bot_username}")

        return "\n".join(lines)

    def keywords_text(self, *, limit: int = KEYWORD_LIST_LIMIT) -> str:
        items = self.keywords.all()
        if not items:
            return "\U0001f9f7 KEYWORDS (0)\n\nNo keywords are configured yet."

        shown = items[:limit]
        body = "\n".join(f"- {item}" for item in shown)
        footer = ""
        if len(items) > limit:
            footer = f"\n\n... and {len(items) - limit} more."
        return f"\U0001f9f7 KEYWORDS ({len(items)})\n\n{body}{footer}"

    def template_text(self, *, with_placeholders: bool = True) -> str:
        text = f"\U0001f4dd CURRENT ALERT TEMPLATE\n\n{self.template}"
        if with_placeholders:
            text += "\n\nPlaceholders: " + ", ".join(
                f"{{{{{name}}}}}" for name in sorted(KNOWN_PLACEHOLDERS)
            )
        return text

    def reload_text(self) -> str:
        return (
            "\U0001f504 Reloaded from disk.\n"
            f"Keywords loaded: {self.keywords.count()}\n"
            f"Watched members: {self.watched.count() if self.watched else 0}\n"
            f"Modes: {self.settings.modes_summary()}"
        )
