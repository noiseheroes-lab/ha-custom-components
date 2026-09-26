"""Tests for turning the plant's phonebook database into a PlantConfig.

The fixture is built programmatically, in the shape of the SQLite file
the Vimar cloud serves (tables and columns as the official app's SDK
reads them). Every name and number in it is invented.
"""

import hashlib
import sqlite3

import pytest

from custom_components.vimar_intercom import plant_config as pc

GROUP = "21"


def _build(
    phonebook=None, actuators=None, rules=None, icons=None, system=None,
    *, with_rules_table=True,
) -> bytes:
    """Serialise a phonebook database with the given rows."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE PHONEBOOK (ID INTEGER PRIMARY KEY, GID INTEGER, "
        "TYPE TEXT, NAME TEXT, NAME_2 TEXT, NAME_EXT TEXT, AUTO INTEGER, "
        "BU INTEGER, ST INTEGER, ENABLE INTEGER, GATE INTEGER, "
        "GATE_2 INTEGER, LIGHT INTEGER, MID INTEGER, ZONE INTEGER)")
    con.execute(
        "CREATE TABLE ACTUATOR_LIST (ID INTEGER PRIMARY KEY, NAME TEXT, "
        "GID_PE INTEGER, ATT_ID INTEGER, MSG TEXT, DTMF TEXT, ICON INTEGER, "
        "CMD TEXT)")
    if with_rules_table:
        con.execute(
            "CREATE TABLE ACTUATOR_RULES (ID INTEGER PRIMARY KEY, "
            "ACTUATOR_ID INTEGER, GA_GID INTEGER)")
    con.execute("CREATE TABLE ICON_LIST (ID INTEGER PRIMARY KEY, NAME TEXT)")
    con.execute(
        "CREATE TABLE SYSTEM (ID INTEGER PRIMARY KEY, PARAM TEXT, VALUE TEXT)")
    for gid, typ, name, auto, bu in phonebook or []:
        con.execute(
            "INSERT INTO PHONEBOOK (GID, TYPE, NAME, AUTO, BU, ENABLE) "
            "VALUES (?, ?, ?, ?, ?, 1)", (gid, typ, name, auto, bu))
    for aid, name, gid_pe, att_id, msg, icon in actuators or []:
        con.execute(
            "INSERT INTO ACTUATOR_LIST (ID, NAME, GID_PE, ATT_ID, MSG, ICON) "
            "VALUES (?, ?, ?, ?, ?, ?)", (aid, name, gid_pe, att_id, msg, icon))
    for actuator_id, ga in rules or []:
        con.execute(
            "INSERT INTO ACTUATOR_RULES (ACTUATOR_ID, GA_GID) VALUES (?, ?)",
            (actuator_id, ga))
    for iid, name in icons or []:
        con.execute("INSERT INTO ICON_LIST (ID, NAME) VALUES (?, ?)", (iid, name))
    for param, value in system or []:
        con.execute(
            "INSERT INTO SYSTEM (PARAM, VALUE) VALUES (?, ?)", (param, value))
    con.commit()
    data = con.serialize()
    con.close()
    return bytes(data)


# The shape of a real two-entrance house: an outdoor panel that the
# apartment group auto-switches to, a relay, an indoor panel, and the
# actuators the installer named.
PHONEBOOK = [
    (55001, "PE", "Front gate", None, 1),
    (45001, "RELE", "Relay 1", None, 1),
    (21, "GA", "21", 55001, 1),
    (55002, "PE", "Lobby", None, 1),
    # Another building of the same plant, and a neighbour's apartment.
    (55009, "PE", "Other building", None, 2),
    (22, "GA", "Neighbour", 55009, 2),
]
ICONS = [(1, "DOOR"), (2, "LIGHT"), (3, "SWITCH")]
ACTUATORS = [
    (1, "Main gate", 55001, 1, "OPEN_2F", 1),
    (2, "Garden light", 55001, 2, "AUX6", 2),
    (3, "Garage", 55001, 3, "AUX7", 3),
    (4, "Relay 1", 45001, 4, "OPEN_2F", 1),
    (5, "Lobby door", 55002, 5, "OPEN_2F", 1),
    (6, "Neighbour gate", 55009, 6, "OPEN_2F", 1),
]
RULES = [(1, 21), (2, 21), (3, 21), (4, 21), (5, 21), (6, 22)]
SYSTEM = [("MAGIC_APT_INTERCOM", "30021"), ("VM_PREFIX", "8"),
          ("MAX_TVCC_TIME", "60")]


def _plant(data: bytes | None = None, group: str = GROUP) -> pc.PlantConfig:
    data = data if data is not None else _build(
        PHONEBOOK, ACTUATORS, RULES, ICONS, SYSTEM)
    return pc.parse_phonebook(
        data, group=group, version=hashlib.md5(data).hexdigest())


# ─── panels ──────────────────────────────────────────────────────────

def test_entrance_panels_of_our_building_are_found_auto_panel_first():
    plant = _plant()
    assert plant.panels == (
        pc.Panel("55001", "Front gate"),
        pc.Panel("55002", "Lobby"),
    )


def test_our_apartment_group_and_its_auto_panel_are_recorded():
    plant = _plant()
    assert plant.group == pc.Group("21", "21", "55001")
    assert plant.default_panel == "55001"


def test_the_auto_panel_leads_even_when_it_has_the_higher_extension():
    data = _build([
        (55001, "PE", "Side", None, 1),
        (55002, "PE", "Main", None, 1),
        (21, "GA", "21", 55002, 1),
    ])
    assert [p.address for p in _plant(data).panels] == ["55002", "55001"]


def test_panels_of_the_global_building_are_always_included():
    data = _build([
        (21, "GA", "21", None, 1),
        (55001, "PE", "Ours", None, 1),
        (55005, "PE", "Shared gate", None, 911),
        (55009, "PE", "Elsewhere", None, 2),
    ])
    assert [p.address for p in _plant(data).panels] == ["55001", "55005"]


def test_every_panel_is_kept_when_our_group_is_not_in_the_phonebook():
    """The app would show only the global building's panels; with our
    group missing the building filter has nothing to go on, and every
    entrance is a better guess than none."""
    data = _build([(55001, "PE", "A", None, 1), (55009, "PE", "B", None, 2)])
    plant = _plant(data, group="99")
    assert [p.address for p in plant.panels] == ["55001", "55009"]
    assert plant.group is None


def test_an_unnamed_panel_gets_a_generic_name():
    data = _build([(55003, "PE", "  ", None, None)])
    assert _plant(data).panels == (pc.Panel("55003", "Panel 55003"),)


def test_names_are_cleaned_of_control_characters_and_trimmed():
    data = _build([(55003, "PE", "Front\r\n\tgate " + "x" * 100, None, None)])
    name = _plant(data).panels[0].name
    assert "\n" not in name and "\t" not in name
    assert name.startswith("Front gate ")
    assert len(name) <= pc.MAX_NAME_LENGTH


def test_neighbouring_apartments_are_not_kept():
    """A block of flats' phonebook names every flat. Only our own group
    is kept: the rest are other people's names, and nothing here needs
    them."""
    plant = _plant()
    assert "Neighbour" not in repr(plant)


# ─── actuators ───────────────────────────────────────────────────────

def test_actuators_granted_to_our_group_are_found():
    plant = _plant()
    assert plant.actuators == (
        pc.Actuator("55001", "OPEN_2F", "Main gate", "DOOR"),
        pc.Actuator("55001", "AUX6", "Garden light", "LIGHT"),
        pc.Actuator("55001", "AUX7", "Garage", "SWITCH"),
        pc.Actuator("45001", "OPEN_2F", "Relay 1", "DOOR"),
        pc.Actuator("55002", "OPEN_2F", "Lobby door", "DOOR"),
    )


def test_door_commands_are_doors_and_aux_commands_are_not():
    by_name = {a.name: a for a in _plant().actuators}
    assert by_name["Main gate"].is_door
    assert by_name["Relay 1"].is_door
    assert not by_name["Garden light"].is_door


def test_an_actuator_without_its_own_attuator_id_goes_to_the_auto_panel():
    """The SDK's second query branch: ATT_ID NULL means GID_PE names a
    phonebook row, and the command goes to that row's AUTO panel."""
    data = _build(
        PHONEBOOK,
        [(7, "Side door", 21, None, "OPEN_1F", 1)],
        [(7, 21)], ICONS)
    assert _plant(data).actuators == (
        pc.Actuator("55001", "OPEN_1F", "Side door", "DOOR"),)


