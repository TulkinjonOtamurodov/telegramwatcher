"""Telegram group to truck mapping.

One group maps to exactly one unit, which is what lets a load confirmation be
attributed without the message itself naming the truck.

The Telegram **chat id** is the identifier. Group titles get renamed and are
stored only for display, refreshed whenever the mapping is touched.

The driver name is intentionally optional. FuelHelper already holds
unit-to-driver on the board, so the mapping stores one only as a fallback for
when that lookup is unavailable -- keeping a second copy of driver names in sync
by hand is exactly the duplication worth avoiding.

Lives in ``data/fuel_units.json`` on the Docker volume; ``/reload`` re-reads it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("fuel.units")

MAX_UNIT_LENGTH = 16
MAX_DRIVER_LENGTH = 80

_SEED: dict[str, Any] = {
    "_readme": (
        "Telegram group -> truck mapping for fuel automation. Keyed by chat id, "
        "which is stable across group renames. 'driver' is an optional fallback: "
        "FuelHelper is preferred as the source of driver names. Send "
        "/mapfuelunit <unit> inside a group to add it, or /reload after editing."
    ),
    "mappings": {},
}


class UnitMappingError(ValueError):
    """Raised with a message safe to show an admin."""


def normalize_unit(raw: Any) -> str:
    """Unit numbers are short alphanumeric tokens: 152, 1290, 9992."""
    unit = str(raw or "").strip().upper()
    unit = unit.lstrip("#").strip()
    if not unit:
        raise UnitMappingError("Unit number is empty.")
    if len(unit) > MAX_UNIT_LENGTH:
        raise UnitMappingError(f"Unit number is longer than {MAX_UNIT_LENGTH} characters.")
    if not unit.replace("-", "").isalnum():
        raise UnitMappingError(f"'{unit}' does not look like a unit number.")
    return unit


def normalize_chat_id(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text.lstrip("-").isdigit():
        raise UnitMappingError(
            f"'{text}' is not a Telegram chat id. Ids look like -1001234567890."
        )
    return str(int(text))


@dataclass
class UnitMapping:
    chat_id: str
    unit: str
    driver: str = ""
    group_title: str = ""

    @property
    def display_title(self) -> str:
        return self.group_title or f"Chat {self.chat_id}"

    def to_json(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "driver": self.driver,
            "group_title": self.group_title,
        }

    def summary(self) -> str:
        line = f"UNIT {self.unit} — {self.display_title}"
        if self.driver:
            line += f"\n   {self.driver}"
        return f"{line}\n   {self.chat_id}"


class UnitMappingStore:
    """Loads, exposes and persists the group-to-unit mappings."""

    def __init__(self, path: Path, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path
        self._lock = asyncio.Lock()
        self._by_chat: dict[str, UnitMapping] = {}

    # -- reading ----------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def get(self, chat_id: Any) -> UnitMapping | None:
        try:
            return self._by_chat.get(normalize_chat_id(chat_id))
        except UnitMappingError:
            return None

    def unit_for(self, chat_id: Any) -> str | None:
        mapping = self.get(chat_id)
        return mapping.unit if mapping else None

    def chats_for_unit(self, unit: str) -> list[str]:
        try:
            wanted = normalize_unit(unit)
        except UnitMappingError:
            return []
        return [
            chat_id
            for chat_id, mapping in self._by_chat.items()
            if mapping.unit == wanted
        ]

    def all(self) -> list[UnitMapping]:
        return sorted(self._by_chat.values(), key=lambda item: item.unit)

    def count(self) -> int:
        return len(self._by_chat)

    # -- loading / saving --------------------------------------------------- #
    def _seed_text(self) -> str:
        if self._defaults_path and self._defaults_path.is_file():
            try:
                return self._defaults_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not read %s (%s); using built-in seed", self._defaults_path, exc)
        return json.dumps(_SEED, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _parse(raw: Any) -> dict[str, UnitMapping]:
        entries = raw.get("mappings", {}) if isinstance(raw, dict) else {}
        if not isinstance(entries, dict):
            logger.error("'mappings' must be an object; ignoring it")
            return {}

        mappings: dict[str, UnitMapping] = {}
        for key, value in entries.items():
            try:
                chat_id = normalize_chat_id(key)
            except UnitMappingError as exc:
                logger.warning("Skipping mapping with bad chat id %r: %s", key, exc)
                continue

            payload = value if isinstance(value, dict) else {"unit": value}
            try:
                unit = normalize_unit(payload.get("unit"))
            except UnitMappingError as exc:
                logger.warning("Skipping mapping for chat %s: %s", chat_id, exc)
                continue

            mappings[chat_id] = UnitMapping(
                chat_id=chat_id,
                unit=unit,
                driver=str(payload.get("driver") or "").strip()[:MAX_DRIVER_LENGTH],
                group_title=str(payload.get("group_title") or "").strip(),
            )
        return mappings

    def _read_from_disk(self) -> dict[str, UnitMapping]:
        if not self._path.is_file():
            logger.info("Unit mapping file %s not found; creating it", self._path)
            text = self._seed_text()
            atomic_write_text(self._path, text)
            try:
                return self._parse(json.loads(text))
            except json.JSONDecodeError:
                return {}

        try:
            return self._parse(json.loads(self._path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            logger.error("%s is not valid JSON (%s); no units are mapped", self._path, exc)
            return {}
        except OSError as exc:
            logger.error("Could not read %s (%s); keeping current mappings", self._path, exc)
            return dict(self._by_chat)

    async def load(self) -> None:
        async with self._lock:
            self._by_chat = await asyncio.to_thread(self._read_from_disk)
        logger.info("Fuel unit mappings loaded | mappings=%d", len(self._by_chat))

    def _payload(self) -> str:
        data = {
            "_readme": _SEED["_readme"],
            "mappings": {
                chat_id: mapping.to_json()
                for chat_id, mapping in sorted(self._by_chat.items())
            },
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    async def _write(self) -> None:
        await asyncio.to_thread(atomic_write_text, self._path, self._payload())

    # -- mutation ----------------------------------------------------------- #
    async def set_mapping(
        self,
        chat_id: Any,
        unit: str,
        *,
        driver: str = "",
        group_title: str = "",
    ) -> UnitMapping:
        """Map a group to a unit, replacing any previous mapping for that group."""
        key = normalize_chat_id(chat_id)
        number = normalize_unit(unit)

        async with self._lock:
            previous = self._by_chat.get(key)
            mapping = UnitMapping(
                chat_id=key,
                unit=number,
                driver=(driver or (previous.driver if previous else "")).strip()[:MAX_DRIVER_LENGTH],
                group_title=(group_title or (previous.group_title if previous else "")).strip(),
            )
            self._by_chat[key] = mapping
            await self._write()

        if previous and previous.unit != number:
            logger.info(
                "Fuel unit remapped | chat=%s | old_unit=%s | unit=%s",
                key,
                previous.unit,
                number,
            )
        else:
            logger.info("Fuel unit mapped | chat=%s | unit=%s", key, number)
        return mapping

    async def set_driver(self, chat_id: Any, driver: str) -> UnitMapping:
        key = normalize_chat_id(chat_id)
        async with self._lock:
            mapping = self._by_chat.get(key)
            if mapping is None:
                raise UnitMappingError("That group is not mapped to a unit yet.")
            mapping.driver = str(driver or "").strip()[:MAX_DRIVER_LENGTH]
            await self._write()
        logger.info("Fuel unit driver set | chat=%s | unit=%s", key, mapping.unit)
        return mapping

    async def remove(self, chat_id: Any) -> str:
        key = normalize_chat_id(chat_id)
        async with self._lock:
            mapping = self._by_chat.pop(key, None)
            if mapping is None:
                raise UnitMappingError("That group is not mapped to a unit.")
            await self._write()
        logger.info("Fuel unit mapping removed | chat=%s | unit=%s", key, mapping.unit)
        return mapping.unit


def describe_mappings(mappings: list[UnitMapping]) -> str:
    if not mappings:
        return (
            "🚛 FUEL UNITS (0)\n\n"
            "No groups are mapped to a truck yet.\n\n"
            "Send /mapfuelunit <unit> inside a group to map it, e.g.\n"
            "/mapfuelunit 152"
        )
    lines = [f"🚛 FUEL UNITS ({len(mappings)})", ""]
    for index, mapping in enumerate(mappings, start=1):
        lines.append(f"{index}. {mapping.summary()}")
    return "\n".join(lines)
