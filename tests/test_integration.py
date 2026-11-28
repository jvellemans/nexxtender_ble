"""Home Assistant level tests with a simulated charger."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.nexxtender_ble import pairing, protocol as p
from custom_components.nexxtender_ble.const import (
    CHAR_ADVANCED, CHAR_BASIC, CHAR_CAR, CHAR_FIRMWARE, CHAR_GENERIC_COMMAND,
    CHAR_GENERIC_DATA, CHAR_GENERIC_STATUS, CHAR_GRID, CHAR_HARDWARE, CHAR_MODEL,
    CHAR_SERIAL, CONF_PASSKEY, DOMAIN,
)

ADDRESS = "FB:11:22:33:44:55"
BLE_DEVICE = BLEDevice(ADDRESS, "HOME", {"path": "/org/bluez/hci0/dev_FB_11_22_33_44_55"})


class FakeCharger:
    """Bleak-client look-alike that implements the Nexxtender handshakes."""

    def __init__(self) -> None:
        self.is_connected = True
        self.callbacks = {}
        self.writes: list[tuple[str, bytes]] = []
        self.config = p.with_crc(
            struct.pack("<bbbbBhhhh", 25, 16, 0, 6, 0, 1320, 420, 0, 0)
        )
        self.clock = 0
        self._pending = None
        self.reacts_to_start = True  # False = full battery, start is ignored
        self.encrypted = True  # False = stale pairing: charger refuses requests
        self.drop_during_subscribe = False
        self.sends_time_success = True
        # True = BlueZ reuses a pairing the charger forgot: the charger drops
        # the link as soon as it's used (field log, 0.3.1).
        self.stale_bond_drops_link = False
        self.disconnected_callback = None

    # bleak API -----------------------------------------------------------
    async def start_notify(self, uuid, callback):
        if self.drop_during_subscribe and uuid == CHAR_GRID:
            self.drop_during_subscribe = False
            self.is_connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)  # bleak resets the client
        self.callbacks[uuid] = callback

    async def read_gatt_char(self, uuid):
        if self.stale_bond_drops_link:
            from bleak.exc import BleakError

            self.is_connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)
            raise BleakError(f"Characteristic {uuid} was not found!")
        if not self.encrypted and uuid in (CHAR_ADVANCED, CHAR_BASIC):
            from bleak.exc import BleakError

            raise BleakError(
                "(<BleakGATTProtocolErrorCode.UNLIKELY_ERROR: 14>, 'GATT Protocol Error: Unlikely Error')"
            )
        return {
            CHAR_MODEL: b"Nexxtender Home",
            CHAR_SERIAL: b"12345-67-8901-23456-78",
            CHAR_FIRMWARE: b"2.53",
            CHAR_HARDWARE: b"1.0",
            CHAR_ADVANCED: struct.pack("<Ihii", 1, 25, 1500, 7400),
            CHAR_BASIC: struct.pack("<HBB4sIBB", 600, 2, ord("C"), b"\0" * 4, 1234, 0, 3),
        }[uuid]

    async def write_gatt_char(self, uuid, data, response=None):
        data = bytes(data)
        self.writes.append((uuid, data))
        loop = asyncio.get_running_loop()
        if uuid == CHAR_GENERIC_COMMAND:
            if data == p.CMD_CONFIG_READ:
                loop.call_soon(self._push, CHAR_GENERIC_DATA, self.config)
                loop.call_soon(self._status, p.GS_CONFIG_READ_POPPED)
            elif data == p.CMD_CONFIG_SET:
                self._pending = "config"
                loop.call_soon(self._status, p.GS_CONFIG_SET_READY)
            elif data == p.CMD_TIME_SET:
                self._pending = "time"
                loop.call_soon(self._status, p.GS_TIME_SET_READY)
            elif data in (p.CMD_START_CHARGE_MAX, p.CMD_START_CHARGE_DEFAULT) and self.reacts_to_start:
                loop.call_soon(self.set_status, "C")
            elif data == p.CMD_STOP_CHARGE:
                loop.call_soon(self.set_status, "B")
            elif data == p.CMD_TIME_READ:
                loop.call_soon(self._push, CHAR_GENERIC_DATA, p.encode_time(self.clock))
                loop.call_soon(self._status, p.GS_TIME_READ_POPPED)
        elif uuid == CHAR_GENERIC_DATA:
            if self._pending == "config":
                assert p.check_crc(data) and len(data) == 15
                self.config = data
                loop.call_soon(self._status, p.GS_CONFIG_SET_SUCCESS)
            elif self._pending == "time":
                self.clock = p.parse_time(data)
                if self.sends_time_success:
                    loop.call_soon(self._status, p.GS_TIME_SET_SUCCESS)
            self._pending = None

    async def disconnect(self):
        self.is_connected = False

    # helpers -------------------------------------------------------------
    def set_status(self, letter):
        self._push(CHAR_BASIC, struct.pack("<HBB4sIBB", 0, 2, ord(letter), b"\0" * 4, 0, 0, 3))

    def _push(self, uuid, data):
        if uuid in self.callbacks:
            self.callbacks[uuid](None, bytearray(data))

    def _status(self, value):
        self._push(CHAR_GENERIC_STATUS, value.to_bytes(2, "little"))


@pytest.fixture
def charger():
    return FakeCharger()


@pytest.fixture
def patched(charger):
    bt = MagicMock()
    bt.async_ble_device_from_address.return_value = BLE_DEVICE
    bt.async_register_callback.return_value = lambda: None
    async def _connect(*args, **kwargs):
        charger.is_connected = True
        charger.disconnected_callback = kwargs.get("disconnected_callback")
        return charger

    async def _fast_sleep(self, seconds):
        await asyncio.sleep(0)

    with (
        patch("custom_components.nexxtender_ble.client.establish_connection",
              AsyncMock(side_effect=_connect)),
        patch("custom_components.nexxtender_ble.pairing.async_pair", AsyncMock()) as pair,
        patch("custom_components.nexxtender_ble.coordinator.bluetooth", bt),
        patch("custom_components.nexxtender_ble.config_flow.bluetooth", bt),
        patch("custom_components.nexxtender_ble.coordinator.NexxtenderCoordinator._sleep", _fast_sleep),
    ):
        yield pair, bt


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSKEY: "123456"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    for _ in range(20):
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)
        if entry.runtime_data.client.data.charger_time is not None:
            break
    return entry


def _state(hass, entity_id):
    st = hass.states.get(entity_id)
    assert st is not None, f"{entity_id} missing; have {hass.states.async_entity_ids()}"
    return st.state


async def test_setup_reads_everything(hass, patched, charger):
    pair, _ = patched
    await _setup(hass)
    pair.assert_awaited_with(ADDRESS, 123456, BLE_DEVICE.details["path"])
    assert _state(hass, "binary_sensor.nexxtender_bluetooth_connected") == "on"
    assert _state(hass, "sensor.nexxtender_status") == "charging"
    assert _state(hass, "binary_sensor.nexxtender_charging") == "on"
    assert float(_state(hass, "sensor.nexxtender_session_energy")) == pytest.approx(1.234)
    assert _state(hass, "sensor.nexxtender_car_power") == "7400"
    assert _state(hass, "sensor.nexxtender_mode") == "eco_private"
    assert _state(hass, "sensor.nexxtender_maximum_car_charging_speed") == "16"
    assert hass.states.get("sensor.nexxtender_peak_consumption_limit") is None
    # The clock is read, not set.
    assert charger.clock == 0
    assert entry_clock(hass) is not None


def entry_clock(hass):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return entry.runtime_data.client.data.charger_time


# Only these requests may be sent. The CBOR config read (04 50) is not one of them:
# firmware 2.53.2 drops the link on it (removed in 0.3.4).
READ_COMMANDS = {p.CMD_CONFIG_READ, p.CMD_TIME_READ}


async def test_connecting_never_writes_to_the_charger(hass, patched, charger):
    """Read-only: connecting, polling and notifications only send read requests."""
    entry = await _setup(hass)
    await entry.runtime_data._async_poll()
    charger.set_status("B")
    await hass.async_block_till_done()
    assert charger.writes, "expected read requests"
    assert all(u == CHAR_GENERIC_COMMAND and d in READ_COMMANDS for u, d in charger.writes)
    assert p.parse_config(charger.config).mode == p.MODE_ECO_PRIVATE


async def test_notifications_update_state(hass, patched, charger):
    await _setup(hass)
    charger._push(CHAR_GRID, struct.pack("<IhhhhH", 1, 123, 45, 67, 2500, 10))
    charger._push(CHAR_CAR, struct.pack("<Ihhhhhh", 1, 160, 160, 160, 3680, 3680, 3680))
    charger._push(CHAR_BASIC, struct.pack("<HBB4sIBB", 0, 3, ord("A"), b"\0" * 4, 0, 0, 0))
    await hass.async_block_till_done()
    assert float(_state(hass, "sensor.nexxtender_grid_current_l1")) == 12.3
    assert float(_state(hass, "sensor.nexxtender_car_current")) == 48.0
    assert _state(hass, "sensor.nexxtender_status") == "unplugged"
    assert _state(hass, "binary_sensor.nexxtender_car_plugged_in") == "off"


async def test_old_write_entities_are_removed(hass, patched, charger):
    """Entities from 0.2.x that wrote to the charger disappear on upgrade."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSKEY: "123456"},
    )
    entry.add_to_hass(hass)
    old = [
        registry.async_get_or_create(domain, DOMAIN, f"{ADDRESS}_{key}", config_entry=entry).entity_id
        for domain, key in (("select", "mode"), ("number", "max_car_charging_speed"),
                            ("switch", "smart_charging"), ("button", "sync_time"))
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) is None for entity_id in old)
    assert hass.states.get("select.nexxtender_mode") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_buttons_send_commands(hass, patched, charger):
    await _setup(hass)
    for entity, cmd in (
        ("button.nexxtender_start_charging", p.CMD_START_CHARGE_DEFAULT),
        ("button.nexxtender_stop_charging", p.CMD_STOP_CHARGE),
    ):
        await hass.services.async_call("button", "press", {"entity_id": entity}, blocking=True)
        assert charger.writes[-1] == (CHAR_GENERIC_COMMAND, cmd)


