"""REST + WebSocket API (PLAN §13). `routers` is what main.py mounts."""

from fastapi import APIRouter

from . import approvals, audit, control, runs, world, ws

routers: list[APIRouter] = [runs.router, approvals.router, control.router, audit.router, world.router, ws.router]

__all__ = ["routers"]
