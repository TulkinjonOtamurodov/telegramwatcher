"""Application entry point: wires everything together and runs until stopped.

    python -m app.main

Shutdown is graceful on SIGINT and SIGTERM, which matters because Docker sends
SIGTERM on every ``docker compose restart`` -- an abrupt kill can leave a locked
SQLite session file behind.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from app import __version__
from app.alerts import AlertDispatcher
from app.bot_commands import BotCommands
from app.clients import (
    ClientStartupError,
    ConnectionMonitor,
    build_bot_client,
    build_user_client,
    connect_user_client,
    disconnect_quietly,
    start_bot_client,
)
from app.config import Config, ConfigError, load_config
from app.keywords import KeywordStore
from app.logging_config import get_logger, register_secret, setup_logging
from app.settings import SettingsStore
from app.utils import display_name
from app.watched_users import WatchedUserStore

logger = get_logger("main")

STARTUP_MESSAGE = "\U0001f7e5 MAKIMA watcher is running.\n\nUse /help in private chat."


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Ask the loop to set ``stop_event`` on SIGINT / SIGTERM."""
    loop = asyncio.get_running_loop()

    def _request_stop(signal_name: str) -> None:
        if not stop_event.is_set():
            logger.info("Received %s; shutting down", signal_name)
            stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop, sig_name)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows has no loop.add_signal_handler; fall back to the classic API.
            try:
                signal.signal(sig, lambda *_args, _n=sig_name: _request_stop(_n))
            except (OSError, ValueError):  # pragma: no cover - non-main thread
                logger.debug("Could not install a handler for %s", sig_name)


def _resolve_admins(config: Config, me: Any) -> list[int]:
    """Admins come from the environment; otherwise it is just my own account."""
    if config.admin_user_ids:
        return sorted(config.admin_user_ids)
    my_id = getattr(me, "id", None)
    if my_id is None:  # pragma: no cover - get_me always has an id
        return []
    logger.info(
        "ADMIN_USER_IDS is empty; defaulting to the watching account (id %s)", my_id
    )
    return [my_id]


def _log_banner(
    me: Any,
    bot_me: Any,
    keywords: KeywordStore,
    settings: SettingsStore,
    watched: WatchedUserStore,
) -> None:
    username = getattr(me, "username", None)
    user_label = display_name(me)
    if username:
        user_label = f"{user_label} (@{username})"

    logger.info("=" * 58)
    logger.info("MAKIMA TELEGRAM WATCHER ONLINE (v%s)", __version__)
    logger.info("User: %s", user_label)
    logger.info("Bot: @%s", getattr(bot_me, "username", "unknown"))
    logger.info("Keywords loaded: %d", keywords.count())
    logger.info("Watched members: %d", watched.count())
    logger.info("Modes: %s", settings.modes_summary())
    logger.info("=" * 58)


async def run() -> int:
    config = load_config()
    register_secret(*config.secrets)
    setup_logging(
        config.log_file,
        level=config.log_level,
        telethon_level=config.telethon_log_level,
    )
    config.ensure_directories()

    logger.info("MAKIMA watcher starting (v%s)", __version__)

    settings = SettingsStore(
        config.settings_file, defaults_path=config.defaults_dir / "watcher_settings.json"
    )
    keywords = KeywordStore(
        config.keywords_file, defaults_path=config.defaults_dir / "keywords.txt"
    )
    watched = WatchedUserStore(
        config.watched_users_file,
        defaults_path=config.defaults_dir / "watched_users.json",
    )
    await settings.load()
    await keywords.load()
    await watched.load()

    user_client = build_user_client(config)
    bot_client = build_bot_client(config)

    dispatcher: AlertDispatcher | None = None
    monitor: ConnectionMonitor | None = None
    watcher: Any = None
    stop_event = asyncio.Event()

    try:
        me = await connect_user_client(user_client, config)
        bot_me = await start_bot_client(bot_client, config)

        admins = _resolve_admins(config, me)
        if not admins:
            logger.error(
                "No alert recipients could be determined. Set ADMIN_USER_IDS in .env."
            )

        dispatcher = AlertDispatcher(bot_client)
        dispatcher.set_recipients(admins)
        await dispatcher.start()

        # Imported here so that a syntax error in the watcher cannot stop the
        # config/logging diagnostics above from running.
        from app.watcher import Watcher

        watcher = Watcher(
            user_client,
            settings=settings,
            keywords=keywords,
            dispatcher=dispatcher,
            watched=watched,
        )
        watcher.bind_identity(me)
        await watcher.start()

        user_display = display_name(me)
        if getattr(me, "username", None):
            user_display = f"{user_display} (@{me.username})"

        commands = BotCommands(
            bot_client,
            settings=settings,
            keywords=keywords,
            admin_ids=lambda: admins,
            watcher=watcher,
            dispatcher=dispatcher,
            watched=watched,
            identity={
                "user_display": user_display,
                "bot_username": getattr(bot_me, "username", None),
            },
        )
        commands.register()

        monitor = ConnectionMonitor({"user": user_client, "bot": bot_client})
        await monitor.start()

        _install_signal_handlers(stop_event)
        _log_banner(me, bot_me, keywords, settings, watched)

        if not await dispatcher.send_now(STARTUP_MESSAGE):
            logger.warning(
                "Startup notice could not be delivered. Open a private chat with "
                "@%s and press Start, then run /status.",
                getattr(bot_me, "username", "your_bot"),
            )

        # Run until a signal arrives or the user client drops for good.
        # The disconnect future is shielded: cancelling our waiter must not
        # cancel Telethon's own internal future.
        disconnected = asyncio.ensure_future(asyncio.shield(user_client.disconnected))
        stopped = asyncio.ensure_future(stop_event.wait())
        done, pending = await asyncio.wait(
            {disconnected, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if disconnected in done and not stop_event.is_set():
            logger.error("User client disconnected permanently; shutting down")

    except ClientStartupError as exc:
        logger.error("Startup failed: %s", exc)
        return 1
    except asyncio.CancelledError:
        logger.info("Cancelled; shutting down")
    except Exception:
        logger.exception("Fatal error in the main loop")
        return 1
    finally:
        logger.info("Stopping MAKIMA...")
        if monitor is not None:
            await monitor.stop()
        if watcher is not None:
            await watcher.stop()
        if dispatcher is not None:
            await dispatcher.stop()
        await disconnect_quietly(user_client, "user")
        await disconnect_quietly(bot_client, "bot")
        logger.info("MAKIMA watcher stopped")

    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(run())
    except ConfigError as exc:
        # Logging may not be configured yet, so speak plainly on stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
