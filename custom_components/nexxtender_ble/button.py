"""Buttons for the Nexxtender BLE integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NexxtenderConfigEntry
from . import protocol as p
from .client import NexxtenderClient
from .entity import NexxtenderEntity


@dataclass(frozen=True, kw_only=True)
class NexxtenderButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[NexxtenderClient], Awaitable[None]]


BUTTONS: tuple[NexxtenderButtonDescription, ...] = (
    NexxtenderButtonDescription(
        key="start_charge_default",
        translation_key="start_charge_default",
        icon="mdi:ev-plug-type2",
        press_fn=lambda c: c.send_command(p.CMD_START_CHARGE_DEFAULT),
    ),
    NexxtenderButtonDescription(
        key="start_charge_max",
        translation_key="start_charge_max",
        icon="mdi:speedometer",
        press_fn=lambda c: c.send_command(p.CMD_START_CHARGE_MAX),
    ),
    NexxtenderButtonDescription(
        key="start_charge_eco",
        translation_key="start_charge_eco",
        icon="mdi:leaf",
        press_fn=lambda c: c.send_command(p.CMD_START_CHARGE_ECO),
    ),
    NexxtenderButtonDescription(
        key="start_charge_auto",
        translation_key="start_charge_auto",
        icon="mdi:auto-mode",
        entity_registry_enabled_default=False,
        press_fn=lambda c: c.send_command(p.CMD_START_CHARGE_AUTO),
    ),
    NexxtenderButtonDescription(
        key="stop_charge",
        translation_key="stop_charge",
        icon="mdi:stop-circle-outline",
        press_fn=lambda c: c.send_command(p.CMD_STOP_CHARGE),
    ),
    NexxtenderButtonDescription(
        key="sync_time",
        translation_key="sync_time",
        icon="mdi:clock-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda c: c.sync_time(),
    ),
    NexxtenderButtonDescription(
        key="refresh_config",
        translation_key="refresh_config",
        icon="mdi:refresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda c: c.refresh_config(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NexxtenderButton(coordinator, desc) for desc in BUTTONS)


class NexxtenderButton(NexxtenderEntity, ButtonEntity):
    entity_description: NexxtenderButtonDescription

    async def async_press(self) -> None:
        client = self.coordinator.client
        await self.coordinator.async_run(lambda: self.entity_description.press_fn(client))
