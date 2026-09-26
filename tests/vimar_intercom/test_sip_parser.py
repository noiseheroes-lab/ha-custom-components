"""Tests for SIP message parsing and transaction correlation."""

import pytest

from custom_components.vimar_intercom import sip_parser as sp

REGISTER_200 = (
    "SIP/2.0 200 OK\r\n"
    "Via: SIP/2.0/TLS 192.0.2.5:5070;branch=z9hG4bKabc123;rport=5070\r\n"
    "From: <sip:60901@example.invalid>;tag=fromtag\r\n"
    "To: <sip:60901@example.invalid>;tag=totag\r\n"
    "Call-ID: reg-deadbeef\r\n"
    "CSeq: 7 REGISTER\r\n"
    "Contact: <sip:60901@192.0.2.5:5070;transport=tls>;expires=1800\r\n"
    "Content-Length: 0\r\n\r\n"
)

INVITE_REQUEST = (
    "INVITE sip:60901@example.invalid SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 192.0.2.9:5060;branch=z9hG4bKzzz\r\n"
    "Via: SIP/2.0/TLS 192.0.2.8:5060;branch=z9hG4bKyyy\r\n"
    "From: <sip:55001@example.invalid>;tag=callertag\r\n"
    "To: <sip:60901@example.invalid>\r\n"
    "Call-ID: call-1234\r\n"
    "CSeq: 1 INVITE\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 5\r\n\r\nv=0\r\n"
)


def test_parse_response_extracts_code_and_headers():
    msg = sp.parse_message(REGISTER_200)
    assert msg.code == 200
    assert msg.method is None
    assert msg.headers["call-id"] == "reg-deadbeef"
    assert msg.start_line == "SIP/2.0 200 OK"


def test_parse_request_extracts_method_and_body():
    msg = sp.parse_message(INVITE_REQUEST)
    assert msg.method == "INVITE"
    assert msg.code is None
    assert msg.body == "v=0\r\n"


def test_parse_keeps_every_via_in_order():
    msg = sp.parse_message(INVITE_REQUEST)
    assert len(msg.via_list) == 2
    assert msg.via_list[0].endswith("branch=z9hG4bKzzz")


def test_via_branch_uses_the_topmost_via():
    msg = sp.parse_message(INVITE_REQUEST)
    assert msg.headers["via"] == msg.via_list[0]
    assert sp.via_branch(msg.headers) == "z9hG4bKzzz"


def test_cseq_parts_splits_sequence_and_method():
    assert sp.cseq_parts({"cseq": "7 REGISTER"}) == (7, "REGISTER")


def test_cseq_parts_tolerates_garbage():
    assert sp.cseq_parts({"cseq": "nonsense"}) == (None, "")
    assert sp.cseq_parts({}) == (None, "")


def test_header_params_parses_quoted_and_bare_values():
    params = sp.header_params(
        'Digest realm="example.invalid", nonce=abc, qop="auth"')
    assert params["realm"] == "example.invalid"
    assert params["nonce"] == "abc"
    assert params["qop"] == "auth"


def test_header_params_stops_at_a_uri_delimiter():
    params = sp.header_params(
        "<sip:60901@192.0.2.5:5070;transport=tls>;expires=1800")
    assert params["transport"] == "tls"
    assert params["expires"] == "1800"


def test_transaction_key_is_stable():
    assert sp.transaction_key("z9hG4bKabc123", 7, "REGISTER") == (
        "z9hG4bKabc123|7|REGISTER")


def test_response_keys_prefer_branch_and_cseq_over_call_id():
    keys = sp.response_keys(sp.parse_message(REGISTER_200))
    assert keys[0] == "z9hG4bKabc123|7|REGISTER"
    assert keys[-1] == "cid:reg-deadbeef|7|REGISTER"


def test_response_keys_fall_back_to_call_id_without_a_branch():
    raw = REGISTER_200.replace(";branch=z9hG4bKabc123", "")
    keys = sp.response_keys(sp.parse_message(raw))
    assert keys == ["cid:reg-deadbeef|7|REGISTER"]


