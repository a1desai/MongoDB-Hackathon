"""Shared plumbing for the mock control systems: per-run state, commands, auth, idempotency.

All mock state for a run lives in `world.mock` (see `ensure_state`). Every accepted POST creates
a `Command` with one slot per target device. `apply_pending` (mock_systems/__init__.py) advances
commands each tick; the verifier reads them back with `command_status`.
"""

import inspect
import re
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

from .. import runtime
from ..config import get_settings
from ..ids import run_tag
from ..sim.world_state import World
from ..timeutil import at, parse_sim, sim_iso
from .keys import is_executor_key

# Slot states.
PENDING, APPLIED, OFFLINE, FAILED, IGNORED, REJECTED = 0, 1, 2, 3, 4, 5

# Device id prefixes, by system. The number is the household number (hh_00042 -> TL-00042).
DEVICE_PREFIX = {"thermlink": "TL", "chargeflow": "CF", "powercell": "PC"}
_NUM = re.compile(r"(\d+)$")

# Command id prefixes, by system.
CMD_PREFIX = {"thermlink": "tlb_", "chargeflow": "cfc_", "powercell": "pcd_", "textline": "txb_", "gridops": "gos_"}


class MockError(Exception):
    """An error response from a mock system (stored for idempotent replay unless 503)."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail


@dataclass
class Command:
    id: str
    handle: int
    system: str
    kind: str  # thermlink: event|restore|end; chargeflow: pause|resume; powercell: dispatch|stop; textline: send; gridops: open|close
    idx: np.ndarray  # household index per slot (-1 for non-household recipients)
    state: np.ndarray  # int8 slot states
    apply_tick: np.ndarray  # tick at which each slot is applied
    created_tick: int
    created_sim_ts: str
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    phase: str = "scheduled"  # scheduled | active | ended
    finished: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return int(self.state.size)


def mock_seed(world: World) -> int:
    sources = (world.mock.get("seed"), getattr(world, "seed", None), world.scenario.get("_seed"), world.scenario.get("seed"))
    for src in sources:
        if isinstance(src, int | np.integer):
            return int(src)
    return int(get_settings().sim_seed)


def ensure_state(world: World) -> dict[str, Any]:
    """Initialise `world.mock` once per run; return it."""
    m = world.mock
    if m.get("_ready"):
        return m
    n = world.n
    seed = mock_seed(world)
    m["seed"] = seed
    m["rng"] = {s: np.random.default_rng([seed, zlib.crc32(s.encode())]) for s in CMD_PREFIX}
    m["idem"] = {}  # "system:key" -> (status, body)
    m["commands"] = {}  # id -> Command
    m["handles"] = []  # handle -> command id
    m["seq"] = {s: 0 for s in CMD_PREFIX}
    # 6% of ThermLink thermostats are offline for the whole run (seeded, fixed set).
    offline_rng = np.random.default_rng([seed, 6006])
    m["thermlink"] = {
        "offline": (offline_rng.random(n) < 0.06) & world.has_thermostat,
        "owner": np.full(n, -1, dtype=np.int64),
    }
    m["chargeflow"] = {"owner": np.full(n, -1, dtype=np.int64)}
    m["powercell"] = {"owner": np.full(n, -1, dtype=np.int64), "stopped_soc": np.zeros(n, dtype=bool)}
    m["textline"] = {"delivered": [], "inbound": [], "last_delivered_idx": np.zeros(0, dtype=np.int64)}
    m["gridops"] = {"sections": {}, "attempted": set()}
    m["dirty"] = {"thermlink": set(), "chargeflow": set(), "powercell": set(), "commands": set(), "gridops": set()}
    m["_ready"] = True
    return m


def new_command(
    world: World,
    system: str,
    kind: str,
    idx: np.ndarray,
    *,
    latency: np.ndarray | int = 1,
    params: dict[str, Any] | None = None,
    request_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Command:
    m = ensure_state(world)
    m["seq"][system] += 1
    cid = f"{CMD_PREFIX[system]}{run_tag(world.run_id)}{m['seq'][system]:04d}"
    idx = np.asarray(idx, dtype=np.int64)
    cmd = Command(
        id=cid,
        handle=len(m["handles"]),
        system=system,
        kind=kind,
        idx=idx,
        state=np.zeros(idx.size, dtype=np.int8),
        apply_tick=world.tick + np.broadcast_to(np.asarray(latency, dtype=np.int64), idx.shape).copy(),
        created_tick=world.tick,
        created_sim_ts=sim_iso(world.sim_now),
        params=params or {},
        request_id=request_id,
        start=start,
        end=end,
        extra=extra or {},
    )
    m["handles"].append(cid)
    m["commands"][cid] = cmd
    m["dirty"]["commands"].add(cid)
    return cmd


def get_command(world: World, cid: str) -> Command:
    cmd = ensure_state(world)["commands"].get(cid)
    if cmd is None:
        raise MockError(404, "not_found", f"unknown id {cid}")
    return cmd


def counts(cmd: Command) -> dict[str, int]:
    s = cmd.state
    return {
        "total": cmd.total,
        "pending": int((s == PENDING).sum()),
        "applied": int((s == APPLIED).sum()),
        "offline": int((s == OFFLINE).sum()),
        "failed": int((s == FAILED).sum()),
        "ignored": int((s == IGNORED).sum()),
        "rejected": int((s == REJECTED).sum()),
    }


# ---------------------------------------------------------------- ids and time

def device_id(system: str, hh_idx: int) -> str:
    return f"{DEVICE_PREFIX[system]}-{hh_idx + 1:05d}"


def parse_device_ids(world: World, ids: list[str], eligible: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Map device ids (TL-00042, hh_00042, ...) to household indices. Returns (idx, rejected_ids)."""
    out: list[int] = []
    rejected: list[str] = []
    seen: set[int] = set()
    n = world.n
    for d in ids:
        mt = _NUM.search(d or "")
        i = int(mt.group(1)) - 1 if mt else -1
        if 0 <= i < n and eligible[i] and i not in seen:
            out.append(i)
            seen.add(i)
        else:
            rejected.append(d)
    return np.asarray(out, dtype=np.int64), rejected