def test_an_actuator_with_no_resolvable_target_is_dropped():
    data = _build(
        [(21, "GA", "21", None, 1)],
        [(7, "Nowhere", 21, None, "OPEN_1F", 1)],
        [(7, 21)], ICONS)
    assert _plant(data).actuators == ()


def test_a_command_that_is_unsafe_on_the_wire_is_dropped():
    data = _build(
        PHONEBOOK,
        [(1, "Evil", 55001, 1, "OPEN_2F\r\nBYE", 1),
         (2, "Empty", 55001, 2, "", 1),
         (3, "Fine", 55001, 3, "OPEN_2F", 1)],
        [(1, 21), (2, 21), (3, 21)], ICONS)
    assert [a.name for a in _plant(data).actuators] == ["Fine"]


def test_a_missing_icon_is_not_fatal():
    data = _build(PHONEBOOK, [(1, "Main gate", 55001, 1, "OPEN_2F", 42)],
                  [(1, 21)], ICONS)
    assert _plant(data).actuators[0].icon is None


def test_duplicate_target_and_command_keep_the_first():
    data = _build(
        PHONEBOOK,
        [(1, "First", 55001, 1, "OPEN_2F", 1),
         (2, "Second", 55001, 2, "OPEN_2F", 1)],
        [(1, 21), (2, 21)], ICONS)
    assert [a.name for a in _plant(data).actuators] == ["First"]