async def test_release_connection_switch(hass, patched, charger):
    entry = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.nexxtender_hold_bluetooth_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert not charger.is_connected
    assert _state(hass, "binary_sensor.nexxtender_bluetooth_connected") == "off"
    assert _state(hass, "sensor.nexxtender_status") == "unavailable"
    assert entry.options["keep_connected"] is False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_user(hass, patched):
    _, bt = patched
    info = MagicMock(address=ADDRESS, service_uuids=[], rssi=-70)
    info.name = "HOME"
    bt.async_discovered_service_info.return_value = [info]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["step_id"] == "passkey"

    with patch(
        "custom_components.nexxtender_ble.client.pairing.async_pair",
        AsyncMock(side_effect=pairing.InvalidPasskey("nope")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "000000"}
        )
    assert result["errors"] == {CONF_PASSKEY: "invalid_passkey"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSKEY: "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nexxtender 12345-67-8901-23456-78"
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PASSKEY: "123456"}


async def test_config_flow_rejects_bad_format(hass, patched):
    _, bt = patched
    info = MagicMock(address=ADDRESS, service_uuids=[], rssi=-70)
    info.name = "HOME"
    bt.async_discovered_service_info.return_value = [info]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    import voluptuous as vol
    from homeassistant.data_entry_flow import InvalidData

    with pytest.raises((InvalidData, vol.Invalid)):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "12ab"}
        )


