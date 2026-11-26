"""Pure-Python encoding/decoding of the Nexxtender Home BLE protocol.

Nothing in this module talks to Bluetooth or Home Assistant, so it can be
unit-tested on its own. The byte layouts come from the community
reverse-engineering in https://github.com/geertmeersman/nexxtender
(ESPHome client) and https://github.com/toSvenson/nexxtender-ble.

All multi-byte integers are little-endian.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

# Byte 3 of the "charging basic data" characteristic is an IEC 61851 state
# letter. Mapped like the ESPHome client does.
STATUS_UNPLUGGED = "unplugged"
STATUS_PLUGGED = "plugged"
STATUS_CHARGING = "charging"
STATUS_FAULT = "fault"
CHARGING_STATUSES = [STATUS_UNPLUGGED, STATUS_PLUGGED, STATUS_CHARGING, STATUS_FAULT]

_STATUS_LETTERS = {
    "B": STATUS_PLUGGED,
    "C": STATUS_CHARGING,
    "D": STATUS_CHARGING,
    "E": STATUS_FAULT,
}

# Byte 2 of the basic data: what the current session is doing.
SESSION_STARTED = "started"
SESSION_CHARGING = "charging"
SESSION_STOPPED = "stopped"
SESSION_UNKNOWN = "unknown"
SESSION_STATES = [SESSION_STARTED, SESSION_CHARGING, SESSION_STOPPED, SESSION_UNKNOWN]
_SESSION_CODES = {1: SESSION_STARTED, 2: SESSION_CHARGING, 3: SESSION_STOPPED}

# Charger operating modes as stored in the configuration.
MODE_ECO_PRIVATE = "eco_private"
MODE_MAX_PRIVATE = "max_private"
MODE_ECO_OPEN = "eco_open"
MODE_MAX_OPEN = "max_open"
MODE_TO_CODE = {
    MODE_ECO_PRIVATE: 0,
    MODE_MAX_PRIVATE: 1,
    MODE_ECO_OPEN: 4,
    MODE_MAX_OPEN: 5,
}
CODE_TO_MODE = {v: k for k, v in MODE_TO_CODE.items()}
MODES = list(MODE_TO_CODE)

# Values seen on the "generic status" characteristic (2 bytes, little-endian).
GS_UNLOCKED = 0x0001
GS_UNLOCKED_FORCE_MAX = 0x0002
GS_UNLOCKED_FORCE_ECO = 0x0003
GS_TIME_SET_READY = 0x4001
GS_TIME_SET_SUCCESS = 0x4002
GS_TIME_READ_POPPED = 0x4003
GS_CONFIG_SET_READY = 0x5001
GS_CONFIG_SET_SUCCESS = 0x5002
GS_CONFIG_READ_POPPED = 0x5003

GENERIC_STATUS_NAMES = {
    GS_UNLOCKED: "Unlocked",
    GS_UNLOCKED_FORCE_MAX: "Unlocked force max",
    GS_UNLOCKED_FORCE_ECO: "Unlocked force eco",
    GS_TIME_SET_READY: "Time set ready",
    GS_TIME_SET_SUCCESS: "Time set success",
    GS_TIME_READ_POPPED: "Time read",
    GS_CONFIG_SET_READY: "Config set ready",
    GS_CONFIG_SET_SUCCESS: "Config write success",
    GS_CONFIG_READ_POPPED: "Config read",
}

# Commands written to the "generic command" characteristic.
CMD_START_CHARGE_DEFAULT = b"\x01\x00"
CMD_START_CHARGE_MAX = b"\x02\x00"
CMD_START_CHARGE_AUTO = b"\x03\x00"
CMD_START_CHARGE_ECO = b"\x04\x00"
CMD_STOP_CHARGE = b"\x06\x00"
CMD_TIME_SET = b"\x01\x40"
CMD_TIME_READ = b"\x02\x40"
CMD_CONFIG_SET = b"\x01\x50"
CMD_CONFIG_READ = b"\x02\x50"

CONFIG_LEN = 15


class ProtocolError(ValueError):
    """Raised when a payload cannot be decoded or encoded."""


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def check_crc(payload: bytes) -> bool:
    """Return True if the last two bytes are a valid CRC of the rest."""
    if len(payload) < 3:
        return False
    return crc16(payload[:-2]) == int.from_bytes(payload[-2:], "little")


def with_crc(body: bytes) -> bytes:
    """Return ``body`` with its CRC appended."""
    return bytes(body) + crc16(body).to_bytes(2, "little")


# ---------------------------------------------------------------------------
# Notification / read payloads
# ---------------------------------------------------------------------------


def _need(data: bytes, length: int, what: str) -> None:
    if len(data) < length:
        raise ProtocolError(f"{what}: expected >= {length} bytes, got {len(data)}")


@dataclass(slots=True)
class BasicData:
    """Charging basic data (characteristic ...75cf)."""

    seconds: int
    session_state: str
    status: str
    status_letter: str
    energy_wh: int
    phase_count: int


def parse_basic(data: bytes) -> BasicData:
    _need(data, 14, "basic data")
    seconds, session_code, letter = struct.unpack_from("<HBB", data, 0)
    (energy,) = struct.unpack_from("<I", data, 8)
    letter_chr = chr(letter) if 32 <= letter < 127 else "?"
    return BasicData(
        seconds=seconds,
        session_state=_SESSION_CODES.get(session_code, SESSION_UNKNOWN),
        status=_STATUS_LETTERS.get(letter_chr, STATUS_UNPLUGGED),
        status_letter=letter_chr,
        energy_wh=energy,
        phase_count=data[13],
    )


@dataclass(slots=True)
class GridData:
    """Grid data (characteristic ...75d0)."""

    timestamp: int
    l1: float  # A
    l2: float
    l3: float
    consumed: int  # W
    interval: int  # seconds into the current quarter-hour (0..900)


def parse_grid(data: bytes) -> GridData:
    _need(data, 14, "grid data")
    ts, l1, l2, l3, consumed, interval = struct.unpack_from("<IhhhhH", data, 0)
    return GridData(ts, l1 / 10, l2 / 10, l3 / 10, consumed, interval)


@dataclass(slots=True)
class CarData:
    """Car data (characteristic ...75da)."""

    timestamp: int
    l1: float  # A
    l2: float
    l3: float
    p1: int  # W
    p2: int
    p3: int

    @property
    def current_total(self) -> float:
        return round(self.l1 + self.l2 + self.l3, 1)


def parse_car(data: bytes) -> CarData:
    _need(data, 16, "car data")
    ts, l1, l2, l3, p1, p2, p3 = struct.unpack_from("<Ihhhhhh", data, 0)
    return CarData(ts, l1 / 10, l2 / 10, l3 / 10, p1, p2, p3)


@dataclass(slots=True)
class AdvancedData:
    """Charging advanced data (characteristic ...75db, read only)."""

    timestamp: int
    available_current: int  # A
    grid_power: int  # W, negative = injecting
    car_power: int  # W


def parse_advanced(data: bytes) -> AdvancedData:
    _need(data, 14, "advanced data")
    ts, i_avail, grid_p, car_p = struct.unpack_from("<Ihii", data, 0)
    return AdvancedData(ts, i_avail, grid_p, car_p)


def parse_generic_status(data: bytes) -> int:
    _need(data, 2, "generic status")
    return int.from_bytes(data[:2], "little")


def parse_time(data: bytes) -> int:
    """Charger clock as a UNIX timestamp (response to CMD_TIME_READ)."""
    _need(data, 4, "time")
    return int.from_bytes(data[:4], "little")


def encode_time(unix_ts: int) -> bytes:
    return int(unix_ts).to_bytes(4, "little", signed=False)


# ---------------------------------------------------------------------------
# Configuration: legacy 15-byte format (CMD_CONFIG_READ / CMD_CONFIG_SET)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Config:
    """Legacy 15 byte configuration."""

    raw: bytes
    i_max: int  # maximum available capacity (A)
    i_evse_max: int  # maximum car charging speed (A)
    mode: str | None  # one of MODES, None if unknown code
    mode_code: int
    i_min: int  # offloading minimum (A)
    week_start: int  # minutes after midnight, UTC
    week_stop: int
    weekend_start: int
    weekend_stop: int


def parse_config(data: bytes) -> Config:
    if len(data) != CONFIG_LEN:
        raise ProtocolError(f"config: expected {CONFIG_LEN} bytes, got {len(data)}")
    if not check_crc(data):
        raise ProtocolError("config: CRC mismatch")
    i_max, i_evse_max, mode_code, i_min = struct.unpack_from("<bbbb", data, 0)
    ws, we, wes, wee = struct.unpack_from("<hhhh", data, 5)
    return Config(
        raw=bytes(data),
        i_max=i_max,
        i_evse_max=i_evse_max,
        mode=CODE_TO_MODE.get(mode_code),
        mode_code=mode_code,
        i_min=i_min,
        week_start=ws,
        week_stop=we,
        weekend_start=wes,
        weekend_stop=wee,
    )


def build_config(
    current: Config,
    *,
    i_max: int | None = None,
    i_evse_max: int | None = None,
    mode: str | None = None,
    i_min: int | None = None,
) -> bytes:
    """Return a new 15-byte config based on ``current`` with fields replaced.

    Only the first four bytes are touched (like the ESPHome client); the rest
    of the payload is kept as read from the charger and the CRC recomputed.
    """
    body = bytearray(current.raw[:-2])
    if i_max is not None:
        body[0] = _check_amps(i_max, "i_max")
    if i_evse_max is not None:
        body[1] = _check_amps(i_evse_max, "i_evse_max")
    if mode is not None:
        if mode not in MODE_TO_CODE:
            raise ProtocolError(f"unknown mode {mode!r}")
        body[2] = MODE_TO_CODE[mode]
    if i_min is not None:
        body[3] = _check_amps(i_min, "i_min")
    return with_crc(bytes(body))


def _check_amps(value: int, name: str) -> int:
    value = int(value)
    if not 0 < value <= 100:
        raise ProtocolError(f"{name}: {value} A is out of range")
    return value


def format_minutes(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