def test_no_rules_table_means_no_actuators_as_in_the_app():
    data = _build(PHONEBOOK, ACTUATORS, icons=ICONS, with_rules_table=False)
    assert _plant(data).actuators == ()


# ─── SYSTEM parameters ───────────────────────────────────────────────

def test_system_parameters_are_kept_for_the_next_phase():
    plant = _plant()
    assert plant.apartment_intercom == "30021"
    assert plant.vm_prefix == "8"
    assert plant.system_param("MAX_TVCC_TIME") == "60"
    assert plant.system_param("NOT_THERE") is None


# ─── integrity ───────────────────────────────────────────────────────

def test_a_download_that_does_not_match_its_version_is_rejected():
    """The SDK only accepts a phonebook whose MD5 is the version the
    indoor unit announced; a truncated or substituted file is not one."""
    data = _build(PHONEBOOK)
    with pytest.raises(ValueError, match="version"):
        pc.parse_phonebook(data, group=GROUP, version="0" * 32)


def test_the_version_comparison_ignores_case():
    data = _build(PHONEBOOK)
    plant = pc.parse_phonebook(
        data, group=GROUP, version=hashlib.md5(data).hexdigest().upper())
    assert plant.version == hashlib.md5(data).hexdigest()


def test_something_that_is_not_sqlite_is_rejected():
    data = b"<html>maintenance</html>"
    with pytest.raises(ValueError):
        pc.parse_phonebook(
            data, group=GROUP, version=hashlib.md5(data).hexdigest())


def test_a_database_without_a_phonebook_table_is_rejected():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE OTHER (X)")
    data = bytes(con.serialize())
    with pytest.raises(ValueError):
        pc.parse_phonebook(
            data, group=GROUP, version=hashlib.md5(data).hexdigest())


def test_an_oversized_download_is_rejected():
    data = b"SQLite format 3\x00" + b"\x00" * pc.MAX_PHONEBOOK_BYTES
    with pytest.raises(ValueError):
        pc.parse_phonebook(data, group=GROUP, version=hashlib.md5(data).hexdigest())


# ─── persistence ─────────────────────────────────────────────────────

def test_a_plant_round_trips_through_its_stored_form():
    plant = _plant()
    assert pc.PlantConfig.from_dict(plant.to_dict()) == plant


def test_the_stored_form_is_plain_json_types():
    import json
    json.dumps(_plant().to_dict())


@pytest.mark.parametrize("bad", [
    None, [], {"version": "x"},
    {"version": "x", "panels": [{"address": "5\r\n5", "name": "x"}],
     "actuators": [], "system": {}},
    {"version": "x", "panels": [],
     "actuators": [{"target": "1", "command": "OPEN 2F", "name": "x"}],
     "system": {}},
])
def test_a_corrupt_stored_form_is_rejected(bad):
    with pytest.raises(ValueError):
        pc.PlantConfig.from_dict(bad)


def test_entities_equal_ignores_version_and_system_parameters():
    plant = _plant()
    other = pc.PlantConfig(
        version="different", group=plant.group, panels=plant.panels,
        actuators=plant.actuators, system=())
    assert plant.entities_equal(other)
    renamed = pc.PlantConfig(
        version=plant.version, group=plant.group,
        panels=(pc.Panel("55001", "Renamed"),) + plant.panels[1:],
        actuators=plant.actuators, system=plant.system)
    assert not plant.entities_equal(renamed)
