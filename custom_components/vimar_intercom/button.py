"""Button platform for Vimar Intercom."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_INTERCOM_ROLE,
    ATTR_PANEL,
    ATTR_PANEL_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    ROLE_ACTUATOR,
    ROLE_ANSWER,
    ROLE_CALL,
    ROLE_HANGUP,
    ROLE_OPEN,
    ROLE_RECONNECT,
)
from .entity_plan import ButtonPlan

# The phonebook's actuator icons, as Home Assistant icons.
_ACTUATOR_ICONS = {
    "DOOR": "mdi:gate-open",
    "LIGHT": "mdi:lightbulb-on-outline",
    "SWITCH": "mdi:electric-switch",
}

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fixed buttons, a call and a door button per panel, and
    a button per non-door actuator of the phonebook."""
    data = hass.data[DOMAIN][entry.entry_id]
    hub, plan = data["hub"], data["plan"]
    entities: list[ButtonEntity] = [
        VimarAnswerButton(hub, entry.entry_id),
        VimarHangupButton(hub, entry.entry_id),
        VimarReconnectButton(hub, entry.entry_id),
    ]
    for panel in plan.panels:
        entities.append(VimarCallButton(hub, entry.entry_id, panel))
        entities.append(VimarDoorButton(hub, entry.entry_id, panel))
    for actuator in plan.actuator_buttons:
        entities.append(VimarActuatorButton(hub, entry.entry_id, actuator))
    async_add_entities(entities)


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarButtonBase(ButtonEntity):
    """Common wiring for the intercom buttons."""

    _attr_has_entity_name = True
    # What the button does, as the dashboard card reads it (see const.py).
    _role: str

    def __init__(self, hub, entry_id: str, unique_suffix: str) -> None:
        """Attach the button to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry_id)
        self._attr_extra_state_attributes = {ATTR_INTERCOM_ROLE: self._role}

    def _belongs_to(self, panel) -> None:
        """Name the panel this button acts on, for the dashboard card.

        The card pairs a panel's call and open buttons and labels its
        chip with these; the entity name alone carries neither reliably,
        since it is the installer's and the user can rename it.
        """
        self._attr_extra_state_attributes[ATTR_PANEL] = panel.address
        self._attr_extra_state_attributes[ATTR_PANEL_NAME] = panel.name


class VimarCallButton(VimarButtonBase):
    """Call one entrance panel."""

    _attr_translation_key = "call"
    _attr_icon = "mdi:phone-outgoing"
    _role = ROLE_CALL

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button calls."""
        super().__init__(hub, entry_id, f"call_{panel.address}")
        self._panel = panel
        self._belongs_to(panel)
        self._attr_name = f"Call {panel.name}"

    async def async_press(self) -> None:
        """Place the call.

        Raising is what puts the reason in front of the user: a button
        press that only logs looks identical to one that worked.
        """
        ok, msg = await self._hub.async_call(target=self._panel.address)
        if not ok:
            _LOGGER.error("Call to %s failed: %s", self._panel.address, msg)
            raise HomeAssistantError(msg)


class VimarDoorButton(VimarButtonBase):
    """Open the door of one entrance panel."""

    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"
    _role = ROLE_OPEN

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button opens."""
        super().__init__(hub, entry_id, f"door_{panel.address}")
        self._panel = panel
        self._belongs_to(panel)
        self._attr_name = f"Open {panel.name}"

    async def async_press(self) -> None:
        """Send the door command."""
        ok, msg = await self._hub.async_door(target=self._panel.address)
        if not ok:
            _LOGGER.error("Opening %s failed: %s", self._panel.address, msg)
            raise HomeAssistantError(msg)


class VimarActuatorButton(VimarButtonBase):
    """Trigger one actuator of the plant: an AUX output, a light, a relay.

    Pressing it sends the actuator's command to its target with
    `Panda: command`, which is all the official app does for it too.
    """

    _attr_icon = "mdi:gesture-tap-button"
    _role = ROLE_ACTUATOR

    def __init__(self, hub, entry_id: str, plan: ButtonPlan) -> None:
        """Remember what this button sends, and to whom."""
        super().__init__(hub, entry_id, plan.unique_suffix)
        self._plan = plan
        self._attr_name = plan.name
        if plan.icon in _ACTUATOR_ICONS:
            self._attr_icon = _ACTUATOR_ICONS[plan.icon]

    async def async_press(self) -> None:
        """Send the command."""
        ok, msg = await self._hub.async_door(
            target=self._plan.target, command=self._plan.command)
        if not ok:
            _LOGGER.error("%s failed: %s", self._plan.name, msg)
            raise HomeAssistantError(msg)


class VimarAnswerButton(VimarButtonBase):
    """Answer an incoming intercom call."""

    _attr_translation_key = "answer"
    _attr_icon = "mdi:phone-incoming"
    _role = ROLE_ANSWER

    def __init__(self, hub, entry_id: str) -> None:
        """Create the answer button."""
        super().__init__(hub, entry_id, "answer")

    async def async_press(self) -> None:
        """Answer the pending call."""
        ok, msg = await self._hub.async_answer()
        if not ok:
            _LOGGER.error("Answer failed: %s", msg)
            raise HomeAssistantError(msg)


class VimarHangupButton(VimarButtonBase):
    """End the current call."""

    _attr_translation_key = "hang_up"
    _attr_icon = "mdi:phone-hangup"
    _role = ROLE_HANGUP

    def __init__(self, hub, entry_id: str) -> None:
        """Create the hang-up button."""
        super().__init__(hub, entry_id, "hangup")

    async def async_press(self) -> None:
        """Send BYE."""
        await self._hub.async_hangup()


class VimarReconnectButton(VimarButtonBase):
    """Rebuild the SIP connection without restarting Home Assistant."""

    _attr_translation_key = "reconnect"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG
    _role = ROLE_RECONNECT

    def __init__(self, hub, entry_id: str) -> None:
        """Create the reconnect button."""
        super().__init__(hub, entry_id, "reconnect")

    async def async_press(self) -> None:
        """Drop the connection so the supervisor rebuilds it."""
        await self._hub.async_reconnect()
