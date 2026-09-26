"""Sensor platform for Vimar Intercom — mailbox, video messages, missed calls."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_INTERCOM_ROLE,
    DOMAIN,
    MANUFACTURER,
    MAX_LISTED_VIDEO_MESSAGES,
    MODEL,
    ROLE_MAILBOX_USAGE,
    ROLE_MISSED_CALLS,
    ROLE_VIDEO_MESSAGES,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mailbox, video-message and missed-call sensors."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarMailboxUsageSensor(hub, entry.entry_id),
        VimarVideoMessagesSensor(hub, entry.entry_id),
        VimarMissedCallsSensor(hub, entry.entry_id),
    ])


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarFeatureSensor(SensorEntity):
    """Common wiring: attached to the device, updated by the hub."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _suffix: str
    _role: str

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the sensor to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{self._suffix}"
        self._attr_device_info = _device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        """Follow the hub's feature updates."""
        self._hub.register_update_callback(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the hub."""
        self._hub.unregister_update_callback(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class VimarMailboxUsageSensor(VimarFeatureSensor):
    """How full the answering machine's mailbox is, in percent."""

    _attr_translation_key = "mailbox_usage"
    _attr_icon = "mdi:email-multiple-outline"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _suffix = "mailbox_usage"
    _role = ROLE_MAILBOX_USAGE

    @property
    def available(self) -> bool:
        """True once the unit has said how large the mailbox is."""
        usage = self._hub.mailbox_usage
        return usage is not None and usage[1] > 0

    @property
    def native_value(self) -> int | None:
        """Messages stored as a share of the capacity."""
        usage = self._hub.mailbox_usage
        if usage is None or usage[1] <= 0:
            return None
        return min(100, round(100 * usage[0] / usage[1]))

    @property
    def extra_state_attributes(self) -> dict:
        """The count and the capacity behind the percentage."""
        usage = self._hub.mailbox_usage
        return {
            ATTR_INTERCOM_ROLE: self._role,
            "used": None if usage is None else usage[0],
            "capacity": None if usage is None else usage[1],
        }


class VimarVideoMessagesSensor(VimarFeatureSensor):
    """Unread video messages, with the mailbox listed in `messages`.

    The list is capped, newest first, and kept out of the recorder: it
    changes with every message and is only useful as it is now.
    """

    _attr_translation_key = "video_messages"
    _attr_icon = "mdi:message-video"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"messages"})
    _suffix = "video_messages"
    _role = ROLE_VIDEO_MESSAGES

    @property
    def available(self) -> bool:
        """True once the mailbox has been read from the indoor unit."""
        return self._hub.video_messages is not None

    @property
    def native_value(self) -> int | None:
        """How many messages have not been played or marked read."""
        messages = self._hub.video_messages
        if messages is None:
            return None
        return sum(1 for m in messages if not m.read)

    @property
    def extra_state_attributes(self) -> dict:
        """The messages, and how many there are in all."""
        messages = self._hub.video_messages or ()
        names = self._hub.panel_names
        return {
            ATTR_INTERCOM_ROLE: self._role,
            "total": len(messages),
            "messages": [m.as_attribute(names)
                         for m in messages[:MAX_LISTED_VIDEO_MESSAGES]],
        }


class VimarMissedCallsSensor(VimarFeatureSensor):
    """Missed calls since the count was last cleared, and the recent rings."""

    _attr_translation_key = "missed_calls"
    _attr_icon = "mdi:phone-missed"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"recent"})
    _suffix = "missed_calls"
    _role = ROLE_MISSED_CALLS

    @property
    def native_value(self) -> int:
        """The missed-call count."""
        return self._hub.call_log.missed_count

    @property
    def extra_state_attributes(self) -> dict:
        """The last rings, newest first, with how each one ended."""
        return {
            ATTR_INTERCOM_ROLE: self._role,
            "recent": self._hub.call_log.recent(),
        }
