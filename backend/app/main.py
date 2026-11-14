"""FastAPI app: REST + WebSocket API, the five mock control systems, change-stream listeners.

Run from backend/: `.venv/bin/uvicorn app.main:app --reload --port 8000`.

On startup one database-level change stream (PLAN §6.5) feeds the console from what is in Atlas,
not from what an agent claims:
  ledger inserts          -> WS {type:"ledger"}
  approvals inserts/updates -> WS {type:"approval"}; decided ones -> Governance.resume_request
  agent_control updates   -> world.halted + WS {type:"control"} and a fresh tick
  action_requests changes -> WS {type:"request"} (joined with decision / approval / execution)
  plans / messages inserts -> WS {type:"plan"} / {type:"message"}
The listener restarts itself (with its resume token) after any error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import runtime
from .api import routers as api_routers
from .api.common import J, live_engine
from .api.ws import hub
from .config import get_settings
from .db import ACTION_REQUESTS, AGENT_CONTROL, APPROVALS, LEDGER, MESSAGES, PLANS, db

log = logging.getLogger("cooldown")

WATCHED = [LEDGER, APPROVALS, AGENT_CONTROL, ACTION_REQUESTS, PLANS, MESSAGES]
CARD_FLUSH_S = 0.25


class ChangeListener:
    """One db-level change stream with automatic restart; dispatches to the WS hub and live engines."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.card_task: asyncio.Task | None = None
        self.resume_token: Any = None
        self.ok = False
        self.events = 0
        self.errors = 0
        self.last_error: str | None = None
        self._cards: dict[str, str] = {}  # request_id -> run_id, coalesced per flush
        self._cards_ready = asyncio.Event()

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="change-stream")
        self.card_task = asyncio.create_task(self._card_pump(), name="request-cards")

    async def stop(self) -> None:
        for t in (self.task, self.card_task):
            if t is not None:
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t

    def status(self) -> dict[str, Any]:
        return {"ok": self.ok, "events": self.events, "errors": self.errors, "last_error": self.last_error}

    async def _run(self) -> None:
        backoff = 1.0
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]},
                                "ns.coll": {"$in": WATCHED}}}]
        while True:
            try:
                kw: dict[str, Any] = {"full_document": "updateLookup"}
                if self.resume_token is not None:
                    kw["resume_after"] = self.resume_token
                async with await db().watch(pipeline, **kw) as stream:
                    self.ok, backoff = True, 1.0
                    log.info("change stream open on %s", ", ".join(WATCHED))
                    async for change in stream:
                        self.resume_token = stream.resume_token
                        self.events += 1
                        try:
                            await self._dispatch(change)
                        except Exception:
                            log.exception("change dispatch failed (%s)", change.get("ns"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ok = False
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if "resume" in str(exc).lower() or "history" in str(exc).lower():
                    self.resume_token = None
                log.warning("change stream error (%s); restarting in %.0fs", self.last_error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _dispatch(self, change: dict[str, Any]) -> None:
        coll = (change.get("ns") or {}).get("coll")
        op = change.get("operationType")
        doc = change.get("fullDocument")
        if not doc:
            return
        if coll == AGENT_CONTROL:
            run_id = str(doc.get("_id"))
            eng = live_engine(run_id)
            if eng is not None:
                eng.on_control_update(doc)
            hub.publish(run_id, {"type": "control", "control": doc})
            return
        run_id = doc.get("run_id")
        if not run_id:
            return
        eng = live_engine(run_id)
        if coll == APPROVALS:
            if eng is not None and op != "insert":
                eng.on_approval_update(doc)
            hub.publish(run_id, {"type": "approval", "approval": doc})
            return
        if not hub.has_subscribers(run_id):
            return
        if coll == LEDGER and op == "insert":
            hub.publish(run_id, {"type": "ledger", "entry": doc})
        elif coll == ACTION_REQUESTS:
            self._cards[str(doc["_id"])] = run_id
            self._cards_ready.set()
        elif coll == PLANS and op == "insert":
            hub.publish(run_id, {"type": "plan", "plan": doc})
        elif coll == MESSAGES and op == "insert":
            hub.publish(run_id, {"type": "message", "entry": doc})

    async def _card_pump(self) -> None:
        """Coalesce request changes and publish one joined card per request per flush."""
        from .api.runs import request_card

        while True:
            await self._cards_ready.wait()
            await asyncio.sleep(CARD_FLUSH_S)
            self._cards_ready.clear()
            batch, self._cards = self._cards, {}
            for rid, run_id in batch.items():
                try:
                    card = await request_card(rid)
                    if card is not None:
                        hub.publish(run_id, {"type": "request", "request": card})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("request card %s failed", rid)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Executor call is too noisy
    s = get_settings()
    app.state.http = httpx.AsyncClient(base_url=s.api_base_url, timeout=15.0)
    listener = ChangeListener()
    app.state.listener = listener
    if s.mongodb_uri:
        listener.start()
    else:
        log.error("MONGODB_URI is not set: no change streams, runs cannot start")
    try:
        yield
    finally:
        for run_id in runtime.live_run_ids():
            eng = live_engine(run_id)
            if eng is not None:
                with contextlib.suppress(Exception):
                    await eng.stop()
        await listener.stop()
        await app.state.http.aclose()


app = FastAPI(title="GridLock", version="0.1.0", lifespan=lifespan,
              description="Governed AI agents keep Toronto's power on through a heat wave, and can prove it.")
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _r in api_routers:
    app.include_router(_r)

try:
    from .mock_systems import routers as mock_routers

    for _r in mock_routers:
        app.include_router(_r)
except Exception:  # the API must come up even if a mock system has a bug
    logging.getLogger("cooldown").exception("mock systems failed to load")


@app.get("/")
async def root() -> Any:
    return J({"name": "GridLock", "docs": "/docs", "health": "/health"})


@app.get("/health")
async def health() -> Any:
    """Liveness plus configuration presence (booleans only, never secret values)."""
    s = get_settings()
    db_ok, db_error = False, None
    try:
        await asyncio.wait_for(db().command("ping"), 3.0)
        db_ok = True
    except Exception as exc:
        db_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    listener: ChangeListener | None = getattr(app.state, "listener", None)
    key = s.openai_api_key if s.llm_provider == "openai" else s.anthropic_api_key
    return J({
        "ok": db_ok, "db": {"ok": db_ok, "name": s.mongodb_db, "error": db_error},
        "change_stream": listener.status() if listener else None,
        "live_runs": runtime.live_run_ids(), "ws": hub.stats(),
        "llm": {"provider": s.llm_provider, "key_set": bool(key), "smart": s.llm_model_smart,
                "fast": s.llm_model_fast, "mode": s.llm_mode},
        "embed_mode": s.embed_mode, "langsmith": bool(s.langsmith_tracing and s.langsmith_api_key),
    })
