"""API keys for the five mock control systems.

Each system issues its own key, and only `agent:executor` holds them (PLAN §7.7, §8.1). These
are fictional sandbox keys for simulated vendors, not secrets. `EXECUTOR_KEY` is the
executor's master credential; it is accepted by every system as well, for tooling and tests.
"""

import hashlib

EXECUTOR_IDENTITY = "agent:executor"

SYSTEMS = ("thermlink", "chargeflow", "powercell", "textline", "gridops")


def _derive(label: str) -> str:
    return hashlib.sha256(f"cooldown-sandbox:{label}".encode()).hexdigest()[:32]


EXECUTOR_KEY = f"cdx_exec_{_derive('agent:executor')}"

# Per-system keys, all issued to agent:executor.
SYSTEM_KEYS: dict[str, str] = {s: f"{s[:2]}_live_{_derive(s + ':agent:executor')}" for s in SYSTEMS}

# Keys held by other identities. The mock systems reject them with 403 (used to demo/test R01).
OTHER_KEYS: dict[str, str] = {
    "agent:planner": f"cdx_plan_{_derive('agent:planner')}",
    "human:operator": f"cdx_oper_{_derive('human:operator')}",
}


def key_for(system: str) -> str:
    """The key the executor sends to `system`."""
    return SYSTEM_KEYS[system]


def is_executor_key(system: str, key: str | None) -> bool:
    return bool(key) and key in (SYSTEM_KEYS.get(system), EXECUTOR_KEY)
