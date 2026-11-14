"""Simulated clock helpers. One tick = TICK_SIM_MINUTES (default 5) simulated minutes.

`physics.world_step` does not move the clock; the engine calls `advance(world)` once per tick
after everything else for that tick has run.
"""

from datetime import datetime, time, timedelta

from ..config import get_settings
from ..timeutil import TZ
from .world_state import World


def tick_minutes() -> int:
    return int(get_settings().tick_sim_minutes)


def dt_hours() -> float:
    return tick_minutes() / 60.0


def advance(world: World, minutes: int | None = None) -> datetime:
    """Move the clock forward one tick (or `minutes`) and return the new sim time."""
    world.sim_now = world.sim_now + timedelta(minutes=minutes if minutes is not None else tick_minutes())
    world.tick += 1
    return world.sim_now


def is_finished(world: World) -> bool:
    return world.sim_now >= world.sim_end


def total_ticks(world: World) -> int:
    return int((world.sim_end - world.sim_start).total_seconds() // 60) // tick_minutes()


def day_midnight(world: World) -> datetime:
    """Midnight at the start of the simulated day (America/Toronto)."""
    return datetime.combine(world.sim_start.astimezone(TZ).date(), time(0, 0), tzinfo=TZ)


def day_minutes(world: World, t: datetime) -> float:
    """Minutes since midnight of the simulated day; exceeds 1440 past midnight (forecasts)."""
    return (t - day_midnight(world)).total_seconds() / 60.0


def day_hours(world: World, t: datetime) -> float:
    return day_minutes(world, t) / 60.0


def hour_of_day(t: datetime) -> float:
    lt = t.astimezone(TZ)
    return lt.hour + lt.minute / 60.0 + lt.second / 3600.0


def sim_minute(world: World, t: datetime | None = None) -> int:
    """Whole minutes since sim_start (the unit of World.notified_sim_minute)."""
    t = t or world.sim_now
    return int((t - world.sim_start).total_seconds() // 60)
