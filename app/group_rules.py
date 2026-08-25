"""Per-group keyword rules and instructions.

The global keyword list in ``data/keywords.txt`` still applies everywhere. A
group rule layers on top of it, and only for that one chat:

* ``keywords_enabled`` -- master switch for keyword matching in this group.
  Turning it off never affects mentions or replies.
* ``ignored_keywords`` -- global keywords that should not fire *here*. They keep
  working in every other group.
* ``extra_keywords`` -- keywords that fire only here, whether or not they are in
  the global list.
* ``description`` -- free text explaining what the group is for and why its
  rules exist. Stored verbatim, never parsed into keywords, and never sent
  anywhere: it is documentation today and a ready-made prompt for the optional
  AI layer later.

Rules are keyed by Telegram chat id, because group titles change and ids do not.
The title is kept alongside purely for display and is refreshed whenever the
group is reconfigured.

Everything lives in ``data/group_rules.json`` -- the same pattern as keywords
and watched members, so it sits on the Docker volume, survives rebuilds, and is
picked up by ``/reload``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.keywords import KeywordError, compile_all, normalize_keyword
from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("grouprules")

#: Longest group instruction accepted.
MAX_DESCRIPTION_LENGTH = 4000

#: Guards against a single group accumulating an unbounded list.
MAX_LIST_LENGTH = 200

_SEED: dict[str, Any] = {
    "_readme": (
        "Per-group keyword rules. Keyed by Telegram chat id. 'ignored_keywords' "
        "are global keywords that should not fire in that group; "
        "'extra_keywords' fire only there; 'keywords_enabled' false disables "
        "keyword matching for the group entirely (mentions and replies still "
        "work); 'description' is free text for humans and future AI use. "
        "Send /reload to the bot after editing by hand."
    ),
    "group_rules": {},
}


def normalize_chat_id(chat_id: Any) -> str:
    """Canonical string form of a Telegram chat id."""
    text = str(chat_id).strip()
    if not text.lstrip("-").isdigit():
        raise KeywordError(
            f"'{text}' is not a Telegram chat id. Ids look like -1001234567890."
        )
    return str(int(text))


@dataclass
class GroupRule:
    """One group's configuration."""

    chat_id: str
    title: str = ""
    keywords_enabled: bool = True
    ignored_keywords: list[str] = field(default_factory=list)
    extra_keywords: list[str] = field(default_factory=list)
    description: str = ""
    #: Compiled on load; never serialised.
    extra_patterns: dict[str, "re.Pattern[str]"] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def display_title(self) -> str:
        return self.title or f"Chat {self.chat_id}"

    def is_configured(self) -> bool:
        """False for a rule that carries no actual configuration."""
        return bool(
            not self.keywords_enabled
            or self.ignored_keywords
            or self.extra_keywords
            or self.description
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "keywords_enabled": self.keywords_enabled,
            "ignored_keywords": list(self.ignored_keywords),
            "extra_keywords": list(self.extra_keywords),
            "description": self.description,
        }

    def summary(self) -> str:
        state = "ON" if self.keywords_enabled else "OFF"
        return (
            f"{self.display_title}\n"
            f"   Keywords: {state}  ·  Ignored: {len(self.ignored_keywords)}"
            f"  ·  Extra: {len(self.extra_keywords)}"
        )


