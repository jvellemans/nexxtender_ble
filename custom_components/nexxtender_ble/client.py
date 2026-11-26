"""BLE connection handling for the Nexxtender Home charger."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from . import pairing, protocol as p
from .const import (
    CHAR_ADVANCED,
    CHAR_BASIC,
    CHAR_CAR,
    CHAR_FIRMWARE,
    CHAR_GENERIC_COMMAND,
    CHAR_GENERIC_DATA,
    CHAR_GENERIC_STATUS,
    CHAR_GRID,
    CHAR_HARDWARE,
    CHAR_MODEL,
    CHAR_SERIAL,
)

_LOGGER = logging.getLogger(__name__)

STATUS_TIMEOUT = 10.0
TIME_CONFIRM_TIMEOUT = 3.0
DATA_TIMEOUT = 3.0
CBOR_MAX_MISSES = 3


class NexxtenderError(Exception):
    """Generic error talking to the charger."""


class NotConnected(NexxtenderError):
    """No BLE connection."""


@dataclass
class NexxtenderData:
    """Latest known state of the charger."""

    basic: p.BasicData | None = None
    grid: p.GridData | None = None
    car: p.CarData | None = None
    advanced: p.AdvancedData | None = None
    config: p.Config | None = None
    cbor: p.CborConfig | None = None
    cbor_supported: bool | None = None
    generic_status: int | None = None
    charger_time: datetime | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    hardware: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class NexxtenderClient:
    """Owns one BLE connection to the charger and speaks its protocol."""

    def __init__(
        self,
        address: str,
        passkey: int,
        ble_device_getter: Callable[[], BLEDevice | None],
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self.address = address
        self.passkey = passkey
        self._get_ble_device = ble_device_getter
        self._on_update = on_update or (lambda: None)
        self.data = NexxtenderData()
        self._client: BleakClient | None = None
        self._ready = False
        self._connect_lock = asyncio.Lock()
        self._cmd_lock = asyncio.Lock()
        self._status_waiters: list[tuple[set[int], asyncio.Future[int]]] = []
        self._generic_data: bytes | None = None
        self._generic_data_event = asyncio.Event()
        self._cbor_misses = 0
        self.disconnected_event = asyncio.Event()
        self.disconnected_event.set()

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        """Link up AND setup finished (paired, verified, subscribed)."""
        return self._ready and self._link_up

    @property
    def _link_up(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _notify_listeners(self) -> None:
        try:
            self._on_update()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error in Nexxtender update listener")

    # ------------------------------------------------------------- connecting

    async def connect(self) -> None:
        """Connect, pair if needed, subscribe and read the initial state."""
        async with self._connect_lock:
            if self.connected:
                return
            ble_device = self._get_ble_device()
            if ble_device is None:
                raise NotConnected(
                    f"Nexxtender {self.address} is not in range of a connectable "
                    "Bluetooth adapter"
                )
            device_path = (ble_device.details or {}).get("path")

            # The charger seems to pair without bonding: a pairing that BlueZ
            # did not keep is useless for a new connection, so drop it first.
            reused_pairing = False
            try:
                paired, bonded = await pairing.async_get_state(self.address, device_path)
            except pairing.PairingError as err:
                _LOGGER.debug("Cannot read the pairing state: %s", err)
            else:
                reused_pairing = paired and bonded is not False
                if paired and bonded is False:
                    _LOGGER.debug("Paired but not bonded: removing the stale pairing")
                    try:
                        await pairing.async_unpair(self.address, device_path)
                    except pairing.PairingError as err:
                        _LOGGER.debug("Unpair failed: %s", err)

            self._ready = False
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                f"Nexxtender {self.address}",
                disconnected_callback=self._on_disconnected,
                ble_device_callback=lambda: self._get_ble_device() or ble_device,
                max_attempts=3,
            )
            self._client = client
            self.disconnected_event.clear()
            try:
                # Pair with the passkey (no-op when BlueZ reports it as paired).
                await pairing.async_pair(self.address, self.passkey, device_path)
                # The charger answers unencrypted requests with "Unlikely Error",
                # which BlueZ does not treat as "please encrypt". Check access
                # explicitly so a stale pairing is detected and redone.
                await self._verify_access()
                await self._subscribe()
                await self._read_initial(poll=False)
                self._ready = True
                await self.poll()
            except pairing.PairingError:
                await self.disconnect()
                raise
            except (AttributeError, BleakError, NexxtenderError) as err:
                # The link dropped mid-setup (AttributeError: client reset by the
                # callback; "Characteristic ... was not found!": services gone).
                link_lost = not self._link_up
                await self.disconnect()
                if reused_pairing and link_lost:
                    # The charger forgets pairings: with a pairing BlueZ kept from
                    # an earlier connection, it drops the link once it is used.
                    # Reusing it again would loop forever; pair again instead.
                    raise pairing.AuthenticationRequired(
                        f"Link dropped right after reusing the existing pairing ({err})"
                    ) from err
                if link_lost:
                    raise NotConnected(f"Connection lost during setup: {err}") from err
                raise
            except Exception:
                await self.disconnect()
                raise
        self._notify_listeners()
        # Configuration and clock are slower handshakes; failures there should
        # not drop the connection. Nothing below writes to the charger.
        try:
            await self.refresh_config()
            self.data.extra.get("errors", {}).pop("config", None)
        except NexxtenderError as err:
            self._record("errors", "config", str(err))
            _LOGGER.warning("Could not read Nexxtender configuration: %s", err)
        # Read-only: the clock is read, never set, on connect.
        try:
            await self.read_time()
            self.data.extra.get("errors", {}).pop("time_read", None)
        except NexxtenderError as err:
            self._record("errors", "time_read", str(err))
            _LOGGER.debug("Could not read the Nexxtender clock: %s", err)

    async def disconnect(self) -> None:
        self._ready = False
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                # BleakError, or a timeout/assertion on a link that is already gone.
                _LOGGER.debug("Error while disconnecting: %r", err)
        self._fail_waiters(NotConnected("Disconnected"))
        self.disconnected_event.set()
        self._notify_listeners()

    def _on_disconnected(self, _client: BleakClient) -> None:
        _LOGGER.debug("Nexxtender %s disconnected", self.address)
        self._ready = False
        self._client = None
        self._fail_waiters(NotConnected("Disconnected"))
        self.disconnected_event.set()
        self._notify_listeners()

    async def _verify_access(self) -> None:
        if not self._link_up:
            raise NotConnected("Connection lost during setup")
        try:
            await self._client.read_gatt_char(CHAR_ADVANCED)  # type: ignore[union-attr]
        except (BleakError, TimeoutError) as err:
            if _is_auth_error(err):
                raise pairing.AuthenticationRequired(
                    f"Charger refused access (link not encrypted?): {err}"
                ) from err
            raise NexxtenderError(f"Access check failed: {err}") from err

    def _notification(
        self, client: BleakClient, handler: Callable[[bytes], None]
    ) -> Callable[[Any, bytearray], None]:
        """Wrap a handler so only the current connection's notifications count.

        When BlueZ forgets the device (re-pairing) before it reports the link as
        down, bleak keeps the old connection's watcher: every notification then
        arrives once per old connection too. Those copies are dropped here.
        """

        def _callback(_sender: Any, data: bytearray) -> None:
            if client is self._client:
                handler(bytes(data))

        return _callback

    async def _subscribe(self) -> None:
        handlers = {
            CHAR_BASIC: self._handle_basic,
            CHAR_GRID: self._handle_grid,
            CHAR_CAR: self._handle_car,
            CHAR_GENERIC_STATUS: self._handle_generic_status,
            CHAR_GENERIC_DATA: self._handle_generic_data,
        }
        for uuid, handler in handlers.items():
            client = self._client
            if client is None or not client.is_connected:
                raise NotConnected("Connection lost during setup")
            try:
                await client.start_notify(uuid, self._notification(client, handler))
            except (BleakError, ValueError) as err:
                # An encryption error here usually means a stale/invalid bond.
                if _is_auth_error(err):
                    raise pairing.AuthenticationRequired(str(err)) from err
                _LOGGER.debug("Cannot subscribe to %s: %s", uuid, err)

    async def _read_initial(self, poll: bool = True) -> None:
        for attr, uuid in (
            ("model", CHAR_MODEL),
            ("serial", CHAR_SERIAL),
            ("firmware", CHAR_FIRMWARE),
            ("hardware", CHAR_HARDWARE),
        ):
            try:
                raw = await self._read(uuid)
                setattr(self.data, attr, raw.decode(errors="replace").strip("\x00 "))
            except NexxtenderError as err:
                _LOGGER.debug("Cannot read %s: %s", attr, err)
        if poll:
            await self.poll()

    # ---------------------------------------------------------- low-level I/O

    async def _read(self, uuid: str) -> bytes:
        if not self._link_up:
            raise NotConnected("Not connected")
        try:
            return bytes(await self._client.read_gatt_char(uuid))  # type: ignore[union-attr]
        except (BleakError, TimeoutError) as err:
            if _is_auth_error(err):
                raise pairing.AuthenticationRequired(str(err)) from err
            raise NexxtenderError(f"Read {uuid} failed: {err}") from err

    async def _write(self, uuid: str, data: bytes) -> None:
        if not self._link_up:
            raise NotConnected("Not connected")
        _LOGGER.debug("Write %s: %s", uuid[-4:], data.hex())
        log = self.data.extra.setdefault("status_log", [])
        log.append(f"{datetime.now(UTC).strftime('%H:%M:%S.%f')[:-3]} write {uuid[-2:]} {data.hex()}")
        del log[:-40]
        try:
            await self._client.write_gatt_char(uuid, data)  # type: ignore[union-attr]
        except (BleakError, TimeoutError) as err:
            raise NexxtenderError(f"Write {uuid} failed: {err}") from err

    def _fail_waiters(self, err: Exception) -> None:
        for _, fut in self._status_waiters:
            if not fut.done():
                fut.set_exception(err)
        self._status_waiters.clear()

    def _expect_status(self, statuses: set[int]) -> asyncio.Future[int]:
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._status_waiters.append((statuses, fut))
        return fut

    async def _await_status(self, fut: asyncio.Future[int], what: str) -> int:
        try:
            return await asyncio.wait_for(fut, STATUS_TIMEOUT)
        except TimeoutError as err:
            raise NexxtenderError(f"Charger did not answer {what}") from err
        finally:
            self._status_waiters = [w for w in self._status_waiters if w[1] is not fut]

    async def _await_generic_data(self) -> bytes:
        if self._generic_data is None:
            try:
                await asyncio.wait_for(self._generic_data_event.wait(), DATA_TIMEOUT)
            except TimeoutError:
                # Fall back to reading the characteristic directly.
                self._generic_data = await self._read(CHAR_GENERIC_DATA)
        assert self._generic_data is not None
        return self._generic_data

    def _record(self, kind: str, key: str, value: Any) -> None:
        """Keep raw payloads / errors for the diagnostics download."""
        self.data.extra.setdefault(kind, {})[key] = value

    def _reset_generic_data(self) -> None:
        self._generic_data = None
        self._generic_data_event.clear()

    # --------------------------------------------------------- notifications

    def _handle_basic(self, data: bytes) -> None:
        try:
            self.data.basic = p.parse_basic(data)
        except p.ProtocolError as err:
            _LOGGER.debug("Bad basic data %s: %s", data.hex(), err)
            return
        self._notify_listeners()

    def _handle_grid(self, data: bytes) -> None:
        try:
            self.data.grid = p.parse_grid(data)
        except p.ProtocolError as err:
            _LOGGER.debug("Bad grid data %s: %s", data.hex(), err)
            return
        self._notify_listeners()

    def _handle_car(self, data: bytes) -> None:
        try:
            self.data.car = p.parse_car(data)
        except p.ProtocolError as err:
            _LOGGER.debug("Bad car data %s: %s", data.hex(), err)
            return
        self._notify_listeners()

    def _handle_generic_status(self, data: bytes) -> None:
        try:
            status = p.parse_generic_status(data)
        except p.ProtocolError:
            return
        _LOGGER.debug("Generic status 0x%04x", status)
        self.data.generic_status = status
        log = self.data.extra.setdefault("status_log", [])
        log.append(f"{datetime.now(UTC).strftime('%H:%M:%S.%f')[:-3]} 0x{status:04x}")
        del log[:-40]
        for statuses, fut in list(self._status_waiters):
            if status in statuses and not fut.done():
                fut.set_result(status)
        self._notify_listeners()

    def _handle_generic_data(self, data: bytes) -> None:
        _LOGGER.debug("Generic data: %s", data.hex())
        log = self.data.extra.setdefault("status_log", [])
        log.append(f"{datetime.now(UTC).strftime('%H:%M:%S.%f')[:-3]} data {data.hex()}")
        del log[:-40]
        self._generic_data = data
        self._generic_data_event.set()

    # ----------------------------------------------------------------- polling

    async def poll(self) -> None:
        """Read the values that are not pushed by notifications."""
        try:
            self.data.advanced = p.parse_advanced(await self._read(CHAR_ADVANCED))
        except p.ProtocolError as err:
            _LOGGER.debug("Bad advanced data: %s", err)
        # The basic data is normally notified, but read it too so the state is
        # correct right after connecting.
        try:
            self.data.basic = p.parse_basic(await self._read(CHAR_BASIC))
        except (p.ProtocolError, NexxtenderError) as err:
            _LOGGER.debug("Cannot read basic data: %s", err)
        self._notify_listeners()

    # ---------------------------------------------------------------- commands

    async def send_command(self, command: bytes) -> None:
        """Fire-and-forget command (start/stop charging)."""
        async with self._cmd_lock:
            await self._write(CHAR_GENERIC_COMMAND, command)

    async def refresh_config(self) -> None:
        async with self._cmd_lock:
            await self._read_config_locked()
            await self._read_cbor_locked()
        self._notify_listeners()

    async def _read_config_locked(self) -> p.Config:
        self._reset_generic_data()
        fut = self._expect_status({p.GS_CONFIG_READ_POPPED})
        await self._write(CHAR_GENERIC_COMMAND, p.CMD_CONFIG_READ)
        await self._await_status(fut, "the configuration read")
        raw = await self._await_generic_data()
        self._record("raw", "config", raw.hex())
        try:
            cfg = p.parse_config(raw)
        except p.ProtocolError as err:
            raise NexxtenderError(f"Invalid config {raw.hex()}: {err}") from err
        self.data.config = cfg
        return cfg

    async def _read_cbor_locked(self) -> p.CborConfig | None:
        if self.data.cbor_supported is False:
            return None
        self._reset_generic_data()
        fut = self._expect_status({p.GS_CBOR_CONFIG_READ_POPPED})
        await self._write(CHAR_GENERIC_COMMAND, p.CMD_CBOR_CONFIG_READ)
        try:
            await self._await_status(fut, "the CBOR configuration read")
        except NotConnected:
            # The link dropped: says nothing about CBOR support, try next time.
            raise
        except NexxtenderError:
            # No answer. Older firmware only has the legacy configuration, but a
            # weak link can also lose the answer: give up after a few misses.
            self._cbor_misses += 1
            self._record("info", "cbor_read", f"no answer ({self._cbor_misses}x)")
            if self._cbor_misses >= CBOR_MAX_MISSES:
                _LOGGER.info("Charger does not support the CBOR configuration")
                self.data.cbor_supported = False
            return None
        self._cbor_misses = 0
        raw = await self._await_generic_data()
        self._record("raw", "cbor_config", raw.hex())
        try:
            cbor = p.parse_cbor_config(raw)
        except p.ProtocolError as err:
            _LOGGER.warning("Could not decode CBOR config %s: %s", raw.hex(), err)
            return None
        self.data.cbor = cbor
        self.data.cbor_supported = True
        self._record("info", "cbor_read", "ok")
        return cbor

    async def set_config(
        self,
        *,
        i_max: int | None = None,
        i_evse_max: int | None = None,
        mode: str | None = None,
        i_min: int | None = None,
    ) -> None:
        """Change fields of the legacy configuration."""
        async with self._cmd_lock:
            # Always start from a fresh copy so we never write stale values.
            current = await self._read_config_locked()
            new = p.build_config(
                current, i_max=i_max, i_evse_max=i_evse_max, mode=mode, i_min=i_min
            )
            if new == current.raw:
                return
            ready = self._expect_status({p.GS_CONFIG_SET_READY})
            await self._write(CHAR_GENERIC_COMMAND, p.CMD_CONFIG_SET)
            await self._await_status(ready, "the configuration write request")
            done = self._expect_status({p.GS_CONFIG_SET_SUCCESS})
            await self._write(CHAR_GENERIC_DATA, new)
            await self._await_status(done, "the configuration write")
            await self._read_config_locked()
            await self._read_cbor_locked()
        self._notify_listeners()

    async def set_cbor_values(self, updates: dict[int, int]) -> None:
        """Change values in the CBOR configuration (e.g. peak consumption)."""
        async with self._cmd_lock:
            current = await self._read_cbor_locked()
            if current is None:
                raise NexxtenderError("This charger has no CBOR configuration")
            try:
                new = p.build_cbor_config(current, updates)
            except p.ProtocolError as err:
                raise NexxtenderError(str(err)) from err
            if new == current.raw:
                return
            ready = self._expect_status({p.GS_CBOR_CONFIG_SET_READY})
            await self._write(CHAR_GENERIC_COMMAND, p.CMD_CBOR_CONFIG_SET)
            await self._await_status(ready, "the CBOR configuration write request")
            done = self._expect_status({p.GS_CBOR_CONFIG_SET_SUCCESS})
            await self._write(CHAR_GENERIC_DATA, new)
            await self._await_status(done, "the CBOR configuration write")
            await self._read_cbor_locked()
            await self._read_config_locked()
        self._notify_listeners()

    async def sync_time(self) -> None:
        """Set the charger clock to the current time.

        Like the ESPHome client, the "time set success" status is not required
        (some chargers never send it); reading the clock back is the check.
        """
        async with self._cmd_lock:
            ready = self._expect_status({p.GS_TIME_SET_READY})
            await self._write(CHAR_GENERIC_COMMAND, p.CMD_TIME_SET)
            await self._await_status(ready, "the clock write request")
            done = self._expect_status({p.GS_TIME_SET_SUCCESS})
            written = int(time.time())
            await self._write(CHAR_GENERIC_DATA, p.encode_time(written))
            confirmed = True
            try:
                await asyncio.wait_for(asyncio.shield(done), TIME_CONFIRM_TIMEOUT)
            except TimeoutError:
                confirmed = False
                _LOGGER.debug("No 'time set success' status; checking by reading the clock")
            finally:
                if not done.done():
                    done.cancel()
                self._status_waiters = [w for w in self._status_waiters if w[1] is not done]
            self._record("info", "last_time_sync", datetime.fromtimestamp(written, UTC).isoformat())
            self._record("info", "time_set_confirmed", confirmed)
            try:
                await self._read_time_locked()
            except NexxtenderError as err:
                self._record("errors", "time_read", str(err))
                if not confirmed:
                    raise NexxtenderError(
                        f"Clock written but neither confirmed nor readable: {err}"
                    ) from err
                # Confirmed but not readable: show the time we set.
                _LOGGER.info("Clock set, but reading it back failed: %s", err)
                self.data.charger_time = datetime.fromtimestamp(written, UTC)
            else:
                drift = abs(self.data.charger_time.timestamp() - written) if self.data.charger_time else None
                self._record("info", "clock_drift_after_sync_s", drift)
                if drift is not None and drift > 120:
                    _LOGGER.warning(
                        "The charger clock is still %.0f s off after setting it", drift
                    )
        self._notify_listeners()

    async def read_time(self) -> None:
        """Read the charger clock (no write to the charger)."""
        async with self._cmd_lock:
            await self._read_time_locked()
        self._notify_listeners()

    async def _read_time_locked(self) -> None:
        self._reset_generic_data()
        fut = self._expect_status({p.GS_TIME_READ_POPPED})
        await self._write(CHAR_GENERIC_COMMAND, p.CMD_TIME_READ)
        await self._await_status(fut, "the clock read")
        raw = await self._await_generic_data()
        self._record("raw", "time", raw.hex())
        try:
            self.data.charger_time = datetime.fromtimestamp(
                p.parse_time(raw), tz=UTC
            )
        except (p.ProtocolError, ValueError, OverflowError) as err:
            _LOGGER.debug("Bad time payload %s: %s", raw.hex(), err)


def _is_auth_error(err: Exception) -> bool:
    text = str(err).lower()
    return any(
        word in text
        for word in (
            "insufficient authentication",
            "insufficient encryption",
            "authenticationfailed",
            "authentication failed",
            # What the Nexxtender answers to requests on an unencrypted link.
            "unlikely error",
            "unlikely_error",
        )
    )


async def async_validate(
    address: str,
    passkey: int,
    ble_device: BLEDevice,
    get_device: Callable[[], BLEDevice | None] | None = None,
) -> NexxtenderData:
    """Pair and read the device information (config flow).

    Strategy 1: connect with bleak (service discovery), then pair.
    Strategy 2 (fallback, if pairing was interrupted or the PIN was never
    requested): drop the connection and let BlueZ connect and pair in one go,
    which is quicker on a weak link; then connect again and read.

    If BlueZ still holds an old bond (e.g. from an earlier setup), pairing is
    skipped and the charger refuses access ("Unlikely Error"). In that case the
    bond is removed and the whole sequence runs once more with the new PIN.
    """
    device = ble_device
    client = NexxtenderClient(address, passkey, lambda: device)

    async def _connect() -> None:
        client._client = await establish_connection(  # noqa: SLF001
            BleakClientWithServiceCache,
            device,
            f"Nexxtender {address}",
            max_attempts=3,
        )

    async def _attempt() -> None:
        path = (device.details or {}).get("path")
        await _connect()
        try:
            await pairing.async_pair(address, passkey, path)
        except (pairing.PairingInterrupted, pairing.PasskeyNotRequested) as err:
            _LOGGER.warning(
                "Pairing after connecting failed (%s); retrying with BlueZ "
                "connecting and pairing directly",
                err,
            )
            await client.disconnect()
            await asyncio.sleep(2)
            await pairing.async_pair(address, passkey, path)
            _LOGGER.info("Direct BlueZ pairing succeeded")
            if not client._link_up:  # noqa: SLF001
                await _connect()
        await client._verify_access()  # noqa: SLF001
        await client._read_initial()  # noqa: SLF001

    try:
        async with asyncio.timeout(240):
            try:
                await _attempt()
            except pairing.AuthenticationRequired as err:
                _LOGGER.info(
                    "Charger refused access (%s); removing the old BlueZ pairing "
                    "and pairing again",
                    err,
                )
                await client.disconnect()
                try:
                    await pairing.async_unpair(address, (device.details or {}).get("path"))
                except pairing.PairingError as unpair_err:
                    _LOGGER.debug("Unpair failed: %s", unpair_err)
                # BlueZ forgets the device on removal; give it time to see the
                # charger advertise again before reconnecting.
                await asyncio.sleep(5)
                if get_device is not None and (fresh := get_device()) is not None:
                    device = fresh
                await _attempt()
    finally:
        await client.disconnect()
    return client.data
