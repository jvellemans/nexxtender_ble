"""Belgian bi-hourly (day/night) off-peak calendars.

Pure functions (no Home Assistant imports) so they can be unit-tested.

Sources (checked November 2026):
- Brussels (Sibelga) and Flanders (Fluvius): off-peak 22:00-07:00 on weekdays
  and the whole weekend (Friday 22:00 to Monday 07:00).
- Wallonia (ORES, RESA, AIEG, AIESH) since 1 January 2026: off-peak
  22:00-07:00 and 11:00-17:00 every day, no weekend distinction.
- About ten municipalities (ORES/Fluvius) use a shifted 21:00-06:00 window.

Public holidays are not handled.
"""

from __future__ import annotations

from datetime import datetime, timedelta

PROFILE_BRUSSELS_FLANDERS = "brussels_flanders"
PROFILE_WALLONIA_2026 = "wallonia_2026"
PROFILE_SHIFTED_21_6 = "shifted_21_6"
PROFILES = [PROFILE_BRUSSELS_FLANDERS, PROFILE_WALLONIA_2026, PROFILE_SHIFTED_21_6]
DEFAULT_PROFILE = PROFILE_BRUSSELS_FLANDERS


def is_off_peak(now: datetime, profile: str) -> bool:
    """Return True if ``now`` (local time) is in the off-peak period."""
    hour = now.hour
    if profile == PROFILE_WALLONIA_2026:
        return hour >= 22 or hour < 7 or 11 <= hour < 17
    if profile == PROFILE_SHIFTED_21_6:
        night_start, night_end = 21, 6
    else:
        night_start, night_end = 22, 7
    if now.weekday() >= 5:  # Saturday, Sunday
        return True
    # Friday from night_start and Monday until night_end are covered here.
    return hour >= night_start or hour < night_end


def next_change(now: datetime, profile: str) -> datetime | None:
    """Next time the off-peak state flips (minute resolution, within 8 days)."""
    current = is_off_peak(now, profile)
    t = now.replace(second=0, microsecond=0)
    # Changes happen on the hour; step hourly from the next full hour.
    t = t.replace(minute=0) + timedelta(hours=1)
    for _ in range(8 * 24):
        if is_off_peak(t, profile) != current:
            return t
        t += timedelta(hours=1)
    return None
