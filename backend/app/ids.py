"""Deterministic, run-scoped document ids.

Ids look like `ar_3f9c2a0007`: prefix + 6-char run tag + 4-digit counter. They are
deterministic for a given run and event order, which keeps LLM cache keys stable when
llm/client.py replaces the run tag with "RUNTAG" before hashing (golden-run replay).
"""

import secrets
from collections import defaultdict

PREFIXES = {
    "action_request": "ar_",
    "decision": "dec_",
    "approval": "apr_",
    "execution": "exe_",
    "message": "msg_",
    "plan": "plan_",
    "forecast": "fc_",
    "memory": "mem_",
}

_counters: dict[tuple[str, str], int] = defaultdict(int)


def new_run_id() -> str:
    return f"run_{secrets.token_hex(3)}"


def run_tag(run_id: str) -> str:
    return run_id.removeprefix("run_")


def new_id(prefix: str, run_id: str) -> str:
    """prefix is one of PREFIXES values, e.g. "ar_"."""
    _counters[(run_id, prefix)] += 1
    return f"{prefix}{run_tag(run_id)}{_counters[(run_id, prefix)]:04d}"


def seed_counter(run_id: str, prefix: str, value: int) -> None:
    """Resume support: start counting after `value` (e.g. the max existing id number)."""
    _counters[(run_id, prefix)] = max(_counters[(run_id, prefix)], value)
