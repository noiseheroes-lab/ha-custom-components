"""Tests for RuntimeConfig derivation."""

import hashlib

import pytest

from custom_components.vimar_intercom import runtime

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud",
    "CPROXY": "ipvdes.vimar.cloud",
    "PROXY": "192.0.2.10",
    "GID": "21",
    "MAC": "00:00:5E:00:53:00",
    "PLANTTYPE": "2FV2",
    "PC": "40515",
}


def test_entry_data_from_qr_maps_every_known_field():
    data = runtime.entry_data_from_qr(QR_FIELDS)
    assert data["sip_user"] == "60901"
    assert data["sip_password"] == "examplepassword"
    assert data["sip_domain"] == "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"
    assert data["cloud_proxy"] == "ipvdes.vimar.cloud"
    assert data["local_proxy"] == "192.0.2.10"
    assert data["group_id"] == "21"
    assert data["mac"] == "00:00:5E:00:53:00"
    assert data["plant_type"] == "2FV2"
    assert data["product_code"] == "40515"


def test_entry_data_from_qr_generates_a_unique_device_identity():
    first = runtime.entry_data_from_qr(QR_FIELDS)
    second = runtime.entry_data_from_qr(QR_FIELDS)
    assert first["device_id"] != second["device_id"]
    assert first["device_uuid"] != second["device_uuid"]
    assert first["push_token"] != second["push_token"]
    assert len(first["device_id"]) == 15
    assert first["device_id"].isdigit()


def test_entry_data_from_qr_applies_defaults():
    data = runtime.entry_data_from_qr({
        "ID": "1", "PWD": "p", "CDOMAIN": "d.invalid"})
    assert data["cloud_proxy"] == "ipvdes.vimar.cloud"
    assert data["group_id"] == "21"
    assert data["local_proxy"] == ""


def test_build_runtime_config_derives_transport():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.proxy_host == "ipvdes.vimar.cloud"
    assert cfg.proxy_port == 7042
    assert cfg.sni == "ipvdes.vimar.cloud"
    assert cfg.route == "ipvdes.vimar.cloud"
    assert cfg.local_proxy == "192.0.2.10"
    assert cfg.local_sip_port == 5060


def test_sip_ha1_is_md5_of_user_realm_password():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    expected = hashlib.md5(
        b"60901:abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud:examplepassword"
    ).hexdigest()
    assert cfg.sip_ha1 == expected


def test_panel_uri_uses_the_sip_domain():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.panel_uri("55001") == (
        "sip:55001@abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud")


def test_door_uri_uses_the_group_id_from_the_qr():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.door_uri == "sip:21@abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"


# ─── QR values are checked before they can reach a SIP message ───────
#
# This replaces a test that asserted `door_uri` renders `sip:@domain`
# for an empty group ID and commented that no validation is done. It
# cemented the absence of the checks below rather than testing anything.

CRLF_INJECTION = "21\r\nRoute: <sip:attacker.invalid;lr>"


@pytest.mark.parametrize("field, value", [
    ("ID", CRLF_INJECTION),
    ("ID", "60901@evil.invalid"),
    ("ID", ""),
    ("GID", CRLF_INJECTION),
    ("GID", "21 21"),
    ("CDOMAIN", CRLF_INJECTION),
    ("CDOMAIN", "plant.invalid;lr"),
    ("CPROXY", CRLF_INJECTION),
    ("CPROXY", "proxy.invalid:7042"),
    ("PROXY", CRLF_INJECTION),
    ("MAC", "[click me](https://evil.invalid)"),
])
def test_a_qr_field_that_could_forge_a_sip_message_is_rejected(field, value):
    """`qr.parse_fields` percent-decodes, so `%0D%0A` arrives as a real
    CRLF and an f-string builds it straight into a request. CPROXY is
    both the connection host and the TLS SNI, so one hostile payload
    could redirect the whole session."""
    fields = dict(QR_FIELDS)
    fields[field] = value
    with pytest.raises(ValueError):
        runtime.entry_data_from_qr(fields)


def test_the_rejection_never_repeats_the_value_it_rejected():
    """The message reaches a log and the setup dialog."""
    fields = dict(QR_FIELDS, ID=CRLF_INJECTION)
    with pytest.raises(ValueError) as excinfo:
        runtime.entry_data_from_qr(fields)
    assert CRLF_INJECTION not in str(excinfo.value)
    assert "ID" in str(excinfo.value)


def test_an_absent_optional_field_is_still_accepted():
    data = runtime.entry_data_from_qr({
        "ID": "60901", "PWD": "p", "CDOMAIN": "plant.invalid"})
    assert data["local_proxy"] == ""
    assert data["mac"] == ""
    assert data["group_id"] == "21"


