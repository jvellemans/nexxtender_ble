"""Passkey pairing with the Nexxtender through BlueZ over D-Bus.

The charger requires LE pairing with a fixed 6-digit passkey (the "PIN").
Home Assistant's Bluetooth stack can only do "Just Works" pairing, so this
module temporarily registers a BlueZ pairing agent (org.bluez.Agent1) on its
own D-Bus connection, answers the charger's passkey request with the PIN,
and asks BlueZ to pair. BlueZ then stores the bond, and later connections
are encrypted automatically.

This only works for local adapters handled by BlueZ (a USB or built-in
adapter on the Home Assistant host), not for ESPHome Bluetooth proxies.

NOTE: do not add ``from __future__ import annotations`` here. dbus-fast reads
the D-Bus signatures ('o', 'u', 's', ...) from the method annotations.
"""

import asyncio
import logging
import uuid

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

# Errors where the remote side refused the authentication. Only treated as
# "wrong PIN" when the charger actually asked for the PIN first.
_REJECT_ERRORS = {
    "org.bluez.Error.AuthenticationFailed",
    "org.bluez.Error.AuthenticationRejected",
}
# Errors that mean the link dropped or timed out during pairing.
_INTERRUPTED_ERRORS = {
    "org.bluez.Error.AuthenticationCanceled",
    "org.bluez.Error.AuthenticationTimeout",
    "org.bluez.Error.ConnectionAttemptFailed",
    "org.bluez.Error.InProgress",
}


class PairingError(Exception):
    """Pairing could not be completed (BlueZ unavailable, timeout, ...)."""


class InvalidPasskey(PairingError):
    """The charger asked for the PIN and then rejected it."""


class PairingInterrupted(PairingError):
    """The connection dropped or timed out during pairing (weak signal?)."""


class PasskeyNotRequested(PairingError):
    """Pairing failed without the charger ever asking for the PIN."""


class AuthenticationRequired(PairingError):
    """The charger refused access because there is no valid bond."""


class _PasskeyAgent(ServiceInterface):
    """Minimal BlueZ agent that only answers for one device."""

    def __init__(self, device_path, passkey):
        super().__init__("org.bluez.Agent1")
        self._device_path = device_path
        self._passkey = passkey
        self.passkey_requested = False
        self.events: list[str] = []

    def _check(self, device):
        if device != self._device_path:
            from dbus_fast import DBusError

            raise DBusError("org.bluez.Error.Rejected", "Unexpected device")

    @method()
    def Release(self):  # noqa: N802
        return

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802, F821
        self.events.append("RequestPasskey")
        self._check(device)
        self.passkey_requested = True
        _LOGGER.debug("Charger requested the passkey, answering")
        return self._passkey

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802, F821
        self.events.append("RequestPinCode")
        self._check(device)
        self.passkey_requested = True
        return f"{self._passkey:06d}"

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802, F821
        self.events.append("DisplayPasskey")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802, F821
        self.events.append("DisplayPinCode")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802, F821
        # Only reached for the device we are pairing with (see _check).
        self.events.append("RequestConfirmation")
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802, F821
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802, F821
        self._check(device)

    @method()
    def Cancel(self):  # noqa: N802
        self.events.append("Cancel")
        _LOGGER.debug("BlueZ cancelled the pairing request")


async def _connect_bus():
    try:
        return await MessageBus(
            bus_type=BusType.SYSTEM, negotiate_unix_fd=False
        ).connect()
    except Exception as err:  # noqa: BLE001
        raise PairingError(f"Cannot connect to the system D-Bus: {err}") from err


async def _call(bus, path, interface, member, signature="", body=None):
    reply = await bus.call(
        Message(
            destination=BLUEZ,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )
    )
    return reply


def _raise_for_error(reply, what):
    if reply.message_type != MessageType.ERROR:
        return
    detail = reply.body[0] if reply.body else ""
    raise PairingError(f"{what}: {reply.error_name} {detail}".strip())


def _raise_for_pair_error(reply, agent):
    """Classify a failed Device1.Pair reply."""
    if reply.message_type != MessageType.ERROR:
        return
    name = reply.error_name or ""
    detail = reply.body[0] if reply.body else ""
    events = ", ".join(agent.events) or "none"
    msg = f"{name} {detail}".strip() + f" (agent calls: {events})"
    _LOGGER.warning("Pairing with the charger failed: %s", msg)
    if name in _REJECT_ERRORS and agent.passkey_requested:
        raise InvalidPasskey(msg)
    if name in _INTERRUPTED_ERRORS or "Cancel" in agent.events:
        raise PairingInterrupted(msg)
    if name in _REJECT_ERRORS:
        raise PasskeyNotRequested(msg)
    raise PairingError(msg)


