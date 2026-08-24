"""Keyword storage and matching.

Matching is case-insensitive and boundary-aware, so ``claim`` does not fire on
``disclaimer``. Multi-word phrases such as ``fuel card`` are supported, and when
a longer phrase matches, the shorter keywords fully contained inside it are
suppressed from the alert preview (``fuel card`` wins over ``fuel``).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.logging_config import get_logger
from app.utils import atomic_write_text

logger = get_logger("keywords")

#: Shipped defaults, used when data/keywords.txt is missing entirely.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "safety",
    "claim",
    "claims",
    "accident",
    "incident",
    "inspection",
    "violation",
    "citation",
    "oos",
    "mvr",
    "psp",
    "insurance",
    "attorney",
    "lawsuit",
    "damage",
    "injury",
    "police",
    "dot",
    "permit",
    "fuel",
    "fuel card",
    "card",
    "camera",
    "termination",
    "terminate",
    "lease",
)

MAX_KEYWORD_LENGTH = 64


class KeywordError(ValueError):
    """Raised when a keyword cannot be accepted."""


def normalize_keyword(raw: str) -> str:
    """Validate and canonicalise a keyword (lowercase, collapsed whitespace)."""
    keyword = " ".join(str(raw).split()).strip().lower()
    if not keyword:
        raise KeywordError("Keyword is empty.")
    if keyword.startswith("#"):
        raise KeywordError("Keywords cannot start with '#' (that marks a comment).")
    if len(keyword) > MAX_KEYWORD_LENGTH:
        raise KeywordError(f"Keyword is longer than {MAX_KEYWORD_LENGTH} characters.")
    return keyword


def compile_keyword(keyword: str) -> re.Pattern[str]:
    """Build a boundary-aware, case-insensitive pattern for one keyword.

    Lookarounds are used instead of ``\\b`` because they behave correctly even
    when the keyword starts or ends with a non-word character (``o.o.s.``).
    """
    escaped = re.escape(keyword)
    prefix = r"(?<!\w)" if keyword[:1].isalnum() or keyword[:1] == "_" else ""
    suffix = r"(?!\w)" if keyword[-1:].isalnum() or keyword[-1:] == "_" else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE | re.UNICODE)


class KeywordStore:
    """Holds the keyword list, its compiled patterns, and persists changes."""

    def __init__(self, path: Path, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path
        self._lock = asyncio.Lock()
        self._keywords: list[str] = []
        self._patterns: dict[str, re.Pattern[str]] = {}

    # -- reading ---------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> list[str]:
        return list(self._keywords)

    def count(self) -> int:
        return len(self._keywords)

    def __contains__(self, keyword: object) -> bool:
        return str(keyword).strip().lower() in self._patterns

    # -- loading / saving -------------------------------------------------- #
    def _seed_text(self) -> str:
        if self._defaults_path and self._defaults_path.is_file():
            try:
                return self._defaults_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "Could not read defaults file %s (%s); using built-in list",
                    self._defaults_path,
                    exc,
                )
        return "\n".join(DEFAULT_KEYWORDS) + "\n"

    @staticmethod
    def _parse(text: str) -> list[str]:
        seen: set[str] = set()
        keywords: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                keyword = normalize_keyword(stripped)
            except KeywordError as exc:
                logger.warning("Skipping keyword line %r: %s", stripped, exc)
                continue
            if keyword not in seen:
                seen.add(keyword)
                keywords.append(keyword)
        return sorted(keywords)

    def _read_from_disk(self) -> list[str]:
        if not self._path.is_file():
            logger.info("Keyword file %s not found; creating it from defaults", self._path)
            text = self._seed_text()
            atomic_write_text(self._path, text)
            return self._parse(text)
        try:
            return self._parse(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.error("Could not read %s (%s); keeping current keywords", self._path, exc)
            return list(self._keywords)

    def _rebuild_patterns(self) -> None:
        patterns: dict[str, re.Pattern[str]] = {}
        for keyword in self._keywords:
            try:
                patterns[keyword] = compile_keyword(keyword)
            except re.error as exc:  # pragma: no cover - re.escape makes this unlikely
                logger.error("Could not compile keyword %r (%s); skipping it", keyword, exc)
        self._patterns = patterns

    async def load(self) -> None:
        async with self._lock:
            self._keywords = await asyncio.to_thread(self._read_from_disk)
            self._rebuild_patterns()
        logger.info("Loaded %d keywords from %s", len(self._keywords), self._path)

    async def _write(self) -> None:
        payload = "\n".join(self._keywords) + ("\n" if self._keywords else "")
        await asyncio.to_thread(atomic_write_text, self._path, payload)

    async def add(self, raw: str) -> str:
        """Add a keyword and persist. Raises :class:`KeywordError` if present."""
        keyword = normalize_keyword(raw)
        async with self._lock:
            if keyword in self._patterns:
                raise KeywordError(f"'{keyword}' is already in the list.")
            self._keywords = sorted([*self._keywords, keyword])
            self._rebuild_patterns()
            await self._write()
        logger.info("Keyword added: %s (total %d)", keyword, len(self._keywords))
        return keyword

    async def remove(self, raw: str) -> str:
        """Remove a keyword and persist. Raises :class:`KeywordError` if absent."""
        keyword = normalize_keyword(raw)
        async with self._lock:
            if keyword not in self._patterns:
                raise KeywordError(f"'{keyword}' is not in the list.")
            self._keywords = [item for item in self._keywords if item != keyword]
            self._rebuild_patterns()
            await self._write()
        logger.info("Keyword removed: %s (total %d)", keyword, len(self._keywords))
        return keyword

    # -- matching ---------------------------------------------------------- #
    def find_hits(self, text: str, limit: int | None = None) -> list[str]:
        """Return the keywords found in ``text``, longest phrase first.

        A keyword whose every match sits inside an already-accepted longer
        match is dropped, so ``"my fuel card declined"`` reports ``fuel card``
        rather than ``fuel card, fuel, card``.
        """
        if not text or not self._patterns:
            return []

        accepted: list[str] = []
        covered: list[tuple[int, int]] = []

        for keyword in sorted(self._patterns, key=lambda item: (-len(item), item)):
            spans = [match.span() for match in self._patterns[keyword].finditer(text)]
            if not spans:
                continue
            if all(
                any(start >= low and end <= high for low, high in covered)
                for start, end in spans
            ):
                continue
            accepted.append(keyword)
            covered.extend(spans)
            if limit is not None and len(accepted) >= limit:
                break

        return accepted

    def matches(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._patterns.values())
