"""REST + WebSocket API (PLAN §13). `routers` is what main.py mounts."""

from fastapi import APIRouter

from . import approvals, audit, control, runs, scenarios, world, ws

routers: list[APIRouter] = [scenarios.router, runs.router, approvals.router, control.router, audit.router, world.router, ws.router]

__all__ = ["routers"]
