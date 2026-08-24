"""Telethon client construction and lifecycle.

Two clients are used, deliberately:

* the **user client** logs in as your personal account and is the only thing
  that reads group messages;
* the **bot client** never joins a group -- it only delivers alerts to you and
  accepts control commands in a private chat.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    ApiIdInvalidError,
    AuthKeyDuplicatedError,
    FloodWaitError,
    RPCError,
)

from app import __version__
from app.config import Config
from app.logging_config import get_logger

logger = get_logger("clients")

#: How often the connection monitor checks that a client is still connected.
MONITOR_INTERVAL_SECONDS = 30


class ClientStartupError(RuntimeError):
    """Raised with a human-readable explanation when a client cannot start."""


class NotAuthorizedError(ClientStartupError):
    """The user session exists but is not logged in."""


def _build(config: Config, session: str, *, label: str) -> TelegramClient:
    return TelegramClient(
        session,
        config.api_id,
        config.api_hash,
        # -1 means "keep retrying forever"; combined with auto_reconnect this
        # survives VPS network blips and Telegram-side restarts.
        connection_retries=config.connection_retries,
        retry_delay=config.retry_delay,
        request_retries=config.request_retries,
        auto_reconnect=True,
        # Telethon sleeps through short flood waits itself; longer ones are
        # raised so our own handlers can decide what to do.
        flood_sleep_threshold=60,
        device_model=f"MAKIMA {label}",
        system_version="Linux",
        app_version=__version__,
    )


def build_user_client(config: Config) -> TelegramClient:
    return _build(config, str(config.user_session), label="watcher")


def build_bot_client(config: Config) -> TelegramClient:
    return _build(config, str(config.bot_session), label="bot")


def _translate_sqlite_error(exc: sqlite3.Error, session_path: str) -> ClientStartupError:
    text = str(exc).lower()
    if "locked" in text:
        return ClientStartupError(
            f"The session database {session_path}.session is locked. Another copy of "
            "MAKIMA is almost certainly already running with the same session. Stop it "
            "first ('docker compose down', or 'pkill -f app.main') and try again."
        )
    if "readonly" in text or "unable to open" in text:
        return ClientStartupError(
            f"Cannot write to {session_path}.session. Check the ownership and "
            "permissions of the sessions/ directory."
        )
    return ClientStartupError(f"Session database error for {session_path}: {exc}")


async def connect_user_client(client: TelegramClient, config: Config) -> Any:
    """Connect the personal account and return the logged-in user.

    The session must already exist -- create it once with
    ``python -m app.auth_user``. This never prompts, so it is safe to run
    unattended inside a container.
    """
    try:
        await client.connect()
    except sqlite3.Error as exc:
        raise _translate_sqlite_error(exc, str(config.user_session)) from exc
    except ApiIdInvalidError as exc:
        raise ClientStartupError(
            "Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH. Re-check both "
            "values at https://my.telegram.org."
        ) from exc
    except AuthKeyDuplicatedError as exc:
        raise ClientStartupError(
            "This session key was used from another IP at the same time. Delete "
            "sessions/user_session.session and run 'python -m app.auth_user' again."
        ) from exc
    except OSError as exc:
        raise ClientStartupError(f"Could not reach Telegram: {exc}") from exc

    if not await client.is_user_authorized():
        raise NotAuthorizedError(
            "The user session is not authorised. Run this once, interactively:\n"
            "    docker compose run --rm makima python -m app.auth_user\n"
            "(or 'python -m app.auth_user' outside Docker)."
        )

    me = await client.get_me()
    logger.info("User account authenticated")
    return me


async def start_bot_client(client: TelegramClient, config: Config) -> Any:
    """Sign the bot in with its token and return the bot user."""
    try:
        await client.start(bot_token=config.bot_token)
    except sqlite3.Error as exc:
        raise _translate_sqlite_error(exc, str(config.bot_session)) from exc
    except (AccessTokenInvalidError, AccessTokenExpiredError) as exc:
        raise ClientStartupError(
            "TELEGRAM_BOT_TOKEN was rejected by Telegram. Get a fresh token from "
            "@BotFather (/mybots -> API Token) and update .env."
        ) from exc
    except ApiIdInvalidError as exc:
        raise ClientStartupError(
            "Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH while starting the bot."
        ) from exc
    except FloodWaitError as exc:
        raise ClientStartupError(
            f"Telegram asked us to wait {exc.seconds}s before signing the bot in again."
        ) from exc
    except RPCError as exc:
        raise ClientStartupError(f"Telegram refused the bot login: {exc}") from exc
    except OSError as exc:
        raise ClientStartupError(f"Could not reach Telegram: {exc}") from exc

    bot_me = await client.get_me()
    logger.info("Bot authenticated")
    return bot_me


async def disconnect_quietly(client: TelegramClient | None, label: str) -> None:
    """Disconnect without letting shutdown errors mask the real reason we stopped."""
    if client is None:
        return
    try:
        if client.is_connected():
            result = client.disconnect()
            if asyncio.iscoroutine(result):
                await result
        logger.info("%s client disconnected", label)
    except Exception:
        logger.warning("Error while disconnecting the %s client", label, exc_info=True)


class ConnectionMonitor:
    """Logs connection state changes and nudges a client that stayed down.

    Telethon reconnects on its own; this exists so that a silent outage shows up
    in ``logs/makima.log`` instead of looking like "MAKIMA stopped working".
    """

    def __init__(self, clients: dict[str, TelegramClient], interval: int = MONITOR_INTERVAL_SECONDS) -> None:
        self._clients = clients
        self._interval = interval
        self._connected: dict[str, bool] = {name: True for name in clients}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="connection-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                for name, client in self._clients.items():
                    is_up = bool(client.is_connected())
                    was_up = self._connected.get(name, True)
                    if was_up and not is_up:
                        logger.warning("Telegram disconnected (%s client)", name)
                    elif is_up and not was_up:
                        logger.info("Telegram reconnected (%s client)", name)
                    elif not is_up:
                        logger.info("Reconnect attempted (%s client)", name)
                        try:
                            await client.connect()
                        except Exception as exc:
                            logger.warning("Reconnect failed for %s client: %s", name, exc)
                    self._connected[name] = bool(client.is_connected())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Connection monitor error")
