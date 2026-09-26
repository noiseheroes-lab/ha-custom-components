"""The plant's own description of itself: panels, actuators, names.

The official app does not ask the user which entrance panels exist or
what the door relays are called. It downloads the plant's *phonebook*
("rubrica") — an SQLite database the installer's configuration produces
and the Vimar cloud serves — and reads the panels and actuators out of
it, with the names set on the indoor unit. This module does the reading
part of that: bytes of the database in, a frozen `PlantConfig` out.

Nothing here imports Home Assistant or does network I/O, so it is unit
tested directly against a database built in the tests. It is blocking
(sqlite3), and the integration runs it in the executor.

Tables and columns are the ones the SDK reads (`db/TABLE_*.java`,
`db/QueriesKt.java`):

- `PHONEBOOK` — one row per addressable thing. `GID` is its SIP
  extension, `TYPE` its role: `PE` an entrance panel, `RELE` a relay,
  `GA` an apartment group, whose `AUTO` names the panel the indoor unit
  auto-switches to (the house's main entrance). `BU` is the building.
- `ACTUATOR_LIST` — what can be triggered: `NAME`, `GID_PE` (who
  receives the command), `ATT_ID`, `MSG` (the command body, `OPEN_2F`,
  `AUX6`...), `ICON`.
- `ACTUATOR_RULES` — which apartment group may use which actuator.
- `ICON_LIST` — icon names (`DOOR`, `LIGHT`, `SWITCH`).
- `SYSTEM` — `PARAM`/`VALUE` pairs, such as the apartment's intercom
  address (`MAGIC_APT_INTERCOM`) and the voicemail prefix (`VM_PREFIX`).

The selection mirrors the SDK's queries so this integration offers what
the app offers, no more:

- panels: `QUERY_FILTER_GET_BUILDING_LIST_FOR_GID_v3` — every `PE` in
  our building or in the global building `911`;
- actuators: `QUERY_FILTER_GET_ACTUATORS_LIST_FOR_GID` — every actuator
  `ACTUATOR_RULES` grants our group. With `ATT_ID` set the command goes
  to `GID_PE`; with `ATT_ID` empty, `GID_PE` names a phonebook row and
  the command goes to that row's `AUTO` panel instead.

The joins are done in Python rather than in SQL. The tables are tiny,
and it lets a missing optional table (icons) or column degrade instead
of failing the whole query the way the SDK's single SQL statement does.
The icon join in particular is an inner join in the SDK, which would
drop an actuator whose icon row is missing; here the icon is cosmetic
and such an actuator is kept.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .runtime import valid_door_command, valid_sip_token

# The phonebook of a large block of flats is a few hundred kilobytes.
# The ceiling only exists so a misbehaving server cannot make the
# integration hold an arbitrarily large blob in memory.
MAX_PHONEBOOK_BYTES = 8 * 1024 * 1024
MAX_NAME_LENGTH = 64

_SQLITE_MAGIC = b"SQLite format 3\x00"
GLOBAL_BUILDING = "911"
TYPE_PANEL = "PE"
TYPE_GROUP = "GA"
# The commands that release a lock. Everything else an actuator sends —
# AUX6, AUX7, a light — is a pulse with no lock semantics.
DOOR_COMMAND_PREFIX = "OPEN_"

PARAM_APARTMENT_INTERCOM = "MAGIC_APT_INTERCOM"
PARAM_VM_PREFIX = "VM_PREFIX"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Panel:
    """An entrance panel, addressed by its SIP extension."""

    address: str
    name: str


@dataclass(frozen=True)
class Group:
    """Our apartment group, and the panel it auto-switches to."""

    address: str
    name: str
    auto: str | None


@dataclass(frozen=True)
class Actuator:
    """One command the plant lets this apartment send.

    Triggering it is a SIP MESSAGE to `target` whose body is `command`,
    with `Panda: command` — exactly what the SDK's
    `sysMsgActuatorAction(gid_pe, msg)` sends.
    """

    target: str
    command: str
    name: str
    icon: str | None = None

    @property
    def is_door(self) -> bool:
        """True for a lock release (OPEN_*), False for any other pulse."""
        return self.command.startswith(DOOR_COMMAND_PREFIX)


@dataclass(frozen=True)
class PlantConfig:
    """What the phonebook says this apartment can see and do.

    `version` is the phonebook version the indoor unit announced — the
    MD5 of the database — so a later status reply can be compared with
    it before anything is downloaded. It is not a secret; the download
    token is, and is never part of this object.
    """

    version: str
    group: Group | None
    panels: tuple[Panel, ...]
    actuators: tuple[Actuator, ...]
    system: tuple[tuple[str, str], ...] = field(default=())

    @property
    def default_panel(self) -> str | None:
        """The panel a call names no target for: our group's auto panel."""
        if self.group and self.group.auto:
            return self.group.auto
        return self.panels[0].address if self.panels else None

    def system_param(self, name: str) -> str | None:
        """One `SYSTEM` table value, or None."""
        for param, value in self.system:
            if param == name:
                return value
        return None

    @property
    def apartment_intercom(self) -> str | None:
        """The apartment's intercom address (the SDK's "SGA")."""
        return self.system_param(PARAM_APARTMENT_INTERCOM)

    @property
    def vm_prefix(self) -> str | None:
        """The prefix dialled to play back a video message."""
        return self.system_param(PARAM_VM_PREFIX)

    def entities_equal(self, other: PlantConfig | None) -> bool:
        """True if `other` would produce exactly the same entities.

        A new phonebook version that only changed something this
        integration does not show — a neighbour's name, a SYSTEM value —
        must not reload the entry and drop the SIP registration for it.
        """
        return (other is not None
                and self.group == other.group
                and self.panels == other.panels
                and self.actuators == other.actuators)

    def to_dict(self) -> dict[str, Any]:
        """The JSON-safe form persisted between restarts."""
        return {
            "version": self.version,
            "group": (None if self.group is None else {
                "address": self.group.address,
                "name": self.group.name,
                "auto": self.group.auto,
            }),
            "panels": [{"address": p.address, "name": p.name}
                       for p in self.panels],
            "actuators": [{"target": a.target, "command": a.command,
                           "name": a.name, "icon": a.icon}
                          for a in self.actuators],
            "system": dict(self.system),
        }

    @classmethod
    def from_dict(cls, data: Any) -> PlantConfig:
        """Rebuild a stored PlantConfig, re-checking every field.

        The store lives in `.storage` and can be edited by hand or left
        half-written by a crash. Whatever reaches a SIP URI or a MESSAGE
        body is checked again here, exactly as it is on download; a bad
        store raises ValueError and the integration falls back to the
        options.
        """
        if not isinstance(data, Mapping):
            raise ValueError("the stored plant configuration is not a mapping")
        try:
            version = str(data["version"])
            raw_group = data.get("group")
            group = None
            if raw_group is not None:
                group = Group(
                    _checked_extension(raw_group["address"]),
                    _clean_name(raw_group.get("name"), raw_group["address"]),
                    _extension_or_none(raw_group.get("auto")))
            panels = tuple(
                Panel(_checked_extension(p["address"]),
                      _clean_name(p.get("name"), f"Panel {p['address']}"))
                for p in data["panels"])
            actuators = tuple(
                Actuator(_checked_extension(a["target"]),
                         _checked_command(a["command"]),
                         _clean_name(a.get("name"), a["command"]),
                         _icon_or_none(a.get("icon")))
                for a in data["actuators"])
            system = tuple(
                (str(k), str(v)) for k, v in dict(data["system"]).items())
        except (KeyError, TypeError, AttributeError) as err:
            raise ValueError(
                "the stored plant configuration is incomplete") from err
        return cls(version=version, group=group, panels=panels,
                   actuators=actuators, system=system)


