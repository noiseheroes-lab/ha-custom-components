"""Download the plant's phonebook from the Vimar cloud.

The URL, the credentials and the authentication scheme are the SDK's
(`VMClient.pathRubricaDownloadCloud`,
`VMClientRepository.startCloudDownloadRubrica`):

    GET https://<cloud proxy>/phonebook/domains/<domain>/<version>

where `<domain>` is the SIP domain with `.<cloud proxy>` taken off and
`<version>` the `rubrica_ver` of the indoor unit's status reply. It is
protected by HTTP Digest authentication with the SIP domain as the user
name and the status reply's `token` as the password. The body is the
SQLite database `plant_config.parse_phonebook` reads.

The server's certificate is a public one (DigiCert-issued for
`*.vimar.cloud`), so the request goes through Home Assistant's shared
aiohttp session with normal verification; Vimar's private CA, which the
SIP connection needs, plays no part here.

Digest authentication is implemented here rather than with aiohttp's
`DigestAuthMiddleware`, although the aiohttp Home Assistant ships has
it. The whole exchange is two requests, RFC 7616 MD5 or SHA-256 with
`qop=auth`; written out, it is unit tested — against RFC 2617's worked
example — without aiohttp or the network, and it makes the security
decisions explicit: a Basic challenge is refused rather than answered
with the token, redirects are not followed with credentials attached,
and the body is read under a size ceiling. The SIP client computes its
own digest responses for the same reasons. The session is duck-typed:
anything with aiohttp's `get(...)` context-manager interface works.

The token is a password. It never reaches a log line or an exception
message, and it is never stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from .plant_config import MAX_PHONEBOOK_BYTES

DOWNLOAD_TIMEOUT = 30  # seconds for the whole two-request exchange
_CHUNK = 64 * 1024

# The version is the MD5 of the file in practice. It is interpolated
# into a URL path, so anything but plain word characters is refused
# rather than escaped.
_VERSION_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")

_HASHES = {
    "MD5": hashlib.md5,
    "MD5-SESS": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-256-SESS": hashlib.sha256,
}

_PARAM_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,\s]*)')


class PhonebookError(Exception):
    """The phonebook could not be downloaded. The message is safe to log."""


def phonebook_url(cloud_proxy: str, sip_domain: str, version: str) -> str:
    """The download URL of phonebook `version` for this plant."""
    if not _VERSION_RE.match(version or ""):
        raise ValueError("the phonebook version is not a plain token")
    domain = sip_domain.removesuffix(f".{cloud_proxy}")
    return f"https://{cloud_proxy}/phonebook/domains/{domain}/{version}"


def parse_challenge(header: str) -> dict[str, str]:
    """The `key=value` pairs of a Digest challenge or Authorization header."""
    text = header.strip()
    if text[:7].lower() == "digest ":
        text = text[7:]
    fields: dict[str, str] = {}
    for key, value in _PARAM_RE.findall(text):
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        fields[key.lower()] = value
    return fields


def digest_authorization(
    method: str,
    uri: str,
    username: str,
    password: str,
    challenge: dict[str, str],
    *,
    cnonce: str | None = None,
    nc: int = 1,
) -> str:
    """The Authorization header answering `challenge` (RFC 7616).

    Only `qop=auth` is implemented — `auth-int` would hash the request
    body, and a GET has none worth protecting — and the RFC 2069 form
    when the server offers no qop at all.
    """
    algorithm = challenge.get("algorithm", "MD5")
    hash_fn = _HASHES.get(algorithm.upper())
    if hash_fn is None:
        raise PhonebookError(
            f"the server asked for an unsupported digest algorithm ({algorithm})")

    def h(text: str) -> str:
        return hash_fn(text.encode()).hexdigest()

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    offered = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    if offered and "auth" not in offered:
        raise PhonebookError("the server offered no supported digest qop")
    qop = "auth" if offered else None
    cnonce = cnonce or secrets.token_hex(8)
    nc_text = f"{nc:08x}"

    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    if qop:
        response = h(f"{ha1}:{nonce}:{nc_text}:{cnonce}:{qop}:{ha2}")
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if qop:
        parts += [f"qop={qop}", f"nc={nc_text}", f'cnonce="{cnonce}"']
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


async def _read_body(response: Any, max_bytes: int) -> bytes:
    """The response body, refusing to hold more than `max_bytes`."""
    data = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        data += chunk
        if len(data) > max_bytes:
            raise PhonebookError("the phonebook download is implausibly large")
    return bytes(data)


async def download_phonebook(
    session: Any,
    url: str,
    username: str,
    password: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_PHONEBOOK_BYTES,
) -> bytes:
    """GET the phonebook, answering the server's digest challenge.

    Raises PhonebookError, whose message names what went wrong — an
    HTTP status, a timeout — and never the URL's credentials or the
    token. Transport exceptions (aiohttp's, OSError) are left to the
    caller, which reports them by type.

    Redirects are not followed: the Authorization header answers a
    challenge for one URI, and a redirect is not something this server
    has ever been seen to send.
    """
    path = urlsplit(url).path
    try:
        async with asyncio.timeout(timeout):
            async with session.get(url, allow_redirects=False) as first:
                if first.status == 200:
                    return await _read_body(first, max_bytes)
                challenge_header = first.headers.get("WWW-Authenticate", "")
                if first.status != 401:
                    raise PhonebookError(
                        f"the phonebook server answered HTTP {first.status}")
            if not challenge_header.lower().startswith("digest"):
                # Anything else — Basic above all — would put the token
                # on the wire in the clear or near enough. Refuse.
                raise PhonebookError(
                    "the phonebook server did not ask for digest authentication")
            authorization = digest_authorization(
                "GET", path, username, password,
                parse_challenge(challenge_header))
            async with session.get(
                    url, headers={"Authorization": authorization},
                    allow_redirects=False) as second:
                if second.status != 200:
                    raise PhonebookError(
                        f"the phonebook server answered HTTP {second.status} "
                        "to the authenticated request")
                return await _read_body(second, max_bytes)
    except TimeoutError as err:
        raise PhonebookError("the phonebook download timed out") from err
