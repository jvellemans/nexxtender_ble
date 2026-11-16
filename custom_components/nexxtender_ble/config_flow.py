"""Config flow for the Nexxtender BLE integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

from bleak.exc import BleakError
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import pairing
from .client import NexxtenderError, async_validate
from .const import CONF_PASSKEY, DOMAIN, LOCAL_NAME, SERVICE_NEXXTENDER

_LOGGER = logging.getLogger(__name__)

PASSKEY_SCHEMA = vol.Schema(
    {vol.Required(CONF_PASSKEY): vol.All(str, vol.Strip, vol.Match(r"^\d{6}$"))}
)


_MAC_RE = re.compile(r"^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$")


def _looks_like_nexxtender(info: BluetoothServiceInfoBleak) -> bool:
    return info.name == LOCAL_NAME or SERVICE_NEXXTENDER in info.service_uuids


def _normalize_address(value: str) -> str | None:
    value = value.strip().upper()
    if not _MAC_RE.match(value):
        return None
    return value.replace("-", ":")


def _label(info: BluetoothServiceInfoBleak) -> str:
    name = info.name if info.name and info.name != info.address.replace(":", "-") else ""
    if _looks_like_nexxtender(info):
        name = f"Nexxtender ({name or 'HOME'})"
    rssi = f", {info.rssi} dBm" if info.rssi is not None else ""
    return f"{name or '(no name)'} - {info.address}{rssi}"


class NexxtenderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._title: str | None = None
        self._error_detail = ""

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"address": discovery_info.address}
        return await self.async_step_passkey()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the charger from nearby devices, or type its MAC address.

        Many chargers do not advertise their "HOME" name, so every nearby
        connectable device is offered, likely Nexxtenders first, then the
        others by signal strength.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            elif (
                bluetooth.async_ble_device_from_address(
                    self.hass, address, connectable=True
                )
                is None
            ):
                errors[CONF_ADDRESS] = "not_in_range"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._address = address
                return await self.async_step_passkey()

        current = self._async_current_ids(include_ignore=False)
        infos = [
            info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if info.address not in current
        ]
        infos.sort(key=lambda i: (not _looks_like_nexxtender(i), -(i.rssi or -127)))
        options = [
            SelectOptionDict(value=info.address, label=_label(info)) for info in infos
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_passkey(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._address is not None
        errors: dict[str, str] = {}
        address = self._address
        if user_input is not None:
            passkey = user_input[CONF_PASSKEY]
            errors = await self._async_try(address, passkey)
            if not errors:
                data = {CONF_ADDRESS: address, CONF_PASSKEY: passkey}
                if self.source == "reauth":
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=data
                    )
                return self.async_create_entry(
                    title=self._title or f"Nexxtender {address}", data=data
                )
        return self.async_show_form(
            step_id="passkey",
            data_schema=PASSKEY_SCHEMA,
            errors=errors,
            description_placeholders={
                "address": address,
                "error_detail": f"\n\n`{self._error_detail}`" if errors else "",
            },
        )

    async def _async_try(self, address: str, passkey: str) -> dict[str, str]:
        self._title = None
        self._error_detail = ""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, address, connectable=True
        )
        if ble_device is None:
            return {"base": "not_in_range"}
        try:
            data = await async_validate(address, int(passkey), ble_device)
        except pairing.InvalidPasskey as err:
            self._error_detail = str(err)
            return {CONF_PASSKEY: "invalid_passkey"}
        except pairing.PairingInterrupted as err:
            self._error_detail = str(err)
            return {"base": "pairing_interrupted"}
        except pairing.PasskeyNotRequested as err:
            self._error_detail = str(err)
            return {"base": "passkey_not_requested"}
        except pairing.AuthenticationRequired as err:
            self._error_detail = str(err)
            _LOGGER.warning("Charger refused access after pairing: %s", err)
            return {"base": "auth_required"}
        except pairing.PairingError as err:
            self._error_detail = str(err)
            _LOGGER.warning("Pairing with %s failed: %s", address, err)
            return {"base": "pairing_failed"}
        except (BleakError, NexxtenderError, TimeoutError) as err:
            self._error_detail = str(err) or type(err).__name__
            _LOGGER.warning("Cannot connect to %s: %s", address, err)
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating %s", address)
            return {"base": "unknown"}
        if data.serial:
            self._title = f"Nexxtender {data.serial}"
        return {}

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        address = entry_data[CONF_ADDRESS]
        self._address = address
        return await self.async_step_passkey()
