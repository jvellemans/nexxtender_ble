"""Sensors for the Nexxtender BLE integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import NexxtenderConfigEntry
from . import protocol as p
from .client import NexxtenderData
from .entity import NexxtenderEntity


@dataclass(frozen=True, kw_only=True)
class NexxtenderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[NexxtenderData], Any]
    available_when_disconnected: bool = False


def _get(obj_attr: str, field: str, scale: float | None = None):
    def _fn(data: NexxtenderData):
        obj = getattr(data, obj_attr)
        if obj is None:
            return None
        value = getattr(obj, field)
        return value if scale is None else round(value * scale, 3)

    return _fn


def _cfg(field: str):
    return lambda d: getattr(d.config, field) if d.config else None


def _cbor(key: int):
    return lambda d: d.cbor.get(key) if d.cbor else None


def _schedule(start: str, stop: str, cbor_start: int, cbor_stop: int):
    """Format a time-of-use window; the charger stores minutes in UTC.

    The CBOR configuration (newer firmware) is preferred; the legacy 15-byte
    configuration is the fallback.
    """

    def _fn(d: NexxtenderData):
        start_min = stop_min = None
        if d.cbor is not None:
            start_min, stop_min = d.cbor.get(cbor_start), d.cbor.get(cbor_stop)
        if (start_min is None or stop_min is None) and d.config:
            start_min, stop_min = getattr(d.config, start), getattr(d.config, stop)
        if start_min is None or stop_min is None:
            return None
        return f"{_utc_minutes_to_local(start_min)} - {_utc_minutes_to_local(stop_min)}"

    return _fn


def _utc_minutes_to_local(minutes: int) -> str:
    midnight_utc = datetime.combine(dt_util.utcnow().date(), time(0), tzinfo=dt_util.UTC)
    return dt_util.as_local(midnight_utc + timedelta(minutes=minutes)).strftime("%H:%M")


A = UnitOfElectricCurrent.AMPERE
W = UnitOfPower.WATT
CURRENT = {"device_class": SensorDeviceClass.CURRENT, "native_unit_of_measurement": A,
           "state_class": SensorStateClass.MEASUREMENT}
POWER = {"device_class": SensorDeviceClass.POWER, "native_unit_of_measurement": W,
         "state_class": SensorStateClass.MEASUREMENT}

SENSORS: tuple[NexxtenderSensorDescription, ...] = (
    # --- session -----------------------------------------------------------
    NexxtenderSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=p.CHARGING_STATUSES,
        value_fn=_get("basic", "status"),
    ),
    NexxtenderSensorDescription(
        key="session_state",
        translation_key="session_state",
        device_class=SensorDeviceClass.ENUM,
        options=p.SESSION_STATES,
        value_fn=_get("basic", "session_state"),
    ),
    NexxtenderSensorDescription(
        key="session_energy",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_get("basic", "energy_wh"),
    ),
    NexxtenderSensorDescription(
        key="session_duration",
        translation_key="session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get("basic", "seconds"),
    ),
    NexxtenderSensorDescription(
        key="phase_count",
        translation_key="phase_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get("basic", "phase_count"),
    ),
    # --- car ---------------------------------------------------------------
    NexxtenderSensorDescription(
        key="car_power", translation_key="car_power", **POWER,
        value_fn=_get("advanced", "car_power"),
    ),
    NexxtenderSensorDescription(
        key="car_current", translation_key="car_current", **CURRENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.car.current_total if d.car else None,
    ),
    *(
        NexxtenderSensorDescription(
            key=f"car_current_l{n}", translation_key=f"car_current_l{n}", **CURRENT,
            suggested_display_precision=1, value_fn=_get("car", f"l{n}"),
        )
        for n in (1, 2, 3)
    ),
    *(
        NexxtenderSensorDescription(
            key=f"car_power_p{n}", translation_key=f"car_power_p{n}", **POWER,
            entity_registry_enabled_default=False, value_fn=_get("car", f"p{n}"),
        )
        for n in (1, 2, 3)
    ),
    # --- grid --------------------------------------------------------------
    NexxtenderSensorDescription(
        key="grid_power", translation_key="grid_power", **POWER,
        value_fn=_get("advanced", "grid_power"),
    ),
    *(
        NexxtenderSensorDescription(
            key=f"grid_current_l{n}", translation_key=f"grid_current_l{n}", **CURRENT,
            suggested_display_precision=1, value_fn=_get("grid", f"l{n}"),
        )
        for n in (1, 2, 3)
    ),
    NexxtenderSensorDescription(
        key="grid_consumed", translation_key="grid_consumed", **POWER,
        value_fn=_get("grid", "consumed"),
    ),
    NexxtenderSensorDescription(
        key="available_current", translation_key="available_current", **CURRENT,
        value_fn=_get("advanced", "available_current"),
    ),
    # --- configuration (read-only views) -------------------------------------
    NexxtenderSensorDescription(
        key="mode",
        translation_key="mode",
        icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        options=p.MODES,
        value_fn=lambda d: (d.config.mode if d.config and d.config.mode else None)
        or (p.cbor_mode(d.cbor) if d.cbor else None),
    ),
    NexxtenderSensorDescription(
        key="max_car_charging_speed",
        translation_key="max_car_charging_speed",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=A,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cfg("i_evse_max"),
    ),
    NexxtenderSensorDescription(
        key="max_available_capacity",
        translation_key="max_available_capacity",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=A,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cfg("i_max"),
    ),
    NexxtenderSensorDescription(
        key="offloading_minimum",
        translation_key="offloading_minimum",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=A,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cfg("i_min"),
    ),
    NexxtenderSensorDescription(
        key="peak_consumption_limit",
        translation_key="peak_consumption_limit",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=A,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cbor(p.CBOR_I_CAPACITY),
    ),
    NexxtenderSensorDescription(
        key="min_car_charging_speed",
        translation_key="min_car_charging_speed",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=A,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cbor(p.CBOR_I_EVSE_MIN),
    ),
    NexxtenderSensorDescription(
        key="connection_type",
        translation_key="connection_type",
        device_class=SensorDeviceClass.ENUM,
        options=p.CONNECTION_TYPES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: p.cbor_connection_type(d.cbor) if d.cbor else None,
    ),
    NexxtenderSensorDescription(
        key="charging_phases",
        translation_key="charging_phases",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cbor(p.CBOR_CHARGING_PHASES),
    ),
    NexxtenderSensorDescription(
        key="week_schedule",
        translation_key="week_schedule",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_schedule("week_start", "week_stop", p.CBOR_TOU_WEEK_START, p.CBOR_TOU_WEEK_STOP),
    ),
    NexxtenderSensorDescription(
        key="weekend_schedule",
        translation_key="weekend_schedule",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_schedule(
            "weekend_start", "weekend_stop", p.CBOR_TOU_WEEKEND_START, p.CBOR_TOU_WEEKEND_STOP
        ),
    ),
    # --- diagnostics ---------------------------------------------------------
    NexxtenderSensorDescription(
        key="charger_time",
        translation_key="charger_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.charger_time,
    ),
    NexxtenderSensorDescription(
        key="generic_status",
        translation_key="generic_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: None
        if d.generic_status is None
        else p.GENERIC_STATUS_NAMES.get(d.generic_status, f"0x{d.generic_status:04x}"),
    ),
    NexxtenderSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        available_when_disconnected=True,
        value_fn=lambda d: d.firmware,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NexxtenderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(NexxtenderSensor(coordinator, desc) for desc in SENSORS)


class NexxtenderSensor(NexxtenderEntity, SensorEntity):
    entity_description: NexxtenderSensorDescription

    @property
    def available(self) -> bool:
        if self.entity_description.available_when_disconnected:
            return self.native_value is not None
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.data)
