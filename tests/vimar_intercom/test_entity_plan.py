"""Tests for which entities exist, and under which unique IDs.

The unique ID is what ties an entity to its registry entry — its
entity_id, its area, the automations that name it. An existing user's
panel 55001 buttons and door lock must keep theirs when the phonebook
starts describing the same things, and the fallback without a phonebook
must be exactly what the integration always created.
"""

from custom_components.vimar_intercom import entity_plan as ep
from custom_components.vimar_intercom import plant_config as pc
from custom_components.vimar_intercom import runtime

ENTRY = "01JTESTENTRY"
QR_FIELDS = {"ID": "60901", "PWD": "examplepassword",
             "CDOMAIN": "abc123.FFFFFFFFFF.example.invalid", "GID": "21"}


def _cfg(options=None, plant=None) -> runtime.RuntimeConfig:
    return runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), options or {}, plant)


PLANT = pc.PlantConfig(
    version="0" * 32,
    group=pc.Group("21", "21", "55001"),
    panels=(pc.Panel("55001", "Front gate"), pc.Panel("55002", "Lobby")),
    actuators=(
        pc.Actuator("55001", "OPEN_2F", "Main gate", "DOOR"),
        pc.Actuator("55001", "AUX6", "Garden light", "LIGHT"),
        pc.Actuator("55001", "AUX7", "Garage", "SWITCH"),
        pc.Actuator("45001", "OPEN_2F", "Relay 1", "DOOR"),
        pc.Actuator("55002", "OPEN_2F", "Lobby door", "DOOR"),
    ),
)


def _ids(plan: ep.EntityPlan) -> set[str]:
    return plan.dynamic_unique_ids(ENTRY)


# ─── without a phonebook: exactly what was always created ────────────

def test_the_fallback_is_the_options_panels_and_the_generic_lock():
    plan = ep.plan_entities(_cfg())
    assert _ids(plan) == {
        f"{ENTRY}_call_55001", f"{ENTRY}_door_55001", f"{ENTRY}_lock"}
    assert plan.locks == (ep.LockPlan("lock", None, None, None),)
    assert plan.actuator_buttons == ()


def test_the_fallback_honours_the_configured_panel_list():
    plan = ep.plan_entities(_cfg({"panels": "55001:A, 55003:B"}))
    assert [p.address for p in plan.panels] == ["55001", "55003"]


# ─── with a phonebook ────────────────────────────────────────────────

def test_panel_buttons_keep_their_unique_ids_and_take_the_phonebook_name():
    plan = ep.plan_entities(_cfg(plant=PLANT), PLANT)
    assert [(p.address, p.name) for p in plan.panels] == [
        ("55001", "Front gate"), ("55002", "Lobby")]
    assert f"{ENTRY}_call_55001" in _ids(plan)
    assert f"{ENTRY}_door_55001" in _ids(plan)


def test_the_main_gate_keeps_the_old_lock_unique_id():
    """The old lock sent the configured door command (OPEN_2F) to the
    apartment group, which the indoor unit relays to its auto panel.
    The phonebook's OPEN_2F on that auto panel is the same door, so it
    inherits `<entry>_lock` rather than appearing as a second lock."""
    plan = ep.plan_entities(_cfg(plant=PLANT), PLANT)
    main = next(lock for lock in plan.locks if lock.unique_suffix == "lock")
    assert main == ep.LockPlan("lock", "Main gate", "55001", "OPEN_2F")


def test_other_door_actuators_are_locks_and_the_rest_are_buttons():
    plan = ep.plan_entities(_cfg(plant=PLANT), PLANT)
    assert [lock.unique_suffix for lock in plan.locks] == [
        "lock", "actuator_45001_OPEN_2F", "actuator_55002_OPEN_2F"]
    assert [(b.unique_suffix, b.name) for b in plan.actuator_buttons] == [
        ("actuator_55001_AUX6", "Garden light"),
        ("actuator_55001_AUX7", "Garage"),
    ]


def test_a_changed_door_command_moves_the_old_id_to_that_actuator():
    """With the door command set to OPEN_1F the old lock opened a
    different relay. It must follow that relay, not OPEN_2F."""
    plant = pc.PlantConfig(
        version="x", group=PLANT.group, panels=PLANT.panels,
        actuators=PLANT.actuators + (
            pc.Actuator("55001", "OPEN_1F", "Side door", "DOOR"),))
    plan = ep.plan_entities(_cfg({"door_command": "OPEN_1F"}, plant), plant)
    main = next(lock for lock in plan.locks if lock.unique_suffix == "lock")
    assert (main.name, main.command) == ("Side door", "OPEN_1F")
    assert any(lock.unique_suffix == "actuator_55001_OPEN_2F"
               for lock in plan.locks)


def test_an_actuator_addressed_to_the_group_itself_also_matches():
    plant = pc.PlantConfig(
        version="x", group=PLANT.group, panels=PLANT.panels,
        actuators=(pc.Actuator("21", "OPEN_2F", "Gate", "DOOR"),))
    plan = ep.plan_entities(_cfg(plant=plant), plant)
    assert plan.locks == (ep.LockPlan("lock", "Gate", "21", "OPEN_2F"),)


def test_without_a_matching_actuator_the_generic_lock_stays():
    plant = pc.PlantConfig(
        version="x", group=PLANT.group, panels=PLANT.panels,
        actuators=(pc.Actuator("55002", "OPEN_2F", "Lobby door", "DOOR"),))
    plan = ep.plan_entities(_cfg(plant=plant), plant)
    assert [lock.unique_suffix for lock in plan.locks] == [
        "lock", "actuator_55002_OPEN_2F"]
    assert plan.locks[0] == ep.LockPlan("lock", None, None, None)


def test_a_phonebook_with_no_panels_falls_back_to_the_options_panels():
    plant = pc.PlantConfig(version="x", group=None, panels=(), actuators=())
    plan = ep.plan_entities(_cfg(plant=plant), plant)
    assert [p.address for p in plan.panels] == ["55001"]


def test_stale_ids_are_those_of_ours_that_the_plan_no_longer_has():
    plan = ep.plan_entities(_cfg(plant=PLANT), PLANT)
    registered = [
        f"{ENTRY}_call_55001",          # still there
        f"{ENTRY}_call_55099",          # a panel that went away
        f"{ENTRY}_actuator_45002_AUX6",  # an actuator that went away
        f"{ENTRY}_camera",              # fixed entity: never ours to remove
        f"{ENTRY}_lock",                # always planned
        "someone_else_call_55099",      # not this entry's
    ]
    assert plan.stale_unique_ids(ENTRY, registered) == [
        f"{ENTRY}_call_55099", f"{ENTRY}_actuator_45002_AUX6"]
