"""Request-graph routing (agents/graph.py) with LangGraph's in-memory saver: no Atlas, no LLM.

gate verdict -> deny: END; allow/partial_allow: execute; needs_approval: request_approval ->
interrupt() -> resume approved: execute / denied or expired: close. A kill-switch refusal parks
the thread in `hold` until it is resumed.
"""

import asyncio
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import DURABILITY, build_request_graph, waiting_on


class FakeSteps:
    def __init__(self, verdict: str, *, halted_executes: int = 0) -> None:
        self.verdict = verdict
        self.halted_executes = halted_executes
        self.calls: list[str] = []
        self.approvals_created = 0

    async def gate(self, state) -> dict[str, Any]:
        self.calls.append("gate")
        status = {"deny": "denied", "needs_approval": "needs_approval"}.get(self.verdict, "approved")
        return {"verdict": self.verdict, "decision_id": "dec_t0001", "status": status}

    async def request_approval(self, state) -> dict[str, Any]:
        self.calls.append("request_approval")
        self.approvals_created += 1
        return {"approval_id": "apr_t0001", "status": "needs_approval"}

    async def approval_status(self, state) -> str | None:
        return "pending"

    async def execute(self, state) -> dict[str, Any]:
        self.calls.append("execute")
        if self.halted_executes > 0:
            self.halted_executes -= 1
            return {"execution_id": "exe_t0001", "held": True, "status": "approved"}
        return {"execution_id": "exe_t0002", "held": False, "status": "executing"}

    async def close(self, state) -> dict[str, Any]:
        self.calls.append("close")
        return {"status": "denied"}


def cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def _start(steps: FakeSteps, thread: str = "ar_t0001"):
    graph = build_request_graph(steps, InMemorySaver())
    final = await graph.ainvoke({"run_id": "run_t", "request_id": thread}, cfg(thread),
                               durability=DURABILITY)
    snap = await graph.aget_state(cfg(thread))
    return graph, final, snap


def test_deny_ends_without_execution():
    async def go():
        steps = FakeSteps("deny")
        _, final, snap = await _start(steps)
        assert steps.calls == ["gate"]
        assert final["status"] == "denied" and not snap.next
    asyncio.run(go())


def test_allow_and_partial_allow_execute():
    async def go():
        for verdict in ("allow", "partial_allow"):
            steps = FakeSteps(verdict)
            _, final, snap = await _start(steps)
            assert steps.calls == ["gate", "execute"], verdict
            assert final["status"] == "executing" and final["execution_id"] == "exe_t0002"
            assert not snap.next
    asyncio.run(go())


def test_needs_approval_interrupts_then_approved_executes():
    async def go():
        steps = FakeSteps("needs_approval")
        graph, _, snap = await _start(steps)
        assert steps.calls == ["gate", "request_approval"]
        assert waiting_on(snap) == "approval"
        interrupts = [i for t in snap.tasks for i in t.interrupts]
        assert interrupts and interrupts[0].value["approval_id"] == "apr_t0001"

        final = await graph.ainvoke(Command(resume={"status": "approved", "by": "human:supervisor"}), cfg("ar_t0001"), durability=DURABILITY)
        assert steps.calls == ["gate", "request_approval", "execute"]
        assert steps.approvals_created == 1  # the approval node is not replayed on resume
        assert final["approval_status"] == "approved" and final["approved_by"] == "human:supervisor"
        assert final["status"] == "executing"
        assert not (await graph.aget_state(cfg("ar_t0001"))).next
    asyncio.run(go())


def test_needs_approval_denied_or_expired_closes():
    async def go():
        for outcome in ("denied", "expired"):
            steps = FakeSteps("needs_approval")
            graph, _, _ = await _start(steps, thread=f"ar_{outcome}")
            final = await graph.ainvoke(Command(resume={"status": outcome, "by": None}), cfg(f"ar_{outcome}"), durability=DURABILITY)
            assert steps.calls == ["gate", "request_approval", "close"], outcome
            assert "execute" not in steps.calls
            assert final["status"] == "denied"
    asyncio.run(go())


def test_approval_decided_before_interrupt_skips_waiting():
    async def go():
        steps = FakeSteps("needs_approval")

        async def already_expired(state):
            return "expired"

        steps.approval_status = already_expired  # type: ignore[method-assign]
        _, final, snap = await _start(steps)
        assert steps.calls == ["gate", "request_approval", "close"]
        assert not snap.next and final["status"] == "denied"
    asyncio.run(go())


def test_kill_switch_refusal_holds_until_resumed():
    async def go():
        steps = FakeSteps("allow", halted_executes=1)
        graph, _, snap = await _start(steps)
        assert steps.calls == ["gate", "execute"]
        assert waiting_on(snap) == "halted"
        final = await graph.ainvoke(Command(resume={"status": "resumed"}), cfg("ar_t0001"), durability=DURABILITY)
        assert steps.calls == ["gate", "execute", "execute"]
        assert final["status"] == "executing" and final["held"] is False
        assert not (await graph.aget_state(cfg("ar_t0001"))).next
    asyncio.run(go())


def test_crashed_node_is_retried_from_checkpoint():
    """Governance.process_request re-invokes with None when a node raised (no interrupt pending)."""
    async def go():
        steps = FakeSteps("allow")
        real_execute = steps.execute
        fail = {"n": 1}

        async def flaky(state):
            if fail["n"]:
                fail["n"] -= 1
                raise RuntimeError("mock system down")
            return await real_execute(state)

        steps.execute = flaky  # type: ignore[method-assign]
        graph = build_request_graph(steps, InMemorySaver())
        try:
            await graph.ainvoke({"run_id": "run_t", "request_id": "ar_t0001"}, cfg("ar_t0001"), durability=DURABILITY)
        except RuntimeError:
            pass
        snap = await graph.aget_state(cfg("ar_t0001"))
        assert snap.values and snap.next == ("execute",) and waiting_on(snap) is None
        final = await graph.ainvoke(None, cfg("ar_t0001"), durability=DURABILITY)
        assert steps.calls == ["gate", "execute"]  # the gate is not re-run
        assert final["status"] == "executing"
    asyncio.run(go())
