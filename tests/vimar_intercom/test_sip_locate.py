"""Tests for locating the SIP server through DNS SRV (RFC 3263/2782).

The ordering, the fallback and the move to the next server are pure or
take their I/O as a parameter, so none of this touches the network.
"""

import asyncio
import logging
import random
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from custom_components.vimar_intercom import sip_locate as locate
from custom_components.vimar_intercom.sip_locate import SrvRecord, Target

DOMAIN = "ipvdes.vimar.cloud"

# The shape the public records for the Vimar cloud had when this was
# written: three equal servers on 7042.
VIMAR_SRV = [
    SrvRecord(0, 30, 7042, "flexiprod1.ipvdes2.vimarsso.cloud"),
    SrvRecord(0, 30, 7042, "flexiprod2.ipvdes2.vimarsso.cloud"),
    SrvRecord(0, 30, 7042, "flexiprod3.ipvdes2.vimarsso.cloud"),
]


def run(coro):
    return asyncio.run(coro)


class _FixedRng:
    """An rng whose draws are scripted, to pin the RFC 2782 arithmetic."""

    def __init__(self, draws):
        self.draws = list(draws)
        self.calls = []

    def randint(self, low, high):
        self.calls.append(("randint", low, high))
        return self.draws.pop(0)

    def randrange(self, stop):
        self.calls.append(("randrange", stop))
        return self.draws.pop(0)


# ─── Ordering ────────────────────────────────────────────────────────

def test_lower_priority_always_comes_first():
    records = [
        SrvRecord(20, 100, 7042, "backup.example.invalid"),
        SrvRecord(10, 1, 7042, "primary.example.invalid"),
    ]
    for seed in range(20):
        order = locate.order_srv(records, random.Random(seed))
        assert [t.host for t in order] == [
            "primary.example.invalid", "backup.example.invalid"]


def test_every_record_appears_exactly_once():
    order = locate.order_srv(VIMAR_SRV, random.Random(1))
    assert sorted(t.host for t in order) == sorted(r.target for r in VIMAR_SRV)
    assert {t.port for t in order} == {7042}


def test_the_weighted_draw_follows_the_running_sum():
    # Weights 10 and 30: a draw of 0..10 lands on the first record,
    # 11..40 on the second.
    records = [
        SrvRecord(0, 10, 7042, "a.example.invalid"),
        SrvRecord(0, 30, 7042, "b.example.invalid"),
    ]
    rng = _FixedRng([11])
    order = locate.order_srv(records, rng)
    assert [t.host for t in order] == ["b.example.invalid", "a.example.invalid"]
    assert rng.calls == [("randint", 0, 40)]


def test_weights_share_the_load_in_proportion():
    records = [
        SrvRecord(0, 10, 7042, "light.example.invalid"),
        SrvRecord(0, 90, 7042, "heavy.example.invalid"),
    ]
    rng = random.Random(42)
    firsts = Counter(locate.order_srv(records, rng)[0].host for _ in range(4000))
    share = firsts["heavy.example.invalid"] / 4000
    assert 0.85 < share < 0.95


def test_equal_weights_spread_across_every_server():
    rng = random.Random(7)
    firsts = Counter(locate.order_srv(VIMAR_SRV, rng)[0].host for _ in range(3000))
    assert set(firsts) == {r.target for r in VIMAR_SRV}
    assert min(firsts.values()) > 800


def test_all_zero_weights_are_still_spread():
    # The RFC's running sum would pick the first record every time.
    records = [SrvRecord(0, 0, 7042, f"s{i}.example.invalid") for i in range(3)]
    rng = random.Random(3)
    firsts = Counter(locate.order_srv(records, rng)[0].host for _ in range(3000))
    assert len(firsts) == 3
    assert min(firsts.values()) > 800


def test_a_zero_weight_record_keeps_a_small_chance():
    records = [
        SrvRecord(0, 0, 7042, "zero.example.invalid"),
        SrvRecord(0, 50, 7042, "fifty.example.invalid"),
    ]
    # A draw of 0 selects the zero-weight record, which RFC 2782 sorts first.
    order = locate.order_srv(records, _FixedRng([0]))
    assert order[0].host == "zero.example.invalid"


# ─── Fallback and overrides ──────────────────────────────────────────

def test_no_records_falls_back_to_the_name_itself():
    assert locate.connect_order("sip.example.invalid", 5061, []) == [
        Target("sip.example.invalid", 5061)]


def test_a_dot_target_means_not_offered_and_falls_back():
    records = [SrvRecord(0, 0, 0, ".")]
    assert locate.connect_order(DOMAIN, 7042, records) == [Target(DOMAIN, 7042)]


def test_srv_targets_replace_the_name_and_keep_their_port():
    order = locate.connect_order(DOMAIN, 7042, VIMAR_SRV, rng=random.Random(0))
    assert DOMAIN not in {t.host for t in order}
    assert {t.port for t in order} == {7042}


def test_an_explicit_port_override_wins_over_the_srv_port():
    order = locate.connect_order(
        DOMAIN, 5061, VIMAR_SRV, port_override=5061, rng=random.Random(0))
    assert {t.port for t in order} == {5061}
    assert {t.host for t in order} == {r.target for r in VIMAR_SRV}