async def test_config_flow_unnamed_device_typed_mac(hass, patched):
    """The charger often advertises without a name: typing the MAC must work."""
    _, bt = patched
    unnamed = MagicMock(address=ADDRESS, service_uuids=[], rssi=-89)
    unnamed.name = ADDRESS.replace(":", "-")  # HA uses the address as name
    other = MagicMock(address="11:22:33:44:55:66", service_uuids=[], rssi=-50)
    other.name = "Phone"
    bt.async_discovered_service_info.return_value = [unnamed, other]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema[CONF_ADDRESS].config["options"]
    assert [o["value"] for o in options] == ["11:22:33:44:55:66", ADDRESS]
    assert options[1]["label"] == f"(no name) - {ADDRESS}, -89 dBm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "not-a-mac"}
    )
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}

    bt.async_ble_device_from_address.return_value = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "aa:bb:cc:dd:ee:ff"}
    )
    assert result["errors"] == {CONF_ADDRESS: "not_in_range"}

    bt.async_ble_device_from_address.return_value = BLE_DEVICE
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS.replace(":", "-").lower()}
    )
    assert result["step_id"] == "passkey"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSKEY: "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == ADDRESS


async def test_config_flow_fallback_and_error_detail(hass, patched):
    """Interrupted pairing retries with direct BlueZ pairing; errors show detail."""
    _, bt = patched
    info = MagicMock(address=ADDRESS, service_uuids=[], rssi=-89)
    info.name = "HOME"
    bt.async_discovered_service_info.return_value = [info]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    # Both strategies interrupted -> specific error + BlueZ detail shown.
    interrupted = pairing.PairingInterrupted("org.bluez.Error.AuthenticationCanceled (agent calls: none)")
    with patch(
        "custom_components.nexxtender_ble.client.pairing.async_pair",
        AsyncMock(side_effect=[interrupted, interrupted]),
    ), patch("custom_components.nexxtender_ble.client.asyncio.sleep", AsyncMock()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "123456"}
        )
    assert result["errors"] == {"base": "pairing_interrupted"}
    assert "AuthenticationCanceled" in result["description_placeholders"]["error_detail"]

    # First strategy interrupted, fallback works -> entry created.
    calls = []

    async def _pair(*args):
        calls.append(args)
        if len(calls) == 1:
            raise interrupted

    pair = AsyncMock(side_effect=_pair)
    with patch("custom_components.nexxtender_ble.client.pairing.async_pair", pair), patch(
        "custom_components.nexxtender_ble.client.asyncio.sleep", AsyncMock()
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "123456"}
        )
    # 1 = interrupted, 2 = direct fallback (then the new entry connects normally).
    assert pair.await_count >= 2
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_config_flow_removes_stale_bond(hass, patched, charger):
    """Field bug: setup showed 'auth_required'. BlueZ still had an old bond, so
    pairing was skipped and the charger refused access. The flow must remove the
    bond and pair again with the new PIN."""
    pair, bt = patched
    info = MagicMock(address=ADDRESS, service_uuids=[], rssi=-89)
    info.name = "HOME"
    bt.async_discovered_service_info.return_value = [info]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    charger.encrypted = False  # stale bond: async_pair is a no-op

    async def _unpair(*args):
        charger.encrypted = True  # the next pairing is a real one

    unpair = AsyncMock(side_effect=_unpair)
    with patch("custom_components.nexxtender_ble.client.pairing.async_unpair", unpair), patch(
        "custom_components.nexxtender_ble.client.asyncio.sleep", AsyncMock()
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "123456"}
        )
    unpair.assert_awaited_once()
    assert pair.await_count >= 2
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_config_flow_auth_required_after_repair(hass, patched, charger):
    """If the charger still refuses after re-pairing, show auth_required."""
    _, bt = patched
    info = MagicMock(address=ADDRESS, service_uuids=[], rssi=-89)
    info.name = "HOME"
    bt.async_discovered_service_info.return_value = [info]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    charger.encrypted = False
    with patch(
        "custom_components.nexxtender_ble.client.pairing.async_unpair", AsyncMock()
    ), patch("custom_components.nexxtender_ble.client.asyncio.sleep", AsyncMock()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSKEY: "123456"}
        )
    assert result["errors"] == {"base": "auth_required"}
    assert "Unlikely Error" in result["description_placeholders"]["error_detail"]


