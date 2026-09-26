"""Vimar Intercom — which host the SIP connection opens its socket to.

The QR's `CPROXY` names a SIP *domain*, not a server. The cloud default,
`ipvdes.vimar.cloud`, accepts nothing on the SIP port; its
`_sips._tcp` SRV records name the hosts that do. RFC 3263 locates a TLS
SIP server exactly this way, and it is what the vendor's own app does.

This module only decides where the TCP connection goes. The TLS SNI,
the certificate hostname check, the SIP `Route` header and the SIP
domain all keep naming the original host, and none of them are handled
here — see `sip_client.connect`.

No Home Assistant import. `aiodns` is a Home Assistant core requirement,
so it is always present in production; it is imported lazily so that the
ordering and fallback logic can be tested without it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .const import SIP_SRV_LOOKUP_TIMEOUT, SIP_SRV_SERVICE

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# c-ares status codes meaning "this name has no such record", as opposed
# to "the lookup itself failed". Numeric so that classifying them does
# not need aiodns imported.
_ARES_ENODATA = 1
_ARES_ENOTFOUND = 4


@dataclass(frozen=True)
class SrvRecord:
    """One SRV answer, reduced to the fields RFC 2782 orders by."""

    priority: int
    weight: int
    port: int
    target: str


@dataclass(frozen=True)
class Target:
    """One TCP destination to try."""

    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


SrvQuery = Callable[[str], Awaitable[list[SrvRecord]]]


# ─── Pure ordering ──────────────────────────────────────────────────

def _pick_weighted(group: list[SrvRecord], rng: Any) -> int:
    """Index of the next record, chosen by RFC 2782's weighted draw."""
    if len(group) == 1:
        return 0
    total = sum(record.weight for record in group)
    if total == 0:
        # The RFC's running sum would always pick the first record when
        # every weight is zero, sending every client to one server of a
        # set that was published as equals.
        return rng.randrange(len(group))
    threshold = rng.randint(0, total)
    running = 0
    for index, record in enumerate(group[:-1]):
        running += record.weight
        if running >= threshold:
            return index
    return len(group) - 1


def order_srv(records: Iterable[SrvRecord], rng: Any = random) -> list[Target]:
    """Order SRV records for connecting, per RFC 2782.

    Lowest priority first; within one priority, a weighted random draw
    without replacement. `rng` needs `randint` and `randrange`, so a
    seeded `random.Random` makes the result reproducible.
    """
    by_priority: dict[int, list[SrvRecord]] = {}
    for record in records:
        by_priority.setdefault(record.priority, []).append(record)

    ordered: list[SrvRecord] = []
    for priority in sorted(by_priority):
        # RFC 2782 puts zero-weight records first, which is what gives
        # them their small but non-zero chance in the running sum.
        group = sorted(by_priority[priority], key=lambda r: r.weight != 0)
        while group:
            ordered.append(group.pop(_pick_weighted(group, rng)))
    return [Target(record.target, record.port) for record in ordered]


def connect_order(
    host: str,
    port: int,
    records: Iterable[SrvRecord],
    *,
    port_override: int | None = None,
    rng: Any = random,
) -> list[Target]:
    """Every destination to try for `host`, in order.

    With no usable SRV record the host itself is dialled on `port`,
    which keeps a literal host name or IP address working. A record whose
    target is "." (RFC 2782: "not offered here") is not usable. When
    `port_override` is set it replaces the port each record advertises:
    a port the user typed in wins over one published in DNS.
    """
    usable = [r for r in records if r.target and r.target != "."]
    if not usable:
        return [Target(host, port)]
    targets = order_srv(usable, rng)
    if port_override is not None:
        targets = [Target(t.host, port_override) for t in targets]
    return targets


