"""Lightweight health check -- no web server, no extra dependencies.

    python -m app.health

Exits 0 when the deployment looks sane and non-zero otherwise, which is what the
Docker ``HEALTHCHECK`` uses. It deliberately does not talk to Telegram, so it
stays fast and cannot trip a rate limit.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from app.config import Config, ConfigError, load_config


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""

    def passed(self, detail: str = "") -> "Check":
        self.ok, self.detail = True, detail
        return self

    def failed(self, detail: str) -> "Check":
        self.ok, self.detail = False, detail
        return self


def _check_env(config: Config) -> Check:
    check = Check("environment variables")
    missing = []
    if not config.api_id:
        missing.append("TELEGRAM_API_ID")
    if not config.api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not config.bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        return check.failed("missing: " + ", ".join(missing))
    admins = len(config.admin_user_ids)
    return check.passed(
        f"api id set, bot token set, {admins} admin id(s)"
        + ("" if admins else " (will default to the watching account)")
    )


def _check_settings(config: Config) -> Check:
    check = Check("settings file")
    path = config.settings_file
    if not path.is_file():
        return check.failed(f"{path} does not exist yet (it is created on first run)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return check.failed(f"{path} is unreadable: {exc}")
    if not isinstance(data, dict):
        return check.failed(f"{path} does not contain a JSON object")
    return check.passed(str(path))


def _check_keywords(config: Config) -> Check:
    check = Check("keyword file")
    path = config.keywords_file
    if not path.is_file():
        return check.failed(f"{path} does not exist yet (it is created on first run)")
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError as exc:
        return check.failed(f"{path} is unreadable: {exc}")
    return check.passed(f"{len(lines)} keyword(s)")


def _check_writable(path: Path, label: str) -> Check:
    check = Check(f"{label} writable")
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".healthcheck-", dir=str(path))
        os.close(fd)
        Path(name).unlink(missing_ok=True)
    except OSError as exc:
        return check.failed(f"{path}: {exc}")
    return check.passed(str(path))


def _check_user_session(config: Config) -> Check:
    check = Check("user session")
    path = Path(f"{config.user_session}.session")
    if not path.is_file():
        return check.failed(
            f"{path} is missing. Run: docker compose run --rm makima python -m app.auth_user"
        )
    size = path.stat().st_size
    if size == 0:
        return check.failed(f"{path} is empty; re-run app.auth_user")
    return check.passed(f"present ({size} bytes)")


def run_checks() -> tuple[list[Check], bool]:
    try:
        config = load_config()
    except ConfigError as exc:
        return [Check("configuration").failed(str(exc))], False

    checks = [
        _check_env(config),
        _check_settings(config),
        _check_keywords(config),
        _check_writable(config.sessions_dir, "sessions directory"),
        _check_writable(config.logs_dir, "logs directory"),
        _check_user_session(config),
    ]
    return checks, all(check.ok for check in checks)


def main() -> None:
    checks, healthy = run_checks()
    for check in checks:
        mark = "OK  " if check.ok else "FAIL"
        detail = f" - {check.detail}" if check.detail else ""
        print(f"[{mark}] {check.name}{detail}")
    print()
    print("HEALTHY" if healthy else "UNHEALTHY")
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
