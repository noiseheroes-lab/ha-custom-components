"""Tests for the phonebook download: URL, HTTP digest auth, and the fetch.

The session is a fake with the slice of aiohttp's interface the
downloader uses, so the tests need neither aiohttp nor the network.
"""

import asyncio
import hashlib

import pytest

from custom_components.vimar_intercom import phonebook as pb

DOMAIN = "abc123.FFFFFFFFFF.ipvdes.vimar.cloud"
PROXY = "ipvdes.vimar.cloud"
VERSION = "0123456789abcdef0123456789abcdef"


# ─── URL ─────────────────────────────────────────────────────────────

def test_the_url_strips_the_proxy_from_the_domain():
    assert pb.phonebook_url(PROXY, DOMAIN, VERSION) == (
        f"https://{PROXY}/phonebook/domains/abc123.FFFFFFFFFF/{VERSION}")


def test_a_domain_outside_the_proxy_is_used_whole():
    assert pb.phonebook_url(PROXY, "plant.example.invalid", VERSION) == (
        f"https://{PROXY}/phonebook/domains/plant.example.invalid/{VERSION}")


@pytest.mark.parametrize("version", ["", "../x", "abc/def", "a b", "a?b=1"])
def test_a_version_unsafe_in_a_path_is_refused(version):
    with pytest.raises(ValueError):
        pb.phonebook_url(PROXY, DOMAIN, version)


# ─── digest ──────────────────────────────────────────────────────────

def test_the_rfc_2617_worked_example():
    """RFC 2617 section 3.5: the response digest for Mufasa."""
    challenge = pb.parse_challenge(
        'Digest realm="testrealm@host.com", qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = pb.digest_authorization(
        "GET", "/dir/index.html", "Mufasa", "Circle Of Life", challenge,
        cnonce="0a4f113b", nc=1)
    fields = pb.parse_challenge(header)
    assert fields["response"] == "6629fae49393a05397450978507c4ef1"
    assert fields["qop"] == "auth"
    assert fields["nc"] == "00000001"
    assert fields["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41"
    assert fields["uri"] == "/dir/index.html"
    assert fields["username"] == "Mufasa"


def test_a_challenge_without_qop_uses_the_rfc_2069_form():
    challenge = {"realm": "r", "nonce": "n"}
    header = pb.digest_authorization("GET", "/p", "u", "pw", challenge)
    fields = pb.parse_challenge(header)
    ha1 = hashlib.md5(b"u:r:pw").hexdigest()
    ha2 = hashlib.md5(b"GET:/p").hexdigest()
    assert fields["response"] == hashlib.md5(
        f"{ha1}:n:{ha2}".encode()).hexdigest()
    assert "qop" not in fields


def test_sha_256_is_supported():
    challenge = {"realm": "r", "nonce": "n", "qop": "auth",
                 "algorithm": "SHA-256"}
    header = pb.digest_authorization(
        "GET", "/p", "u", "pw", challenge, cnonce="c", nc=1)
    fields = pb.parse_challenge(header)
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    expected = h(f"{h('u:r:pw')}:n:00000001:c:auth:{h('GET:/p')}")
    assert fields["response"] == expected
    assert fields["algorithm"] == "SHA-256"


def test_an_unknown_algorithm_is_refused():
    with pytest.raises(pb.PhonebookError):
        pb.digest_authorization(
            "GET", "/p", "u", "pw", {"realm": "r", "nonce": "n",
                                     "algorithm": "SHA-512-256"})


def test_a_challenge_offering_only_auth_int_is_refused():
    with pytest.raises(pb.PhonebookError):
        pb.digest_authorization(
            "GET", "/p", "u", "pw", {"realm": "r", "nonce": "n",
                                     "qop": "auth-int"})


def test_parse_challenge_handles_commas_inside_quotes():
    fields = pb.parse_challenge('Digest realm="a, b", nonce="n", qop=auth')
    assert fields == {"realm": "a, b", "nonce": "n", "qop": "auth"}


# ─── fetch ───────────────────────────────────────────────────────────

class FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


class FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers with a digest challenge, then checks the credentials."""

    def __init__(self, body=b"SQLite format 3\x00data", *, password="tok",
                 first_status=401, challenge=None):
        self.body = body
        self.password = password
        self.first_status = first_status
        self.challenge = challenge or (
            'Digest realm="phonebook", nonce="abc", qop="auth", '
            'algorithm=MD5')
        self.requests: list[dict] = []

    def get(self, url, headers=None, allow_redirects=True):
        self.requests.append({"url": url, "headers": dict(headers or {}),
                              "allow_redirects": allow_redirects})
        auth = (headers or {}).get("Authorization")
        if auth is None:
            return FakeResponse(
                self.first_status, {"WWW-Authenticate": self.challenge})
        fields = pb.parse_challenge(auth)
        expected = pb.parse_challenge(pb.digest_authorization(
            "GET", fields["uri"], DOMAIN, self.password,
            pb.parse_challenge(self.challenge),
            cnonce=fields["cnonce"], nc=int(fields["nc"], 16)))
        if fields["response"] != expected["response"]:
            return FakeResponse(401, {"WWW-Authenticate": self.challenge})
        return FakeResponse(200, body=self.body)


URL = pb.phonebook_url(PROXY, DOMAIN, VERSION)


def test_the_download_answers_the_challenge_and_returns_the_body():
    session = FakeSession()
    data = asyncio.run(pb.download_phonebook(session, URL, DOMAIN, "tok"))
    assert data == session.body
    assert len(session.requests) == 2
    auth = pb.parse_challenge(session.requests[1]["headers"]["Authorization"])
    assert auth["uri"] == f"/phonebook/domains/abc123.FFFFFFFFFF/{VERSION}"
    assert auth["username"] == DOMAIN
    assert all(r["allow_redirects"] is False for r in session.requests)


def test_wrong_credentials_raise_with_the_status_and_not_the_token():
    session = FakeSession(password="other")
    with pytest.raises(pb.PhonebookError) as err:
        asyncio.run(pb.download_phonebook(session, URL, DOMAIN, "tok"))
    assert "401" in str(err.value)
    assert "tok" not in str(err.value)


def test_a_server_that_does_not_challenge_is_an_error():
    session = FakeSession(first_status=404)
    with pytest.raises(pb.PhonebookError, match="404"):
        asyncio.run(pb.download_phonebook(session, URL, DOMAIN, "tok"))


def test_a_basic_challenge_is_refused_rather_than_sending_the_token():
    session = FakeSession(challenge='Basic realm="x"')
    with pytest.raises(pb.PhonebookError):
        asyncio.run(pb.download_phonebook(session, URL, DOMAIN, "tok"))
    assert len(session.requests) == 1


def test_an_oversized_body_is_cut_off():
    session = FakeSession(body=b"x" * 100)
    with pytest.raises(pb.PhonebookError):
        asyncio.run(pb.download_phonebook(
            session, URL, DOMAIN, "tok", max_bytes=10))


def test_a_stalled_server_times_out():
    class Stalled(FakeSession):
        def get(self, url, headers=None, allow_redirects=True):
            outer = self

            class Slow(FakeResponse):
                async def __aenter__(self):
                    await asyncio.sleep(10)
                    return self
            outer.requests.append({})
            return Slow(200)

    with pytest.raises(pb.PhonebookError, match="time"):
        asyncio.run(pb.download_phonebook(
            Stalled(), URL, DOMAIN, "tok", timeout=0.05))