def records_from_answer(answer: Iterable[Any]) -> list[SrvRecord]:
    """SRV records out of a pycares answer section.

    The answer can also carry the CNAME records of a chain; anything
    without the four SRV fields is skipped. c-ares returns targets
    without the root dot, but one is stripped anyway so a target never
    reaches a log line or `getaddrinfo` in two spellings.
    """
    records = []
    for rr in answer:
        data = getattr(rr, "data", None)
        if not all(hasattr(data, f) for f in ("priority", "weight", "port", "target")):
            continue
        target = str(data.target)
        target = target.rstrip(".") or "."
        records.append(SrvRecord(
            int(data.priority), int(data.weight), int(data.port), target))
    return records


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def describe_error(err: BaseException) -> str:
    """A log-safe account of why a lookup or a connection failed."""
    if isinstance(err, TimeoutError):
        return "timed out"
    text = str(err)
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


# ─── I/O ────────────────────────────────────────────────────────────

async def _aiodns_srv(name: str) -> list[SrvRecord]:
    """Query SRV records through aiodns, without blocking the loop.

    Returns [] when the name has no SRV record. Any other failure raises.
    """
    # A Home Assistant core requirement, imported here so the rest of this
    # module loads, and is tested, without it.
    import aiodns

    async with aiodns.DNSResolver(loop=asyncio.get_running_loop()) as resolver:
        try:
            result = await resolver.query_dns(name, "SRV")
        except aiodns.error.DNSError as err:
            if err.args and err.args[0] in (_ARES_ENODATA, _ARES_ENOTFOUND):
                return []
            raise
    return records_from_answer(result.answer)


async def resolve_targets(
    host: str,
    port: int,
    *,
    port_override: int | None = None,
    query: SrvQuery | None = None,
    timeout: float = SIP_SRV_LOOKUP_TIMEOUT,
    rng: Any = random,
) -> list[Target]:
    """Look up where to connect for SIP domain `host`, freshly each time.

    Called on every reconnect, so a server change or a DNS failover is
    picked up without restarting anything. A failed lookup falls back to
    dialling `host` directly instead of raising: that keeps a literal
    host working behind a resolver that mishandles SRV, and for a domain
    that does need SRV the connect timeout and the supervisor's backoff
    bring the next attempt, which looks up again.
    """
    if _is_ip_literal(host):
        return [Target(host, port)]

    name = f"{SIP_SRV_SERVICE}.{host}"
    lookup = query or _aiodns_srv
    try:
        records = await asyncio.wait_for(lookup(name), timeout)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - any lookup failure falls back
        _LOGGER.warning(
            "SRV lookup for %s failed (%s); connecting to %s:%d directly",
            name, describe_error(err), host, port)
        records = []
    else:
        if not records:
            _LOGGER.debug(
                "No SRV record for %s; connecting to %s:%d directly",
                name, host, port)

    return connect_order(
        host, port, records, port_override=port_override, rng=rng)


async def open_first(
    targets: Sequence[Target],
    opener: Callable[[Target], Awaitable[_T]],
    *,
    timeout: float,
    domain: str,
) -> tuple[Target, _T]:
    """Open the first target that answers within `timeout` seconds each.

    A target that fails or stalls is logged and the next one tried, so
    one dead server costs one timeout rather than a whole backoff cycle.
    Only when every target has failed does this raise, and then the
    supervisor's backoff takes over.
    """
    if not targets:
        raise ValueError("no SIP server to connect to")
    last_error: BaseException | None = None
    for number, target in enumerate(targets, 1):
        _LOGGER.info(
            "Connecting to SIP server %s for %s (%d of %d)",
            target, domain, number, len(targets))
        try:
            result = await asyncio.wait_for(opener(target), timeout)
        except OSError as err:
            # TimeoutError, socket.gaierror, ssl.SSLError and a refused or
            # reset connection are all OSError subclasses.
            _LOGGER.warning(
                "SIP server %s did not connect: %s",
                target, describe_error(err))
            last_error = err
            continue
        return target, result
    raise ConnectionError(
        f"no SIP server for {domain} connected ({len(targets)} tried, "
        f"last: {describe_error(last_error)})") from last_error
