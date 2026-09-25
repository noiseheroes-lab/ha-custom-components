"""Tests for RuntimeConfig derivation."""

import dataclasses
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


def test_door_uri_with_an_empty_group_id_has_no_user_part():
    # build_runtime_config never produces an empty group_id (it falls back
    # to DEFAULT_GROUP_ID), so this only happens if a RuntimeConfig is
    # built directly with group_id="". door_uri does no defensive
    # handling of its own; it renders whatever group_id holds.
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    cfg = dataclasses.replace(cfg, group_id="")
    assert cfg.door_uri == "sip:@abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"


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