async def test_clock_fallback_and_diagnostics(hass, patched, charger):
    """If the clock read fails, the connection still works; diagnostics dump raw data."""
    orig = charger.write_gatt_char

    async def no_time_read(uuid, data, response=None):
        if uuid == CHAR_GENERIC_COMMAND and bytes(data) == p.CMD_TIME_READ:
            charger.writes.append((uuid, bytes(data)))
            return  # charger never answers the clock read
        await orig(uuid, data, response)

    charger.write_gatt_char = no_time_read
    with patch("custom_components.nexxtender_ble.client.STATUS_TIMEOUT", 0.05):
        entry = await _setup(hass)
        for _ in range(30):
            await hass.async_block_till_done()
            await asyncio.sleep(0.02)
            if entry.runtime_data.client.data.charger_time:
                break
    assert entry.runtime_data.client.connected
    assert _state(hass, "sensor.nexxtender_charger_clock") == "unavailable"
    week = _state(hass, "sensor.nexxtender_off_peak_schedule_week")
    assert week.endswith(":00") and week.split(" - ")[0][3:] == "00"

    from custom_components.nexxtender_ble.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_PASSKEY] == "**REDACTED**"
    assert "cbor" not in diag
    assert diag["schedules"]["legacy_minutes"]["week"] == [1320, 420]
    assert "config" in diag["extra"]["raw"]
    assert "time_read" in diag["extra"]["errors"]
    assert any("write dd 0240" in line for line in diag["extra"]["status_log"])



