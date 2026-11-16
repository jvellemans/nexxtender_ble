"""Base entity for the Nexxtender BLE integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .client import NexxtenderData
from .const import DOMAIN, MANUFACTURER
from .coordinator import NexxtenderCoordinator


class NexxtenderEntity(Entity):
    """Entity backed by the coordinator's push updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Entities that stay meaningful while disconnected override this.
    _available_when_disconnected = False

    def __init__(
        self, coordinator: NexxtenderCoordinator, description: EntityDescription
    ) -> None:
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        data = coordinator.client.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            name="Nexxtender",
            model=data.model or "Nexxtender Home",
        )

    @property
    def data(self) -> NexxtenderData:
        return self.coordinator.client.data

    @property
    def available(self) -> bool:
        return self._available_when_disconnected or self.coordinator.client.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))

    def _handle_update(self) -> None:
        self.async_write_ha_state()
