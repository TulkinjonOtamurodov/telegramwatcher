"""Private control panel: the bot commands that drive MAKIMA at runtime.

Every command is handled by a single dispatcher so that authorisation, error
handling and long-reply chunking live in exactly one place. Nothing here needs a
restart -- keyword and settings changes are written to disk immediately.

The actual work lives in :class:`~app.actions.MakimaActions`, which the inline
button panel in :mod:`app.control_panel` also uses. A command and its equivalent
button therefore run the same code path.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Iterable, NamedTuple

from telethon import events
from telethon.errors import FloodWaitError, RPCError

from app.actions import (
    KEYWORD_LIST_LIMIT,
    KNOWN_PLACEHOLDERS,
    MAX_TEMPLATE_LENGTH,
    ActionError,
    MakimaActions,
    on_off,
)
from app.ai_classifier import CATEGORIES
from app.control_panel import ControlPanel
from app.keywords import KeywordStore
from app.logging_config import get_logger
from app.settings import MAX_MESSAGE_CHARS, MIN_MESSAGE_CHARS, SettingsStore
from app.utils import chunk_text

logger = get_logger("commands")

_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@[\w_]+)?(?:[ \t]+([\s\S]+))?$")

_ON_OFF = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}

#: Shown under the button panel by /help.
HELP_NOTE = (
    "Slash commands still work as well: /status /keywords /addkeyword "
    "/removekeyword /setmentions /setreplies /setkeywords /setmaxchars "
    "/template /settemplate /reload /grouprules\n\n"
    "Send /grouprules inside a group to configure that group's keyword rules — "
    "ignored words, group-only keywords, and its description."
)


class Command(NamedTuple):
    name: str
    usage: str
    description: str


COMMANDS: tuple[Command, ...] = (
    Command("start", "/start", "Open the control panel"),
    Command("help", "/help", "Open the control panel and list the commands"),
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
    Command(
        "excludekeywords",
        "/excludekeywords [chat id]",
        "Silence keyword alerts for a group (run it inside the group)",
    ),
    Command(
        "allowkeywords",
        "/allowkeywords [chat id]",
        "Re-enable keyword alerts for a group",
    ),
    Command(
        "keywordexclusions",
        "/keywordexclusions",
        "List the configured group rules",
    ),
    Command(
        "grouprules",
        "/grouprules [chat id]",
        "Per-group keyword rules (run it inside a group to configure that one)",
    ),
)


class BotCommands:
    """Wires the command handler and the button panel onto the bot client."""

    def __init__(
        self,
        bot_client: Any,
        *,
        settings: SettingsStore,
        keywords: KeywordStore,
        admin_ids: Callable[[], Iterable[int]],
        watcher: Any = None,
        dispatcher: Any = None,
        watched: Any = None,
        group_rules: Any = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self._bot = bot_client
        self._settings = settings
        self._keywords = keywords
        self._admin_ids = admin_ids
        self._identity = identity or {}
        self._registered = False

        self.actions = MakimaActions(
            settings=settings,
            keywords=keywords,
            watcher=watcher,
            dispatcher=dispatcher,
            watched=watched,
            group_rules=group_rules,
            identity=self._identity,
        )
        self.panel = ControlPanel(
            bot_client,
            actions=self.actions,
            is_authorized=self.is_authorized,
        )

    # -- lifecycle --------------------------------------------------------- #
    def register(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(
            self._on_command,
            events.NewMessage(incoming=True, pattern=_COMMAND_RE, func=lambda e: e.is_private),
        )
        self.panel.register()
        self._registered = True
        logger.info("Bot command handler registered (%d commands)", len(COMMANDS))

    def set_identity(self, **values: Any) -> None:
        self._identity.update(values)
        self.actions.identity.update(values)

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
                    f"Unknown command /{command}. Send /help for the control panel.",
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
        await self.panel.send_panel(event)

    async def _cmd_help(self, event: Any, argument: str) -> None:
        await self.panel.send_panel(event, note=HELP_NOTE)

    async def _cmd_status(self, event: Any, argument: str) -> None:
        await self._reply(event, self.actions.status_text())

    async def _cmd_keywords(self, event: Any, argument: str) -> None:
        if self._keywords.count() == 0:
            await self._reply(
                event,
                "No keywords are configured. Add one with /addkeyword <word>.",
            )
            return
        await self._reply(event, self.actions.keywords_text(limit=KEYWORD_LIST_LIMIT))

    async def _cmd_addkeyword(self, event: Any, argument: str) -> None:
        if not argument:
            await self._reply(event, "Usage: /addkeyword <word or phrase>")
            return
        try:
            keyword = await self.actions.add_keyword(argument)
        except ActionError as exc:
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
            keyword = await self.actions.remove_keyword(argument)
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(
            event,
            f"✅ Removed '{keyword}'. Now watching {self._keywords.count()} keywords.",
        )

    async def _toggle(self, event: Any, argument: str, mode: str, label: str) -> None:
        choice = _ON_OFF.get(argument.strip().lower())
        if choice is None:
            current = on_off(self.actions.watch_enabled(mode))
            await self._reply(
                event,
                f"Usage: /set{label.lower()} on|off  (currently {current})",
            )
            return
        await self.actions.set_watch(mode, choice)
        await self._reply(event, f"✅ {label} alerts are now {on_off(choice)}.")

    async def _cmd_setmentions(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "mentions", "Mentions")

    async def _cmd_setreplies(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "replies", "Replies")

    async def _cmd_setkeywords(self, event: Any, argument: str) -> None:
        await self._toggle(event, argument, "keywords", "Keywords")

    async def _cmd_setmaxchars(self, event: Any, argument: str) -> None:
        if not argument.strip():
            await self._reply(
                event,
                f"Usage: /setmaxchars <{MIN_MESSAGE_CHARS}-{MAX_MESSAGE_CHARS}>  "
                f"(currently {self.actions.max_chars})",
            )
            return
        try:
            value = await self.actions.set_max_chars(argument)
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(event, f"✅ Alert previews are now capped at {value} characters.")

    async def _cmd_template(self, event: Any, argument: str) -> None:
        await self._reply(
            event,
            self.actions.template_text(with_placeholders=True)
            + "\n\n(copy, edit, and send it back after /settemplate)",
        )

    async def _cmd_settemplate(self, event: Any, argument: str) -> None:
        if not argument:
            await self._reply(
                event,
                "Usage: /settemplate <text>\n\n"
                "The text may span multiple lines. A literal \\n is also accepted "
                "and converted to a line break. Send /settemplate default to restore "
                f"the shipped template. Limit: {MAX_TEMPLATE_LENGTH} characters.",
            )
            return

        if argument.strip().lower() == "default":
            await self.actions.reset_template()
            await self._reply(event, "✅ Template restored to the default.")
            return

        try:
            unknown = await self.actions.set_template(argument)
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return

        note = ""
        if unknown:
            note = (
                "\n\n⚠️ These placeholders are not recognised and will be "
                "printed as-is: " + ", ".join(f"{{{{{name}}}}}" for name in unknown)
            )
        await self._reply(event, f"✅ Template updated.{note}")

    async def _cmd_reload(self, event: Any, argument: str) -> None:
        await self.actions.reload()
        await self._reply(event, self.actions.reload_text())

    # -- keyword exclusions ------------------------------------------------- #
    async def _cmd_excludekeywords(self, event: Any, argument: str) -> None:
        if not argument.strip():
            await self._reply(
                event,
                "Send /excludekeywords **inside the group** you want to silence — "
                "the chat id is taken from the message itself.\n\n"
                "From here, pass the id instead: /excludekeywords -1001234567890\n"
                "Use /keywordexclusions to see the current list.",
            )
            return
        try:
            title = await self.actions.exclude_chat(argument.strip())
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(
            event,
            f"✅ Keyword alerts disabled for {title}.\n"
            "Mentions and replies still alert there.",
        )

    async def _cmd_allowkeywords(self, event: Any, argument: str) -> None:
        if not argument.strip():
            await self._reply(
                event,
                "Send /allowkeywords **inside the group** you want to re-enable.\n\n"
                "From here, pass the id instead: /allowkeywords -1001234567890",
            )
            return
        try:
            title = await self.actions.allow_chat(argument.strip())
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self._reply(event, f"✅ Keyword alerts enabled for {title}.")

    async def _cmd_keywordexclusions(self, event: Any, argument: str) -> None:
        await self._reply(event, self.actions.group_rules_text())

    async def _cmd_grouprules(self, event: Any, argument: str) -> None:
        """List configured groups, or open one by id."""
        target = argument.strip()
        if not target:
            await self._reply(
                event,
                self.actions.group_rules_text()
                + "\n\nOpen the panel with /start → 🏢 Group Rules, or send "
                "/grouprules inside a group to configure that one.",
            )
            return
        try:
            await self.actions.ensure_group(target)
        except ActionError as exc:
            await self._reply(event, f"⚠️ {exc}")
            return
        await self.panel.send_group_panel(event, target)

    async def handle_group_exclusion_command(
        self, command: str, chat_id: int, title: str, sender_id: int | None
    ) -> None:
        """Apply an exclusion command typed inside a group.

        Called by the watcher, which only forwards messages my own account sent.
        The confirmation goes to the private bot chat rather than back into the
        group, so nothing extra is posted where other people can see it.
        """
        if not (self.is_authorized(sender_id) or sender_id is None):
            logger.warning(
                "Ignoring in-group /%s from unauthorised id %s", command, sender_id
            )
            return

        # /grouprules opens the configuration panel for this group, privately.
        if command == "grouprules":
            try:
                await self.actions.ensure_group(chat_id, title)
            except ActionError as exc:
                logger.warning("Could not prepare group rules for %s: %s", chat_id, exc)
                return
            for recipient in sorted(set(self._admin_ids())):
                await self.panel.send_group_panel_to(recipient, str(chat_id))
            return

        try:
            if command == "excludekeywords":
                label = await self.actions.exclude_chat(chat_id, title)
                message = (
                    f"✅ Keyword alerts disabled for {label}.\n"
                    "Mentions and replies still alert there."
                )
            else:
                label = await self.actions.allow_chat(chat_id)
                message = f"✅ Keyword alerts enabled for {label}."
        except ActionError as exc:
            message = f"⚠️ {exc}"

        dispatcher = self.actions.dispatcher
        if dispatcher is not None:
            await dispatcher.send_now(message)
        else:  # pragma: no cover - dispatcher is always wired in main
            logger.info("Exclusion result (no dispatcher to report it): %s", message)

    # Categories are exposed so a future AI command can list them.
    @staticmethod
    def categories() -> tuple[str, ...]:
        return CATEGORIES


__all__ = ["BotCommands", "COMMANDS", "Command", "KNOWN_PLACEHOLDERS"]
