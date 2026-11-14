"""Load identities and the active, versioned policy set from Atlas (with a short cache).

The Gate calls these on every decision, so results are cached for a couple of wall seconds.
If the collections are empty (an unseeded database), we fall back to `governance/seed.py`,
which is the single source of truth for policy params.
"""

import logging
import time
from typing import Any

from app import db as dbm
from app.models import Identity, Policy

log = logging.getLogger(__name__)

CACHE_SECONDS = 2.0
_cache: dict[str, tuple[float, Any]] = {}


def invalidate() -> None:
    """Drop cached identities/policies (call after editing them)."""
    _cache.clear()


def _cached(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def seed_identities() -> dict[str, Identity]:
    try:
        from app.governance.seed import IDENTITIES
    except Exception:  # pragma: no cover - seed is owned by another workstream
        return {}
    return {i.id: i for i in IDENTITIES}


def seed_policies() -> tuple[dict[str, Policy], str]:
    try:
        from app.governance.seed import POLICIES, POLICY_VERSION
    except Exception:  # pragma: no cover
        return {}, "unversioned"
    return {p.id: p for p in POLICIES}, POLICY_VERSION


async def load_identities() -> dict[str, Identity]:
    """All identities keyed by id ("agent:planner", "human:supervisor", ...)."""
    if (hit := _cached("identities")) is not None:
        return hit
    out: dict[str, Identity] = {}
    try:
        async for doc in dbm.db()[dbm.IDENTITIES].find({}):
            try:
                ident = Identity.model_validate(doc)
                out[ident.id] = ident
            except Exception as exc:
                log.warning("skipping invalid identity %s: %s", doc.get("_id"), exc)
    except Exception as exc:
        log.warning("load_identities failed, using seed: %s", exc)
    if not out:
        out = seed_identities()
    _cache["identities"] = (time.monotonic(), out)
    return out


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in v.replace("-", ".").split("."))


async def load_policies() -> tuple[dict[str, Policy], str]:
    """Active policies of the newest policy-set version, and that version string."""
    if (hit := _cached("policies")) is not None:
        return hit
    docs: list[Policy] = []
    try:
        async for doc in dbm.db()[dbm.POLICIES].find({}):
            try:
                docs.append(Policy.model_validate(doc))
            except Exception as exc:
                log.warning("skipping invalid policy %s: %s", doc.get("_id"), exc)
    except Exception as exc:
        log.warning("load_policies failed, using seed: %s", exc)
    if docs:
        version = max((p.version for p in docs), key=_version_key)
        policies = {p.id: p for p in docs if p.version == version}
        result = (policies, version)
    else:
        result = seed_policies()
    _cache["policies"] = (time.monotonic(), result)
    return result