def test_call_id_fallback_separates_a_retry_from_its_original():
    # A REGISTER and its authenticated retry share a Call-ID. If the
    # fallback key did not carry the CSeq, a late response to the first
    # would be delivered to the second and accepted as its final answer.
    first = sp.response_keys(sp.parse_message(
        REGISTER_200.replace(";branch=z9hG4bKabc123", "")))
    retry = sp.response_keys(sp.parse_message(
        REGISTER_200.replace(";branch=z9hG4bKabc123", "")
                    .replace("CSeq: 7", "CSeq: 8")))
    assert first != retry


def test_response_keys_are_empty_without_a_parseable_cseq():
    raw = REGISTER_200.replace("CSeq: 7 REGISTER", "CSeq: nonsense")
    assert sp.response_keys(sp.parse_message(raw)) == []


def test_two_transactions_on_one_call_id_get_different_keys():
    first = sp.response_keys(sp.parse_message(REGISTER_200))[0]
    second = sp.response_keys(sp.parse_message(
        REGISTER_200.replace("CSeq: 7", "CSeq: 8")
                    .replace("branch=z9hG4bKabc123", "branch=z9hG4bKdef456")))[0]
    assert first != second


def test_addr_uri_reads_the_bracketed_form():
    assert sp.addr_uri("<sip:55001@example.com>;tag=callertag") == (
        "sip:55001@example.com")


def test_addr_uri_reads_the_bracketed_form_with_a_display_name():
    assert sp.addr_uri('"Front Door" <sip:55001@example.com>;tag=xyz') == (
        "sip:55001@example.com")


def test_addr_uri_falls_back_to_the_bare_addr_spec():
    # A From header carrying a bare addr-spec, with no angle brackets at
    # all, is legal SIP. The URI is whatever precedes the first `;`.
    assert sp.addr_uri("sip:55001@example.com;tag=abc") == (
        "sip:55001@example.com")


def test_addr_uri_of_an_empty_header_is_empty():
    assert sp.addr_uri("") == ""


def test_addr_uri_with_neither_brackets_nor_a_uri_is_empty():
    # No angle brackets and nothing before the first `;` either.
    assert sp.addr_uri(";tag=abc") == ""


def test_tag_of_reads_the_tag_parameter():
    assert sp.tag_of("<sip:a@b>;tag=totag") == "totag"
    assert sp.tag_of("<sip:a@b>") == ""


def test_granted_expiry_reads_the_contact_expires_parameter():
    msg = sp.parse_message(REGISTER_200)
    assert sp.granted_expiry(msg, "60901", 3600) == 1800


def test_granted_expiry_falls_back_to_the_expires_header():
    raw = REGISTER_200.replace(";expires=1800", "").replace(
        "Content-Length: 0", "Expires: 600\r\nContent-Length: 0")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 600


def test_granted_expiry_falls_back_to_the_requested_value():
    raw = REGISTER_200.replace(";expires=1800", "")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 3600


def test_granted_expiry_ignores_a_contact_for_another_user():
    raw = REGISTER_200.replace("<sip:60901@192.0.2.5", "<sip:99999@192.0.2.5")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 3600


def test_header_values_returns_every_occurrence_in_order():
    raw = ("INVITE sip:a@example.invalid SIP/2.0\r\n"
           "Call-ID: first@host\r\n"
           "call-id: second10ch\r\n"
           "X-Call-ID: custom\r\n"
           "Content-Length: 0\r\n\r\n"
           "Call-ID: in-the-body")
    assert sp.header_values(raw, "Call-ID") == ["first@host", "second10ch"]
    assert sp.header_values(raw, "Call-ID", "X-Call-ID") == [
        "first@host", "second10ch", "custom"]
    assert sp.header_values(raw, "Absent") == []


@pytest.mark.parametrize(("value", "expected"), [
    ('SIP;cause=200;text="Call completed elsewhere"', 200),
    ("SIP ;cause=487", 487),
    ("Q.850;cause=16, SIP;cause=200", 200),
    ("Q.850;cause=16", None),
    ("", None),
    ("SIP;text=x", None),
])
def test_reason_cause_reads_the_sip_entry(value, expected):
    assert sp.reason_cause(value) == expected
