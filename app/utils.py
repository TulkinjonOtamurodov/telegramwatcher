"""Small shared helpers: merging, atomic writes and Telegram link building."""

from __future__ import annotations

import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

#: Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_MESSAGE = 4096

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# --------------------------------------------------------------------------- #
# dict / file helpers
# --------------------------------------------------------------------------- #
def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """Return ``base`` merged with ``override``, recursing into nested dicts.

    ``base`` is never mutated. Keys present only in ``base`` survive, which is
    what lets a future release add settings without breaking an existing
    ``watcher_settings.json``.
    """
    merged = deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` without ever leaving a half-written file.

    The temp file is created in the same directory, so ``os.replace`` is an
    atomic rename rather than a cross-device copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def get_by_path(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """Look up dotted paths such as ``alerts.max_message_chars``."""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def set_by_path(data: dict, dotted: str, value: Any) -> None:
    """Assign a dotted path, creating intermediate dicts as needed."""
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def utc_timestamp(
    fmt: str = "%Y-%m-%d %H:%M:%S UTC", when: datetime | None = None
) -> str:
    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        return moment.strftime(fmt)
    except (ValueError, TypeError):
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def truncate(text: str, limit: int, suffix: str = " [...]") -> str:
    """Shorten ``text`` to ``limit`` characters, appending ``suffix`` if cut."""
    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(1, limit - len(suffix))
    return text[:keep].rstrip() + suffix


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """Replace ``{{name}}`` placeholders. Unknown names are left untouched."""

    def _swap(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in variables:
            value = variables[name]
            return "" if value is None else str(value)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_swap, template)


def template_placeholders(template: str) -> set[str]:
    return {match.group(1) for match in _PLACEHOLDER_RE.finditer(template)}


def chunk_text(text: str, limit: int = TELEGRAM_MAX_MESSAGE - 96) -> Iterator[str]:
    """Split a long reply into Telegram-sized pieces, preferring line breaks."""
    if not text:
        yield ""
        return
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        window = remaining[:limit]
        split_at = window.rfind("\n")
        if split_at < limit // 2:
            split_at = limit
        yield remaining[:split_at].rstrip("\n")
        remaining = remaining[split_at:].lstrip("\n")


# --------------------------------------------------------------------------- #
# Telegram link helpers
# --------------------------------------------------------------------------- #
def normalize_channel_id(chat_id: int | str) -> int:
    """Return the bare channel ID used by ``https://t.me/c/<id>/<msg>`` links.

    Telethon exposes two shapes for the same supergroup: ``event.chat_id`` is
    ``-1001234567890`` while ``entity.id`` is already ``1234567890``. Both are
    normalised to the latter here.
    """
    text = str(chat_id).strip()
    if text.startswith("-100"):
        return int(text[4:])
    return abs(int(text))


def is_channel(chat: Any) -> bool:
    """True for supergroups and broadcast channels, false for basic groups."""
    return (
        getattr(chat, "megagroup", None) is not None
        or getattr(chat, "broadcast", None) is not None
    )


def chat_username(chat: Any) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return str(username)
    # Telegram supports multiple usernames per chat; take the first active one.
    for entry in getattr(chat, "usernames", None) or []:
        name = getattr(entry, "username", None)
        if name and getattr(entry, "active", True):
            return str(name)
    return None


def build_group_link(chat: Any, fallback: str = "-") -> str:
    username = chat_username(chat)
    if username:
        return f"https://t.me/{username}"
    if is_channel(chat):
        return f"https://t.me/c/{normalize_channel_id(getattr(chat, 'id', 0))}"
    return fallback


def build_message_link(chat: Any, message_id: int, fallback: str = "-") -> str:
    username = chat_username(chat)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if is_channel(chat):
        chat_id = normalize_channel_id(getattr(chat, "id", 0))
        return f"https://t.me/c/{chat_id}/{message_id}"
    # Legacy basic groups have no public or private deep-link form.
    return fallback


def display_name(entity: Any, fallback: str = "Unknown") -> str:
    """Best-effort human name for a user, chat or channel."""
    if entity is None:
        return fallback
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = (getattr(entity, "first_name", None) or "").strip()
    last = (getattr(entity, "last_name", None) or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if full:
        return full
    username = chat_username(entity)
    if username:
        return f"@{username}"
    return fallback


def join_nonempty(parts: Iterable[str], separator: str = " | ") -> str:
    return separator.join(part for part in parts if part)
