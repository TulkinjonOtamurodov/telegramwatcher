"""One-time interactive login for the personal Telegram account.

Run this once per machine, attached to a terminal:

    python -m app.auth_user
    docker compose run --rm makima python -m app.auth_user

It writes ``sessions/user_session.session``. That file is the only thing the
watcher needs afterwards -- it never prompts again, so the container can restart
unattended. Treat the session file exactly like a password.
"""

from __future__ import annotations

import asyncio
import sys
from getpass import getpass

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from app.clients import build_user_client
from app.config import ConfigError, load_config
from app.utils import display_name


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        print(
            "\nNo input available. Run this command with a terminal attached, e.g.\n"
            "    docker compose run --rm makima python -m app.auth_user",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value or (default or "")


async def authenticate() -> int:
    # The bot token is irrelevant here, so do not demand it.
    config = load_config(require_bot_token=False)
    config.ensure_directories()

    client = build_user_client(config)
    print("MAKIMA - Telegram user authentication")
    print(f"Session file: {config.user_session}.session")
    print()

    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            username = f" (@{me.username})" if getattr(me, "username", None) else ""
            print(f"Already authorised as {display_name(me)}{username}.")
            print("Nothing to do. Start the watcher with: docker compose up -d")
            return 0

        phone = config.phone or _prompt("Phone (international format, e.g. +15551234567)")
        if not phone:
            print("A phone number is required.", file=sys.stderr)
            return 2

        print(f"Requesting a login code for {phone} ...")
        sent = await client.send_code_request(phone)

        print("Telegram has sent a code to your Telegram app (not SMS, usually).")
        code = _prompt("Telegram login code")
        if not code:
            print("No code entered.", file=sys.stderr)
            return 2

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            print("Two-factor authentication is enabled on this account.")
            password = getpass("2FA password (hidden): ")
            if not password:
                print("A password is required.", file=sys.stderr)
                return 2
            await client.sign_in(password=password)

        me = await client.get_me()
        username = f" (@{me.username})" if getattr(me, "username", None) else ""
        print()
        print(f"Signed in as {display_name(me)}{username} (id {me.id}).")
        print(f"Session saved to {config.user_session}.session")
        print()
        print("Next steps:")
        print("  1. Open a private chat with your MAKIMA bot and press Start.")
        print("  2. Launch the watcher:  docker compose up -d")
        return 0

    except PhoneCodeInvalidError:
        print("That login code is not correct. Run the command again.", file=sys.stderr)
        return 1
    except PhoneCodeExpiredError:
        print("That login code expired. Run the command again.", file=sys.stderr)
        return 1
    except PhoneNumberInvalidError:
        print("Telegram does not recognise that phone number.", file=sys.stderr)
        return 1
    except PhoneNumberBannedError:
        print("This phone number is banned from Telegram.", file=sys.stderr)
        return 1
    except ApiIdInvalidError:
        print(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH were rejected. Re-check both at "
            "https://my.telegram.org.",
            file=sys.stderr,
        )
        return 1
    except FloodWaitError as exc:
        print(
            f"Telegram is rate-limiting logins. Wait {exc.seconds} seconds and try again.",
            file=sys.stderr,
        )
        return 1
    except RPCError as exc:
        print(f"Telegram refused the login: {exc}", file=sys.stderr)
        return 1
    finally:
        result = client.disconnect()
        if asyncio.iscoroutine(result):
            await result


def main() -> None:
    try:
        raise SystemExit(asyncio.run(authenticate()))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
