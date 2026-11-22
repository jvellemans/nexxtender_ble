"""Constants for the Nexxtender BLE integration."""

from __future__ import annotations

DOMAIN = "nexxtender_ble"
MANUFACTURER = "Powerdale"

CONF_PASSKEY = "passkey"
OPT_KEEP_CONNECTED = "keep_connected"

# The charger advertises under this local name.
LOCAL_NAME = "HOME"

_PREFIX = "fd47416a-95fb-4206-88b5-b4a8045f75"
SERVICE_NEXXTENDER = f"{_PREFIX}c1"
CHAR_BASIC = f"{_PREFIX}cf"  # notify/read
CHAR_GRID = f"{_PREFIX}d0"  # notify
CHAR_CAR = f"{_PREFIX}da"  # notify
CHAR_ADVANCED = f"{_PREFIX}db"  # read
CHAR_GENERIC_COMMAND = f"{_PREFIX}dd"  # write
CHAR_GENERIC_STATUS = f"{_PREFIX}de"  # notify
CHAR_GENERIC_DATA = f"{_PREFIX}df"  # notify/write

# Standard Device Information service characteristics.
CHAR_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_HARDWARE = "00002a27-0000-1000-8000-00805f9b34fb"

POLL_INTERVAL_SECONDS = 15
RECONNECT_MIN_SECONDS = 10
RECONNECT_MAX_SECONDS = 300
