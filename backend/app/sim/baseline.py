"""Status-quo operator for baseline runs (PLAN §7.8): no agents, same seed and weather.

- uniform +2 °C setback for every enrolled thermostat 16:00-20:00: no pre-cool, no targeting,
  no vulnerability exemptions
- one broadcast template SMS appeal at 17:00 (no incentive)
- while there is a shortfall, rolling outages in fixed section order, 60 minutes each, skipping
  protected sections (at most one new section per tick, plus hand-offs as cuts expire)
- simultaneous restore of every setback and outage at 20:00, which causes the rebound

It acts on the World directly (there is no governed pipeline in baseline mode) and returns
world_event dicts for the engine to log as actor "sim:world".
"""

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..timeutil import at, hhmm, sim_iso
from . import behavior, grid, metrics, thermal
from .world_state import World

ACTOR = "sim:baseline_operator"
SETBACK_C = 2.0
SETBACK_START, SETBACK_END = "16:00", "20:00"
APPEAL_AT = "17:00"
RESTORE_AT = "20:00"
OUTAGE_MIN = 60
APPEAL_ACCEPT_SCALE = 0.5  # an appeal without an incentive persuades about half as many homes
APPEAL_BODY = ("Lakeshore Grid Ops: the grid is under strain this evening. Please reduce air conditioning "
               "and delay large appliances until 20:00. Reply STOP to opt out")


def _event(summary: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "world_event", "summary": summary, "payload": {"actor": ACTOR, **payload}}


