"""Home Assistant decides when the car charges (off-peak hours only).

The charger is put in "Max private" mode, so it never starts a session on its
own when the car is plugged in. This module then sends "start charging (max)"
when the car is plugged in during off-peak hours (or when "Charge now" is on),
and "stop charging" when off-peak hours end.

Commands are only sent when the desired state changes, with a few spaced-out
retries, so a full battery (plugged in but not drawing) does not cause a stream
of start commands.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import protocol as p, tariff
from .const import OPT_CHARGE_NOW, OPT_SMART_CHARGING, OPT_TARIFF_PROFILE

if TYPE_CHECKING:
    from .coordinator import NexxtenderCoordinator

_LOGGER = logging.getLogger(__name__)

START_RETRY_SECONDS = 120
START_MAX_ATTEMPTS = 3
STOP_RETRY_SECONDS = 60
STOP_MAX_ATTEMPTS = 5


class SmartCharger:
    """Off-peak charging controller."""

    def __init__(self, coordinator: NexxtenderCoordinator) -> None:
        self.coordinator = coordinator
        self.now: Callable[[], datetime] = dt_util.now
        self.monotonic: Callable[[], float] = time.monotonic
        self._prev_plugged: bool | None = None
        self._prev_want: bool | None = None
        self._attempts = 0
        self._last_attempt = 0.0
        self._busy = False
        self._last_mode_fix = -1e9
        self.last_action: str | None = None
        self.last_action_time: datetime | None = None

    # ------------------------------------------------------------- settings

    @property
    def _options(self):
        return self.coordinator.entry.options

    @property
    def enabled(self) -> bool:
        return bool(self._options.get(OPT_SMART_CHARGING, False))

    @property
    def profile(self) -> str:
        return self._options.get(OPT_TARIFF_PROFILE, tariff.DEFAULT_PROFILE)

    @property
    def charge_now(self) -> bool:
        return bool(self._options.get(OPT_CHARGE_NOW, False))

    @property
    def off_peak(self) -> bool:
        return tariff.is_off_peak(self.now(), self.profile)

    @property
    def next_change(self) -> datetime | None:
        return tariff.next_change(self.now(), self.profile)

    async def async_set_enabled(self, value: bool) -> None:
        self.coordinator.async_set_option(OPT_SMART_CHARGING, value)
        self._prev_want = None  # act on the next evaluation
        if value:
            await self._ensure_private_mode()
        self.async_evaluate()

    async def async_set_profile(self, value: str) -> None:
        if value not in tariff.PROFILES:
            raise HomeAssistantError(f"Unknown profile {value}")
        self.coordinator.async_set_option(OPT_TARIFF_PROFILE, value)
        self.async_evaluate()

    async def async_set_charge_now(self, value: bool) -> None:
        self.coordinator.async_set_option(OPT_CHARGE_NOW, value)
        self.async_evaluate()

    async def _ensure_private_mode(self) -> None:
        """Max private: no auto-start on plug-in, no charger-side schedule."""
        client = self.coordinator.client
        cfg = client.data.config
        if not client.connected or cfg is None or cfg.mode == p.MODE_MAX_PRIVATE:
            return
        _LOGGER.info("Smart charging: switching the charger to Max private mode")
        try:
            await self.coordinator.async_run(
                lambda: client.set_config(mode=p.MODE_MAX_PRIVATE)
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch the charger to Max private: %s", err)

    # ------------------------------------------------------------ decisions

    @callback
    def async_evaluate(self, _now=None) -> None:
        """Called on every data update and every minute."""
        if self._busy:
            return
        client = self.coordinator.client
        basic = client.data.basic
        plugged = None if basic is None else basic.status != p.STATUS_UNPLUGGED

        # "Charge now" lasts until the car is unplugged.
        if self._prev_plugged and plugged is False and self.charge_now:
            _LOGGER.info("Car unplugged: turning off 'Charge now'")
            self.coordinator.async_set_option(OPT_CHARGE_NOW, False)
        if plugged is not None:
            if plugged != self._prev_plugged:
                self._prev_want = None  # new plug-in: act again
            self._prev_plugged = plugged

        if not self.enabled or not client.connected or basic is None:
            return

        # Make sure the charger cannot start on its own (e.g. after a reset).
        cfg = client.data.config
        if (
            cfg is not None
            and cfg.mode != p.MODE_MAX_PRIVATE
            and self.monotonic() - self._last_mode_fix > 300
        ):
            self._last_mode_fix = self.monotonic()
            self._spawn(self._ensure_private_mode())
            return

        want = bool(plugged) and (self.off_peak or self.charge_now)
        charging = basic.status == p.STATUS_CHARGING
        if want != self._prev_want:
            self._prev_want = want
            self._attempts = 0
            self._last_attempt = 0.0

        if want and not charging:
            self._maybe_send(p.CMD_START_CHARGE_MAX, "start", START_RETRY_SECONDS, START_MAX_ATTEMPTS)
        elif not want and charging:
            self._maybe_send(p.CMD_STOP_CHARGE, "stop", STOP_RETRY_SECONDS, STOP_MAX_ATTEMPTS)

    def _maybe_send(self, command: bytes, label: str, retry: int, max_attempts: int) -> None:
        if self._attempts >= max_attempts:
            return
        if self._attempts and self.monotonic() - self._last_attempt < retry:
            return
        self._attempts += 1
        self._last_attempt = self.monotonic()
        self._spawn(self._send(command, label))

    def _spawn(self, coro) -> None:
        self.coordinator.entry.async_create_background_task(
            self.coordinator.hass, self._guarded(coro), "nexxtender smart charging"
        )

    async def _guarded(self, coro) -> None:
        self._busy = True
        try:
            await coro
        finally:
            self._busy = False

    async def _send(self, command: bytes, label: str) -> None:
        client = self.coordinator.client
        reason = "off-peak" if self.off_peak else ("charge now" if self.charge_now else "peak hours")
        _LOGGER.info("Smart charging: %s (%s, attempt %s)", label, reason, self._attempts)
        try:
            await self.coordinator.async_run(lambda: client.send_command(command))
        except HomeAssistantError as err:
            _LOGGER.warning("Smart charging %s failed: %s", label, err)
            return
        self.last_action = label
        self.last_action_time = self.now()
        self.coordinator.async_notify_listeners()
