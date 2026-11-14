"""One physics step for the whole world: weather -> thermal -> follower behaviour (PLAN §7.2).

`world_step` simulates the interval [sim_now, sim_now + dt) and does not move the clock; the
engine calls `clock.advance(world)` at the end of the tick. Grid aggregation, contingencies and
the uncontrolled-trip check live in sim/grid.py and run after the mock systems have applied
their commands.
"""

import numpy as np

from . import behavior, thermal
from .weather import outdoor_c
from .world_state import World


def t_safe_c(world: World) -> float:
    try:
        return float(world.scenario.get("t_safe_c", 29.0))
    except (TypeError, ValueError):
        return 29.0


def _accumulate(world: World, minutes: int) -> None:
    """Per-home and per-section minute counters for the interval just simulated."""
    off = ~world.power_on
    if off.any():
        world.outage_minutes[off] += minutes
    cut = np.asarray(world.section_cut, dtype=bool)
    if cut.any():
        world.section_cut_minutes_today[cut] += minutes
    hot = world.t_in > t_safe_c(world)
    if hot.any():
        world.over_t_safe_minutes[hot] += minutes


def world_step(world: World, dt_h: float) -> list[dict]:
    """Advance physics by dt_h hours at world.sim_now; returns world_event dicts."""
    t_out = outdoor_c(world, world.sim_now)
    world.metrics["last_outdoor_c"] = round(float(t_out), 2)
    minutes = int(round(dt_h * 60))
    thermal.step_thermal(world, dt_h, t_out)
    events = behavior.step_behavior(world, dt_h)
    # behaviour may start or stop chargers/batteries: refresh the load for this tick
    world.p_kw[:] = thermal.household_kw(world, world.sim_now)
    _accumulate(world, minutes)
    return events