class BaselineOperator:
    """Applies the status-quo playbook to the world, one call per tick (after grid.aggregate)."""

    def __init__(self, world: World | None = None) -> None:
        self.setback_on = False
        self.setback_done = False
        self.appeal_done = False
        self.restored = False
        self.pointer = 0  # next position in the fixed eligible-section order
        self.order: list[int] | None = None
        self.cut_until: dict[int, datetime] = {}
        if world is not None:
            self._order(world)

    # ------------------------------------------------------------ helpers

    def _order(self, world: World) -> list[int]:
        if self.order is None:
            self.order = [int(i) for i in np.argsort(world.section_ids) if not bool(world.section_protected[i])]
        return self.order

    def _next_section(self, world: World) -> int | None:
        order = self._order(world)
        for _ in range(len(order)):
            s = int(order[self.pointer % len(order)])
            self.pointer += 1
            if not world.section_cut[s]:
                return s
        return None

    def _homes(self, world: World, mask: np.ndarray) -> tuple[int, int]:
        return grid.real(world, int(mask.sum())), grid.real(world, int((mask & world.vulnerable).sum()))

    # ------------------------------------------------------------ playbook steps

    def _start_setback(self, world: World) -> list[dict]:
        m = world.has_thermostat & world.consent_thermostat
        idx = np.flatnonzero(m)
        mw = float(thermal.kw_saved_estimate(world, idx, SETBACK_C).sum()) * world.scale / 1000.0
        world.offset_c[idx] = SETBACK_C
        world.setback_active[idx] = True
        world.precool_active[idx] = False
        world.setback_count_today[idx] += 1
        self.setback_on = self.setback_done = True
        metrics.record_voluntary(world, "thermostat_setback", mw)
        homes, vul = self._homes(world, m)
        return [_event(
            f"Baseline: uniform +{SETBACK_C:.0f} °C setback on {homes:,} enrolled homes {SETBACK_START}-{SETBACK_END} "
            f"(no pre-cool, no exemptions; {vul:,} vulnerable homes included), est. {mw:,.0f} MW",
            {"kind": "baseline_setback", "offset_c": SETBACK_C, "homes_real": homes, "vulnerable_real": vul,
             "est_mw": round(mw, 1)})]

    def _appeal(self, world: World) -> list[dict]:
        m = world.sms_opt_in & ~world.sms_stopped
        idx = np.flatnonzero(m)
        before = world.duty.copy()
        accepted = behavior.apply_sms_offer(world, idx, at(world.sim_now, RESTORE_AT), accept_scale=APPEAL_ACCEPT_SCALE,
                                            include_vulnerable=True, include_featured=True, count_message=True)
        acc = world.sms_accepted & m
        mw = float((world.ac_kw * before * (1 - thermal.SMS_DUTY_FACTOR))[acc].sum()) * world.scale / 1000.0
        metrics.record_voluntary(world, "send_sms", mw)
        self.appeal_done = True
        homes, _ = self._homes(world, m)
        return [_event(
            f"Baseline: broadcast appeal texted to {homes:,} homes; {grid.real(world, accepted):,} cut back cooling "
            f"(est. {mw:,.0f} MW)",
            {"kind": "baseline_sms_appeal", "homes_real": homes, "accepted_real": grid.real(world, accepted),
             "est_mw": round(mw, 1), "body": APPEAL_BODY})]

    def _cut(self, world: World, s: int) -> dict:
        until = world.sim_now + timedelta(minutes=OUTAGE_MIN)
        mw = float(world.section_demand_mw[s])
        grid.set_section_power(world, s, False, until, source="baseline")
        self.cut_until[s] = until
        mask = world.section_idx == s
        homes, vul = self._homes(world, mask)
        sid = world.section_ids[s]
        return _event(
            f"Baseline: rolling outage {sid} for {OUTAGE_MIN} min ({homes:,} homes, {vul:,} vulnerable, "
            f"~{mw:,.0f} MW) with no notice",
            {"kind": "baseline_outage", "section_id": sid, "until": sim_iso(until), "duration_min": OUTAGE_MIN,
             "homes_real": homes, "vulnerable_real": vul, "mw": round(mw, 1)})

    def _restore_section(self, world: World, s: int, why: str) -> dict:
        grid.set_section_power(world, s, True, None)
        self.cut_until.pop(s, None)
        sid = world.section_ids[s]
        return _event(f"Baseline: {sid} re-energised ({why})", {"kind": "baseline_restore", "section_id": sid})

    def _restore_all(self, world: World) -> list[dict]:
        events: list[dict] = []
        m = world.setback_active.copy()
        world.offset_c[m] = 0.0
        world.setback_active[m] = False
        self.setback_on = False
        cut = [s for s in list(self.cut_until) if world.section_cut[s]]
        for s in cut:
            grid.set_section_power(world, s, True, None)
        self.cut_until.clear()
        self.restored = True
        homes, _ = self._homes(world, m)
        events.append(_event(
            f"Baseline: simultaneous restore at {RESTORE_AT}: {homes:,} thermostats back to normal and "
            f"{len(cut)} section(s) re-energised at once",
            {"kind": "baseline_restore_all", "homes_real": homes,
             "sections": [world.section_ids[s] for s in cut]}))
        return events

    # ------------------------------------------------------------ tick

    def step(self, world: World) -> list[dict]:
        """Apply this tick's status-quo actions to the world; returns world_event dicts."""
        now = world.sim_now
        events: list[dict] = []
        if not self.setback_done and at(now, SETBACK_START) <= now < at(now, SETBACK_END):
            events += self._start_setback(world)
        if not self.appeal_done and now >= at(now, APPEAL_AT):
            events += self._appeal(world)
        if not self.restored and now >= at(now, RESTORE_AT):
            events += self._restore_all(world)

        # rolling outages: release expired cuts, then hand off / add sections while short
        released_mw = 0.0
        released = 0
        for s, until in list(self.cut_until.items()):
            if now >= until:
                released_mw += float(world.section_demand_mw[s])
                released += 1
                events.append(self._restore_section(world, s, f"{OUTAGE_MIN}-minute rolling outage ended at {hhmm(now)}"))
        need = (world.demand_mw - world.supply_mw) + released_mw  # signed: a surplus needs no hand-off
        budget = released + 1  # hand-offs plus at most one new section per tick
        while need > 0 and budget > 0:
            s = self._next_section(world)
            if s is None:
                break
            events.append(self._cut(world, s))
            need -= max(float(world.section_demand_mw[s]), 1.0)
            budget -= 1
        return events