def _clean_name(value: Any, fallback: str) -> str:
    """A display name: printable, single-spaced, bounded, never empty."""
    text = "" if value is None else str(value)
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    return (text or fallback)[:MAX_NAME_LENGTH].strip()


def _extension(value: Any) -> str | None:
    """A SIP extension from a phonebook cell, or None.

    The cells are INTEGER in practice, but SQLite is typeless and a
    string of digits must read the same. Zero and negatives are the
    SDK's "none".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text.isdigit() and int(text) > 0:
        return str(int(text))
    return None


def _checked_extension(value: Any) -> str:
    text = _extension(value)
    if text is None or not valid_sip_token(text):
        raise ValueError("a stored extension is not a SIP extension")
    return text


def _extension_or_none(value: Any) -> str | None:
    return None if value is None else _checked_extension(value)


def _checked_command(value: Any) -> str:
    text = "" if value is None else str(value)
    if not valid_door_command(text):
        raise ValueError("a stored command is not a valid command")
    return text


def _icon_or_none(value: Any) -> str | None:
    return None if value is None else _clean_name(value, "")[:16] or None


def open_sqlite_bytes(data: bytes) -> sqlite3.Connection:
    """Open downloaded SQLite bytes as an in-memory database.

    Shared with `voicemail.py`: the mailbox arrives the same way, as the
    bytes of a whole database file.
    """
    con = sqlite3.connect(":memory:")
    if hasattr(con, "deserialize"):
        con.deserialize(data)
        return con
    # SQLite built without serialize support: go through a temporary
    # file, which is gone again as soon as the data is in memory.
    con.close()
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        tmp.write(data)
        tmp.flush()
        disk = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        con = sqlite3.connect(":memory:")
        disk.backup(con)
        disk.close()
    return con


def table_rows(con: sqlite3.Connection, table: str) -> list[dict[str, Any]] | None:
    """Every row of `table` as a dict, or None if the table is missing."""
    # SQLite table names are case-insensitive, and the devices do not all
    # spell them the way the app's queries do.
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = ? COLLATE NOCASE",
        (table,)).fetchone()
    if not exists:
        return None
    cursor = con.execute(f'SELECT * FROM "{exists[0]}"')  # noqa: S608 - a name read from the schema
    columns = [d[0].upper() for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def parse_phonebook(data: bytes, *, group: str, version: str) -> PlantConfig:
    """Read a downloaded phonebook for apartment group `group`.

    Raises ValueError when the bytes are not the phonebook `version`
    names — the SDK only accepts a file whose MD5 is that version — or
    not a phonebook at all.
    """
    if len(data) > MAX_PHONEBOOK_BYTES:
        raise ValueError("the phonebook is larger than any real plant's")
    digest = hashlib.md5(data).hexdigest()
    if digest != version.strip().lower():
        raise ValueError("the phonebook does not match its announced version")
    if not data.startswith(_SQLITE_MAGIC):
        raise ValueError("the phonebook is not an SQLite database")

    try:
        con = open_sqlite_bytes(data)
    except sqlite3.Error as err:
        raise ValueError("the phonebook database cannot be opened") from err
    try:
        phonebook = table_rows(con, "PHONEBOOK")
        if phonebook is None:
            raise ValueError("the database has no PHONEBOOK table")
        actuator_rows = table_rows(con, "ACTUATOR_LIST") or []
        rule_rows = table_rows(con, "ACTUATOR_RULES")
        icon_rows = table_rows(con, "ICON_LIST") or []
        system_rows = table_rows(con, "SYSTEM") or []
    except sqlite3.Error as err:
        raise ValueError("the phonebook database is unreadable") from err
    finally:
        con.close()

    our_group = _extension(group)
    group_row = next(
        (row for row in phonebook
         if _extension(row.get("GID")) == our_group
         and row.get("TYPE") == TYPE_GROUP), None)
    ours = None
    if group_row is not None and our_group is not None:
        ours = Group(our_group, _clean_name(group_row.get("NAME"), our_group),
                     _extension(group_row.get("AUTO")))

    panels = _panels(phonebook, our_group, ours)
    actuators = _actuators(
        phonebook, actuator_rows, rule_rows, icon_rows, our_group)

    system: dict[str, str] = {}
    for row in system_rows:
        param, value = row.get("PARAM"), row.get("VALUE")
        if param is None or value is None:
            continue
        system.setdefault(str(param), str(value))

    return PlantConfig(version=digest, group=ours, panels=panels,
                       actuators=actuators, system=tuple(system.items()))


def _panels(
    phonebook: list[dict[str, Any]], our_group: str | None, ours: Group | None,
) -> tuple[Panel, ...]:
    """The entrance panels of our building, our auto panel first."""
    buildings = {
        str(row.get("BU")) for row in phonebook
        if our_group is not None
        and _extension(row.get("GID")) == our_group
        and row.get("BU") is not None
    }
    found: dict[str, Panel] = {}
    for row in sorted(phonebook, key=lambda r: int(_extension(r.get("GID")) or 0)):
        if row.get("TYPE") != TYPE_PANEL:
            continue
        address = _extension(row.get("GID"))
        if address is None or address in found:
            continue
        # With our group absent there is no building to filter by; see
        # the module docstring for why every panel is then kept.
        if buildings and str(row.get("BU")) not in buildings | {GLOBAL_BUILDING}:
            continue
        name = row.get("NAME") or row.get("NAME_2")
        found[address] = Panel(address, _clean_name(name, f"Panel {address}"))
    panels = list(found.values())
    auto = ours.auto if ours else None
    panels.sort(key=lambda p: p.address != auto)
    return tuple(panels)


def _actuators(
    phonebook: list[dict[str, Any]],
    actuator_rows: list[dict[str, Any]],
    rule_rows: list[dict[str, Any]] | None,
    icon_rows: list[dict[str, Any]],
    our_group: str | None,
) -> tuple[Actuator, ...]:
    """The actuators ACTUATOR_RULES grants our group, resolved to a target."""
    if rule_rows is None or our_group is None:
        # No rules table: the SDK's query fails and the app offers no
        # actuators. Doing the same keeps this from exposing a command
        # the plant never granted this apartment.
        return ()
    granted = {
        row.get("ACTUATOR_ID") for row in rule_rows
        if _extension(row.get("GA_GID")) == our_group
    }
    icons = {row.get("ID"): row.get("NAME") for row in icon_rows}
    autos = {
        _extension(row.get("GID")): _extension(row.get("AUTO"))
        for row in phonebook if _extension(row.get("GID")) is not None
    }

    found: dict[tuple[str, str], Actuator] = {}
    for row in sorted(actuator_rows, key=lambda r: r.get("ID") or 0):
        if row.get("ID") not in granted:
            continue
        gid_pe = _extension(row.get("GID_PE"))
        target = gid_pe if row.get("ATT_ID") is not None else autos.get(gid_pe)
        command = "" if row.get("MSG") is None else str(row.get("MSG"))
        if target is None or not valid_sip_token(target):
            continue
        if not valid_door_command(command):
            continue
        key = (target, command)
        if key in found:
            continue
        icon = icons.get(row.get("ICON"))
        found[key] = Actuator(
            target, command, _clean_name(row.get("NAME"), command),
            _icon_or_none(icon))
    return tuple(found.values())
