"""Dreame Cloud API client."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .const import (
    ALL_PROPS,
    API_BASE_URL,
    BASIC_AUTH,
    TENANT_ID,
    TOKEN_REFRESH_MARGIN,
)

_LOGGER = logging.getLogger(__name__)


class DreameApiError(Exception):
    """Base exception for Dreame API errors."""


class DreameAuthError(DreameApiError):
    """Authentication error."""


class DreameCloudAPI:
    """Client for Dreame Cloud REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        token_expiry: float,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expiry = token_expiry
        self._lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def token_expiry(self) -> float:
        return self._token_expiry

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {BASIC_AUTH}",
            "Dreame-Auth": f"Bearer {self._access_token}",
            "Tenant-Id": TENANT_ID,
            "Content-Type": "application/json",
        }

    async def _ensure_token(self) -> None:
        """Refresh the access token if it's about to expire."""
        if time.time() < self._token_expiry - TOKEN_REFRESH_MARGIN:
            return
        async with self._lock:
            # Double-check after acquiring lock
            if time.time() < self._token_expiry - TOKEN_REFRESH_MARGIN:
                return
            await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        """Refresh the OAuth access token."""
        _LOGGER.debug("Refreshing Dreame access token")
        url = f"{API_BASE_URL}/dreame-auth/oauth/token"
        headers = {
            "Authorization": f"Basic {BASIC_AUTH}",
            "Tenant-Id": TENANT_ID,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = f"grant_type=refresh_token&refresh_token={self._refresh_token}"

        async with self._session.post(url, headers=headers, data=data) as resp:
            if resp.status != 200:
                raise DreameAuthError(f"Token refresh failed: HTTP {resp.status}")
            result = await resp.json()

        if "access_token" not in result:
            raise DreameAuthError(f"Token refresh failed: {result}")

        self._access_token = result["access_token"]
        self._refresh_token = result["refresh_token"]
        self._token_expiry = time.time() + result.get("expires_in", 7200)
        _LOGGER.debug("Token refreshed, expires in %ds", result.get("expires_in", 7200))

    async def _request(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Make an authenticated API request."""
        await self._ensure_token()
        url = f"{API_BASE_URL}/{endpoint}"
        async with self._session.post(
            url, headers=self._headers(), json=payload
        ) as resp:
            if resp.status != 200:
                raise DreameApiError(f"API error: HTTP {resp.status}")
            result = await resp.json()

        if not result.get("success", False) and result.get("code") != 0:
            # Check for auth errors
            code = result.get("code", -1)
            if code in (401, 10001, 10002):
                raise DreameAuthError(f"Auth error: {result}")
            raise DreameApiError(f"API error: {result}")

        return result

    async def get_devices(self) -> list[dict[str, Any]]:
        """Get list of user's devices."""
        result = await self._request(
            "dreame-user-iot/iotuserbind/device/listV2", {}
        )
        records = result.get("data", {}).get("page", {}).get("records", [])
        return records

    async def get_props(
        self, did: str, keys: list[str] | None = None
    ) -> dict[str, Any]:
        """Get cached device properties."""
        if keys is None:
            keys = ALL_PROPS
        key_str = ",".join(keys)
        result = await self._request(
            "dreame-user-iot/iotstatus/props",
            {"did": did, "keys": key_str},
        )
        props: dict[str, Any] = {}
        for item in result.get("data", []):
            key = item.get("key")
            value = item.get("value")
            if key and value is not None:
                props[key] = value
        return props

    async def send_command(
        self,
        did: str,
        method: str,
        params: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send a command to the device via sendCommand endpoint."""
        import random

        cmd_id = random.randint(100, 500100)
        payload = {
            "id": cmd_id,
            "did": did,
            "data": {
                "id": cmd_id,
                "did": did,
                "from": "ha",
                "method": method,
                "params": params,
            },
        }
        return await self._request(
            "dreame-iot-com-10000/device/sendCommand", payload
        )

    async def set_property(
        self, did: str, siid: int, piid: int, value: Any
    ) -> dict[str, Any]:
        """Set a device property."""
        return await self.send_command(
            did,
            "set_properties",
            [{"did": did, "siid": siid, "piid": piid, "value": value}],
        )

    async def call_action(
        self,
        did: str,
        siid: int,
        aiid: int,
        params: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Call a device action."""
        import random

        cmd_id = random.randint(100, 500100)
        action_params: dict[str, Any] = {
            "did": did,
            "siid": siid,
            "aiid": aiid,
        }
        if params:
            action_params["in"] = params

        payload = {
            "id": cmd_id,
            "did": did,
            "data": {
                "from": "ha",
                "id": cmd_id,
                "method": "action",
                "params": action_params,
            },
        }
        return await self._request(
            "dreame-iot-com-10000/device/sendCommand", payload
        )

    async def start_clean(self, did: str) -> dict[str, Any]:
        """Start cleaning (mopping)."""
        return await self.set_property(did, 2, 1, 1)

    async def start_vacuum(self, did: str) -> dict[str, Any]:
        """Start vacuuming."""
        return await self.set_property(did, 2, 1, 8)

    async def pause(self, did: str) -> dict[str, Any]:
        """Pause current operation."""
        return await self.set_property(did, 2, 1, 10)

    async def stop(self, did: str) -> dict[str, Any]:
        """Stop and return to standby."""
        return await self.set_property(did, 2, 1, 3)

    async def start_self_clean(self, did: str) -> dict[str, Any]:
        """Start self-cleaning."""
        return await self.set_property(did, 2, 1, 5)

    async def start_drying(self, did: str) -> dict[str, Any]:
        """Start drying."""
        return await self.set_property(did, 2, 1, 6)
