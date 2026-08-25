"""Runtime settings backed by ``data/watcher_settings.json``.

The file on disk is deep-merged onto :data:`DEFAULT_SETTINGS`, so new keys added
in a future version appear automatically and a partial (or slightly out of date)
file never crashes startup. All writes go through an :class:`asyncio.Lock` and
are atomic.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.logging_config import get_logger
from app.utils import atomic_write_text, deep_merge, get_by_path, set_by_path

logger = get_logger("settings")

#: The alert format: a short instruction from a supervisor, not a system dump.
#: The message link is not printed -- it becomes an inline button instead.
DEFAULT_TEMPLATE = (
    "{{tags}}\n"
    "\n"
    "\U0001f534 LOOK AT THIS.\n"
    "{{triggers}}\n"
    "\n"
    "\U0001f464 {{sender}}\n"
    "\U0001f3e2 {{group}}\n"
    "\n"
    "{{message_block}}\n"
    "\n"
    "⚠️ DON'T LEAVE IT UNATTENDED."
)

#: Every default this project has shipped. A stored template matching one of
#: these was never customised, so it is safe to upgrade in place; anything else
#: is the user's own work and is left exactly as it is.
_TEMPLATE_V1 = (
    "\U0001f7e5 \U0001d40c\U0001d400\U0001d40a\U0001d408\U0001d40c\U0001d400 "
    "\U0001d400\U0001d40b\U0001d404\U0001d411\U0001d413\n"
    "━━━━━━━━━"
    "━━━━━━━━━\n"
    "\U0001f552 {{timestamp}}\n"
    "\U0001f9e0 Reason: {{reasons}}\n"
    "\U0001f465 Group: {{group}}\n"
    "\U0001f517 Group Link: {{group_link}}\n"
    "\U0001f464 From: {{sender}}\n"
    "\U0001f9f7 Keyword hits: {{keyword_hits}}\n"
    "\U0001f4dd Message:\n"
    "{{message_text}}\n"
    "─────────"
    "─────────\n"
    "\U0001f449 Message: {{message_link}}"
)

LEGACY_TEMPLATES: tuple[str, ...] = (_TEMPLATE_V1,)

#: Every setting the application understands, with its shipped default.
DEFAULT_SETTINGS: dict[str, Any] = {
    "watching": {
        "mentions": True,
        "replies": True,
        "keywords": True,
    },
    "alerts": {
        "include_message_text": True,
        "max_message_chars": 500,
        "max_keyword_preview": 8,
        "template": DEFAULT_TEMPLATE,
    },
    "formatting": {
        "timestamp_format": "%Y-%m-%d %H:%M:%S UTC",
    },
    "ai": {
        "enabled": False,
    },
}

MIN_MESSAGE_CHARS = 20
MAX_MESSAGE_CHARS = 4000


class SettingsStore:
    """Loads, exposes and persists the watcher settings."""

    def __init__(self, path: Path, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = deepcopy(DEFAULT_SETTINGS)

    # -- reading ---------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    @property
    def data(self) -> dict[str, Any]:
        """The live settings dict. Treat as read-only; use :meth:`set` to write."""
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        value = get_by_path(self._data, dotted, None)
        if value is None:
            fallback = get_by_path(DEFAULT_SETTINGS, dotted, default)
            return default if fallback is None else fallback
        return value

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        return bool(self.get(dotted, default))

    def get_int(self, dotted: str, default: int) -> int:
        try:
            return int(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._data)

    # -- loading / saving ------------------------------------------------- #
    def _read_from_disk(self) -> dict[str, Any]:
        """Merge the on-disk file onto the defaults, tolerating a bad file."""
        seed = deepcopy(DEFAULT_SETTINGS)
        if self._defaults_path and self._defaults_path.is_file():
            try:
                seed = deep_merge(
                    seed, json.loads(self._defaults_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Ignoring unreadable defaults file %s (%s)", self._defaults_path, exc
                )

        if not self._path.is_file():
            logger.info("Settings file %s not found; creating it from defaults", self._path)
            atomic_write_text(self._path, json.dumps(seed, indent=2, ensure_ascii=False) + "\n")
            return seed

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            logger.error(
                "Settings file %s is not valid JSON (%s). Keeping a copy at %s and "
                "falling back to defaults.",
                self._path,
                exc,
                backup,
            )
            try:
                backup.write_text(self._path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                logger.warning("Could not write the corrupt-settings backup", exc_info=True)
            return seed
        except OSError as exc:
            logger.error("Could not read %s (%s); using defaults", self._path, exc)
            return seed

        if not isinstance(raw, dict):
            logger.error("Settings file %s must contain a JSON object; using defaults", self._path)
            return seed

        merged = deep_merge(seed, raw)
        if self._migrate(merged):
            atomic_write_text(
                self._path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            )
        return merged

    @staticmethod
    def _migrate(data: dict[str, Any]) -> bool:
        """Upgrade an untouched shipped template. Returns True if changed.

        A template the user has edited is never overwritten -- only one that is
        byte-identical to a previous release's default, which means they never
        customised it and would otherwise be stuck on the old format forever.
        """
        stored = get_by_path(data, "alerts.template")
        if isinstance(stored, str) and stored in LEGACY_TEMPLATES:
            set_by_path(data, "alerts.template", DEFAULT_TEMPLATE)
            logger.info(
                "Alert template was still the previous shipped default; "
                "upgraded it to the current one. Send /template to review it."
            )
            return True
        return False

    async def load(self) -> None:
        """(Re)read the settings file."""
        async with self._lock:
            self._data = await asyncio.to_thread(self._read_from_disk)
        logger.info("Settings loaded from %s", self._path)

    async def save(self) -> None:
        async with self._lock:
            await self._write()

    async def _write(self) -> None:
        payload = json.dumps(self._data, indent=2, ensure_ascii=False) + "\n"
        await asyncio.to_thread(atomic_write_text, self._path, payload)

    async def set(self, dotted: str, value: Any) -> None:
        """Set one dotted key and persist immediately."""
        async with self._lock:
            set_by_path(self._data, dotted, value)
            await self._write()
        logger.info("Setting updated: %s", dotted)

    # -- convenience accessors used across the app ------------------------ #
    @property
    def watch_mentions(self) -> bool:
        return self.get_bool("watching.mentions", True)

    @property
    def watch_replies(self) -> bool:
        return self.get_bool("watching.replies", True)

    @property
    def watch_keywords(self) -> bool:
        return self.get_bool("watching.keywords", True)

    @property
    def include_message_text(self) -> bool:
        return self.get_bool("alerts.include_message_text", True)

    @property
    def max_message_chars(self) -> int:
        value = self.get_int("alerts.max_message_chars", 500)
        return max(MIN_MESSAGE_CHARS, min(MAX_MESSAGE_CHARS, value))

    @property
    def max_keyword_preview(self) -> int:
        return max(1, self.get_int("alerts.max_keyword_preview", 8))

    @property
    def template(self) -> str:
        value = self.get("alerts.template", DEFAULT_TEMPLATE)
        return value if isinstance(value, str) and value.strip() else DEFAULT_TEMPLATE

    @property
    def timestamp_format(self) -> str:
        value = self.get("formatting.timestamp_format", "%Y-%m-%d %H:%M:%S UTC")
        return value if isinstance(value, str) and value else "%Y-%m-%d %H:%M:%S UTC"

    @property
    def ai_enabled(self) -> bool:
        return self.get_bool("ai.enabled", False)

    def modes_summary(self) -> str:
        parts = [
            f"mentions={'on' if self.watch_mentions else 'off'}",
            f"replies={'on' if self.watch_replies else 'off'}",
            f"keywords={'on' if self.watch_keywords else 'off'}",
            f"ai={'on' if self.ai_enabled else 'off'}",
        ]
        return ", ".join(parts)
