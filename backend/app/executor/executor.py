"""The Executor (PLAN §10.2): the only code that calls the mock control systems.

For an approved request it re-checks the kill switch (R15) and scope/authority (R01), reads the
pre-state, writes an `executions` doc with an undo command *before* calling, POSTs to the mock
system with Idempotency-Key = request id (retrying 503s up to 3 times with the same key), then
marks the request "executing" and logs `execution_sent`. "Sent" is never "took effect": the
verifier decides that later by reading the system back.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import numpy as np

from ..config import get_settings
from ..db import ACTION_REQUESTS, AGENT_CONTROL, APPROVALS, EXECUTIONS, IDENTITIES, MESSAGES, db
from ..ids import new_id
from ..models import ActionRequest, Decision, Execution, Identity, Message, UndoCommand
from ..mock_systems import chargeflow, gridops, powercell, textline, thermlink
from ..mock_systems.common import device_id
from ..mock_systems.keys import EXECUTOR_IDENTITY, key_for
from ..sim.world_state import World
from ..timeutil import sim_iso

log = logging.getLogger(__name__)

MAX_RETRIES = 3  # retries after the first attempt, same Idempotency-Key
COMMAND_ID_KEYS = ("batch_id", "dispatch_id", "command_id", "switch_id")
CORRECTION_BODY = "CoolShift update: please disregard our previous message, no action is needed. Reply STOP to opt out"
SMS_TEMPLATE = (
    "CoolShift: demand is high this evening. Easing your cooling a little from {start} to {end} helps keep "
    "power on for everyone. Reply STOP to opt out"
)
MAX_MESSAGE_DOCS = 25  # outbound thread docs + message_out entries per request (featured + businesses)


# ---------------------------------------------------------------- shared helpers

async def ledger_append(run_id: str, type_: str, actor: str, summary: str, **kw: Any) -> None:
    """Append through governance/ledger.py (the only ledger writer). Never raises into the sim."""
    try:
        from ..governance.ledger import append
    except ImportError:
        log.warning("governance.ledger not available; ledger entry dropped: %s", summary)
        return
    try:
        await append(run_id, type_, actor, summary, **kw)
    except Exception:
        log.exception("ledger append failed: %s", summary)


async def set_request_status(request_id: str, status: str, sim_ts: str, **extra: Any) -> None:
    await db()[ACTION_REQUESTS].update_one({"_id": request_id}, {"$set": {"status": status, "sim_ts": sim_ts, **extra}})


def command_id_of(response: dict[str, Any]) -> str | None:
    return next((response[k] for k in COMMAND_ID_KEYS if response.get(k)), None)


@dataclass
class Call:
    system: str
    path: str
    body: dict[str, Any]
    pre_state: dict[str, Any]
    undo: UndoCommand | None
    targets: int  # devices / recipients / sections addressed
    what: str  # human summary of the command
    messages: list[dict[str, Any]] = field(default_factory=list)


class Refused(Exception):
    def __init__(self, rule_id: str, detail: str, fail_request: bool) -> None:
        super().__init__(detail)
        self.rule_id, self.detail, self.fail_request = rule_id, detail, fail_request


# ---------------------------------------------------------------- executor

class Executor:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(base_url=get_settings().api_base_url, timeout=15.0)
        self.identity = EXECUTOR_IDENTITY

    async def execute(self, request: ActionRequest, decision: Decision, world: World) -> Execution:
        existing = await db()[EXECUTIONS].find_one(
            {"request_id": request.id, "status": {"$in": ["sent", "verified", "partial", "rolled_back"]}}
        )
        if existing:  # LangGraph may replay a node; never send twice
            return Execution.model_validate(existing)
        try:
            if request.action_type != "undo":  # a supervisor's rollback must still work while agents are halted
                await self._check_kill_switch(request, world)
            await self._check_authority(request, decision)
            call = await self._build(request, decision, world)
        except Refused as r:
            return await self._refuse(request, world, r)
        return await self._send(request, decision, world, call)

    # ------------------------------------------------------------ re-checks

    async def _check_kill_switch(self, request: ActionRequest, world: World) -> None:
        agents = (self.identity, request.proposed_by)
        if world.halted and world.halted_scope in ("all", *agents):
            raise Refused("R15", f"kill switch active (scope {world.halted_scope})", fail_request=False)
        try:
            from ..governance.control import is_halted
        except ImportError:
            is_halted = None
        if is_halted is not None:
            for agent in agents:
                if await is_halted(request.run_id, agent):
                    raise Refused("R15", f"kill switch active for {agent}", fail_request=False)
            return
        ctl = await db()[AGENT_CONTROL].find_one({"_id": request.run_id})
        if ctl and ctl.get("halted") and ctl.get("scope", "all") in ("all", *agents):
            raise Refused("R15", f"kill switch active (scope {ctl.get('scope', 'all')})", fail_request=False)

    async def _identities(self) -> dict[str, Identity]:
        try:
            from ..governance.identities import load_identities
        except ImportError:
            load_identities = None
        if load_identities is not None:
            return await load_identities()
        docs = await db()[IDENTITIES].find({}).to_list(None)
        return {d["_id"]: Identity.model_validate(d) for d in docs}

    async def _check_authority(self, request: ActionRequest, decision: Decision) -> None:
        ids = await self._identities()
        me = ids.get(self.identity)
        system = request.system
        if request.action_type == "undo" and request.undo_of:
            orig = await db()[EXECUTIONS].find_one({"_id": request.undo_of}, {"system": 1})
            system = (orig or {}).get("system", system)
        if me is None or me.status != "active":
            raise Refused("R01", f"{self.identity} is not an active identity", fail_request=True)
        if not ({request.action_type, system, "*"} & set(me.scopes.execute)):
            raise Refused("R01", f"{self.identity} has no execute scope for {request.action_type}", fail_request=True)
        human = ids.get(request.acting_for)
        if human is None or human.status != "active" or not human.on_shift:
            raise Refused("R01", f"acting-for {request.acting_for} is not active and on shift", fail_request=True)
        proposer = ids.get(request.proposed_by)
        if proposer is None or proposer.status != "active":
            raise Refused("R01", f"proposer {request.proposed_by} is not an active identity", fail_request=True)
        if decision.request_id != request.id:
            raise Refused("R01", f"decision {decision.id} is for {decision.request_id}", fail_request=True)
        if decision.verdict in ("allow", "partial_allow"):
            return
        if decision.verdict == "needs_approval" and request.approval_id:
            apr = await db()[APPROVALS].find_one({"_id": request.approval_id}, {"status": 1})
            if apr and apr.get("status") == "approved":
                return
        raise Refused("R01", f"not authorised by the gate (verdict {decision.verdict})", fail_request=True)

    # ------------------------------------------------------------ command building

    def _targets(self, request: ActionRequest, decision: Decision) -> list[str]:
        ids = list(request.targets.household_ids)
        if decision.verdict == "partial_allow" or decision.allowed_household_ids:
            allowed = set(decision.allowed_household_ids)
            ids = [h for h in ids if h in allowed] if ids else list(decision.allowed_household_ids)
        return ids

    def _window(self, request: ActionRequest, world: World) -> tuple[str | None, str | None]:
        p = request.params
        start = request.window.start if request.window else p.get("start")
        end = request.window.end if request.window else p.get("end") or p.get("until")
        if not end and p.get("duration_min"):
            end = sim_iso(world.sim_now + timedelta(minutes=int(p["duration_min"])))
        return start, end

    def _idx(self, world: World, hh_ids: list[str]) -> list[int]:
        out = []
        for h in hh_ids:
            try:
                i = world.hh_index(h)
            except ValueError:
                continue
            if 0 <= i < world.n:
                out.append(i)
        return out

    async def _build(self, request: ActionRequest, decision: Decision, world: World) -> Call:
        a = request.action_type
        rid = request.id
        start, end = self._window(request, world)
        if a in ("precool", "thermostat_setback", "thermostat_restore"):
            idx = self._idx(world, self._targets(request, decision))
            if not idx:
                raise Refused("R01", "no executable thermostats in the allowed set", fail_request=True)
            devices = [device_id("thermlink", i) for i in idx]
            if a == "thermostat_restore":
                offset, start, end = 0.0, None, None
            else:
                offset = float(request.params.get("offset_c", -2.0 if a == "precool" else 2.0))
            body = {"device_ids": devices, "offset_c": offset, "start": start, "end": end, "request_id": rid}
            if request.params.get("precool_offset_c") is not None:
                body["precool_offset_c"] = float(request.params["precool_offset_c"])
            undo = None
            if a != "thermostat_restore":  # refined to /events/{batch_id}/end once the batch id is known
                undo = UndoCommand(
                    system="thermlink", path="/mock/thermlink/v1/events", body={"device_ids": devices, "offset_c": 0.0}
                )
            label = {"precool": "pre-cool", "thermostat_setback": "setback", "thermostat_restore": "restore"}[a]
            win = f" {start}–{end}" if start and end else ""
            return Call(
                "thermlink", "/mock/thermlink/v1/events", body, thermlink.snapshot(world, np.asarray(idx)), undo,
                len(idx), f"ThermLink {label} {offset:+.1f} °C{win}",
            )
        if a == "ev_pause":
            idx = self._idx(world, self._targets(request, decision))
            if not idx:
                raise Refused("R01", "no executable EV sessions in the allowed set", fail_request=True)
            sessions = [device_id("chargeflow", i) for i in idx]
            body = {"session_ids": sessions, "until": end, "request_id": rid}
            undo = UndoCommand(system="chargeflow", path="/mock/chargeflow/v1/resume", body={"session_ids": sessions})
            return Call(
                "chargeflow", "/mock/chargeflow/v1/pause", body, chargeflow.snapshot(world, np.asarray(idx)), undo,
                len(idx), f"ChargeFlow pause until {end or '+60 min'}",
            )
        if a == "battery_discharge":
            idx = self._idx(world, self._targets(request, decision))
            if not idx:
                raise Refused("R01", "no executable PowerCell units in the allowed set", fail_request=True)
            units = [device_id("powercell", i) for i in idx]
            kw = float(request.params.get("kw", powercell.MAX_KW))
            body = {"unit_ids": units, "kw": kw, "start": start, "end": end, "request_id": rid}
            undo = UndoCommand(system="powercell", path="/mock/powercell/v1/stop", body={"unit_ids": units})
            return Call(
                "powercell", "/mock/powercell/v1/dispatch", body, powercell.snapshot(world, np.asarray(idx)), undo,
                len(idx), f"PowerCell discharge {kw:.1f} kW/unit" + (f" {start}–{end}" if start and end else ""),
            )
        if a in ("send_sms", "business_curtail_request"):
            msgs = self._messages(request, decision, world, start, end)
            if not msgs:
                raise Refused("R01", "no allowed recipients", fail_request=True)
            to = [m["to"] for m in msgs]
            undo = UndoCommand(
                system="textline",
                path="/mock/textline/v1/messages",
                body={"messages": [{"to": t, "body": CORRECTION_BODY} for t in to]},
            )
            label = "business curtailment request" if a == "business_curtail_request" else "SMS"
            return Call(
                "textline", "/mock/textline/v1/messages", {"messages": msgs, "request_id": rid},
                textline.snapshot(world, to), undo, len(msgs), f"TextLine {label}", messages=msgs,
            )
        if a in ("feeder_outage", "feeder_restore"):
            sid = request.targets.section_id
            if not sid:
                raise Refused("R01", "no target section", fail_request=True)
            pre = await self._read_section(world, sid)
            if a == "feeder_outage":
                duration = int(request.params.get("duration_min", 60))
                body = {"approval_id": request.approval_id, "duration_min": duration, "request_id": rid}
                undo = UndoCommand(system="gridops", path=f"/mock/gridops/v1/sections/{sid}/close", body={})
                return Call("gridops", f"/mock/gridops/v1/sections/{sid}/open", body, pre, undo, 1,
                            f"Lakeshore Grid Ops open {sid} for {duration} min")
            return Call("gridops", f"/mock/gridops/v1/sections/{sid}/close", {"request_id": rid}, pre, None, 1,
                        f"Lakeshore Grid Ops close {sid}")
        if a == "undo":
            orig = await db()[EXECUTIONS].find_one({"_id": request.undo_of}) if request.undo_of else None
            if not orig or not orig.get("undo"):
                raise Refused("R01", f"execution {request.undo_of} has no undo command", fail_request=True)
            if orig.get("status") == "rolled_back":
                raise Refused("R01", f"execution {request.undo_of} is already rolled back", fail_request=True)
            u = UndoCommand.model_validate(orig["undo"])
            body = {**u.body, "request_id": rid}
            pre = {"undo_of": orig["_id"], "original_request_id": orig.get("request_id"), "original_status": orig.get("status")}
            n = len(body.get("device_ids") or body.get("session_ids") or body.get("unit_ids") or body.get("messages") or [1])
            return Call(u.system, u.path, body, pre, None, n, f"rollback of {orig['_id']} via {u.path}")
        raise Refused("R01", f"unsupported action {a}", fail_request=True)

    def _messages(
        self, request: ActionRequest, decision: Decision, world: World, start: str | None, end: str | None
    ) -> list[dict[str, Any]]:
        allowed = set(decision.allowed_household_ids) if decision.verdict == "partial_allow" else None
        if request.messages:
            msgs = [m.model_dump(exclude_none=True) for m in request.messages]
        else:
            recips = list(request.targets.recipient_ids) or (
                [request.targets.business_id] if request.targets.business_id else list(request.targets.household_ids)
            )
            body = SMS_TEMPLATE.format(start=start or "now", end=end or "later")
            msgs = [{"to": r, "body": body} for r in recips]
        if allowed is not None:
            msgs = [m for m in msgs if m["to"] in allowed]
        return msgs

    async def _read_section(self, world: World, section_id: str) -> dict[str, Any]:
        """Pre-state read straight from the GridOps API."""
        try:
            r = await self.http.get(f"/mock/gridops/v1/sections/{section_id}", headers=self._headers("gridops", world))
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError as e:
            log.warning("gridops pre-state read failed: %s", e)
        return gridops.section_state(world, section_id)

    def _headers(self, system: str, world: World, key: str | None = None) -> dict[str, str]:
        h = {"X-Api-Key": key_for(system), "X-Run-Id": world.run_id}
        if key:
            h["Idempotency-Key"] = key
        return h

    # ------------------------------------------------------------ refusal

    async def _refuse(self, request: ActionRequest, world: World, r: Refused) -> Execution:
        """Nothing is sent. R15 leaves the request as is (held until resume); R01 fails it."""
        ts = sim_iso(world.sim_now)
        exe = Execution(
            id=new_id("exe_", request.run_id), run_id=request.run_id, request_id=request.id,
            executed_by=self.identity, system=request.system, idempotency_key=request.id, path="",
            response={"refused": True, "rule_id": r.rule_id, "detail": r.detail}, status="failed", attempts=0,
            sent_sim_ts=ts, sent_tick=world.tick, sim_ts=ts, action_type=request.action_type, refused=True,
        )
        await db()[EXECUTIONS].insert_one(exe.to_mongo())
        if r.fail_request:
            await set_request_status(request.id, "failed", ts, execution_id=exe.id)
            request.status = "failed"
        await ledger_append(
            request.run_id, "execution_failed", self.identity,
            f"Executor refused {request.id} ({request.action_type}): {r.detail} [{r.rule_id}]"
            + ("" if r.fail_request else "; held until resume"),
            sim_ts=ts, acting_for=request.acting_for,
            refs={"request_id": request.id, "decision_id": request.decision_id, "approval_id": request.approval_id,
                  "execution_id": exe.id},
            payload={"refused": True, "rule_id": r.rule_id, "detail": r.detail},
            langsmith_run_id=request.langsmith_run_id,
        )
        return exe

    # ------------------------------------------------------------ send

    async def _send(self, request: ActionRequest, decision: Decision, world: World, call: Call) -> Execution:
        ts = sim_iso(world.sim_now)
        exe = Execution(
            id=new_id("exe_", request.run_id),
            run_id=request.run_id,
            request_id=request.id,
            executed_by=self.identity,
            system=call.system,
            idempotency_key=request.id,
            path=call.path,
            body=_compact(call.body),
            pre_state=call.pre_state,
            undo=call.undo,
            status="sent",
            attempts=0,
            sent_sim_ts=ts,
            sent_tick=world.tick,
            sim_ts=ts,
            targets=call.targets,
            action_type=request.action_type,
            undo_of=request.undo_of,
        )
        # The executions doc (pre_state + undo) exists before the system is touched.
        await db()[EXECUTIONS].insert_one(exe.to_mongo())

        status_code, payload, attempts = await self._post(call, request.id, world)
        exe.attempts = attempts
        refs = {
            "request_id": request.id,
            "decision_id": decision.id,
            "approval_id": request.approval_id,
            "execution_id": exe.id,
            "plan_version": request.plan_version,
        }
        homes_real = int(round(call.targets * world.scale)) if call.system in ("thermlink", "chargeflow", "powercell") else None

        if status_code != 200:
            exe.status = "failed"
            exe.response = {"status_code": status_code, **payload}
            await db()[EXECUTIONS].update_one(
                {"_id": exe.id}, {"$set": {"status": "failed", "response": exe.response, "attempts": attempts}}
            )
            await set_request_status(request.id, "failed", ts, execution_id=exe.id)
            request.status, request.execution_id = "failed", exe.id
            err = payload.get("error", "transport_error")
            await ledger_append(
                request.run_id, "execution_failed", self.identity,
                f"{call.what} for {request.id} was rejected by {call.system} ({status_code} {err}) after {attempts} attempt(s)",
                sim_ts=ts, acting_for=request.acting_for, refs=refs,
                payload={"system": call.system, "path": call.path, "status_code": status_code, "error": payload,
                         "attempts": attempts, "idempotency_key": request.id},
                langsmith_run_id=request.langsmith_run_id,
            )
            return exe

        cid = command_id_of(payload)
        exe.response = _compact(payload)
        if call.undo and cid:
            if call.system == "thermlink":
                exe.undo = UndoCommand(system="thermlink", path=f"/mock/thermlink/v1/events/{cid}/end", body={})
            elif call.system == "powercell":
                exe.undo = UndoCommand(system="powercell", path="/mock/powercell/v1/stop", body={"dispatch_id": cid})
        await db()[EXECUTIONS].update_one(
            {"_id": exe.id},
            {"$set": {"response": exe.response, "attempts": attempts, "command_id": cid,
                      "undo": exe.undo.model_dump() if exe.undo else None}},
        )
        exe.command_id = cid  # type: ignore[attr-defined]  (extra field)
        await set_request_status(request.id, "executing", ts, execution_id=exe.id)
        request.status, request.execution_id = "executing", exe.id

        scale_txt = f" (~{homes_real:,} real homes)" if homes_real else ""
        retry_txt = f" after {attempts - 1} retr{'y' if attempts == 2 else 'ies'} on 503" if attempts > 1 else ""
        await ledger_append(
            request.run_id, "execution_sent", self.identity,
            f"Sent {call.what} to {call.targets:,} target(s){scale_txt}: {call.system} id {cid}, "
            f"Idempotency-Key {request.id}{retry_txt}. Awaiting read-back.",
            sim_ts=ts, acting_for=request.acting_for, refs=refs,
            payload={"system": call.system, "path": call.path, "command_id": cid, "attempts": attempts,
                     "idempotency_key": request.id, "targets": call.targets, "homes_real": homes_real,
                     "rejected_count": payload.get("rejected_count", 0), "pre_state": call.pre_state,
                     "undo": exe.undo.model_dump() if exe.undo else None},
            langsmith_run_id=request.langsmith_run_id,
        )
        if call.messages:
            await self._record_messages(request, world, exe, call.messages, payload, refs)
        return exe

    async def _post(self, call: Call, key: str, world: World) -> tuple[int, dict[str, Any], int]:
        headers = self._headers(call.system, world, key)
        status, payload = 0, {"error": "not_sent"}
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                r = await self.http.post(call.path, json=call.body, headers=headers)
                status = r.status_code
                try:
                    payload = r.json()
                except ValueError:
                    payload = {"error": "bad_response", "detail": r.text[:200]}
                if isinstance(payload, dict) and "detail" in payload and isinstance(payload["detail"], dict):
                    payload = payload["detail"]  # HTTPException body from the auth dependency
            except httpx.TransportError as e:
                status, payload = 0, {"error": "transport_error", "detail": str(e)[:200]}
            if status not in (0, 503):
                return status, payload, attempt
            if attempt <= MAX_RETRIES:
                await asyncio.sleep(0.05 * attempt)
        return status, payload, MAX_RETRIES + 1

    async def _record_messages(
        self, request: ActionRequest, world: World, exe: Execution, msgs: list[dict[str, Any]],
        payload: dict[str, Any], refs: dict[str, Any],
    ) -> None:
        """Thread docs + message_out ledger entries for featured residents and businesses."""
        rejected = {r["to"] for r in payload.get("rejected", [])}
        featured = {i: r for r, i in world.featured.items()}
        ts = sim_iso(world.sim_now)
        notable = []
        for m in msgs:
            to = m["to"]
            if to in rejected:
                continue
            if to in world.businesses:
                name = world.businesses[to].get("name", to)
            else:
                try:
                    i = world.hh_index(to)
                except ValueError:
                    continue
                if i not in featured:
                    continue
                name = f"{featured[i]} ({to})"
            notable.append((m, name))
            if len(notable) >= MAX_MESSAGE_DOCS:
                break
        for m, name in notable:
            msg = Message(
                id=new_id("msg_", request.run_id), run_id=request.run_id, thread_id=m["to"], from_=request.proposed_by,
                to=m["to"], body=m["body"], direction="out", trust="system", sim_ts=ts, request_id=request.id,
                offer=m.get("offer"),
            )
            await db()[MESSAGES].insert_one(msg.to_mongo())
            await ledger_append(
                request.run_id, "message_out", self.identity,
                f"TextLine message to {name}: {m['body'][:90]}",
                sim_ts=ts, acting_for=request.acting_for, refs={**refs, "message_id": msg.id},
                payload={"to": m["to"], "body": m["body"], "offer": m.get("offer"), "trust": "system"},
            )


def _compact(d: dict[str, Any], max_list: int = 50) -> dict[str, Any]:
    """Keep large id lists out of the ledger/execution payloads (first `max_list` + a count)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, list) and len(v) > max_list:
            out[k] = v[:max_list]
            out[f"{k}_count"] = len(v)
        else:
            out[k] = v
    return out