async def test_reconnect_with_stale_pairing_repairs(hass, patched, charger):
    """Reproduces the field log: after a reconnect the link is not encrypted,
    the charger answers 'Unlikely Error'. The integration must drop the pairing
    and pair again with the PIN, then work."""
    pair_mock, _ = patched
    state = {"paired": True}  # BlueZ thinks it is still paired
    charger.encrypted = False

    async def _pair(address, passkey, path=None, *a, **k):
        if not state["paired"]:  # real pairing -> encrypted link
            state["paired"] = True
            charger.encrypted = True

    async def _unpair(address, path=None):
        state["paired"] = False
        charger.encrypted = False

    async def _get_state(address, path=None):
        return state["paired"], None  # old BlueZ: no Bonded property

    pair_mock.side_effect = _pair
    with patch("custom_components.nexxtender_ble.pairing.async_unpair", AsyncMock(side_effect=_unpair)) as unpair, \
         patch("custom_components.nexxtender_ble.pairing.async_get_state", AsyncMock(side_effect=_get_state)):
        entry = await _setup(hass)
        for _ in range(30):
            if entry.runtime_data.client.connected:
                break
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()
        assert unpair.await_count == 1
        assert entry.runtime_data.client.connected
        assert _state(hass, "binary_sensor.nexxtender_bluetooth_connected") == "on"
        assert _state(hass, "sensor.nexxtender_car_power") == "7400"


async def test_paired_not_bonded_is_cleared_before_connecting(hass, patched, charger):
    pair_mock, _ = patched
    with patch("custom_components.nexxtender_ble.pairing.async_unpair", AsyncMock()) as unpair, \
         patch("custom_components.nexxtender_ble.pairing.async_get_state", AsyncMock(return_value=(True, False))):
        entry = await _setup(hass)
        assert unpair.await_count >= 1
        assert entry.runtime_data.client.connected


async def test_link_drop_during_setup_is_clean(hass, patched, charger):
    """Field log: "'NoneType' object has no attribute 'start_notify'"."""
    charger.drop_during_subscribe = True
    entry = await _setup(hass)
    for _ in range(30):
        if entry.runtime_data.client.connected:
            break
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    assert entry.runtime_data.client.connected  # retried and connected
    errors = entry.runtime_data.client.data.extra
    assert "NoneType" not in str(entry.runtime_data.last_error or "")



async def test_device_registry_filled_without_deprecated_api(hass, patched, charger, caplog):
    from homeassistant.helpers import device_registry as dr

    await _setup(hass)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ADDRESS)) if hasattr(
        dr.async_get(hass), "async_get_device_by_identifier"
    ) else next(
        d for d in dr.async_get(hass).devices.values() if (DOMAIN, ADDRESS) in d.identifiers
    )
    assert device.serial_number == "12345-67-8901-23456-78"
    assert device.sw_version == "2.53"
    assert "deprecated" not in caplog.text



async def test_clock_sync_without_success_status(hass, patched, charger):
    """Field log: 'Charger did not answer the clock write'. The charger may never
    send 'time set success'; reading the clock back is enough. (Not used on
    connect since 0.3.0, kept for when writes come back.)"""
    charger.sends_time_success = False
    entry = await _setup(hass)
    with patch("custom_components.nexxtender_ble.client.TIME_CONFIRM_TIMEOUT", 0.05):
        await entry.runtime_data.client.sync_time()
    data = entry.runtime_data.client.data
    assert charger.clock > 1_700_000_000
    assert data.charger_time is not None
    assert data.extra["info"]["time_set_confirmed"] is False
    assert data.extra["info"]["clock_drift_after_sync_s"] < 5
    assert "time_sync" not in data.extra.get("errors", {})


