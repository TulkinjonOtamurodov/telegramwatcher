"""Inline-keyboard control panel for the MAKIMA bot.

Built entirely on Telethon's own ``Button`` and ``events.CallbackQuery`` -- no
extra Telegram library. Every button calls the same :class:`MakimaActions`
methods the slash commands call, so the two interfaces cannot drift apart and
settings changed from a button persist identically.

Flows that need typed input (add/remove keyword, new template, custom preview
length) use a small per-admin pending-input state. It is scoped to the private
bot chat, keyed by user id so two admins never collide, and always escapable
with the Cancel button or by sending any slash command.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable

from telethon import Button, events
from telethon.errors import (
    FloodWaitError,
    MessageIdInvalidError,
    MessageNotModifiedError,
    QueryIdInvalidError,
    RPCError,
)

from app.actions import ActionError, MakimaActions, on_off
from app.alert_lifecycle import CB_ALERT_SEEN
from app.logging_config import get_logger
from app.settings import MAX_MESSAGE_CHARS, MIN_MESSAGE_CHARS
from app.utils import TELEGRAM_MAX_MESSAGE, truncate

logger = get_logger("panel")

#: Leaves room for the buttons Telegram attaches below the text.
MAX_PANEL_CHARS = TELEGRAM_MAX_MESSAGE - 256

#: Quick-select values offered on the preview-length menu.
PREVIEW_PRESETS = (200, 500, 1000, 2000)

#: A message starting like this is a command; it escapes any pending input flow
#: and is left for the slash-command handler to process.
_LOOKS_LIKE_COMMAND = re.compile(r"^/[A-Za-z0-9_]+(?:@[\w_]+)?(?:\s|$)")

# -- callback data ---------------------------------------------------------- #
# Kept short: Telegram caps callback data at 64 bytes.
CB_MAIN = b"M"
CB_STATUS = b"S"
CB_KEYWORDS = b"K"
CB_KW_ADD = b"KA"
CB_KW_DEL = b"KD"
CB_TOGGLE = {b"TM": "mentions", b"TR": "replies", b"TK": "keywords"}
CB_TEMPLATE = b"T"
CB_TPL_SET = b"TS"
CB_TPL_DEFAULT = b"TD"
CB_PREVIEW = b"P"
CB_PREVIEW_CUSTOM = b"PC"
CB_RELOAD = b"R"
CB_CANCEL = b"X"
CB_EXCLUSIONS = b"E"
CB_EXC_LIST = b"EL"
CB_EXC_ADD = b"EA"
CB_EXC_DEL = b"ED"

_PREVIEW_PREFIX = b"PV"

# -- pending input actions -------------------------------------------------- #
ADD_KEYWORD = "add_keyword"
REMOVE_KEYWORD = "remove_keyword"
SET_TEMPLATE = "set_template"
SET_MAX_CHARS = "set_max_chars"
EXCLUDE_CHAT = "exclude_chat"
ALLOW_CHAT = "allow_chat"

_PROMPTS: dict[str, str] = {
    ADD_KEYWORD: (
        "➕ ADD KEYWORD\n\n"
        "Send the keyword or phrase you want to add.\n"
        "Examples: broker · fuel card"
    ),
    REMOVE_KEYWORD: (
        "➖ REMOVE KEYWORD\n\n" "Send the keyword or phrase you want to remove."
    ),
    SET_TEMPLATE: (
        "✏️ CHANGE ALERT TEMPLATE\n\n"
        "Send the new template. Multiple lines are fine, and a literal \\n also "
        "becomes a line break.\n\n"
        "Placeholders: {{timestamp}} {{reasons}} {{group}} {{group_link}} "
        "{{sender}} {{keyword_hits}} {{message_text}} {{message_link}}"
    ),
    SET_MAX_CHARS: (
        "✏️ CUSTOM PREVIEW LENGTH\n\n"
        f"Send a number between {MIN_MESSAGE_CHARS} and {MAX_MESSAGE_CHARS}."
    ),
    EXCLUDE_CHAT: (
        "➕ EXCLUDE A GROUP\n\n"
        "Send the group's chat id, e.g. -1001234567890.\n\n"
        "Easier: send /excludekeywords inside the group itself — the id is "
        "then picked up automatically."
    ),
    ALLOW_CHAT: (
        "➖ REMOVE AN EXCLUSION\n\n"
        "Send the chat id to re-enable keyword alerts for.\n"
        "Tap 📋 Excluded Groups first if you need the id."
    ),
}

#: Which menu each input flow returns to once it completes.
_RETURN_VIEW: dict[str, str] = {
    ADD_KEYWORD: "keywords",
    REMOVE_KEYWORD: "keywords",
    SET_TEMPLATE: "template",
    SET_MAX_CHARS: "preview",
    EXCLUDE_CHAT: "exclusions",
    ALLOW_CHAT: "exclusions",
}


@dataclass
class Pending:
    """One admin's in-progress typed-input flow."""

    action: str
    chat_id: int
    message_id: int


