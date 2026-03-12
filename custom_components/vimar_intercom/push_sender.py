"""APNs VoIP Push Sender — sends push notifications to wake iOS app via PushKit.

Uses JWT-based authentication (.p8 key from Apple Developer Portal).
Pushes are sent to the voip topic (bundle_id.voip) which triggers PushKit
on the device, which then calls reportNewIncomingCall() for CallKit.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

try:
    import jwt  # PyJWT
    import aiohttp
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

_LOGGER = logging.getLogger(__name__)

# APNs endpoints
APNS_PRODUCTION = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.sandbox.push.apple.com"

# Tokens file — persisted across restarts
_TOKENS_FILE = Path(__file__).parent / "push_tokens.json"


class APNsPushSender:
    """Sends VoIP push notifications via APNs HTTP/2."""

    def __init__(
        self,
        key_path: str,
        key_id: str,
        team_id: str,
        bundle_id: str = "noiseheroes.Home",
        sandbox: bool = True,
    ):
        self._key_path = key_path
        self._key_id = key_id
        self._team_id = team_id
        self._bundle_id = bundle_id
        self._base_url = APNS_SANDBOX if sandbox else APNS_PRODUCTION
        self._jwt_token: str | None = None
        self._jwt_issued_at: float = 0
        self._tokens: dict[str, str] = {}  # token → device_name
        self._load_tokens()

    def _load_tokens(self):
        """Load registered push tokens from disk."""
        try:
            if _TOKENS_FILE.exists():
                self._tokens = json.loads(_TOKENS_FILE.read_text())
                _LOGGER.info("Loaded %d push tokens", len(self._tokens))
        except Exception as e:
            _LOGGER.error("Failed to load push tokens: %s", e)

    def _save_tokens(self):
        """Persist push tokens to disk."""
        try:
            _TOKENS_FILE.write_text(json.dumps(self._tokens, indent=2))
        except Exception as e:
            _LOGGER.error("Failed to save push tokens: %s", e)

    def register_token(self, token: str, device_name: str = "unknown"):
        """Register a device push token."""
        self._tokens[token] = device_name
        self._save_tokens()
        _LOGGER.info("Registered push token for %s (%s...)", device_name, token[:16])

    def unregister_token(self, token: str):
        """Remove a device push token."""
        if token in self._tokens:
            name = self._tokens.pop(token)
            self._save_tokens()
            _LOGGER.info("Unregistered push token for %s", name)

    @property
    def registered_devices(self) -> dict[str, str]:
        return dict(self._tokens)

    def _get_jwt(self) -> str:
        """Get or refresh APNs JWT token (valid for 1 hour)."""
        now = time.time()
        if self._jwt_token and (now - self._jwt_issued_at) < 3000:  # refresh at 50min
            return self._jwt_token

        with open(self._key_path, "r") as f:
            key = f.read()

        self._jwt_issued_at = now
        self._jwt_token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        return self._jwt_token

    async def send_voip_push(
        self,
        caller: str = "55001",
        panel: str = "esterna",
    ) -> int:
        """Send VoIP push to all registered devices. Returns number of successful sends."""
        if not self._tokens:
            _LOGGER.warning("No push tokens registered — cannot send VoIP push")
            return 0

        payload = json.dumps({
            "caller": caller,
            "panel": panel,
        })

        topic = f"{self._bundle_id}.voip"
        token = self._get_jwt()
        headers = {
            "authorization": f"bearer {token}",
            "apns-topic": topic,
            "apns-push-type": "voip",
            "apns-priority": "10",
            "apns-expiration": "0",
        }

        sent = 0
        dead_tokens = []

        async with aiohttp.ClientSession() as session:
            for device_token, device_name in self._tokens.items():
                url = f"{self._base_url}/3/device/{device_token}"
                try:
                    async with session.post(
                        url, data=payload, headers=headers
                    ) as resp:
                        if resp.status == 200:
                            sent += 1
                            _LOGGER.info(
                                "VoIP push sent to %s (%s)", device_name, device_token[:16]
                            )
                        elif resp.status == 410:
                            # Token no longer valid
                            dead_tokens.append(device_token)
                            _LOGGER.warning(
                                "Push token expired for %s — removing", device_name
                            )
                        else:
                            body = await resp.text()
                            _LOGGER.error(
                                "APNs push failed for %s: %d %s",
                                device_name, resp.status, body,
                            )
                except Exception as e:
                    _LOGGER.error("Push send error for %s: %s", device_name, e)

        for t in dead_tokens:
            self.unregister_token(t)

        _LOGGER.info("VoIP push: %d/%d successful", sent, len(self._tokens))
        return sent


# Module-level singleton — initialized by hub
_sender: APNsPushSender | None = None


def init(key_path: str, key_id: str, team_id: str, bundle_id: str = "noiseheroes.Home", sandbox: bool = True):
    """Initialize the push sender singleton."""
    global _sender
    if not _HAS_DEPS:
        _LOGGER.error("PyJWT or aiohttp not installed — push disabled. Run: pip install PyJWT aiohttp")
        return
    _sender = APNsPushSender(key_path, key_id, team_id, bundle_id, sandbox)


def get_sender() -> APNsPushSender | None:
    return _sender
