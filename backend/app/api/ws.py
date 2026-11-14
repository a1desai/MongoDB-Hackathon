"""WebSocket fan-out: `/ws?run_id=`. publish() never blocks the tick loop.

Each client gets a bounded queue drained by its own sender task; slow clients drop the
oldest messages instead of stalling the simulation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .common import dumps, live_engine

log = logging.getLogger("cooldown.ws")

router = APIRouter(tags=["ws"])

ALL = "*"  # subscribers that passed no run_id receive every run's messages
QUEUE_MAX = 2000


class _Client:
    def __init__(self, ws: WebSocket, run_id: str):
        self.ws = ws
        self.run_id = run_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0

    def put(self, text: str) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(text)

    async def pump(self) -> None:
        while True:
            text = await self.queue.get()
            await self.ws.send_text(text)


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[_Client]] = defaultdict(set)

    def add(self, client: _Client) -> None:
        self._subs[client.run_id].add(client)

    def remove(self, client: _Client) -> None:
        subs = self._subs.get(client.run_id)
        if subs is not None:
            subs.discard(client)
            if not subs:
                self._subs.pop(client.run_id, None)

    def has_subscribers(self, run_id: str) -> bool:
        return bool(self._subs.get(run_id) or self._subs.get(ALL))

    def publish(self, run_id: str, msg: dict[str, Any]) -> None:
        """Queue `msg` for every client of `run_id` (and wildcard clients). Synchronous, non-blocking."""
        targets = list(self._subs.get(run_id, ())) + list(self._subs.get(ALL, ()))
        if not targets:
            return
        if "run_id" not in msg:
            msg = {**msg, "run_id": run_id}
        try:
            text = dumps(msg)
        except Exception:
            log.exception("could not serialise %s message", msg.get("type"))
            return
        for c in targets:
            c.put(text)

    def stats(self) -> dict[str, int]:
        return {rid: len(cs) for rid, cs in self._subs.items()}


hub = Hub()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, run_id: str | None = None) -> None:
    await ws.accept()
    client = _Client(ws, run_id or ALL)
    hub.add(client)
    # Snapshot so a late joiner sees the current clock, map and sections immediately.
    eng = live_engine(run_id) if run_id else None
    if eng is not None:
        try:
            for msg in eng.snapshot_messages():
                client.put(dumps(msg))
        except Exception:
            log.exception("snapshot for %s failed", run_id)
    sender = asyncio.create_task(client.pump(), name=f"ws-send:{run_id}")
    try:
        while True:
            text = await ws.receive_text()
            if text == "ping":
                client.put('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws %s closed", run_id, exc_info=True)
    finally:
        hub.remove(client)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
