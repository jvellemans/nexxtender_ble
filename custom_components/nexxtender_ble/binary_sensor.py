"""Binary sensors for the Nexxtender BLE integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NexxtenderConfigEntry
from . import protocol as p
from .coordinator import NexxtenderCoordinator
from .entity import NexxtenderEntity


@dataclass(frozen=True, kw_only=True)
class NexxtenderBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[NexxtenderCoordinator], bool | None]
    always_available: bool = False


BINARY_SENSORS: tuple[NexxtenderBinaryDescription, ...] = (
    NexxtenderBinaryDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_available=True,
        value_fn=lambda c: c.client.connected,
    ),
    NexxtenderBinaryDescription(
        key="plugged",
        translation_key="plugged",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda c: None
        if c.client.data.basic is None
        else c.client.data.basic.status != p.STATUS_UNPLUGGED,
    ),
    NexxtenderBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda c: None
        if c.client.data.basic is None
        else c.client.data.basic.status == p.STATUS_CHARGING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NexxtenderBinarySensor(coordinator, desc) for desc in BINARY_SENSORS
    )


class NexxtenderBinarySensor(NexxtenderEntity, BinarySensorEntity):
    entity_description: NexxtenderBinaryDescription

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            return True
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self):
        key = self.entity_description.key
        if key == "connected" and self.coordinator.last_error:
            return {"last_error": self.coordinator.last_error}
        return None
