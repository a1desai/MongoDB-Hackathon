"""Simulated time is America/Toronto. sim_ts fields are ISO strings with the UTC offset."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")


def sim_iso(dt: datetime) -> str:
    """'2031-06-24T17:40:00-04:00'"""
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse_sim(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(TZ)


def hhmm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def at(day: datetime, hh_mm: str) -> datetime:
    """The given 'HH:MM' on the same simulated day as `day`."""
    h, m = (int(x) for x in hh_mm.split(":"))
    return datetime.combine(day.astimezone(TZ).date(), time(h, m), tzinfo=TZ)


def wall_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
