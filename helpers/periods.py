"""weekly/monthly leaderboard period numbering (UTC)

each guess-points row is filed under a week number and a month number. numbering starts at 1 in
the launch week/month, so the current period resets naturally when a new week/month begins (the
new number simply has no rows yet) while past periods stay stored forever.
"""

from __future__ import annotations

import datetime

# week 1 starts monday 2026-07-13 (the launch week); month 1 is july 2026. all boundaries are UTC
_EPOCH_MONDAY = datetime.date(2026, 7, 13)
_EPOCH_YEAR = 2026
_EPOCH_MONTH = 7


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def week_index(day: "datetime.date | None" = None) -> int:
    """1-based number of the week (monday-anchored) containing the day"""
    day = day or _today()
    monday = day - datetime.timedelta(days=day.weekday())
    return (monday - _EPOCH_MONDAY).days // 7 + 1


def month_index(day: "datetime.date | None" = None) -> int:
    """1-based number of the month containing the day"""
    day = day or _today()
    return (day.year - _EPOCH_YEAR) * 12 + (day.month - _EPOCH_MONTH) + 1


def _midnight(day: datetime.date) -> datetime.datetime:
    return datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)


def next_week_reset(day: "datetime.date | None" = None) -> datetime.datetime:
    """the upcoming monday 00:00 UTC when the weekly board rolls over"""
    day = day or _today()
    return _midnight(day + datetime.timedelta(days=7 - day.weekday()))


def next_month_reset(day: "datetime.date | None" = None) -> datetime.datetime:
    """the upcoming 1st-of-month 00:00 UTC when the monthly board rolls over"""
    day = day or _today()
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return _midnight(datetime.date(year, month, 1))
