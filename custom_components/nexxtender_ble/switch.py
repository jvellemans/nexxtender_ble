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


SMART_CHARGING = SwitchEntityDescription(
    key="smart_charging",
    translation_key="smart_charging",
    icon="mdi:home-lightning-bolt-outline",
)

CHARGE_NOW = SwitchEntityDescription(
    key="charge_now",
    translation_key="charge_now",
    icon="mdi:flash",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NexxtenderKeepConnectedSwitch(coordinator, KEEP_CONNECTED),
            NexxtenderSmartChargingSwitch(coordinator, SMART_CHARGING),
            NexxtenderChargeNowSwitch(coordinator, CHARGE_NOW),
        ]
    )


class NexxtenderKeepConnectedSwitch(NexxtenderEntity, SwitchEntity):
    _available_when_disconnected = True

    @property
    def is_on(self) -> bool:
        return self.coordinator.keep_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_keep_connected(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_keep_connected(False)


class NexxtenderSmartChargingSwitch(NexxtenderEntity, SwitchEntity):
    """Home Assistant decides when to charge (off-peak hours)."""

    _available_when_disconnected = True

    @property
    def is_on(self) -> bool:
        return self.coordinator.smart.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.smart.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.smart.async_set_enabled(False)


class NexxtenderChargeNowSwitch(NexxtenderEntity, SwitchEntity):
    """Charge right away, even in peak hours, until the car is unplugged."""

    _available_when_disconnected = True

    @property
    def is_on(self) -> bool:
        return self.coordinator.smart.charge_now

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.smart.async_set_charge_now(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.smart.async_set_charge_now(False)