async def _find_device_path(bus, address, preferred_path=None):
    reply = await _call(bus, "/", OBJECT_MANAGER_IFACE, "GetManagedObjects")
    _raise_for_error(reply, "GetManagedObjects")
    address = address.upper()
    found = []
    for path, interfaces in reply.body[0].items():
        props = interfaces.get(DEVICE_IFACE)
        if props and str(props["Address"].value).upper() == address:
            found.append((path, props))
    if not found:
        raise PairingError(
            f"BlueZ does not know {address}; is the charger in range of a local adapter?"
        )
    for path, props in found:
        if path == preferred_path:
            return path, props
    # Prefer an adapter where the device is currently connected.
    found.sort(key=lambda item: not item[1].get("Connected", Variant("b", False)).value)
    return found[0]


async def async_get_state(address, preferred_path=None):
    """Return (paired, bonded). ``bonded`` is None on BlueZ < 5.66 (no property)."""
    bus = await _connect_bus()
    try:
        _, props = await _find_device_path(bus, address, preferred_path)
        paired = bool(props.get("Paired", Variant("b", False)).value)
        bonded = props.get("Bonded")
        return paired, None if bonded is None else bool(bonded.value)
    finally:
        bus.disconnect()


async def async_is_paired(address, preferred_path=None):
    """Return True if BlueZ has a bond for this device."""
    bus = await _connect_bus()
    try:
        _, props = await _find_device_path(bus, address, preferred_path)
        return bool(props.get("Paired", Variant("b", False)).value)
    finally:
        bus.disconnect()


async def async_pair(address, passkey, preferred_path=None, pair_timeout=45.0):
    """Pair ``address`` with BlueZ using ``passkey``. No-op if already paired."""
    if not 0 <= int(passkey) <= 999999:
        raise PairingError("The PIN must be 6 digits")

    bus = await _connect_bus()

    agent_path = f"/io/github/nexxtender_ble/agent_{uuid.uuid4().hex[:8]}"
    try:
        device_path, props = await _find_device_path(bus, address, preferred_path)
        if props.get("Paired", Variant("b", False)).value:
            _LOGGER.debug("%s is already paired", address)
            return

        agent = _PasskeyAgent(device_path, int(passkey))
        bus.export(agent_path, agent)
        reply = await _call(
            bus,
            "/org/bluez",
            AGENT_MANAGER_IFACE,
            "RegisterAgent",
            "os",
            [agent_path, "KeyboardOnly"],
        )
        _raise_for_error(reply, "RegisterAgent")
        try:
            _LOGGER.debug("Pairing %s (%s)", address, device_path)
            try:
                reply = await asyncio.wait_for(
                    _call(bus, device_path, DEVICE_IFACE, "Pair"), pair_timeout
                )
            except TimeoutError as err:
                raise PairingInterrupted(
                    f"Timed out after {pair_timeout:.0f}s "
                    f"(agent calls: {', '.join(agent.events) or 'none'})"
                ) from err
            if (
                reply.message_type == MessageType.ERROR
                and reply.error_name == "org.bluez.Error.AlreadyExists"
            ):
                _LOGGER.debug("%s: BlueZ reports already paired", address)
            else:
                _raise_for_pair_error(reply, agent)
            # Trust the device so BlueZ does not ask again on reconnect.
            reply = await _call(
                bus,
                device_path,
                PROPS_IFACE,
                "Set",
                "ssv",
                [DEVICE_IFACE, "Trusted", Variant("b", True)],
            )
            if reply.message_type == MessageType.ERROR:
                _LOGGER.debug("Could not set Trusted: %s", reply.error_name)
            _LOGGER.info("Paired with Nexxtender %s", address)
        finally:
            await _call(
                bus, "/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [agent_path]
            )
            bus.unexport(agent_path)
    finally:
        bus.disconnect()


async def async_unpair(address, preferred_path=None):
    """Remove the BlueZ bond for ``address`` (used before re-pairing)."""
    bus = await _connect_bus()
    try:
        device_path, _ = await _find_device_path(bus, address, preferred_path)
        adapter_path = device_path.rsplit("/", 1)[0]
        reply = await _call(
            bus, adapter_path, "org.bluez.Adapter1", "RemoveDevice", "o", [device_path]
        )
        _raise_for_error(reply, "RemoveDevice")
    finally:
        bus.disconnect()
