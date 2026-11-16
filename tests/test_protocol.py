"""Unit tests for the pure protocol layer."""

import importlib.util
import pathlib
import struct
import sys

import pytest

# Import protocol.py directly (it has no Home Assistant dependencies).
_PATH = pathlib.Path(__file__).parents[1] / "custom_components/nexxtender_ble/protocol.py"
_spec = importlib.util.spec_from_file_location("nexx_protocol", _PATH)
p = importlib.util.module_from_spec(_spec)
sys.modules["nexx_protocol"] = p
_spec.loader.exec_module(p)


def test_crc16_modbus_check_value():
    # Standard check value for CRC-16/MODBUS.
    assert p.crc16(b"123456789") == 0x4B37


def test_crc_roundtrip():
    payload = p.with_crc(b"\x01\x02\x03")
    assert p.check_crc(payload)
    assert not p.check_crc(payload[:-1] + bytes([payload[-1] ^ 1]))


def test_parse_basic_matches_reference_layout():
    # seconds=3661, discriminator=2 (charging), status 'C', energy=12345 Wh, 3 phases
    raw = struct.pack("<HBB4sIBB", 3661, 2, ord("C"), b"\0" * 4, 12345, 0, 3)
    b = p.parse_basic(raw)
    assert b.seconds == 3661
    assert b.session_state == p.SESSION_CHARGING
    assert b.status == p.STATUS_CHARGING
    assert b.energy_wh == 12345
    assert b.phase_count == 3


@pytest.mark.parametrize(
    ("letter", "status"),
    [("A", "unplugged"), ("B", "plugged"), ("C", "charging"), ("D", "charging"),
     ("E", "fault"), ("F", "unplugged")],
)
def test_status_letters(letter, status):
    raw = struct.pack("<HBB4sIBB", 0, 0, ord(letter), b"\0" * 4, 0, 0, 1)
    assert p.parse_basic(raw).status == status


def test_parse_grid_car_advanced():
    grid = p.parse_grid(struct.pack("<IhhhhH", 100, 123, -5, 0, 2500, 450))
    assert (grid.l1, grid.l2, grid.l3, grid.consumed, grid.interval) == (12.3, -0.5, 0, 2500, 450)
    car = p.parse_car(struct.pack("<Ihhhhhh", 1, 160, 159, 161, 3680, 3657, 3703))
    assert car.current_total == 48.0
    assert (car.p1, car.p3) == (3680, 3703)
    adv = p.parse_advanced(struct.pack("<Ihii", 1, 25, -1500, 11000))
    assert (adv.available_current, adv.grid_power, adv.car_power) == (25, -1500, 11000)


def test_short_payload_raises():
    with pytest.raises(p.ProtocolError):
        p.parse_basic(b"\x00\x01")


def _legacy_config(i_max=25, evse=16, mode=0, i_min=6):
    body = struct.pack("<bbbbB hhhh", i_max, evse, mode, i_min, 0, 1320, 420, 0, 0)
    assert len(body) == 13
    return p.with_crc(body)


def test_config_parse_and_build():
    cfg = p.parse_config(_legacy_config())
    assert (cfg.i_max, cfg.i_evse_max, cfg.mode, cfg.i_min) == (25, 16, p.MODE_ECO_PRIVATE, 6)
    assert (cfg.week_start, cfg.week_stop) == (1320, 420)
    new = p.build_config(cfg, mode=p.MODE_MAX_OPEN, i_evse_max=32)
    assert p.check_crc(new) and len(new) == 15
    cfg2 = p.parse_config(new)
    assert (cfg2.mode, cfg2.i_evse_max, cfg2.i_max, cfg2.i_min) == (p.MODE_MAX_OPEN, 32, 25, 6)
    # Untouched bytes are preserved.
    assert new[4:13] == cfg.raw[4:13]


def test_config_rejects_bad_crc_and_values():
    raw = bytearray(_legacy_config())
    raw[0] ^= 0xFF
    with pytest.raises(p.ProtocolError):
        p.parse_config(bytes(raw))
    cfg = p.parse_config(_legacy_config())
    with pytest.raises(p.ProtocolError):
        p.build_config(cfg, i_max=0)
    with pytest.raises(p.ProtocolError):
        p.build_config(cfg, mode="turbo")


def _cbor_payload(i_capacity=16):
    # 18 integer entries, like the 0xB2 map the ESPHome client parses.
    entries = {1: 0, 2: 1, 3: 10, 4: 25, 5: 16, 6: 6, 7: 6, 8: 0, 9: 0, 10: 3,
               11: 123456, 12: 1320, 13: 420, 14: 0, 15: 0, 16: 0, 17: 5,
               19: i_capacity}
    out = bytearray(b"\x01\x02")  # unknown prefix bytes must be preserved
    out += bytes([0xB2])
    for k, v in entries.items():
        out += p._cbor_encode_int(k) + p._cbor_encode_int(v)
    return p.with_crc(bytes(out)), entries


def test_cbor_parse():
    raw, entries = _cbor_payload()
    cfg = p.parse_cbor_config(raw)
    assert cfg.values == entries
    assert cfg.prefix == b"\x01\x02"
    assert p.cbor_mode(cfg) == p.MODE_ECO_PRIVATE
    assert p.cbor_connection_type(cfg) == p.CONNECTION_MONO_TRI_N


@pytest.mark.parametrize("new_value", [10, 23, 24, 25, 40])
def test_cbor_rewrite_changes_only_target(new_value):
    raw, entries = _cbor_payload(i_capacity=16)
    cfg = p.parse_cbor_config(raw)
    new = p.build_cbor_config(cfg, {p.CBOR_I_CAPACITY: new_value})
    assert p.check_crc(new)
    cfg2 = p.parse_cbor_config(new)
    assert cfg2.values == {**entries, 19: new_value}
    assert cfg2.prefix == cfg.prefix


def test_cbor_rewrite_noop_is_identical():
    raw, _ = _cbor_payload()
    cfg = p.parse_cbor_config(raw)
    assert p.build_cbor_config(cfg, {19: 16}) == raw


def test_cbor_refuses_unknown_key():
    raw, _ = _cbor_payload()
    with pytest.raises(p.ProtocolError):
        p.build_cbor_config(p.parse_cbor_config(raw), {99: 1})


def test_time_roundtrip():
    assert p.parse_time(p.encode_time(1763215800)) == 1763215800


def test_generic_status_little_endian():
    assert p.parse_generic_status(b"\x03\x50") == p.GS_CONFIG_READ_POPPED