class ControlPanel:
    """Renders the panel and handles every callback query."""

    def __init__(
        self,
        bot_client: Any,
        *,
        actions: MakimaActions,
        is_authorized: Callable[[int | None], bool],
    ) -> None:
        self._bot = bot_client
        self._actions = actions
        self._is_authorized = is_authorized
        self._pending: dict[int, Pending] = {}
        self._registered = False

    # -- lifecycle --------------------------------------------------------- #
    def register(self) -> None:
        if self._registered:
            return
        self._bot.add_event_handler(self._on_callback, events.CallbackQuery())
        self._bot.add_event_handler(
            self._on_text,
            events.NewMessage(incoming=True, func=lambda e: e.is_private),
        )
        self._registered = True
        logger.info("Control panel registered (inline keyboard + input flows)")

    # -- keyboards --------------------------------------------------------- #
    def _main_keyboard(self) -> list[list[Any]]:
        act = self._actions
        return [
            [
                Button.inline("📊 Status", CB_STATUS),
                Button.inline("🔑 Keywords", CB_KEYWORDS),
            ],
            [
                Button.inline(
                    f"👤 Mentions: {on_off(act.watch_enabled('mentions'))}", b"TM"
                ),
                Button.inline(
                    f"↩️ Replies: {on_off(act.watch_enabled('replies'))}", b"TR"
                ),
            ],
            [
                Button.inline(
                    f"🎯 Keywords: {on_off(act.watch_enabled('keywords'))}", b"TK"
                ),
                Button.inline("📝 Template", CB_TEMPLATE),
            ],
            [
                Button.inline(f"📏 Preview: {act.max_chars}", CB_PREVIEW),
                Button.inline(
                    f"🚫 Exclusions: {act.excluded_count()}", CB_EXCLUSIONS
                ),
            ],
            [Button.inline("🔄 Reload", CB_RELOAD)],
        ]

    @staticmethod
    def _exclusions_keyboard() -> list[list[Any]]:
        return [
            [
                Button.inline("➕ Exclude Group", CB_EXC_ADD),
                Button.inline("➖ Remove Exclusion", CB_EXC_DEL),
            ],
            [
                Button.inline("📋 Excluded Groups", CB_EXC_LIST),
                Button.inline("⬅️ Back", CB_MAIN),
            ],
        ]

    @staticmethod
    def _back_keyboard() -> list[list[Any]]:
        return [[Button.inline("⬅️ Back", CB_MAIN)]]

    @staticmethod
    def _keywords_keyboard() -> list[list[Any]]:
        return [
            [
                Button.inline("➕ Add Keyword", CB_KW_ADD),
                Button.inline("➖ Remove Keyword", CB_KW_DEL),
            ],
            [Button.inline("⬅️ Back", CB_MAIN)],
        ]

    @staticmethod
    def _template_keyboard() -> list[list[Any]]:
        return [
            [
                Button.inline("✏️ Change Template", CB_TPL_SET),
                Button.inline("♻️ Reset Default", CB_TPL_DEFAULT),
            ],
            [Button.inline("⬅️ Back", CB_MAIN)],
        ]

    @staticmethod
    def _preview_keyboard() -> list[list[Any]]:
        row = [
            Button.inline(str(value), _PREVIEW_PREFIX + str(value).encode())
            for value in PREVIEW_PRESETS
        ]
        return [
            row[:2],
            row[2:],
            [
                Button.inline("✏️ Custom", CB_PREVIEW_CUSTOM),
                Button.inline("⬅️ Back", CB_MAIN),
            ],
        ]

    @staticmethod
    def _cancel_keyboard() -> list[list[Any]]:
        return [[Button.inline("❌ Cancel", CB_CANCEL)]]

    # -- views ------------------------------------------------------------- #
    def _view(self, name: str, note: str = "") -> tuple[str, list[list[Any]]]:
        """Return the (text, keyboard) pair for one menu."""
        act = self._actions
        prefix = f"{note}\n\n" if note else ""

        if name == "status":
            return prefix + act.status_text(), self._back_keyboard()

        if name == "keywords":
            return prefix + act.keywords_text(), self._keywords_keyboard()

        if name == "template":
            body = act.template_text(with_placeholders=False)
            return prefix + body, self._template_keyboard()

        if name == "exclusions":
            body = (
                "🚫 KEYWORD EXCLUSIONS\n\n"
                f"Groups excluded: {act.excluded_count()}\n\n"
                "In an excluded group, keyword matches are ignored — but "
                "mentions of watched members and replies to you still alert.\n\n"
                "Quickest way to exclude one: send /excludekeywords inside the "
                "group itself."
            )
            return prefix + body, self._exclusions_keyboard()

        if name == "exclusions_list":
            return prefix + act.exclusions_text(), self._exclusions_keyboard()

        if name == "preview":
            body = (
                "📏 ALERT PREVIEW LENGTH\n\n"
                f"Currently: {act.max_chars} characters\n"
                f"Allowed range: {MIN_MESSAGE_CHARS}–{MAX_MESSAGE_CHARS}\n\n"
                "How much of a matched message each alert includes."
            )
            return prefix + body, self._preview_keyboard()

        # main
        body = "⚙️ MAKIMA CONTROL PANEL\nWatcher controls and alert settings."
        return prefix + body, self._main_keyboard()

    # -- entry point used by /start and /help ------------------------------ #
    async def send_panel(self, event: Any, note: str = "") -> None:
        """Send a fresh panel message (a command deserves a new message)."""
        text, buttons = self._view("main", note)
        try:
            await event.respond(
                truncate(text, MAX_PANEL_CHARS),
                buttons=buttons,
                link_preview=False,
                parse_mode=None,
            )
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while sending the panel", exc.seconds)
        except RPCError:
            logger.exception("Telegram rejected the control panel message")

    # -- callback handling -------------------------------------------------- #
    async def _on_callback(self, event: Any) -> None:
        # Alert buttons belong to app.alert_lifecycle, which is registered first
        # and stops propagation. This guard is belt-and-braces: if the ordering
        # ever changes, the panel still must not redraw somebody's alert.
        if bytes(getattr(event, "data", b"") or b"") == CB_ALERT_SEEN:
            return

        sender_id = getattr(event, "sender_id", None)

        if not self._is_authorized(sender_id):
            logger.warning("Unauthorised callback from user id %s", sender_id)
            await self._answer(event, "⛔ Not authorised.", alert=True)
            return

        data: bytes = bytes(getattr(event, "data", b"") or b"")

        try:
            await self._dispatch(event, sender_id, data)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while handling a button", exc.seconds)
        except Exception:
            logger.exception("Failed to handle callback %r", data)
            await self._answer(event, "⚠️ Something went wrong.", alert=True)

    async def _dispatch(self, event: Any, sender_id: int, data: bytes) -> None:
        act = self._actions

        # Telethon's CallbackQuery.edit() auto-answers the query, and answering
        # is idempotent -- so the toast must be sent *before* the redraw or it
        # would be swallowed by that implicit empty answer.

        # --- navigation: entering any menu abandons a half-finished input ---
        if data in (
            CB_MAIN,
            CB_STATUS,
            CB_KEYWORDS,
            CB_TEMPLATE,
            CB_PREVIEW,
            CB_EXCLUSIONS,
            CB_EXC_LIST,
        ):
            self._pending.pop(sender_id, None)
            view = {
                CB_MAIN: "main",
                CB_STATUS: "status",
                CB_KEYWORDS: "keywords",
                CB_TEMPLATE: "template",
                CB_PREVIEW: "preview",
                CB_EXCLUSIONS: "exclusions",
                CB_EXC_LIST: "exclusions_list",
            }[data]
            await self._answer(event)
            await self._render(event, *self._view(view))
            return

        # --- cancel an input flow ---
        if data == CB_CANCEL:
            self._pending.pop(sender_id, None)
            await self._answer(event, "Cancelled")
            await self._render(event, *self._view("main", "❌ Cancelled."))
            return

        # --- watch-mode toggles ---
        if data in CB_TOGGLE:
            mode = CB_TOGGLE[data]
            new_state = await act.toggle_watch(mode)
            await self._answer(event, f"{mode.capitalize()}: {on_off(new_state)}")
            await self._render(event, *self._view("main"))
            return

        # --- prompts that need typed input ---
        prompt_map = {
            CB_KW_ADD: ADD_KEYWORD,
            CB_KW_DEL: REMOVE_KEYWORD,
            CB_TPL_SET: SET_TEMPLATE,
            CB_PREVIEW_CUSTOM: SET_MAX_CHARS,
            CB_EXC_ADD: EXCLUDE_CHAT,
            CB_EXC_DEL: ALLOW_CHAT,
        }
        if data in prompt_map:
            await self._begin_input(event, sender_id, prompt_map[data])
            return

        # --- template reset ---
        if data == CB_TPL_DEFAULT:
            await act.reset_template()
            await self._answer(event, "Template reset")
            await self._render(
                event, *self._view("template", "♻️ Template reset to the default.")
            )
            return

        # --- preview presets ---
        if data.startswith(_PREVIEW_PREFIX):
            raw = data[len(_PREVIEW_PREFIX) :].decode("utf-8", "ignore")
            try:
                value = await act.set_max_chars(raw)
            except ActionError as exc:
                await self._answer(event, f"⚠️ {exc}", alert=True)
                return
            await self._answer(event, f"Preview: {value}")
            await self._render(
                event, *self._view("preview", f"✅ Preview length set to {value}.")
            )
            return

        # --- reload ---
        if data == CB_RELOAD:
            await act.reload()
            await self._answer(event, "Reloaded")
            await self._render(
                event, *self._view("main", "✅ Keywords and settings reloaded.")
            )
            return

        # --- anything else is a stale button from an older version ---
        logger.info("Unknown callback data %r from %s", data, sender_id)
        await self._answer(event, "Menu refreshed")
        await self._render(event, *self._view("main", "This menu was out of date."))

    async def _begin_input(self, event: Any, sender_id: int, action: str) -> None:
        """Switch the panel into a prompt and wait for the admin's next message."""
        chat_id = getattr(event, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if message_id is None:
            # Older Telethon exposes it only on the raw query object.
            message_id = getattr(getattr(event, "query", None), "msg_id", None)
        if chat_id is None or message_id is None:
            await self._answer(event, "⚠️ Could not start that.", alert=True)
            return

        self._pending[sender_id] = Pending(action, int(chat_id), int(message_id))
        await self._answer(event)
        await self._render(event, _PROMPTS[action], self._cancel_keyboard())

    # -- typed input -------------------------------------------------------- #
    async def _on_text(self, event: Any) -> None:
        """Consume the next private message when an input flow is pending."""
        try:
            sender_id = getattr(event, "sender_id", None)
            raw = event.raw_text or ""

            # A slash command always wins: it cancels the flow and is handled
            # by the command dispatcher instead.
            if _LOOKS_LIKE_COMMAND.match(raw.strip()):
                if self._pending.pop(sender_id, None) is not None:
                    logger.info("Pending input for %s cancelled by a command", sender_id)
                return

            if not self._is_authorized(sender_id):
                return

            pending = self._pending.get(sender_id)
            if pending is None:
                # No flow in progress -- the panel is the useful answer.
                await self.send_panel(event)
                return

            await self._consume_input(event, sender_id, pending, raw)

        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while reading panel input", exc.seconds)
        except Exception:
            logger.exception("Failed to handle control-panel input")

    async def _consume_input(
        self, event: Any, sender_id: int, pending: Pending, raw: str
    ) -> None:
        act = self._actions
        action = pending.action

        try:
            if action == ADD_KEYWORD:
                keyword = await act.add_keyword(raw)
                note = f"✅ Added '{keyword}'."
            elif action == REMOVE_KEYWORD:
                keyword = await act.remove_keyword(raw)
                note = f"✅ Removed '{keyword}'."
            elif action == SET_TEMPLATE:
                unknown = await act.set_template(raw)
                note = "✅ Template updated."
                if unknown:
                    note += " Unrecognised placeholders will print as-is: " + ", ".join(
                        f"{{{{{name}}}}}" for name in unknown
                    )
            elif action == SET_MAX_CHARS:
                value = await act.set_max_chars(raw)
                note = f"✅ Preview length set to {value}."
            elif action == EXCLUDE_CHAT:
                label = await act.exclude_chat(raw.strip())
                note = f"✅ Keyword alerts disabled for {label}."
            elif action == ALLOW_CHAT:
                label = await act.allow_chat(raw.strip())
                note = f"✅ Keyword alerts enabled for {label}."
            else:  # pragma: no cover - guarded by _RETURN_VIEW
                self._pending.pop(sender_id, None)
                return

        except ActionError as exc:
            # Stay in the flow so the admin can simply try again.
            await self._edit_pending(
                pending,
                f"⚠️ {exc}\n\n{_PROMPTS[action]}",
                self._cancel_keyboard(),
            )
            return

        self._pending.pop(sender_id, None)
        text, buttons = self._view(_RETURN_VIEW[action], note)
        await self._edit_pending(pending, text, buttons)

    # -- Telegram plumbing -------------------------------------------------- #
    async def _render(self, event: Any, text: str, buttons: list[list[Any]]) -> None:
        """Edit the message the button sits on, tolerating the usual failures."""
        try:
            await event.edit(
                truncate(text, MAX_PANEL_CHARS),
                buttons=buttons,
                link_preview=False,
                parse_mode=None,
            )
        except MessageNotModifiedError:
            # Double-tap on the same button: nothing to do.
            pass
        except (MessageIdInvalidError, QueryIdInvalidError):
            logger.info("Panel message is gone or the query expired; skipping edit")
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while redrawing the panel", exc.seconds)
        except RPCError:
            logger.exception("Telegram rejected a panel edit")

    async def _edit_pending(
        self, pending: Pending, text: str, buttons: list[list[Any]]
    ) -> None:
        """Redraw the panel message an input flow started from."""
        try:
            await self._bot.edit_message(
                pending.chat_id,
                pending.message_id,
                truncate(text, MAX_PANEL_CHARS),
                buttons=buttons,
                link_preview=False,
                parse_mode=None,
            )
            return
        except MessageNotModifiedError:
            return
        except (MessageIdInvalidError, RPCError, ValueError) as exc:
            logger.info("Could not edit the panel message (%s); sending a new one", exc)

        # The original panel is unreachable -- send a replacement.
        try:
            sent = await self._bot.send_message(
                pending.chat_id,
                truncate(text, MAX_PANEL_CHARS),
                buttons=buttons,
                link_preview=False,
                parse_mode=None,
            )
            pending.message_id = sent.id
        except RPCError:
            logger.exception("Could not send a replacement panel message")

    async def _answer(self, event: Any, message: str = "", *, alert: bool = False) -> None:
        """Acknowledge the callback so Telegram drops the loading spinner."""
        try:
            await event.answer(message or None, alert=alert)
        except (QueryIdInvalidError, MessageIdInvalidError):
            logger.debug("Callback query expired before it could be answered")
        except FloodWaitError as exc:
            logger.warning("Flood wait (%ss) while answering a callback", exc.seconds)
        except RPCError:
            logger.debug("Telegram rejected a callback answer", exc_info=True)
