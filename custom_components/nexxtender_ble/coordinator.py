"""Keeps the BLE connection alive and fans updates out to entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from . import pairing
from .client import NexxtenderClient, NexxtenderError, NotConnected
from .const import (
    CONF_PASSKEY,
    DOMAIN,
    MANUFACTURER,
    OPT_KEEP_CONNECTED,
    POLL_INTERVAL_SECONDS,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class NexxtenderCoordinator:
    """Owns the client, reconnects it, and notifies entities on changes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.client = NexxtenderClient(
            self.address,
            int(entry.data[CONF_PASSKEY]),
            self._ble_device,
            self._async_notify,
        )
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def async_notify_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _async_notify(self) -> None:
        """Data changed: refresh entities."""
        self.async_notify_listeners()

    # -------------------------------------------------------------- lifecycle

    @property
    def keep_connected(self) -> bool:
        return self.entry.options.get(OPT_KEEP_CONNECTED, True)

    @callback
    def _ble_device(self):
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def async_start(self) -> None:
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), f"{DOMAIN} connection {self.address}"
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=POLL_INTERVAL_SECONDS)
            )
        )
        # Reconnect quickly when the charger shows up again.
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.address, connectable=True
                ),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    async def async_stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task:
            self._task.cancel()
        await self.client.disconnect()

    @callback
    def _async_advertisement(self, _info, _change) -> None:
        if not self.client.connected:
            self._wake.set()

    async def async_set_keep_connected(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, OPT_KEEP_CONNECTED: value}
        )
        if value:
            self._wake.set()
        else:
            await self.client.disconnect()
        self._async_notify()

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass

    async def _run(self) -> None:
        backoff = RECONNECT_MIN_SECONDS
        auth_repairs = 0
        rejected = 0
        while not self._stopping:
            if not self.keep_connected:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self.client.connect()
            except pairing.AuthenticationRequired as err:
                # The link is not encrypted with a valid pairing. Remove it and
                # pair again with the PIN (like the ESP32 client does on every
                # connection).
                auth_repairs += 1
                if auth_repairs <= 3:
                    _LOGGER.info("%s; re-pairing (attempt %s)", err, auth_repairs)
                    await self._unpair()
                    await self._sleep(3)
                    continue
                self._set_error(f"Re-pairing did not help: {err}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
                auth_repairs = 0
                continue
            except pairing.InvalidPasskey as err:
                # The charger asked for the PIN and refused it.
                rejected += 1
                if rejected < 2:
                    await self._unpair()
                    continue
                self._set_error(f"Pairing failed: {err}")
                self.entry.async_start_reauth(self.hass)
                await self._sleep(RECONNECT_MAX_SECONDS)
                continue
            except Exception as err:  # noqa: BLE001
                self._set_error(str(err) or type(err).__name__)
                _LOGGER.debug("Connection to %s failed: %s", self.address, err)
                await self._sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
                continue

            backoff = RECONNECT_MIN_SECONDS
            auth_repairs = rejected = 0
            self._set_error(None)
            self._update_device_registry()
            _LOGGER.info("Connected to Nexxtender %s", self.address)
            await self.client.disconnected_event.wait()
            if not self._stopping:
                _LOGGER.info("Lost connection to Nexxtender %s", self.address)
                await self._sleep(RECONNECT_MIN_SECONDS)

    @callback
    def _update_device_registry(self) -> None:
        """Store model / serial / firmware read from the charger."""
        data = self.client.data
        if not data.serial:
            return
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.address)},
            connections={(dr.CONNECTION_BLUETOOTH, self.address)},
            manufacturer=MANUFACTURER,
            name="Nexxtender",
            model=data.model or "Nexxtender Home",
            serial_number=data.serial,
            sw_version=data.firmware,
            hw_version=data.hardware,
        )

    async def _unpair(self) -> None:
        try:
            await pairing.async_unpair(self.address)
        except pairing.PairingError as err:
            _LOGGER.debug("Unpair failed: %s", err)

    def _set_error(self, err: str | None) -> None:
        if err != self.last_error:
            self.last_error = err
            if err:
                _LOGGER.warning("Nexxtender %s: %s", self.address, err)
            self._async_notify()

    async def _async_poll(self, _now=None) -> None:
        if not self.client.connected:
            return
        try:
            await self.client.poll()
        except NexxtenderError as err:
            _LOGGER.debug("Poll failed: %s", err)
        except pairing.AuthenticationRequired as err:
            # Encryption lost on a live link: reconnect, which re-pairs.
            _LOGGER.info("Charger refused access during polling (%s); reconnecting", err)
            await self.client.disconnect()

    # ----------------------------------------------------------- actions API

    async def async_run(self, coro_factory: Callable[[], object]) -> None:
        """Run a client action, translating errors for the UI."""
        if not self.client.connected:
            raise HomeAssistantError("The charger is not connected over Bluetooth")
        try:
            await coro_factory()  # type: ignore[misc]
        except NotConnected as err:
            raise HomeAssistantError("The charger is not connected over Bluetooth") from err
        except NexxtenderError as err:
            raise HomeAssistantError(f"Nexxtender: {err}") from err
