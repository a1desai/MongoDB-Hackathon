"""Shared API helpers: NaN/ObjectId-safe JSON, error mapping, live-engine lookup."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import numpy as np
from bson import ObjectId
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import runtime


def jsonable(obj: Any) -> Any:
    """Convert Mongo docs, Pydantic models and numpy values into strict JSON (NaN/inf become null)."""
    if obj is None or isinstance(obj, (bool, str, int)) and not isinstance(obj, np.generic):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, BaseModel):
        return jsonable(obj.model_dump(by_alias=True, exclude_none=True))
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return jsonable(float(obj))
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode()
    return str(obj)


def dumps(obj: Any) -> str:
    return json.dumps(jsonable(obj), separators=(",", ":"), ensure_ascii=False)


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")


def J(content: Any, status_code: int = 200) -> SafeJSONResponse:
    return SafeJSONResponse(content, status_code=status_code)


def http_error(exc: Exception) -> HTTPException:
    """Map governance-layer exceptions onto HTTP status codes."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc) or "forbidden")
    if isinstance(exc, (KeyError, LookupError, FileNotFoundError)):
        msg = exc.args[0] if exc.args else "not found"
        return HTTPException(404, str(msg))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


def live_engine(run_id: str) -> Any | None:
    """The RunEngine for a run live in this process, or None."""
    try:
        return runtime.get_engine(run_id)
    except KeyError:
        return None


def require_engine(run_id: str) -> Any:
    eng = live_engine(run_id)
    if eng is None:
        raise HTTPException(409, f"run {run_id} is not live in this process")
    return eng


def latest_live_run_id() -> str | None:
    ids = runtime.live_run_ids()
    return ids[-1] if ids else None


def require_human(by: str, *, allowed: tuple[str, ...] | None = None) -> None:
    """Humans act through the UI identity switcher; agents never call these endpoints."""
    if not by.startswith("human:"):
        raise HTTPException(403, f"{by} is not a human identity")
    if allowed is not None and by not in allowed:
        raise HTTPException(403, f"{by} may not do this; requires {' or '.join(allowed)}")
