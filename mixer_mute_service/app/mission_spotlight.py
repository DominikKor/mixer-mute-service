"""Mission Spotlight: a desktop profile whose path is a folder; the file of
the current week is picked by date.

    <folder>\\<Q>. Quartal\\<WW>*      e.g. I:\\Spotlight\\3. Quartal\\01_Testvideo.mp4

Q is the quarter of today's date, WW the week within it, two digits: days
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


def subfolder(quarter: int) -> str:
    return f"{quarter}. Quartal"


def week_prefix(week: int) -> str:
    return f"{week:02d}"


def spotlight_path(folder: str, today: Optional[date] = None) -> str:
    quarter, week = quarter_and_week(today)
    return f"{folder.rstrip(chr(92) + '/')}\\{subfolder(quarter)}\\{week_prefix(week)}*"


def current(today: Optional[date] = None) -> dict:
    """What the settings show: quarter, week and the names that are looked for."""
    quarter, week = quarter_and_week(today)
    return {"quarter": quarter, "week": week, "subfolder": subfolder(quarter), "file_prefix": week_prefix(week)}


def profile_path(profile, today: Optional[date] = None) -> str:
    """The path the display agent gets for a profile."""
    return spotlight_path(profile.path, today) if profile.mission_spotlight else profile.path
