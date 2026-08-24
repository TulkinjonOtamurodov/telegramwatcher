"""Logging setup: a rotating file log plus stdout for container log drivers.

A redaction filter is installed so that even an accidental ``logger.info(token)``
somewhere in the codebase cannot leak a secret into ``logs/makima.log``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_SECRETS: set[str] = set()
_CONFIGURED = False

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def register_secret(*values: str | None) -> None:
    """Mark values that must never appear in log output."""
    for value in values:
        if value and len(value) >= 6:
            _SECRETS.add(value)


class _RedactSecretsFilter(logging.Filter):
    """Replace any registered secret with a placeholder."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SECRETS:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        redacted = message
        for secret in _SECRETS:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    log_file: Path,
    *,
    level: str = "INFO",
    telethon_level: str = "WARNING",
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure root logging once. Calling it again is a no-op."""
    global _CONFIGURED

    if _CONFIGURED:
        return logging.getLogger("makima")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    redactor = _RedactSecretsFilter()
    resolved_level = getattr(logging, level.upper(), logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(resolved_level)
    stream.setFormatter(formatter)
    stream.addFilter(redactor)
    root.addHandler(stream)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    except OSError as exc:
        # An unwritable logs/ volume must not stop the watcher from running.
        root.warning(
            "Could not open log file %s (%s); logging to stdout only", log_file, exc
        )

    # Telethon is very chatty below WARNING; keep its network chatter out of the way.
    logging.getLogger("telethon").setLevel(
        getattr(logging, telethon_level.upper(), logging.WARNING)
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _CONFIGURED = True
    return logging.getLogger("makima")


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger such as ``makima.watcher``."""
    return logging.getLogger(f"makima.{name}")
