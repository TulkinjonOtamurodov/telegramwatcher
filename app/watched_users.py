"""Watched members -- the people whose Telegram mentions MAKIMA tags.

This is deliberately **not** the same list as ``ADMIN_USER_IDS``. Admins control
the bot and receive alerts; watched members are simply people whose name in a
group message is worth flagging. An admin is only watched if you also add them
here, and a watched member needs no admin rights.

Stored in ``data/watched_users.json`` -- the same pattern as keywords and
settings, so it lives on the Docker volume, survives rebuilds, and is picked up
by ``/reload`` without a restart.

    {
      "members": [
        {"tag": "RAYN", "user_id": 8361140465, "username": "Rayn_ST"},
        {"tag": "THOMAS", "user_id": 123456789, "username": "thomas_username"}
      ]
    }

``user_id`` and ``username`` are both optional individually, but at least one
must be present or the entry can never match anything.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from telethon.tl.types import MessageEntityMentionName

from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("watched")

MAX_TAG_LENGTH = 24

_SEED: dict[str, Any] = {
    "_readme": (
        "People whose Telegram mentions MAKIMA should tag. Separate from "
        "ADMIN_USER_IDS. Each entry needs a tag plus a user_id and/or a "
        "username. Send /reload to the bot after editing."
    ),
    "_example": {"tag": "RAYN", "user_id": 8361140465, "username": "Rayn_ST"},
    "members": [],
}


def normalize_tag(raw: str) -> str:
    """Turn a display name into a Telegram-safe hashtag body (no '#')."""
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "", str(raw).strip().lstrip("#"))
    return cleaned.upper()[:MAX_TAG_LENGTH]


@dataclass(frozen=True)
class WatchedUser:
    """One configured member and the ways a message can refer to them."""

    tag: str
    user_id: int | None = None
    username: str | None = None

    @property
    def hashtag(self) -> str:
        return f"#{self.tag}"


class WatchedUserStore:
    """Loads the watched-member list and matches mentions against it."""

    def __init__(self, path: Path, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path
        self._lock = asyncio.Lock()
        self._members: list[WatchedUser] = []
        self._patterns: dict[str, re.Pattern[str]] = {}
        self._by_id: dict[int, WatchedUser] = {}

    # -- reading ----------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> list[WatchedUser]:
        return list(self._members)

    def count(self) -> int:
        return len(self._members)

    # -- loading ----------------------------------------------------------- #
    def _seed_text(self) -> str:
        if self._defaults_path and self._defaults_path.is_file():
            try:
                return self._defaults_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "Could not read %s (%s); using the built-in seed",
                    self._defaults_path,
                    exc,
                )
        return json.dumps(_SEED, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _parse(raw: Any) -> list[WatchedUser]:
        """Build the member list, skipping (and reporting) bad entries."""
        if isinstance(raw, dict):
            entries = raw.get("members", [])
        elif isinstance(raw, list):
            entries = raw
        else:
            logger.error("watched_users.json must be an object or a list; ignoring it")
            return []

        if not isinstance(entries, list):
            logger.error("'members' must be a list; ignoring it")
            return []

        members: list[WatchedUser] = []
        seen_tags: set[str] = set()

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                logger.warning("Watched member #%d is not an object; skipped", index + 1)
                continue

            tag = normalize_tag(entry.get("tag") or entry.get("name") or "")
            if not tag:
                logger.warning("Watched member #%d has no usable tag; skipped", index + 1)
                continue

            raw_id = entry.get("user_id")
            user_id: int | None = None
            if raw_id not in (None, ""):
                try:
                    user_id = int(raw_id)
                except (TypeError, ValueError):
                    logger.warning(
                        "Watched member '%s' has a non-numeric user_id (%r); ignoring the id",
                        tag,
                        raw_id,
                    )

            # str() first: a username written unquoted in JSON arrives as a number.
            username = str(entry.get("username") or "").strip().lstrip("@") or None

            if user_id is None and not username:
                logger.warning(
                    "Watched member '%s' has neither user_id nor username; skipped", tag
                )
                continue

            if tag in seen_tags:
                logger.warning("Watched member tag '%s' is duplicated; skipped", tag)
                continue

            seen_tags.add(tag)
            members.append(WatchedUser(tag=tag, user_id=user_id, username=username))

        return members

    def _read_from_disk(self) -> list[WatchedUser]:
        if not self._path.is_file():
            logger.info("Watched-member file %s not found; creating it", self._path)
            text = self._seed_text()
            atomic_write_text(self._path, text)
            try:
                return self._parse(json.loads(text))
            except json.JSONDecodeError:
                return []

        try:
            return self._parse(json.loads(self._path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            logger.error(
                "%s is not valid JSON (%s); no members are being watched", self._path, exc
            )
            return []
        except OSError as exc:
            logger.error("Could not read %s (%s); keeping the current list", self._path, exc)
            return list(self._members)

    def _rebuild_index(self) -> None:
        patterns: dict[str, re.Pattern[str]] = {}
        by_id: dict[int, WatchedUser] = {}
        for member in self._members:
            if member.username:
                patterns[member.tag] = re.compile(
                    rf"(?<!\w)@{re.escape(member.username)}(?!\w)", re.IGNORECASE
                )
            if member.user_id is not None:
                by_id[member.user_id] = member
        self._patterns = patterns
        self._by_id = by_id

    async def load(self) -> None:
        async with self._lock:
            self._members = await asyncio.to_thread(self._read_from_disk)
            self._rebuild_index()
        logger.info("Loaded %d watched member(s) from %s", len(self._members), self._path)

    # -- matching ----------------------------------------------------------- #
    def find_mentions(self, text: str, entities: Iterable[Any] | None = None) -> list[WatchedUser]:
        """Return the watched members this message reliably refers to.

        Only two signals count, both exact: a literal ``@username`` on a word
        boundary, and a Telegram text-mention entity carrying the numeric user
        id. Names appearing as ordinary words never match.
        """
        if not self._members:
            return []

        hits: list[WatchedUser] = []
        seen: set[str] = set()

        for member in self._members:
            pattern = self._patterns.get(member.tag)
            if pattern and text and pattern.search(text):
                hits.append(member)
                seen.add(member.tag)

        for entity in entities or []:
            if not isinstance(entity, MessageEntityMentionName):
                continue
            member = self._by_id.get(getattr(entity, "user_id", None))
            if member is not None and member.tag not in seen:
                hits.append(member)
                seen.add(member.tag)

        return hits

    @staticmethod
    def tags(members: Sequence[WatchedUser]) -> list[str]:
        return [member.tag for member in members]
