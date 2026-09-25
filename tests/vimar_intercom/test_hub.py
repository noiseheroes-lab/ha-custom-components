"""Tests for VimarIntercomHub._panel_for.

hub.py imports only sip_client, media_handler, const and runtime — none
of which import Home Assistant (see tests/conftest.py) — so it loads
through the same stub-package mechanism as the other pure modules and
is unit-testable directly, without a running Home Assistant.
"""

from custom_components.vimar_intercom import hub, runtime

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "example.invalid",
}


def _hub(panels: str = "55001:Front Door,55002:Garage") -> hub.VimarIntercomHub:
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"panels": panels})
    return hub.VimarIntercomHub(cfg)


def test_panel_for_matches_a_configured_panel():
    h = _hub()
    assert h._panel_for("sip:55001@example.invalid") == (
        "55001", "Front Door")


def test_panel_for_echoes_the_address_when_the_caller_is_unconfigured():
    h = _hub()
    assert h._panel_for("sip:99999@example.invalid") == ("99999", "99999")


def test_panel_for_of_an_empty_uri_is_unknown():
    h = _hub()
    assert h._panel_for("") == ("", "unknown")


def test_panel_for_of_a_malformed_uri_with_no_at_sign():
    h = _hub()
    # No "@" at all: the whole string (after stripping any "sip:"
    # prefix) is treated as the address.
    assert h._panel_for("not-a-uri") == ("not-a-uri", "not-a-uri")