async def test_notifications_from_an_old_connection_are_ignored(hass, patched, charger):
    """Field diagnostics: every status was logged 2-5 times. After re-pairing,
    bleak keeps the old connection's watcher, so each notification also arrives
    through the old connection. Only the current connection may count."""
    entry = await _setup(hass)
    client = entry.runtime_data.client
    old_status_callback = charger.callbacks[CHAR_GENERIC_STATUS]
    client._client = FakeCharger()  # a newer connection replaced the old one
    log_len = len(client.data.extra["status_log"])
    old_status_callback(None, bytearray(p.GS_CONFIG_READ_POPPED.to_bytes(2, "little")))
    assert len(client.data.extra["status_log"]) == log_len


async def test_cbor_leftovers_are_cleaned_up(hass, patched, charger):
    """0.3.4 removed the CBOR config entirely: the flag saved by 0.3.3 and the
    4 CBOR sensors disappear, and the CBOR request (04 50) is never sent."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSKEY: "123456", "cbor_supported": False},
    )
    entry.add_to_hass(hass)
    old = [
        registry.async_get_or_create("sensor", DOMAIN, f"{ADDRESS}_{key}", config_entry=entry).entity_id
        for key in ("peak_consumption_limit", "min_car_charging_speed",
                    "connection_type", "charging_phases")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    for _ in range(30):
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)
        if entry.runtime_data.client.data.charger_time is not None:
            break
    assert entry.runtime_data.client.connected
    assert "cbor_supported" not in entry.data
    assert all(registry.async_get(entity_id) is None for entity_id in old)
    assert (CHAR_GENERIC_COMMAND, b"\x04\x50") not in charger.writes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_link_dropped_with_reused_pairing_pairs_again(hass, patched, charger):
    """Field log (0.3.1): 'is already paired', then 'disconnected', then
    'Access check failed: Characteristic ...db was not found!', over and over.
    BlueZ reused a pairing the charger had forgotten. The integration must
    remove it and pair again with the PIN instead of reusing it forever."""
    pair_mock, _ = patched
    state = {"paired": True}
    charger.stale_bond_drops_link = True

    async def _pair(address, passkey, path=None, *a, **k):
        state["paired"] = True  # a fresh pairing: the charger accepts it

    async def _unpair(address, path=None):
        state["paired"] = False
        charger.stale_bond_drops_link = False

    async def _get_state(address, path=None):
        return state["paired"], True  # BlueZ: paired and bonded

    pair_mock.side_effect = _pair
    with patch("custom_components.nexxtender_ble.pairing.async_unpair", AsyncMock(side_effect=_unpair)) as unpair, \
         patch("custom_components.nexxtender_ble.pairing.async_get_state", AsyncMock(side_effect=_get_state)):
        entry = await _setup(hass)
        for _ in range(30):
            if entry.runtime_data.client.connected:
                break
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()
        assert unpair.await_count == 1
        assert entry.runtime_data.client.connected
        assert _state(hass, "sensor.nexxtender_car_power") == "7400"


async def test_reused_pairing_detected_by_the_pairing_step(hass, patched, charger):
    """Field log (Nov 28): 'is already paired', link dropped, then only a plain
    'Connection failed: Access check failed ... not found', no re-pairing. The
    pairing state query before connecting can fail; the pairing step's own
    'already paired' answer must be enough to trigger a fresh pairing."""
    pair_mock, _ = patched
    state = {"paired": True}
    charger.stale_bond_drops_link = True

    async def _pair(address, passkey, path=None, *a, **k):
        if state["paired"]:
            return False  # "is already paired": BlueZ reused its bond
        state["paired"] = True
        return True

    async def _unpair(address, path=None):
        state["paired"] = False
        charger.stale_bond_drops_link = False

    pair_mock.side_effect = _pair
    with patch("custom_components.nexxtender_ble.pairing.async_unpair", AsyncMock(side_effect=_unpair)) as unpair, \
         patch("custom_components.nexxtender_ble.pairing.async_get_state",
               AsyncMock(side_effect=pairing.PairingError("BlueZ does not know the device"))):
        entry = await _setup(hass)
        for _ in range(30):
            if entry.runtime_data.client.connected:
                break
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()
        assert unpair.await_count == 1
        assert entry.runtime_data.client.connected
