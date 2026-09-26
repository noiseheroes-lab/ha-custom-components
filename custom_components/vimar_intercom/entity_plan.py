"""Which entities an entry has, decided once, without Home Assistant.

The platforms used to derive their entities straight from the options.
With the plant's phonebook in the picture there are two sources, a
unique-ID scheme existing users depend on, and entities that must be
removed again when the installer deletes a relay. Deciding all of that
in one pure function keeps the platforms trivial and the whole policy
unit testable: the platforms create what the plan lists, and
`__init__.py` removes what the registry holds and the plan does not.

Unique IDs, all prefixed with the config entry ID:

- `call_<address>`, `door_<address>` — per entrance panel, as before.
  A phonebook panel keeps the ID the options-based panel with the same
  extension had; only its name changes.
- `lock` — the door lock. Without a phonebook it is the generic lock
  it always was. With one, it becomes the phonebook actuator that is
  the same door: the configured door command (OPEN_2F by default) on
  our apartment group's auto panel, or on the group itself. That is
  what the old lock opened — it sent that command to the group, and the
  indoor unit relays a group's command to its auto panel — so the
  user's lock, its entity ID and its automations carry on, now under
  the installer's name for it. If no actuator matches, the generic lock
  stays.
- `actuator_<target>_<command>` — every other actuator. Keyed on what
  it does rather than on the phonebook's row ID, which the installer's
  software is free to renumber.

Door-type actuators (OPEN_*) are locks, everything else (AUX6, AUX7, a
light) is a button. The SDK itself makes no such distinction — every
actuator is one MESSAGE, and the app shows them all as the same tile —
but in Home Assistant a lock is what a door release is expected to be:
it is what voice assistants and the lock card act on, and it asks for
confirmation where a button does not. An AUX output drives anything
from a stair light to a second gate, so claiming lock semantics for it
would be a guess; a button claims nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .plant_config import PlantConfig
from .runtime import PanelConfig, RuntimeConfig

LOCK_SUFFIX = "lock"
# The families of unique IDs this plan owns. Anything of this entry's
# with one of these prefixes that the plan no longer lists is stale.
# The fixed entities (camera, doorbell, sensors, the answer/hang-up/
# reconnect buttons) never match, so they are never touched.
DYNAMIC_PREFIXES = ("call_", "door_", "actuator_")


@dataclass(frozen=True)
class LockPlan:
    """One lock entity.

    `target` and `command` are None for the generic lock, which leaves
    the choice to `hub.async_door()` — the configured command to the
    relay group, or OPEN_CURRENT during a call — exactly as before. An
    actuator lock always sends its own command to its own target, the
    way the app does.
    """

    unique_suffix: str
    name: str | None
    target: str | None
    command: str | None


@dataclass(frozen=True)
class ButtonPlan:
    """One actuator button: `command` to `target`, nothing more."""

    unique_suffix: str
    name: str
    target: str
    command: str
    # The phonebook's icon name (DOOR, LIGHT, SWITCH), for the entity icon.
    icon: str | None = None


@dataclass(frozen=True)
class EntityPlan:
    """Every entity whose existence depends on the plant."""

    panels: tuple[PanelConfig, ...]
    locks: tuple[LockPlan, ...]
    actuator_buttons: tuple[ButtonPlan, ...]

    def dynamic_unique_ids(self, entry_id: str) -> set[str]:
        """The full unique IDs of the plant-dependent entities."""
        suffixes = [f"call_{p.address}" for p in self.panels]
        suffixes += [f"door_{p.address}" for p in self.panels]
        suffixes += [lock.unique_suffix for lock in self.locks]
        suffixes += [b.unique_suffix for b in self.actuator_buttons]
        return {f"{entry_id}_{s}" for s in suffixes}

    def stale_unique_ids(
        self, entry_id: str, registered: Iterable[str]
    ) -> list[str]:
        """The registered IDs of ours this plan no longer creates."""
        wanted = self.dynamic_unique_ids(entry_id)
        prefixes = tuple(f"{entry_id}_{p}" for p in DYNAMIC_PREFIXES)
        return [uid for uid in registered
                if uid.startswith(prefixes) and uid not in wanted]


def _actuator_suffix(target: str, command: str) -> str:
    return f"actuator_{target}_{command}"


def plan_entities(
    cfg: RuntimeConfig, plant: PlantConfig | None = None
) -> EntityPlan:
    """Decide the plant-dependent entities for this entry.

    `cfg.panels` already holds the phonebook's panels when there is a
    phonebook with any (see `runtime.build_runtime_config`), so panels
    come from there either way.
    """
    if plant is None or not plant.actuators:
        return EntityPlan(
            panels=cfg.panels,
            locks=(LockPlan(LOCK_SUFFIX, None, None, None),),
            actuator_buttons=())

    main_targets = {cfg.group_id}
    if plant.group is not None:
        main_targets.add(plant.group.address)
        if plant.group.auto:
            main_targets.add(plant.group.auto)
    main = next(
        (a for a in plant.actuators
         if a.is_door and a.command == cfg.door_command
         and a.target in main_targets),
        None)

    locks: list[LockPlan] = []
    if main is not None:
        locks.append(LockPlan(LOCK_SUFFIX, main.name, main.target, main.command))
    else:
        locks.append(LockPlan(LOCK_SUFFIX, None, None, None))
    buttons: list[ButtonPlan] = []
    for actuator in plant.actuators:
        if actuator is main:
            continue
        suffix = _actuator_suffix(actuator.target, actuator.command)
        if actuator.is_door:
            locks.append(LockPlan(
                suffix, actuator.name, actuator.target, actuator.command))
        else:
            buttons.append(ButtonPlan(
                suffix, actuator.name, actuator.target, actuator.command,
                actuator.icon))
    return EntityPlan(
        panels=cfg.panels, locks=tuple(locks), actuator_buttons=tuple(buttons))
