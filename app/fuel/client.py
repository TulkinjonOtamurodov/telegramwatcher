"""HTTP client for the FuelHelper API.

Uses ``PATCH /api/units/{unit}`` -- the endpoint the deployed API already has --
rather than inventing new ones. MAKIMA only ever needs to write one field:

    {"fuel_status": "Need to arrange"}

Built on ``urllib`` in a worker thread rather than an async HTTP library: this
makes a handful of small requests a day, and the project stays dependency-light.

**Every call fails safely.** A FuelHelper outage must never stop MAKIMA
alerting -- MAKIMA's own state file is authoritative for scheduling, and
FuelHelper is the operational mirror. Nothing here raises; failures are logged
and returned as ``False``.

The bearer token is read from the environment, never logged and never echoed.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.fuel.state import BOARD_VALUE
from app.logging_config import get_logger

logger = get_logger("fuel.client")

DEFAULT_TIMEOUT_SECONDS = 10

UNITS_PATH = "/api/units"
HEALTH_PATH = "/api/health"


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    status: int = 0
    body: Any = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


class FuelHelperClient:
    """Talks to FuelHelper. Never raises, never logs the token."""

    def __init__(
        self,
        base_url: str | None,
        token: str | None,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = (token or "").strip()
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._base)

    @property
    def base_url(self) -> str:
        return self._base

    # -- transport ---------------------------------------------------------- #
    def _request(self, method: str, path: str, payload: dict | None = None) -> ApiResult:
        """Blocking request. Always called through ``asyncio.to_thread``."""
        url = f"{self._base}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        request = urllib.request.Request(url=url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw
                return ApiResult(True, response.status, body)

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # pragma: no cover - body already consumed
                pass
            return ApiResult(False, exc.code, None, detail or exc.reason or "")

        except urllib.error.URLError as exc:
            return ApiResult(False, 0, None, str(exc.reason))

        except (TimeoutError, OSError) as exc:
            return ApiResult(False, 0, None, str(exc))

    async def _call(self, method: str, path: str, payload: dict | None = None) -> ApiResult:
        if not self.configured:
            return ApiResult(False, 0, None, "FUELHELPER_API_URL is not set")
        return await asyncio.to_thread(self._request, method, path, payload)

    # -- operations ---------------------------------------------------------- #
    async def health(self) -> bool:
        result = await self._call("GET", HEALTH_PATH)
        if not result:
            logger.warning(
                "FuelHelper health check failed | %s", result.error or result.status
            )
        return bool(result)

    async def get_unit(self, unit: str) -> dict | None:
        """Fetch a unit, primarily to read its driver name off the board."""
        result = await self._call("GET", f"{UNITS_PATH}/{unit}")
        if result and isinstance(result.body, dict):
            return result.body
        if not result and result.status != 404:
            logger.info(
                "FuelHelper unit lookup failed | unit=%s | %s",
                unit,
                result.error or result.status,
            )
        return None

    async def push_status(
        self, unit: str, status: str, *, driver: str | None = None
    ) -> bool:
        """Write a unit's fuel status to the board.

        The unit is created if the board has never seen it, so a newly mapped
        truck does not silently fail to sync.
        """
        if not self.configured:
            return False

        board = BOARD_VALUE.get(status, status)
        result = await self._call("PATCH", f"{UNITS_PATH}/{unit}", {"fuel_status": board})

        if result.status == 404:
            created = await self._call(
                "POST",
                UNITS_PATH,
                {"unit_number": unit, "driver": driver or None, "fuel_status": board},
            )
            if not created and created.status != 409:
                logger.warning(
                    "FuelHelper could not create unit %s | %s",
                    unit,
                    created.error or created.status,
                )
                return False
            result = await self._call(
                "PATCH", f"{UNITS_PATH}/{unit}", {"fuel_status": board}
            )

        if result:
            logger.info("FuelHelper updated | unit=%s | status=%s", unit, board)
            return True

        logger.warning(
            "FuelHelper status push failed | unit=%s | %s",
            unit,
            result.error or result.status,
        )
        return False