class GroupRulesStore:
    """Loads, exposes and persists every group rule."""

    def __init__(self, path: Path, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path
        self._lock = asyncio.Lock()
        self._rules: dict[str, GroupRule] = {}

    # -- reading ----------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def get(self, chat_id: Any) -> GroupRule | None:
        if chat_id is None:
            return None
        try:
            return self._rules.get(normalize_chat_id(chat_id))
        except KeywordError:
            return None

    def all(self) -> list[GroupRule]:
        return sorted(self._rules.values(), key=lambda rule: rule.display_title.lower())

    def count(self) -> int:
        return len(self._rules)

    def disabled_count(self) -> int:
        return sum(1 for rule in self._rules.values() if not rule.keywords_enabled)

    # -- loading / saving --------------------------------------------------- #
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
    def _clean_list(raw: Any, label: str, chat_id: str) -> list[str]:
        cleaned: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                keyword = normalize_keyword(item)
            except KeywordError as exc:
                logger.warning(
                    "Dropping %s entry %r for chat %s: %s", label, item, chat_id, exc
                )
                continue
            if keyword not in cleaned:
                cleaned.append(keyword)
        return sorted(cleaned)

    @classmethod
    def _parse(cls, raw: Any) -> dict[str, GroupRule]:
        entries = raw.get("group_rules", {}) if isinstance(raw, dict) else {}
        if not isinstance(entries, dict):
            logger.error("'group_rules' must be an object; ignoring it")
            return {}

        rules: dict[str, GroupRule] = {}
        for key, value in entries.items():
            try:
                chat_id = normalize_chat_id(key)
            except KeywordError as exc:
                logger.warning("Skipping group rule with bad id %r: %s", key, exc)
                continue
            if not isinstance(value, dict):
                logger.warning("Skipping group rule %s: not an object", chat_id)
                continue

            rule = GroupRule(
                chat_id=chat_id,
                title=str(value.get("title") or "").strip(),
                keywords_enabled=bool(value.get("keywords_enabled", True)),
                ignored_keywords=cls._clean_list(
                    value.get("ignored_keywords"), "ignored_keywords", chat_id
                ),
                extra_keywords=cls._clean_list(
                    value.get("extra_keywords"), "extra_keywords", chat_id
                ),
                description=str(value.get("description") or "")[:MAX_DESCRIPTION_LENGTH],
            )
            rule.extra_patterns = compile_all(rule.extra_keywords)
            rules[chat_id] = rule

        return rules

    def _read_from_disk(self) -> dict[str, GroupRule]:
        if not self._path.is_file():
            logger.info("Group-rules file %s not found; creating it", self._path)
            text = self._seed_text()
            atomic_write_text(self._path, text)
            try:
                return self._parse(json.loads(text))
            except json.JSONDecodeError:
                return {}

        try:
            return self._parse(json.loads(self._path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            logger.error(
                "%s is not valid JSON (%s); no group rules are active", self._path, exc
            )
            return {}
        except OSError as exc:
            logger.error("Could not read %s (%s); keeping current rules", self._path, exc)
            return dict(self._rules)

    async def load(self) -> None:
        async with self._lock:
            self._rules = await asyncio.to_thread(self._read_from_disk)
        logger.info("Group rules loaded | groups=%d", len(self._rules))

    def _payload(self) -> str:
        data = {
            "_readme": _SEED["_readme"],
            "group_rules": {
                chat_id: rule.to_json() for chat_id, rule in sorted(self._rules.items())
            },
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    async def _write(self) -> None:
        await asyncio.to_thread(atomic_write_text, self._path, self._payload())

    # -- mutation ----------------------------------------------------------- #
    def _require(self, chat_id: Any) -> GroupRule:
        key = normalize_chat_id(chat_id)
        rule = self._rules.get(key)
        if rule is None:
            raise KeywordError("That group has no rules yet. Add it first.")
        return rule

    async def ensure(self, chat_id: Any, title: str = "") -> GroupRule:
        """Return the rule for a chat, creating an empty one if needed."""
        key = normalize_chat_id(chat_id)
        async with self._lock:
            rule = self._rules.get(key)
            created = rule is None
            if rule is None:
                rule = GroupRule(chat_id=key, title=str(title or "").strip())
                self._rules[key] = rule
            elif title and rule.title != str(title).strip():
                # Titles drift; the id is what identifies the group.
                rule.title = str(title).strip()
            else:
                return rule
            await self._write()
        if created:
            logger.info("Group rule created | chat=%s | group=%s", key, rule.display_title)
        return rule

    async def remove(self, chat_id: Any) -> str:
        key = normalize_chat_id(chat_id)
        async with self._lock:
            rule = self._rules.pop(key, None)
            if rule is None:
                raise KeywordError("That group has no rules to remove.")
            await self._write()
        logger.info("Group rule removed | chat=%s | group=%s", key, rule.display_title)
        return rule.display_title

    async def set_keywords_enabled(self, chat_id: Any, enabled: bool) -> bool:
        async with self._lock:
            rule = self._require(chat_id)
            rule.keywords_enabled = bool(enabled)
            await self._write()
        logger.info(
            "Group keyword monitoring %s | chat=%s",
            "enabled" if enabled else "disabled",
            rule.chat_id,
        )
        return rule.keywords_enabled

    async def toggle_keywords(self, chat_id: Any) -> bool:
        rule = self._require(chat_id)
        return await self.set_keywords_enabled(chat_id, not rule.keywords_enabled)

    async def _mutate_list(
        self, chat_id: Any, attr: str, word: str, *, add: bool
    ) -> str:
        keyword = normalize_keyword(word)
        async with self._lock:
            rule = self._require(chat_id)
            items: list[str] = getattr(rule, attr)
            if add:
                if keyword in items:
                    raise KeywordError(f"'{keyword}' is already in that list.")
                if len(items) >= MAX_LIST_LENGTH:
                    raise KeywordError(f"That list is full ({MAX_LIST_LENGTH} entries).")
                items.append(keyword)
                items.sort()
            else:
                if keyword not in items:
                    raise KeywordError(f"'{keyword}' is not in that list.")
                items.remove(keyword)
            if attr == "extra_keywords":
                rule.extra_patterns = compile_all(rule.extra_keywords)
            await self._write()
        logger.info(
            "Group %s %s | chat=%s | keyword=%s",
            attr,
            "added" if add else "removed",
            rule.chat_id,
            keyword,
        )
        return keyword

    async def add_ignored(self, chat_id: Any, word: str) -> str:
        return await self._mutate_list(chat_id, "ignored_keywords", word, add=True)

    async def remove_ignored(self, chat_id: Any, word: str) -> str:
        return await self._mutate_list(chat_id, "ignored_keywords", word, add=False)

    async def add_extra(self, chat_id: Any, word: str) -> str:
        return await self._mutate_list(chat_id, "extra_keywords", word, add=True)

    async def remove_extra(self, chat_id: Any, word: str) -> str:
        return await self._mutate_list(chat_id, "extra_keywords", word, add=False)

    async def clear_list(self, chat_id: Any, attr: str) -> int:
        async with self._lock:
            rule = self._require(chat_id)
            items: list[str] = getattr(rule, attr)
            removed = len(items)
            items.clear()
            if attr == "extra_keywords":
                rule.extra_patterns = {}
            await self._write()
        logger.info("Group %s cleared | chat=%s | removed=%d", attr, rule.chat_id, removed)
        return removed

    async def set_description(self, chat_id: Any, text: str, admin: Any = None) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) > MAX_DESCRIPTION_LENGTH:
            raise KeywordError(
                f"Description is too long ({len(cleaned)} chars, "
                f"limit {MAX_DESCRIPTION_LENGTH})."
            )
        async with self._lock:
            rule = self._require(chat_id)
            rule.description = cleaned
            await self._write()
        logger.info(
            "Group description updated | chat=%s | admin=%s | chars=%d",
            rule.chat_id,
            admin,
            len(cleaned),
        )
        return cleaned

    # -- migration ---------------------------------------------------------- #
    async def migrate_from_settings(self, settings: Any) -> int:
        """Fold the older ``keyword_excluded_chats`` map into group rules.

        The previous release stored whole-group keyword exclusions in
        ``watcher_settings.json``. Those become ``keywords_enabled: false`` here,
        and the old key is cleared so there is only ever one source of truth.
        Nothing is lost and no manual migration is needed.
        """
        legacy = settings.get("keyword_excluded_chats", {})
        if not isinstance(legacy, dict) or not legacy:
            return 0

        migrated = 0
        for chat_id, title in legacy.items():
            try:
                key = normalize_chat_id(chat_id)
            except KeywordError:
                logger.warning("Skipping legacy exclusion with bad id %r", chat_id)
                continue
            async with self._lock:
                rule = self._rules.get(key)
                if rule is None:
                    rule = GroupRule(chat_id=key, title=str(title or "").strip())
                    self._rules[key] = rule
                rule.keywords_enabled = False
                migrated += 1
            # written once below, outside the loop

        if migrated:
            async with self._lock:
                await self._write()
            await settings.set("keyword_excluded_chats", {})
            logger.info(
                "Migrated %d legacy keyword exclusion(s) into group rules", migrated
            )
        return migrated

    # -- matching support ---------------------------------------------------- #
    def effective_patterns(
        self, chat_id: Any, global_patterns_for: Any
    ) -> tuple[dict[str, "re.Pattern[str]"], GroupRule | None]:
        """The pattern map to match a message in ``chat_id`` against.

        ``global_patterns_for`` is ``KeywordStore.patterns_excluding`` -- passing
        it in keeps this module free of any dependency on the keyword store
        instance while still using its single compiled pattern set.

        Returns ``({}, rule)`` when the group has keyword matching switched off.
        """
        rule = self.get(chat_id)
        if rule is None:
            return global_patterns_for(()), None
        if not rule.keywords_enabled:
            return {}, rule

        patterns = global_patterns_for(rule.ignored_keywords)
        # Group keywords win on a name clash: they were configured deliberately.
        patterns.update(rule.extra_patterns)
        return patterns, rule


def describe_rule(rule: GroupRule | None, chat_id: Any = None) -> str:
    """The group-details screen text."""
    if rule is None:
        return (
            "🏢 GROUP RULES\n\n"
            f"Chat ID:\n{chat_id}\n\n"
            "No rules configured — global keywords apply here as normal."
        )

    lines = [
        "🏢 GROUP RULES",
        "",
        rule.display_title,
        "",
        "Chat ID:",
        rule.chat_id,
        "",
        "Keyword monitoring:",
        "ON" if rule.keywords_enabled else "OFF",
        "",
        "Ignored global keywords:",
        "\n".join(rule.ignored_keywords) if rule.ignored_keywords else "(none)",
        "",
        "Group-specific keywords:",
        "\n".join(rule.extra_keywords) if rule.extra_keywords else "(none)",
        "",
        "Description:",
        rule.description or "(none)",
    ]
    return "\n".join(lines)


def describe_all(rules: Iterable[GroupRule]) -> str:
    """The configured-groups summary."""
    items = list(rules)
    if not items:
        return (
            "🏢 GROUP RULES (0)\n\n"
            "No groups are configured. Global keywords apply everywhere.\n\n"
            "Send /grouprules inside a group to configure it."
        )
    lines = [f"🏢 GROUP RULES ({len(items)})", ""]
    for index, rule in enumerate(items, start=1):
        lines.append(f"{index}. {rule.summary()}")
    return "\n".join(lines)
