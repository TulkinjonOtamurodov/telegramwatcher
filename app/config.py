"""Environment and path configuration.

Secrets are read from the environment (or a local ``.env`` file) and are never
written to logs. Everything else in the application takes its paths from the
:class:`Config` object built here, so nothing hard-codes a location.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: Repository root (the directory that contains ``app/``, ``data/``, ...).
BASE_DIR = Path(__file__).resolve().parent.parent

# The .env file is optional -- on a server you may prefer real exported
# environment variables, or Docker's env_file. Existing variables win.
load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _env(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().strip('"').strip("'")
    return raw or default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    return Path(raw).expanduser() if raw else default


def _parse_admin_ids(raw: str | None) -> frozenset[int]:
    """Parse ``ADMIN_USER_IDS`` -- comma, space or semicolon separated."""
    ids: set[int] = set()
    for chunk in re.split(r"[,\s;]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not re.fullmatch(r"-?\d+", chunk):
            raise ConfigError(
                f"ADMIN_USER_IDS contains a non-numeric entry: {chunk!r}. "
                "Use numeric Telegram user IDs, e.g. ADMIN_USER_IDS=12345,67890"
            )
        ids.add(int(chunk))
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    """Fully resolved runtime configuration."""

    api_id: int
    api_hash: str
    bot_token: str
    phone: str | None
    admin_user_ids: frozenset[int]

    base_dir: Path
    data_dir: Path
    sessions_dir: Path
    logs_dir: Path

    log_level: str = "INFO"
    telethon_log_level: str = "WARNING"

    ai_provider: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None

    connection_retries: int = -1  # -1 == retry forever
    retry_delay: int = 5
    request_retries: int = 5

    @property
    def keywords_file(self) -> Path:
        return self.data_dir / "keywords.txt"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "watcher_settings.json"

    @property
    def watched_users_file(self) -> Path:
        return self.data_dir / "watched_users.json"

    @property
    def group_rules_file(self) -> Path:
        return self.data_dir / "group_rules.json"

    @property
    def defaults_dir(self) -> Path:
        return self.data_dir / "defaults"

    @property
    def user_session(self) -> Path:
        return self.sessions_dir / "user_session"

    @property
    def bot_session(self) -> Path:
        return self.sessions_dir / "bot_session"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "makima.log"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must be redacted before anything reaches a log file."""
        values = [self.api_hash, self.bot_token]
        if self.phone:
            values.append(self.phone)
        if self.ai_api_key:
            values.append(self.ai_api_key)
        return tuple(v for v in values if v)

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.sessions_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)


def load_config(*, require_bot_token: bool = True) -> Config:
    """Build a :class:`Config` from the environment.

    ``require_bot_token`` is relaxed by ``app.auth_user``, which only needs the
    user-account credentials to create a Telethon session.
    """
    raw_api_id = _env("TELEGRAM_API_ID")
    if not raw_api_id:
        raise ConfigError(
            "TELEGRAM_API_ID is not set. Copy .env.example to .env and fill it in "
            "(get the value from https://my.telegram.org -> API development tools)."
        )
    if not re.fullmatch(r"\d+", raw_api_id):
        raise ConfigError(f"TELEGRAM_API_ID must be a number, got {raw_api_id!r}.")

    api_hash = _env("TELEGRAM_API_HASH")
    if not api_hash:
        raise ConfigError(
            "TELEGRAM_API_HASH is not set. Get it from https://my.telegram.org."
        )
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        # Not fatal -- Telegram has never changed the format, but a typo here
        # produces a confusing 'invalid api hash' error much later.
        raise ConfigError(
            "TELEGRAM_API_HASH does not look like a 32-character hex string. "
            "Check for stray quotes or spaces in .env."
        )

    bot_token = _env("TELEGRAM_BOT_TOKEN") or ""
    if require_bot_token:
        if not bot_token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and "
                "paste the token into .env."
            )
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", bot_token):
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is malformed. It looks like "
                "'123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'."
            )

    phone = _env("TELEGRAM_PHONE")
    if phone and not re.fullmatch(r"\+?\d[\d\s()-]{5,}", phone):
        raise ConfigError(
            "TELEGRAM_PHONE should be an international number such as +15551234567."
        )

    data_dir = _env_path("MAKIMA_DATA_DIR", BASE_DIR / "data")
    sessions_dir = _env_path("MAKIMA_SESSIONS_DIR", BASE_DIR / "sessions")
    logs_dir = _env_path("MAKIMA_LOGS_DIR", BASE_DIR / "logs")

    return Config(
        api_id=int(raw_api_id),
        api_hash=api_hash,
        bot_token=bot_token,
        phone=phone,
        admin_user_ids=_parse_admin_ids(_env("ADMIN_USER_IDS")),
        base_dir=BASE_DIR,
        data_dir=data_dir,
        sessions_dir=sessions_dir,
        logs_dir=logs_dir,
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        telethon_log_level=(_env("TELETHON_LOG_LEVEL", "WARNING") or "WARNING").upper(),
        ai_provider=_env("AI_PROVIDER"),
        ai_api_key=_env("AI_API_KEY"),
        ai_model=_env("AI_MODEL"),
    )
