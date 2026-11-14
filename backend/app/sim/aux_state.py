"""Sim-private bookkeeping that is not part of the frozen World contract.

Stored as a plain instance attribute (`world._sim_aux`). A copied World (`copy.copy` or
`copy.deepcopy`, e.g. the Forecaster's look-ahead) gets its own deep copy on first access, so
look-ahead steps never mutate the live run's bookkeeping. A World built some other way gets a
fresh one lazily. Nothing here is persisted or sent to the UI.
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .world_state import World

_ATTR = "_sim_aux"


@dataclass
class SimAux:
    sms_seen: np.ndarray  # int, sms_count_today already processed for acceptance
    sms_accept_until: np.ndarray  # int, sim minute when an accepted offer ends, -1 none
    ev_comply_u: np.ndarray  # float U(0,1) per home: owner honours a pause if u < ev_pause_acceptance
    featured_mask: np.ndarray  # bool
    cut_source: list[str | None]  # per section: "world" | "agent" | "baseline" | None
    fired_contingencies: set[int] = field(default_factory=set)
    contingency_restores: list[tuple[datetime, float, str]] = field(default_factory=list)
    override_new: int = 0  # overrides since the last override world_event
    override_last_emit_min: int = -10_000
    ev_override_new: int = 0
    ev_override_last_emit_min: int = -10_000
    owner: int = 0  # id() of the World this aux belongs to


def seed_of(world: World) -> int:
    try:
        return int(world.scenario.get("_seed", 0))
    except (TypeError, ValueError):
        return 0


def _new(world: World) -> SimAux:
    n = world.n
    # Static per-home draws come from their own seeded stream so they never depend on how
    # much of world.rng has been consumed (keeps governed and baseline runs comparable).
    static = np.random.default_rng([seed_of(world), 7_001])
    featured = np.zeros(n, dtype=bool)
    for idx in world.featured.values():
        if 0 <= int(idx) < n:
            featured[int(idx)] = True
    return SimAux(
        sms_seen=world.sms_count_today.astype(np.int64).copy(),
        sms_accept_until=np.full(n, -1, dtype=np.int64),
        ev_comply_u=static.random(n),
        featured_mask=featured,
        cut_source=["world" if c else None for c in world.section_cut],
    )


def get_aux(world: World) -> SimAux:
    aux = world.__dict__.get(_ATTR)
    if aux is not None and aux.owner != id(world):
        aux = copy.deepcopy(aux)  # inherited from the World this one was copied from
        aux.owner = id(world)
        world.__dict__[_ATTR] = aux
    if aux is None or aux.sms_seen.shape[0] != world.n or len(aux.cut_source) != len(world.section_ids):
        aux = _new(world)
        aux.owner = id(world)
        world.__dict__[_ATTR] = aux
    return aux
