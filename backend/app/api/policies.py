"""Read the effective policy catalog used by the gate."""

from typing import Any

from fastapi import APIRouter

from ..governance.identities import load_policies
from .common import J

router = APIRouter(tags=["policies"])


@router.get("/policies")
async def policies() -> Any:
    rules, version = await load_policies()
    return J({"version": version, "rules": [rules[key].to_mongo() for key in sorted(rules)]})
