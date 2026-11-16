"""Current limits for the Nexxtender BLE integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NexxtenderConfigEntry
from . import protocol as p
from .client import NexxtenderClient, NexxtenderData
from .entity import NexxtenderEntity


@dataclass(frozen=True, kw_only=True)
class NexxtenderNumberDescription(NumberEntityDescription):
    value_fn: Callable[[NexxtenderData], int | None]
    set_fn: Callable[[NexxtenderClient, int], Awaitable[None]]


def _amps(**kwargs) -> dict:
    return {
        "device_class": NumberDeviceClass.CURRENT,
        "native_unit_of_measurement": UnitOfElectricCurrent.AMPERE,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        **kwargs,
    }


NUMBERS: tuple[NexxtenderNumberDescription, ...] = (
    NexxtenderNumberDescription(
        key="max_car_charging_speed",
        translation_key="max_car_charging_speed",
        **_amps(native_min_value=6, native_max_value=32),
        value_fn=lambda d: d.config.i_evse_max if d.config else None,
        set_fn=lambda c, v: c.set_config(i_evse_max=v),
    ),
    NexxtenderNumberDescription(
        key="max_available_capacity",
        translation_key="max_available_capacity",
        **_amps(native_min_value=6, native_max_value=40),
        value_fn=lambda d: d.config.i_max if d.config else None,
        set_fn=lambda c, v: c.set_config(i_max=v),
    ),
    NexxtenderNumberDescription(
        key="offloading_minimum",
        translation_key="offloading_minimum",
        **_amps(native_min_value=6, native_max_value=32),
        value_fn=lambda d: d.config.i_min if d.config else None,
        set_fn=lambda c, v: c.set_config(i_min=v),
    ),
    NexxtenderNumberDescription(
        key="peak_consumption_limit",
        translation_key="peak_consumption_limit",
        **_amps(native_min_value=6, native_max_value=40),
        value_fn=lambda d: d.cbor.get(p.CBOR_I_CAPACITY) if d.cbor else None,
        set_fn=lambda c, v: c.set_cbor_values({p.CBOR_I_CAPACITY: v}),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NexxtenderNumber(coordinator, desc) for desc in NUMBERS)


class NexxtenderNumber(NexxtenderEntity, NumberEntity):
    entity_description: NexxtenderNumberDescription

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        value = self.entity_description.value_fn(self.data)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        client = self.coordinator.client
        await self.coordinator.async_run(
            lambda: self.entity_description.set_fn(client, int(value))
        )
