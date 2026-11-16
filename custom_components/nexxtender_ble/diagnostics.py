"""Diagnostics download: raw payloads to debug protocol differences."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NexxtenderConfigEntry
from . import protocol as p
from .const import CONF_PASSKEY

TO_REDACT = {CONF_PASSKEY, "serial"}


def _plain(obj: Any) -> Any:
    if is_dataclass(obj):
        out = asdict(obj)
        if "raw" in out and isinstance(out["raw"], bytes):
            out["raw"] = out["raw"].hex()
        return out
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NexxtenderConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.client.data
    schedules: dict[str, Any] = {}
    if data.config:
        c = data.config
        schedules["legacy_minutes"] = {
            "week": [c.week_start, c.week_stop],
            "weekend": [c.weekend_start, c.weekend_stop],
        }
    if data.cbor:
        schedules["cbor_minutes"] = {
            "week": [data.cbor.get(p.CBOR_TOU_WEEK_START), data.cbor.get(p.CBOR_TOU_WEEK_STOP)],
            "weekend": [
                data.cbor.get(p.CBOR_TOU_WEEKEND_START),
                data.cbor.get(p.CBOR_TOU_WEEKEND_STOP),
            ],
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "connected": coordinator.client.connected,
        "last_error": coordinator.last_error,
        "device": async_redact_data(
            {
                "model": data.model,
                "serial": data.serial,
                "firmware": data.firmware,
                "hardware": data.hardware,
            },
            TO_REDACT,
        ),
        "basic": _plain(data.basic),
        "grid": _plain(data.grid),
        "car": _plain(data.car),
        "advanced": _plain(data.advanced),
        "config": _plain(data.config),
        "cbor_supported": data.cbor_supported,
        "cbor": None
        if data.cbor is None
        else {
            "raw": data.cbor.raw.hex(),
            "prefix": data.cbor.prefix.hex(),
            "trailer": data.cbor.trailer.hex(),
            "values": data.cbor.values,
        },
        "schedules": schedules,
        "generic_status": data.generic_status,
        "charger_time": data.charger_time.isoformat() if data.charger_time else None,
        "extra": data.extra,
    }
