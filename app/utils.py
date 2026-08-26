"""Small shared helpers: merging, atomic writes and Telegram link building."""

from __future__ import annotations

import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from telethon import utils as tl_utils
from telethon.tl import types as tl_types

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


#: A first line longer than this is prose, not a heading.
HEADING_MAX_CHARS = 60
HEADING_MAX_WORDS = 8

#: Sentence-ending punctuation means the line is a sentence, not a title.
_HEADING_REJECT_SUFFIX = (".", "!", "?", ",", ";")

#: Bullets, list numbering and hashtags start a line of content, not a title.
_HEADING_REJECT_PREFIX = "-*•>#+0123456789"


def split_heading(text: str) -> tuple[str | None, str]:
    """Split a leading title line off a message, if it plausibly has one.

    Deliberately conservative: a heading is only recognised when the message has
    a short, punctuation-free first line *and* something underneath it. When in
    doubt the whole message is returned as the body, because inventing a title
    the sender did not write would be worse than showing none.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, ""

    lines = stripped.splitlines()
    if len(lines) < 2:
        return None, stripped

    first = lines[0].strip()
    rest = "\n".join(lines[1:]).strip()
    if not first or not rest:
        return None, stripped
    if len(first) > HEADING_MAX_CHARS:
        return None, stripped
    if len(first.split()) > HEADING_MAX_WORDS:
        return None, stripped
    if first.endswith(_HEADING_REJECT_SUFFIX):
        return None, stripped
    if first[0] in _HEADING_REJECT_PREFIX:
        return None, stripped

    return first, rest


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


#: ``kind`` values returned by :func:`build_message_url` on success.
LINK_PUBLIC = "public"
LINK_PUBLIC_TOPIC = "public_topic"
LINK_PRIVATE_SUPERGROUP = "private_supergroup"
LINK_PRIVATE_TOPIC = "private_supergroup_topic"

#: ...and the reasons it can fail.
NO_MESSAGE_ID = "no_message_id"
NO_CHAT_METADATA = "no_chat_metadata"
BASIC_GROUP = "basic_group"


def forum_topic_id(message: Any) -> int | None:
    """The topic a message sits in, for supergroups with Topics enabled.

    Telegram marks such messages with ``reply_to.forum_topic``. A message posted
    straight into a topic carries the topic root in ``reply_to_msg_id``; a reply
    *within* a topic carries it in ``reply_to_top_id`` and uses
    ``reply_to_msg_id`` for the message actually being replied to.
    """
    reply_to = getattr(message, "reply_to", None)
    if reply_to is None or not getattr(reply_to, "forum_topic", False):
        return None
    topic = getattr(reply_to, "reply_to_top_id", None) or getattr(
        reply_to, "reply_to_msg_id", None
    )
    try:
        return int(topic) if topic else None
    except (TypeError, ValueError):
        return None


#: More reasons a link cannot be built.
LEGACY_PRIVATE_GROUP = "legacy_private_group"
PRIVATE_CHAT = "private_chat"
UNKNOWN_PEER = "unknown_peer"


def describe_media(message: Any) -> str:
    """Short label for whatever media a message carries, or '' for plain text."""
    for attribute in ("photo", "video", "voice", "audio", "sticker", "document"):
        if getattr(message, attribute, None):
            return attribute
    return "media" if getattr(message, "media", None) else ""


def build_telegram_message_url(
    chat: Any, message: Any, *, chat_id: Any = None
) -> tuple[str | None, str]:
    """The one place a message deep link is built. Returns ``(url, kind)``.

    Resolves the peer through Telethon's own ``get_peer_id`` / ``resolve_id``
    rather than sniffing attributes off the chat entity. That matters because
    the chat entity is not always a fully-populated ``Channel``: it can be a
    partial object, a ``ChannelForbidden``, or missing entirely when
    ``get_chat()`` fails. The message's own ``peer_id`` is always present and
    always authoritative -- and it is present identically for text and media, so
    a photo links exactly like a sentence does.

    Media is never linked to its file or CDN URL: the link is to the *message*.
    """
    message_id = getattr(message, "id", None)
    if not message_id:
        return None, NO_MESSAGE_ID

    # 1. Prefer the peer carried on the message itself.
    marked: int | None = None
    peer = getattr(message, "peer_id", None)
    if peer is not None:
        try:
            marked = int(tl_utils.get_peer_id(peer))
        except (TypeError, ValueError, AttributeError):
            marked = None

    # 2. Fall back to the event's chat id, then to the entity's own id.
    if marked is None and chat_id is not None:
        try:
            marked = int(chat_id)
        except (TypeError, ValueError):
            marked = None
    if marked is None and chat is not None:
        try:
            marked = int(tl_utils.get_peer_id(chat))
        except (TypeError, ValueError, AttributeError):
            marked = None

    if marked is None:
        return None, NO_CHAT_METADATA

    try:
        internal_id, peer_cls = tl_utils.resolve_id(marked)
    except (TypeError, ValueError):
        return None, UNKNOWN_PEER

    # Only channels and supergroups have a message deep-link form at all.
    if peer_cls is not tl_types.PeerChannel:
        if peer_cls is tl_types.PeerChat:
            return None, LEGACY_PRIVATE_GROUP
        if peer_cls is tl_types.PeerUser:
            return None, PRIVATE_CHAT
        return None, UNKNOWN_PEER

    topic_id = forum_topic_id(message)
    in_topic = bool(topic_id) and topic_id != message_id
    suffix = f"{topic_id}/{message_id}" if in_topic else f"{message_id}"

    username = chat_username(chat)
    if username:
        return (
            f"https://t.me/{username}/{suffix}",
            LINK_PUBLIC_TOPIC if in_topic else LINK_PUBLIC,
        )

    return (
        f"https://t.me/c/{internal_id}/{suffix}",
        LINK_PRIVATE_TOPIC if in_topic else LINK_PRIVATE_SUPERGROUP,
    )


def build_message_url(
    chat: Any,
    message_id: int | None,
    *,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> tuple[str | None, str]:
    """The single source of truth for message deep links.

    Returns ``(url, kind)``. On success ``kind`` says which form was used; on
    failure ``url`` is ``None`` and ``kind`` is the reason, which callers log.

    * Public groups and channels -> ``t.me/<username>/<id>``
    * Private supergroups and channels -> ``t.me/c/<internal id>/<id>``
    * Inside a forum topic both gain a topic segment before the message id --
      without it Telegram opens the group but does not land on the message.
    * Legacy basic groups have no deep-link form at all.

    ``chat_id`` is a fallback for when the chat entity could not be resolved:
    ``event.chat_id`` is still a ``-100...`` peer id, and that is enough to
    build the private form. Passing it turns some would-be missing buttons into
    working ones.
    """
    if not message_id:
        return None, NO_MESSAGE_ID

    # The topic root itself needs no topic segment -- it *is* the topic.
    suffix = f"{message_id}"
    in_topic = bool(topic_id) and topic_id != message_id
    if in_topic:
        suffix = f"{topic_id}/{message_id}"

    username = chat_username(chat)
    if username:
        kind = LINK_PUBLIC_TOPIC if in_topic else LINK_PUBLIC
        return f"https://t.me/{username}/{suffix}", kind

    internal = 0
    if chat is not None and is_channel(chat):
        internal = normalize_channel_id(getattr(chat, "id", 0))
    # No usable entity: a raw -100... peer id still identifies a supergroup or
    # channel, which is exactly what the t.me/c/ form needs.
    if not internal and chat_id is not None and str(chat_id).startswith("-100"):
        internal = normalize_channel_id(chat_id)

    if internal:
        kind = LINK_PRIVATE_TOPIC if in_topic else LINK_PRIVATE_SUPERGROUP
        return f"https://t.me/c/{internal}/{suffix}", kind

    if chat is None:
        return None, NO_CHAT_METADATA
    return None, BASIC_GROUP


def message_url(
    chat: Any,
    message_id: int | None,
    *,
    chat_id: int | None = None,
    topic_id: int | None = None,
) -> str | None:
    """Just the URL from :func:`build_message_url`, or ``None``."""
    return build_message_url(chat, message_id, chat_id=chat_id, topic_id=topic_id)[0]


def build_message_link(chat: Any, message_id: int, fallback: str = "-") -> str:
    """String form, kept for the legacy ``{{message_link}}`` placeholder."""
    return message_url(chat, message_id) or fallback


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