def test_a_local_proxy_given_as_an_ip_address_is_accepted():
    data = runtime.entry_data_from_qr(dict(QR_FIELDS, PROXY="192.0.2.10"))
    assert data["local_proxy"] == "192.0.2.10"


def test_the_door_uri_always_has_a_user_part():
    """Every accepted group ID produces a routable URI."""
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(dict(QR_FIELDS, GID="7")), {})
    assert cfg.door_uri == "sip:7@abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"


# ─── the door command is a SIP body ──────────────────────────────────

@pytest.mark.parametrize("command", ["OPEN_2F", "OPEN_CURRENT", "OPEN1"])
def test_a_plain_door_command_is_accepted(command):
    assert runtime.valid_door_command(command) is True


@pytest.mark.parametrize("command", [
    "", "OPEN 2F", "OPEN_2F\r\nMESSAGE sip:21@x SIP/2.0", "APRÌ_2F"])
def test_a_door_command_that_would_corrupt_the_stream_is_rejected(command):
    """A CRLF forges a second request; a non-ASCII character makes the
    byte count of the body differ from what any character count says."""
    assert runtime.valid_door_command(command) is False


def test_an_unusable_stored_door_command_falls_back_to_the_default():
    """Entries saved before the options flow checked this still load."""
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"door_command": "APRÌ_2F"})
    assert cfg.door_command == "OPEN_2F"


def test_default_panels_is_a_single_entry():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert [p.address for p in cfg.panels] == ["55001"]
    assert cfg.default_panel.address == "55001"


def test_parse_panels_accepts_address_and_optional_name():
    panels = runtime.parse_panels("55001:Street Gate, 55002 , 55003:Garage")
    assert [(p.address, p.name) for p in panels] == [
        ("55001", "Street Gate"),
        ("55002", "Panel 55002"),
        ("55003", "Garage"),
    ]


def test_parse_panels_rejects_non_numeric_addresses():
    with pytest.raises(ValueError):
        runtime.parse_panels("55001, not-an-extension")


def test_parse_panels_rejects_an_empty_list():
    with pytest.raises(ValueError):
        runtime.parse_panels("   ")


def test_options_override_transport_and_panels():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS),
        {
            "sip_port": 5061,
            "panels": "55001:Gate,55002:Door",
            "rtp_port_base": 8000,
            "door_command": "OPEN_1F",
        },
    )
    assert cfg.proxy_port == 5061
    assert [p.name for p in cfg.panels] == ["Gate", "Door"]
    assert cfg.rtp_audio_port == 8000
    assert cfg.rtp_video_port == 10000
    assert cfg.door_command == "OPEN_1F"


def test_prefer_local_switches_the_proxy_host():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"prefer_local": True})
    assert cfg.proxy_host == "192.0.2.10"
    assert cfg.proxy_port == 5060
    # The SNI and Route still name the cloud, which is what the cert covers.
    assert cfg.sni == "ipvdes.vimar.cloud"


def test_prefer_local_ignores_the_cloud_proxy_port():
    # sip_port configures the cloud proxy. The options flow always
    # persists it, so honouring it locally would send a saved 7042 at a
    # panel that listens on 5060 — breaking the very user who turned
    # prefer_local on.
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS),
        {"prefer_local": True, "sip_port": 7042},
    )
    assert cfg.proxy_host == "192.0.2.10"
    assert cfg.proxy_port == 5060


def test_prefer_local_is_ignored_without_a_local_proxy():
    data = runtime.entry_data_from_qr({
        "ID": "1", "PWD": "p", "CDOMAIN": "d.invalid"})
    cfg = runtime.build_runtime_config(data, {"prefer_local": True})
    assert cfg.proxy_host == "ipvdes.vimar.cloud"


def test_runtime_config_repr_hides_the_password():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert "examplepassword" not in repr(cfg)


def test_the_cloud_proxy_is_located_through_srv():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.locate_by_srv is True
    # The name stays the SIP domain everywhere it is used; SRV only
    # changes where the socket goes.
    assert cfg.proxy_host == cfg.sni == cfg.route == "ipvdes.vimar.cloud"


def test_the_local_panel_is_never_located_through_srv():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"prefer_local": True})
    assert cfg.locate_by_srv is False
    assert cfg.proxy_port_override is None


@pytest.mark.parametrize("options", [{}, {"sip_port": 7042}])
def test_the_default_port_defers_to_srv(options):
    # The options flow always saves sip_port, so the default cannot be
    # read as a deliberate choice.
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), options)
    assert cfg.proxy_port == 7042
    assert cfg.proxy_port_override is None


def test_a_changed_port_overrides_srv():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"sip_port": 5061})
    assert cfg.proxy_port == 5061
    assert cfg.proxy_port_override == 5061
