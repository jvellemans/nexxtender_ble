"""Off-peak calendar tests."""

from datetime import datetime
import importlib.util
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).parents[1] / "custom_components/nexxtender_ble/tariff.py"
_spec = importlib.util.spec_from_file_location("nexx_tariff", _PATH)
t = importlib.util.module_from_spec(_spec)
sys.modules["nexx_tariff"] = t
_spec.loader.exec_module(t)

# 2026-11-16 is a Monday.
MON, FRI, SAT, SUN = 16, 20, 21, 22


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (at(MON, 6, 59), True),   # Monday morning, still night
        (at(MON, 7, 0), False),
        (at(MON, 12, 0), False),  # no midday window in Brussels
        (at(MON, 21, 59), False),
        (at(MON, 22, 0), True),
        (at(FRI, 21, 0), False),
        (at(FRI, 22, 30), True),  # weekend starts Friday 22:00
        (at(SAT, 12, 0), True),
        (at(SUN, 18, 0), True),
    ],
)
def test_brussels_flanders(when, expected):
    assert t.is_off_peak(when, t.PROFILE_BRUSSELS_FLANDERS) is expected


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (at(MON, 10, 59), False),
        (at(MON, 11, 0), True),   # midday window
        (at(MON, 16, 59), True),
        (at(MON, 17, 0), False),
        (at(SAT, 9, 0), False),   # no weekend bonus anymore
        (at(SUN, 23, 0), True),
    ],
)
def test_wallonia_2026(when, expected):
    assert t.is_off_peak(when, t.PROFILE_WALLONIA_2026) is expected


def test_shifted_21_6():
    assert t.is_off_peak(at(MON, 21, 0), t.PROFILE_SHIFTED_21_6)
    assert not t.is_off_peak(at(MON, 6, 0), t.PROFILE_SHIFTED_21_6)
    assert t.is_off_peak(at(SAT, 12, 0), t.PROFILE_SHIFTED_21_6)


def test_next_change():
    assert t.next_change(at(MON, 15, 20), t.PROFILE_BRUSSELS_FLANDERS) == at(MON, 22)
    assert t.next_change(at(FRI, 23, 0), t.PROFILE_BRUSSELS_FLANDERS) == datetime(2026, 11, 23, 7)
    assert t.next_change(at(MON, 8, 0), t.PROFILE_WALLONIA_2026) == at(MON, 11)
