"""Nexxtender Home EV charger over Bluetooth Low Energy."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import NexxtenderCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Entities that wrote to the charger, removed in 0.3.0 (read-only + start/stop).
REMOVED_ENTITIES: tuple[tuple[str, str], ...] = (
    ("number", "max_car_charging_speed"),
    ("number", "max_available_capacity"),
    ("number", "offloading_minimum"),
    ("number", "peak_consumption_limit"),
    ("select", "mode"),
    ("select", "tariff_profile"),
    ("switch", "smart_charging"),
    ("switch", "charge_now"),
    ("binary_sensor", "off_peak"),
    ("button", "start_charge_max"),
    ("button", "start_charge_eco"),
    ("button", "start_charge_auto"),
    ("button", "sync_time"),
)

type NexxtenderConfigEntry = ConfigEntry[NexxtenderCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NexxtenderConfigEntry) -> bool:
    _async_remove_old_entities(hass, entry)
    coordinator = NexxtenderCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NexxtenderConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


@callback
def _async_remove_old_entities(hass: HomeAssistant, entry: NexxtenderConfigEntry) -> None:
    registry = er.async_get(hass)
    address = entry.data[CONF_ADDRESS]
    for domain, key in REMOVED_ENTITIES:
        if entity_id := registry.async_get_entity_id(domain, DOMAIN, f"{address}_{key}"):
            registry.async_remove(entity_id)
