"""Charge mode select for the Nexxtender BLE integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NexxtenderConfigEntry
from . import protocol as p, tariff
from .entity import NexxtenderEntity

MODE = SelectEntityDescription(
    key="mode",
    translation_key="mode",
    icon="mdi:ev-station",
    options=p.MODES,
)


TARIFF = SelectEntityDescription(
    key="tariff_profile",
    translation_key="tariff_profile",
    icon="mdi:clock-time-ten-outline",
    entity_category=EntityCategory.CONFIG,
    options=tariff.PROFILES,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NexxtenderModeSelect(coordinator, MODE),
            NexxtenderTariffSelect(coordinator, TARIFF),
        ]
    )


class NexxtenderModeSelect(NexxtenderEntity, SelectEntity):
    """Eco/Max and Private/Open mode, stored in the charger configuration."""

    @property
    def available(self) -> bool:
        return super().available and self.current_option is not None

    @property
    def current_option(self) -> str | None:
        cfg = self.data.config
        if cfg is not None and cfg.mode is not None:
            return cfg.mode
        if self.data.cbor is not None:
            return p.cbor_mode(self.data.cbor)
        return None

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        await self.coordinator.async_run(lambda: client.set_config(mode=option))


class NexxtenderTariffSelect(NexxtenderEntity, SelectEntity):
    """Which off-peak calendar smart charging follows."""

    _available_when_disconnected = True

    @property
    def current_option(self) -> str:
        return self.coordinator.smart.profile

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.smart.async_set_profile(option)
