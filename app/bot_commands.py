"""Private control panel: the bot commands that drive MAKIMA at runtime.

Every command is handled by a single dispatcher so that authorisation, error
handling and long-reply chunking live in exactly one place. Nothing here needs a
restart -- keyword and settings changes are written to disk immediately.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Iterable, NamedTuple

from telethon import events
from telethon.errors import FloodWaitError, RPCError

from app.ai_classifier import CATEGORIES, has_backend
from app.keywords import KeywordError, KeywordStore
from app.logging_config import get_logger
from app.settings import (
    DEFAULT_TEMPLATE,
    MAX_MESSAGE_CHARS,
    MIN_MESSAGE_CHARS,
    SettingsStore,
)
from app.utils import chunk_text, template_placeholders

logger = get_logger("commands")

#: How many keywords /keywords prints before summarising the rest.
KEYWORD_LIST_LIMIT = 120

#: Longest template accepted by /settemplate.
MAX_TEMPLATE_LENGTH = 2000

_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[\w_]+)?(?:[ \t]+([\s\S]+))?$")

_ON_OFF = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}

KNOWN_PLACEHOLDERS = {
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


class Command(NamedTuple):
    name: str
    usage: str
    description: str


COMMANDS: tuple[Command, ...] = (
    Command("start", "/start", "Wake the bot and confirm alerts can reach you"),
    Command("help", "/help", "Show this list"),
    Command("status", "/status", "Current modes, keyword count and counters"),
    Command("keywords", "/keywords", "List the active keywords"),
    Command("addkeyword", "/addkeyword <word>", "Add a keyword or phrase"),
    Command("removekeyword", "/removekeyword <word>", "Remove a keyword"),
    Command("setmentions", "/setmentions on|off", "Alert when you are mentioned"),
    Command("setreplies", "/setreplies on|off", "Alert on replies to your messages"),
    Command("setkeywords", "/setkeywords on|off", "Alert on keyword matches"),
    Command("setmaxchars", "/setmaxchars <20-4000>", "Message preview length in alerts"),
    Command("template", "/template", "Show the current alert template"),
    Command("settemplate", "/settemplate <text>", "Replace the alert template"),
    Command("reload", "/reload", "Re-read keywords and settings from disk"),
)


def _on_off(value: bool) -> str:
    return "ON" if value else "OFF"


class BotCommands:
    """Wires the command handler onto the bot client."""

    def __init__(
        self,
        bot_client: Any,
        *,
        settings: SettingsStore,
        keywords: KeywordStore,
        admin_ids: Callable[[], Iterable[int]],
        watcher: Any = None,
        dispatcher: Any = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self._bot = bot_client
        self._settings = settings
        self._keywords = keywords
        self._admin_ids = admin_ids
        self._watcher = watcher
        self._dispatcher = dispatcher
        self._identity = identity or {}
        self._started_at = time.monotonic()
        self._registered = False

    # -- lifecycle --------------------------------------------------------- #
    def register(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(
            self._on_command,
            events.NewMessage(incoming=True, pattern=_COMMAND_RE, func=lambda e: e.is_private),
        )
        self._registered = True
        logger.info("Bot command handler registered (%d commands)", len(COMMANDS))

    def set_identity(self, **values: Any) -> None:
        self._identity.update(values)

    def is_authorized(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in set(self._admin_ids())

    # -- dispatch ---------------------------------------------------------- #
    async def _on_command(self, event: Any) -> None:
        try:
            sender_id = event.sender_id
            command = (event.pattern_match.group(1) or "").lower()
            argument = (event.pattern_match.group(2) or "").strip()

            if not self.is_authorized(sender_id):
                logger.warning(
                    "Unauthorised /%s from user id %s (add it to ADMIN_USER_IDS to allow)",
                    command,
                    sender_id,
                )
                await self._reply(
                    event,
                    "⛔ You are not authorised to use this bot.\n"
                    f"Your Telegram user ID is: {sender_id}",
                )
                return

            handler = getattr(self, f"_cmd_{command}", None)
            if handler is None:
                await self._reply(
                    event,
                    f"Unknown command /{command}. Send /help for the list.",
                )
                return

            logger.info("Command /%s from %s", command, sender_id)
            await handler(event, argument)

        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while answering a command", exc.seconds)
        except Exception:
            logger.exception("Failed to handle a bot command")
            await self._reply(event, "⚠️ Something went wrong. Check logs/makima.log.")

    async def _reply(self, event: Any, text: str) -> None:
        """Answer, splitting anything Telegram would reject as too long."""
        for piece in chunk_text(text):
            if not piece:
                continue
            try:
                await event.respond(piece, link_preview=False, parse_mode=None)
            except FloodWaitError as exc:
                logger.warning("Flood wait (%ss) while replying", exc.seconds)
                await asyncio.sleep(min(int(exc.seconds) + 2, 60))
            except RPCError:
                logger.exception("Telegram rejected a command reply")
                return

    # -- commands ---------------------------------------------------------- #
    async def _cmd_start(self, event: Any, argument: str) -> None:
        name = self._identity.get("user_display", "your account")
        await self._reply(
            event,
            "\U0001f7e5 MAKIMA is online.\n\n"
            f"Watching Telegram as: {name}\n"
            f"Keywords loaded: {self._keywords.count()}\n"
            f"Modes: {self._settings.modes_summary()}\n\n"
            "Send /help to see everything you can change from here.",
        )

    async def _cmd_help(self, event: Any, argument: str) -> None:
        lines = ["\U0001f4d6 MAKIMA COMMANDS", ""]
        width = max(len(cmd.usage) for cmd in COMMANDS)
        for cmd in COMMANDS:
            lines.append(f"{cmd.usage.ljust(width)}  -  {cmd.description}")
        lines += [
            "",
            "Keywords are case-insensitive and match on word boundaries, so",
            "'claim' does not fire on 'disclaimer'. Multi-word phrases such as",
            "'fuel card' are supported.",
        ]
        await self._reply(event, "\n".join(lines))

    async def _cmd_status(self, event: Any, argument: str) -> None:
        settings = self._settings
        uptime = int(time.monotonic() - self._started_at)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        lines = [
            "⚙️ MAKIMA STATUS",
            "",
            f"Mentions: {_on_off(settings.watch_mentions)}",
            f"Replies: {_on_off(settings.watch_replies)}",
            f"Keywords: {_on_off(settings.watch_keywords)}",
            f"Keywords loaded: {self._keywords.count()}",
            f"Max preview chars: {settings.max_message_chars}",
            f"AI classification: {_on_off(settings.ai_enabled)}"
            + ("" if not settings.ai_enabled else f" ({'backend' if has_backend() else 'rule-based'})"),
            "",
            f"Uptime: {hours}h {minutes}m {seconds}s",
        ]

        if self._watcher is not None:
            lines.append(f"Messages inspected: {self._watcher.messages_seen}")
            lines.append(f"Alerts raised: {self._watcher.alerts_raised}")
        if self._dispatcher is not None:
            stats = self._dispatcher.stats
            lines.append(
                f"Alerts delivered: {stats['sent']} (failed {stats['failed']}, "
                f"queued {stats['queued']})"
            )

        user_display = self._identity.get("user_display")
        bot_username = self._identity.get("bot_username")
        if user_display:
            lines.append(f"Account: {user_display}")
        if bot_username:
            lines.append(f"Bot: @{bot_username}")

        await self._reply(event, "\n".join(lines))

    async def _cmd_keywords(self, event: Any, argument: str) -> None:
        items = self._keywords.all()
        if not items:
            await self._reply(
                event,
                "No keywords are configured. Add one with /addkeyword <word>.",
            )
            return

        shown = items[:KEYWORD_LIST_LIMIT]
        header = f"\U0001f9f7 KEYWORDS ({len(items)})"
        body = "\n".join(f"- {item}" for item in shown)
        footer = ""
        if len(items) > KEYWORD_LIST_LIMIT:
            footer = f"\n\n... and {len(items) - KEYWORD_LIST_LIMIT} more."
        await self._reply(event, f"{header}\n\n{body}{footer}")

    async def _cmd_addkeyword(self, event: Any, argument: str) -> None:
        if not argument:
            await self._reply(event, "Usage: /addkeyword <word or phrase>")
            return
        try:
            keyword = await self._keywords.add(argument)
        except KeywordError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(
            event,
            f"✅ Added '{keyword}'. Now watching {self._keywords.count()} keywords.",
        )

    async def _cmd_removekeyword(self, event: Any, argument: str) -> None:
        if not argument:
            await self._reply(event, "Usage: /removekeyword <word or phrase>")
            return
        try:
            keyword = await self._keywords.remove(argument)
        except KeywordError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(
            event,
            f"✅ Removed '{keyword}'. Now watching {self._keywords.count()} keywords.",
        )

    async def _toggle(self, event: Any, argument: str, path: str, label: str) -> None:
        choice = _ON_OFF.get(argument.strip().lower())
        if choice is None:
            current = _on_off(self._settings.get_bool(path, True))
            await self._reply(
                event,
                f"Usage: /set{label.lower()} on|off  (currently {current})",
            )
            return
        await self._settings.set(path, choice)
        await self._reply(event, f"✅ {label} alerts are now {_on_off(choice)}.")

    async def _cmd_setmentions(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "watching.mentions", "Mentions")

    async def _cmd_setreplies(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "watching.replies", "Replies")

    async def _cmd_setkeywords(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "watching.keywords", "Keywords")

    async def _cmd_setmaxchars(self, event: Any, argument: str) -> None:
        raw = argument.strip()
        if not raw.lstrip("-").isdigit():
            await self._reply(
                event,
                f"Usage: /setmaxchars <{MIN_MESSAGE_CHARS}-{MAX_MESSAGE_CHARS}>  "
                f"(currently {self._settings.max_message_chars})",
            )
            return
        value = int(raw)
        if not MIN_MESSAGE_CHARS <= value <= MAX_MESSAGE_CHARS:
            await self._reply(
                event,
                f"⚠️ Value must be between {MIN_MESSAGE_CHARS} and {MAX_MESSAGE_CHARS}.",
            )
            return
        await self._settings.set("alerts.max_message_chars", value)
        await self._reply(event, f"✅ Alert previews are now capped at {value} characters.")

    async def _cmd_template(self, event: Any, argument: str) -> None:
        template = self._settings.template
        await self._reply(
            event,
            "\U0001f4dd CURRENT ALERT TEMPLATE\n"
            "(copy, edit, and send it back after /settemplate)\n\n"
            f"{template}\n\n"
            "Placeholders: " + ", ".join(f"{{{{{name}}}}}" for name in sorted(KNOWN_PLACEHOLDERS)),
        )

    async def _cmd_settemplate(self, event: Any, argument: str) -> None:
        if not argument:
            await self._reply(
                event,
                "Usage: /settemplate <text>\n\n"
                "The text may span multiple lines. A literal \\n is also accepted "
                "and converted to a line break. Send /settemplate default to restore "
                "the shipped template.",
            )
            return

        if argument.strip().lower() == "default":
            await self._settings.set("alerts.template", DEFAULT_TEMPLATE)
            await self._reply(event, "✅ Template restored to the default.")
            return

        template = argument.replace("\\n", "\n")
        if len(template) > MAX_TEMPLATE_LENGTH:
            await self._reply(
                event,
                f"⚠️ Template is too long ({len(template)} chars, "
                f"limit {MAX_TEMPLATE_LENGTH}).",
            )
            return

        unknown = sorted(template_placeholders(template) - KNOWN_PLACEHOLDERS)
        await self._settings.set("alerts.template", template)

        note = ""
        if unknown:
            note = (
                "\n\n⚠️ These placeholders are not recognised and will be "
                "printed as-is: " + ", ".join(f"{{{{{name}}}}}" for name in unknown)
            )
        await self._reply(event, f"✅ Template updated.{note}")

    async def _cmd_reload(self, event: Any, argument: str) -> None:
        await self._settings.load()
        await self._keywords.load()
        await self._reply(
            event,
            "\U0001f504 Reloaded from disk.\n"
            f"Keywords loaded: {self._keywords.count()}\n"
            f"Modes: {self._settings.modes_summary()}",
        )

    # Categories are exposed so a future AI command can list them.
    @staticmethod
    def categories() -> tuple[str, ...]:
        return CATEGORIES
