"""Switch to hold or release the Bluetooth connection.

The charger accepts a single BLE connection. Turn this off to let the phone
app (Nexxtmove) connect, then turn it back on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NexxtenderConfigEntry
from .entity import NexxtenderEntity

KEEP_CONNECTED = SwitchEntityDescription(
    key="keep_connected",
    translation_key="keep_connected",
    icon="mdi:bluetooth-connect",
    entity_category=EntityCategory.CONFIG,
)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([NexxtenderKeepConnectedSwitch(coordinator, KEEP_CONNECTED)])


class NexxtenderKeepConnectedSwitch(NexxtenderEntity, SwitchEntity):
    _available_when_disconnected = True

    @property
    def is_on(self) -> bool:
        return self.coordinator.keep_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_keep_connected(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_keep_connected(False)
