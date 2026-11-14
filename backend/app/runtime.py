"""Process-wide registry of live runs, so mock-system routers and APIs can find a run's World."""

from typing import Any

from .sim.world_state import World

_worlds: dict[str, World] = {}
_engines: dict[str, Any] = {}  # run_id -> sim.engine.RunEngine


def register(world: World, engine: Any) -> None:
    _worlds[world.run_id] = world
    _engines[world.run_id] = engine


def unregister(run_id: str) -> None:
    _worlds.pop(run_id, None)
    _engines.pop(run_id, None)


def get_world(run_id: str) -> World:
    try:
        return _worlds[run_id]
    except KeyError:
        raise KeyError(f"run {run_id} is not live in this process") from None


def get_engine(run_id: str) -> Any:
    return _engines[run_id]


def live_run_ids() -> list[str]:
    return list(_worlds)
