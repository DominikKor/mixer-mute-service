"""Mission Spotlight: a desktop profile whose path is a folder; the file of
the current week is picked by date.

    <folder>\\<YYYY>_<Q>.Quartal\\<WW>*   e.g. I:\\Spotlight\\2026_3.Quartal\\01_Testvideo.mp4

YYYY is the year and Q the quarter of today's date (one folder per year and
quarter, no fallback to a folder without the year), WW the week within the
quarter, two digits: days
1-7 of the quarter are week 01, days 8-14 week 02, and so on (the last days
of a quarter may make a week 14). The display agent fills in "*" on the
target PC; with several matching files the newest one wins.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


def quarter_and_week(today: Optional[date] = None) -> tuple[int, int]:
    today = today or date.today()
    quarter = (today.month - 1) // 3 + 1
    start = date(today.year, 3 * (quarter - 1) + 1, 1)
    return quarter, (today - start).days // 7 + 1


def subfolder(year: int, quarter: int) -> str:
    return f"{year}_{quarter}.Quartal"


def week_prefix(week: int) -> str:
    return f"{week:02d}"


def spotlight_path(folder: str, today: Optional[date] = None) -> str:
    today = today or date.today()
    quarter, week = quarter_and_week(today)
    return f"{folder.rstrip(chr(92) + '/')}\\{subfolder(today.year, quarter)}\\{week_prefix(week)}*"


def current(today: Optional[date] = None) -> dict:
    """What the settings show: year, quarter, week and the names that are looked for."""
    today = today or date.today()
    quarter, week = quarter_and_week(today)
    return {"year": today.year, "quarter": quarter, "week": week,
            "subfolder": subfolder(today.year, quarter), "file_prefix": week_prefix(week)}


def profile_path(profile, today: Optional[date] = None) -> str:
    """The path the display agent gets for a profile."""
    return spotlight_path(profile.path, today) if profile.mission_spotlight else profile.path