def parse_when(world: World, value: str | None, default: datetime) -> datetime:
    """'HH:MM' (same simulated day) or an ISO timestamp."""
    if not value:
        return default
    try:
        if len(value) <= 5 and ":" in value:
            return at(world.sim_now, value)
        return parse_sim(value)
    except ValueError as e:
        raise MockError(422, "bad_time", f"cannot parse time {value!r}") from e


def world_event(summary: str, **payload: Any) -> dict[str, Any]:
    return {"type": "world_event", "summary": summary, "payload": payload}


# ---------------------------------------------------------------- request plumbing

@dataclass
class MockCtx:
    system: str
    world: World

    async def once(self, key: str, fn: Callable[[], Any | Awaitable[Any]]) -> JSONResponse:
        """Run fn at most once per Idempotency-Key; replay the stored response on repeats.

        fn returns a JSON-able body (status 200) or raises MockError. 503s are not stored, so a
        retry with the same key gets a fresh attempt.
        """
        store = ensure_state(self.world)["idem"]
        slot = f"{self.system}:{key}"
        if slot in store:
            status, body = store[slot]
            return JSONResponse(body, status_code=status, headers={"Idempotent-Replayed": "true"})
        try:
            res = fn()
            if inspect.isawaitable(res):
                res = await res
            status, body = 200, res
        except MockError as e:
            status, body = e.status, {"error": e.code, "detail": e.detail}
        if status != 503:
            store[slot] = (status, body)
        return JSONResponse(body, status_code=status)

    async def read(self, fn: Callable[[], Any]) -> JSONResponse:
        try:
            return JSONResponse(fn())
        except MockError as e:
            return JSONResponse({"error": e.code, "detail": e.detail}, status_code=e.status)


def context(system: str) -> Callable[..., Awaitable[MockCtx]]:
    """FastAPI dependency: checks X-Api-Key (executor only, else 403) and resolves X-Run-Id."""

    async def dep(
        x_api_key: str | None = Header(default=None),
        x_run_id: str | None = Header(default=None),
    ) -> MockCtx:
        if not is_executor_key(system, x_api_key):
            raise HTTPException(403, {"error": "forbidden", "detail": f"{system} accepts only the agent:executor key"})
        if not x_run_id:
            raise HTTPException(400, {"error": "missing_run_id", "detail": "X-Run-Id header is required"})
        try:
            world = runtime.get_world(x_run_id)
        except KeyError:
            raise HTTPException(404, {"error": "unknown_run", "detail": f"run {x_run_id} is not live"}) from None
        ensure_state(world)
        return MockCtx(system=system, world=world)

    return dep


async def idempotency_key(idempotency_key: str | None = Header(default=None)) -> str:
    if not idempotency_key:
        raise HTTPException(400, {"error": "missing_idempotency_key", "detail": "Idempotency-Key header is required"})
    return idempotency_key
