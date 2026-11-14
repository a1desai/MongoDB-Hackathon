"""Headless sim runner: steps a World 12:00-21:30 without the API, agents or database.

Tick order matches PLAN §7.2 (minus mock systems and agents):
    physics.world_step -> contingencies -> expired world cuts -> grid.aggregate
    -> uncontrolled-trip check -> baseline operator (baseline mode) -> metrics.update -> clock.advance

    cd backend && .venv/bin/python -m app.sim.headless                # synthetic 2,000-home world
    cd backend && .venv/bin/python -m app.sim.headless --files        # data/processed/*.json
    cd backend && .venv/bin/python -m app.sim.headless --uncontrolled # no operator at all
"""

import argparse
import time
from typing import Any

from . import clock, grid, metrics, physics
from .baseline import BaselineOperator
from .world_state import World


def step_once(world: World, operator: BaselineOperator | None = None) -> tuple[dict, list[dict]]:
    """One full tick; returns (aggregate, world events)."""
    dt_h = clock.dt_hours()
    events = physics.world_step(world, dt_h)
    events += grid.check_contingencies(world)
    for sid in grid.release_expired_cuts(world):
        events.append({"type": "world_event", "summary": f"{sid} re-energised after the uncontrolled trip",
                       "payload": {"kind": "trip_restore", "section_id": sid}})
    agg = grid.aggregate(world)
    trip = grid.check_uncontrolled_trip(world)
    if trip:
        events.append(trip)
    if operator is not None:
        events += operator.step(world)
    metrics.update(world, dt_h)
    clock.advance(world)
    return agg, events


def run(world: World, operator: BaselineOperator | None = None, verbose: bool = False) -> dict[str, Any]:
    """Step to sim_end; returns {"summary", "timeline", "events", "ms_per_tick"}."""
    timeline: list[dict[str, Any]] = []
    events: list[dict] = []
    t0 = time.perf_counter()
    ticks = 0
    while not clock.is_finished(world):
        ts = world.sim_now.strftime("%H:%M")
        agg, evs = step_once(world, operator)
        ticks += 1
        row = {"t": ts, "demand": agg["demand_mw"], "supply": agg["supply_mw"], "short": agg["shortfall_mw"],
               "cut": int(world.section_cut.sum()), "out_c": world.metrics.get("last_outdoor_c")}
        timeline.append(row)
        for e in evs:
            events.append({"t": ts, **e})
            if verbose:
                print(f"  {ts} [{e['type']}] {e['summary']}")
        if verbose and world.sim_now.minute % 30 == 0:
            print(f"{ts}  out {row['out_c']:>5} C  demand {row['demand']:>8.1f}  supply {row['supply']:>7.1f}  "
                  f"short {row['short']:>6.1f}  sections cut {row['cut']}")
    ms = (time.perf_counter() - t0) * 1000 / max(ticks, 1)
    return {"summary": metrics.summary(world), "timeline": timeline, "events": events, "ms_per_tick": ms}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", action="store_true", help="build from data/processed/*.json instead of synthetic")
    ap.add_argument("--n", type=int, default=2000, help="synthetic household count")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--uncontrolled", action="store_true", help="no baseline operator")
    args = ap.parse_args()
    from . import loader, scenario, synthetic

    sc = scenario.load_scenario(scenario.DEFAULT_SCENARIO_ID)
    if args.files:
        world = loader.build_world("run_headless", "baseline", sc, args.seed, loader.file_docs())
    else:
        world = synthetic.make_world(args.n, args.seed, "baseline", sc, run_id="run_headless")
    print(f"{world.n} homes, scale {world.scale:.1f}, calibration {world.metrics.get('calibration')}")
    out = run(world, None if args.uncontrolled else BaselineOperator(world), verbose=True)
    print(f"\n{out['ms_per_tick']:.2f} ms/tick")
    for k, v in out["summary"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
