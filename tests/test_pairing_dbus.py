"""End-to-end test of the pairing agent against a fake BlueZ on a real D-Bus.

Starts a private dbus-daemon, exports a minimal fake ``org.bluez`` (object
manager, agent manager, one device). The fake device's Pair() calls back into
whichever agent the caller registered and checks the passkey, which is what
BlueZ does for passkey-entry pairing.
"""

import asyncio
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method

pytestmark = [
    pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="no dbus-daemon"),
]


@pytest.fixture(autouse=True)
def _allow_unix_sockets():
    """The HA test plugin blocks socket connects; this test needs D-Bus."""
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    pytest_socket._remove_restrictions()  # noqa: SLF001
    yield

_PATH = pathlib.Path(__file__).parents[1] / "custom_components/nexxtender_ble/pairing.py"
_spec = importlib.util.spec_from_file_location("nexx_pairing", _PATH)
pairing = importlib.util.module_from_spec(_spec)
sys.modules["nexx_pairing"] = pairing
_spec.loader.exec_module(pairing)

ADDRESS = "FB:11:22:33:44:55"
DEV_PATH = "/org/bluez/hci0/dev_FB_11_22_33_44_55"
CORRECT_PIN = 123456


class FakeAgentManager(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.AgentManager1")
        self.agents = {}  # sender -> (path, capability)

    @method()
    def RegisterAgent(self, agent: "o", capability: "s"):  # noqa: F821
        self.agents[self._sender] = (agent, capability)

    @method()
    def UnregisterAgent(self, agent: "o"):  # noqa: F821
        self.agents.pop(self._sender, None)


class FakeDevice(ServiceInterface):
    def __init__(self, bus, agent_manager):
        super().__init__("org.bluez.Device1")
        self.bus = bus
        self.agent_manager = agent_manager
        self.paired = False
        self.trusted = False
        self.capability_seen = None
        self.mode = "normal"  # or "no_passkey" / "dropped"

    @method()
    async def Pair(self):  # noqa: N802
        sender = self._sender
        if sender not in self.agent_manager.agents:
            raise DBusError("org.bluez.Error.AuthenticationFailed", "no agent")
        path, cap = self.agent_manager.agents[sender]
        self.capability_seen = cap
        if self.mode == "no_passkey":
            raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        if self.mode == "dropped":
            raise DBusError("org.bluez.Error.AuthenticationCanceled", "Authentication Canceled")
        reply = await self.bus.call(
            Message(destination=sender, path=path, interface="org.bluez.Agent1",
                    member="RequestPasskey", signature="o", body=[DEV_PATH])
        )
        if reply.message_type == MessageType.ERROR or reply.body[0] != CORRECT_PIN:
            raise DBusError("org.bluez.Error.AuthenticationFailed", "bad passkey")
        self.paired = True

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":  # noqa: F821
        return ADDRESS

    @dbus_property(access=PropertyAccess.READ)
    def Paired(self) -> "b":  # noqa: F821
        return self.paired

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> "b":  # noqa: F821
        return True

    @dbus_property()
    def Trusted(self) -> "b":  # noqa: F821
        return self.trusted

    @Trusted.setter
    def Trusted(self, value: "b"):  # noqa: F821
        self.trusted = value


@pytest.fixture
def dbus_address(tmp_path):
    sock = tmp_path / "bus"
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address",
         f"--address=unix:path={sock}"],
        stdout=subprocess.PIPE, text=True,
    )
    address = proc.stdout.readline().strip()
    yield address
    proc.terminate()
    proc.wait()


async def _fake_bluez(address):
    bus = await MessageBus(bus_address=address).connect()
    agent_manager = FakeAgentManager()
    device = FakeDevice(bus, agent_manager)
    # dbus-fast does not expose the caller; capture it via a message handler.
    def _capture_sender(msg):
        for iface in (agent_manager, device):
            iface._sender = msg.sender
        return None
    bus.add_message_handler(_capture_sender)
    bus.export("/org/bluez", agent_manager)
    bus.export(DEV_PATH, device)
    await bus.request_name("org.bluez")
    return bus, device


@pytest.fixture
def patched_bus(monkeypatch, dbus_address):
    real = MessageBus

    def _factory(*args, bus_type=None, **kwargs):
        return real(bus_address=dbus_address, **kwargs)

    monkeypatch.setattr(pairing, "MessageBus", _factory)
    return dbus_address


async def test_pair_with_correct_pin(patched_bus):
    bus, device = await _fake_bluez(patched_bus)
    try:
        await pairing.async_pair(ADDRESS, CORRECT_PIN)
        assert device.paired
        assert device.trusted
        assert device.capability_seen == "KeyboardOnly"
        # Second call is a no-op because BlueZ reports Paired.
        assert await pairing.async_is_paired(ADDRESS)
        await pairing.async_pair(ADDRESS, CORRECT_PIN)
    finally:
        bus.disconnect()


async def test_pair_with_wrong_pin(patched_bus):
    bus, device = await _fake_bluez(patched_bus)
    try:
        with pytest.raises(pairing.InvalidPasskey):
            await pairing.async_pair(ADDRESS, 654321)
        assert not device.paired
    finally:
        bus.disconnect()


async def test_unknown_device(patched_bus):
    bus, _ = await _fake_bluez(patched_bus)
    try:
        with pytest.raises(pairing.PairingError):
            await pairing.async_pair("AA:BB:CC:DD:EE:FF", CORRECT_PIN)
    finally:
        bus.disconnect()


async def test_failure_without_passkey_request_is_not_wrong_pin(patched_bus):
    bus, device = await _fake_bluez(patched_bus)
    device.mode = "no_passkey"
    try:
        with pytest.raises(pairing.PasskeyNotRequested) as exc:
            await pairing.async_pair(ADDRESS, CORRECT_PIN)
        assert not isinstance(exc.value, pairing.InvalidPasskey)
        assert "agent calls: none" in str(exc.value)
    finally:
        bus.disconnect()


async def test_dropped_link_is_interrupted(patched_bus):
    bus, device = await _fake_bluez(patched_bus)
    device.mode = "dropped"
    try:
        with pytest.raises(pairing.PairingInterrupted):
            await pairing.async_pair(ADDRESS, CORRECT_PIN)
    finally:
        bus.disconnect()


async def test_get_state(patched_bus):
    bus, _ = await _fake_bluez(patched_bus)
    try:
        assert await pairing.async_get_state(ADDRESS) == (False, None)
        await pairing.async_pair(ADDRESS, CORRECT_PIN)
        assert await pairing.async_get_state(ADDRESS) == (True, None)
    finally:
        bus.disconnect()