def test_records_from_a_pycares_answer():
    def rr(data):
        return SimpleNamespace(data=data)

    answer = [
        rr(SimpleNamespace(cname="alias.example.invalid")),  # a CNAME in the chain
        rr(SimpleNamespace(priority=0, weight=30, port=7042,
                           target="flexiprod1.ipvdes2.vimarsso.cloud.")),
        rr(SimpleNamespace(priority=5, weight=0, port=0, target=".")),
    ]
    assert locate.records_from_answer(answer) == [
        SrvRecord(0, 30, 7042, "flexiprod1.ipvdes2.vimarsso.cloud"),
        SrvRecord(5, 0, 0, "."),
    ]


# ─── Resolution ──────────────────────────────────────────────────────

def test_resolution_queries_the_sips_tcp_service_of_the_name():
    asked = []

    async def query(name):
        asked.append(name)
        return VIMAR_SRV

    targets = run(locate.resolve_targets(DOMAIN, 7042, query=query))
    assert asked == ["_sips._tcp.ipvdes.vimar.cloud"]
    assert {t.host for t in targets} == {r.target for r in VIMAR_SRV}


def test_resolution_without_records_dials_the_name():
    async def query(_name):
        return []

    assert run(locate.resolve_targets(
        "sip.example.invalid", 5061, query=query)) == [
            Target("sip.example.invalid", 5061)]


def test_a_failed_lookup_falls_back_and_says_why(caplog):
    async def query(_name):
        raise OSError("resolver unreachable")

    with caplog.at_level(logging.WARNING, logger=locate.__name__):
        targets = run(locate.resolve_targets(DOMAIN, 7042, query=query))
    assert targets == [Target(DOMAIN, 7042)]
    assert "resolver unreachable" in caplog.text


def test_a_stalled_lookup_is_bounded():
    async def query(_name):
        await asyncio.Event().wait()

    started = time.monotonic()
    targets = run(locate.resolve_targets(
        DOMAIN, 7042, query=query, timeout=0.05))
    assert time.monotonic() - started < 2
    assert targets == [Target(DOMAIN, 7042)]


def test_an_ip_literal_is_never_looked_up():
    async def query(_name):
        raise AssertionError("an IP address has no SRV records to find")

    assert run(locate.resolve_targets("192.0.2.20", 5061, query=query)) == [
        Target("192.0.2.20", 5061)]


# ─── Moving to the next server ───────────────────────────────────────

TARGETS = [
    Target("a.example.invalid", 7042),
    Target("b.example.invalid", 7042),
    Target("c.example.invalid", 7042),
]


def test_a_failed_server_moves_on_to_the_next(caplog):
    tried = []

    async def opener(target):
        tried.append(target.host)
        if target.host == "a.example.invalid":
            raise ConnectionRefusedError("refused")
        return "stream"

    with caplog.at_level(logging.INFO, logger=locate.__name__):
        target, result = run(locate.open_first(
            TARGETS, opener, timeout=1, domain=DOMAIN))
    assert target == TARGETS[1]
    assert result == "stream"
    assert tried == ["a.example.invalid", "b.example.invalid"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "a.example.invalid:7042" in warnings[0].getMessage()
    assert "refused" in warnings[0].getMessage()


def test_a_server_that_never_answers_times_out_and_the_next_is_tried(caplog):
    async def opener(target):
        if target.host == "a.example.invalid":
            await asyncio.Event().wait()  # a blackholed SYN
        return "stream"

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=locate.__name__):
        target, _ = run(locate.open_first(
            TARGETS, opener, timeout=0.05, domain=DOMAIN))
    assert time.monotonic() - started < 2
    assert target == TARGETS[1]
    assert "timed out" in caplog.text


def test_every_server_failing_raises_for_the_supervisor():
    async def opener(_target):
        await asyncio.Event().wait()

    started = time.monotonic()
    with pytest.raises(ConnectionError) as info:
        run(locate.open_first(TARGETS, opener, timeout=0.05, domain=DOMAIN))
    assert time.monotonic() - started < 2
    assert "3 tried" in str(info.value)
    assert isinstance(info.value.__cause__, TimeoutError)


def test_an_empty_target_list_is_a_bug_not_a_retry():
    async def opener(_target):
        return "stream"

    with pytest.raises(ValueError):
        run(locate.open_first([], opener, timeout=1, domain=DOMAIN))


# ─── The aiodns adapter ──────────────────────────────────────────────

def test_aiodns_no_record_answers_mean_no_records(monkeypatch):
    aiodns = pytest.importorskip("aiodns")

    class _Resolver:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def query_dns(self, name, qtype):
            assert qtype == "SRV"
            code = 4 if "nxdomain" in name else 1
            raise aiodns.error.DNSError(code, "no data")

    monkeypatch.setattr(aiodns, "DNSResolver", _Resolver)
    assert run(locate._aiodns_srv("_sips._tcp.nxdomain.example.invalid")) == []
    assert run(locate._aiodns_srv("_sips._tcp.nodata.example.invalid")) == []


def test_aiodns_other_failures_raise(monkeypatch):
    aiodns = pytest.importorskip("aiodns")

    class _Resolver:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def query_dns(self, name, qtype):
            raise aiodns.error.DNSError(11, "SERVFAIL")

    monkeypatch.setattr(aiodns, "DNSResolver", _Resolver)
    with pytest.raises(aiodns.error.DNSError):
        run(locate._aiodns_srv("_sips._tcp.example.invalid"))
