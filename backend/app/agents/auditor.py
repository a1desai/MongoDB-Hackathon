"""Auditor agent (PLAN §8.2, P1): answers questions about a run from the ledger.

Retrieval is done in code before the single SMART-model call:
1. regex for ids in the question (TOR-S.., ar_/dec_/apr_/exe_/msg_ ids, hh_ ids, rule ids,
   times, "seq N", resident and business names);
2. hybrid search over ledger.summary (`memory.retrieval.ledger_search`: $rankFusion of Atlas
   Search + Vector Search, degrading to $text / $regex);
3. exact ledger lookups for the ids, then every ledger entry of the related action requests
   (proposal -> decision -> approval -> execution), plus their Gate decisions and approvals;
4. policy rule texts (`memory.retrieval.policy_snippets` + the rules those decisions cite).

The answer cites ledger seqs ([seq N]); citations of seqs that were not retrieved are rejected.
With no LLM (or on any LLM failure) a deterministic answer is built from the retrieved entries.
Every question is logged as ledger `audit_query`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..config import get_settings
from ..db import APPROVALS, DECISIONS, IDENTITIES, LEDGER, POLICY_DOCS, db
from ..llm.client import complete_json, current_trace_id, load_prompt, traced, wrap_untrusted
from ..models import AuditAnswer, TimelineItem
from ..timeutil import hhmm, parse_sim
from ..world_agents import _common as common

log = logging.getLogger(__name__)

AGENT = "agent:auditor"
DB_TIMEOUT_S = 5.0
MAX_LLM_ENTRIES = 40
MAX_TIMELINE = 12
MAX_REQUESTS = 8
ANSWER_MAX = 1200
TIME_WINDOW_MIN = 6

KEY_TYPES = ("request_proposed", "decision", "approval_requested", "approval_decided",
             "execution_sent", "execution_verified", "execution_failed", "rollback_requested",
             "halt", "resume", "contingency", "replan_triggered", "plan_created", "message_in",
             "message_out")
DISPLAY_FALLBACK = {"human:supervisor": "A. Rahman", "human:auditor": "J. Okafor"}
_LEDGER_FIELDS = {"_id": 0, "seq": 1, "sim_ts": 1, "type": 1, "actor": 1, "acting_for": 1,
                  "summary": 1, "refs": 1, "payload.trust": 1, "langsmith_run_id": 1}

_ID_PATTERNS = {
    "requests": re.compile(r"\bar_[0-9a-z]{4,}\b", re.I),
    "decisions": re.compile(r"\bdec_[0-9a-z]{4,}\b", re.I),
    "approvals": re.compile(r"\bapr_[0-9a-z]{4,}\b", re.I),
    "executions": re.compile(r"\bexe_[0-9a-z]{4,}\b", re.I),
    "messages": re.compile(r"\bmsg_[0-9a-z]{4,}\b", re.I),
    "plans": re.compile(r"\bplan_[0-9a-z]{4,}\b", re.I),
    "households": re.compile(r"\bhh_\d{5}\b", re.I),
}
_SECTION = re.compile(r"\bTOR-S\d{1,3}\b", re.I)
_RULE = re.compile(r"\bR(\d{1,2})\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_SEQ = re.compile(r"\bseq(?:uence)?\s*(?:no\.?|number)?\s*#?\s*(\d+)\b", re.I)
_CITE = re.compile(r"\bseq\s*#?\s*(\d+)", re.I)


# ---------------------------------------------------------------- id extraction


def extract_ids(question: str, residents: dict[str, dict] | None = None,
                businesses: dict[str, dict] | None = None) -> dict[str, list[str]]:
    """Identifiers mentioned in a question (pure; names resolved against the given lookups)."""
    q = question or ""
    ids: dict[str, list[str]] = {k: sorted({m.lower() for m in p.findall(q)})
                                 for k, p in _ID_PATTERNS.items()}
    ids["sections"] = sorted({m.upper() for m in _SECTION.findall(q)})
    ids["rules"] = sorted({f"R{int(n):02d}" for n in _RULE.findall(q) if 1 <= int(n) <= 16})
    ids["times"] = sorted({f"{int(h):02d}:{m}" for h, m in _TIME.findall(q)})
    ids["seqs"] = sorted({m for m in _SEQ.findall(q)}, key=int)
    names: list[str] = []
    for rid, doc in (residents or {}).items():
        name = str(doc.get("name") or "")
        parts = name.split()
        if not parts:
            continue
        hit = re.search(rf"\b{re.escape(name)}\b", q, re.I) or (
            len(parts) > 1 and len(parts[-1]) >= 4 and re.search(rf"\b{re.escape(parts[-1])}\b", q, re.I)
        ) or (len(parts[0]) >= 3 and re.search(rf"\b{re.escape(parts[0])}\b", q))
        if hit:
            names.append(name)
            hh = doc.get("household_id")
            if hh and hh not in ids["households"]:
                ids["households"].append(str(hh))
    for bid, doc in (businesses or {}).items():
        name = str(doc.get("name") or "")
        if bid.lower() in q.lower() or (name and re.search(rf"\b{re.escape(name)}\b", q, re.I)):
            names.append(name or bid)
    ids["names"] = sorted(set(names))
    return ids


def _has_ids(ids: dict[str, list[str]]) -> bool:
    return any(ids.get(k) for k in ("requests", "decisions", "approvals", "executions", "messages",
                                    "households", "sections", "seqs", "names"))


# ---------------------------------------------------------------- retrieval


async def _find(coll: str, flt: dict, projection: dict | None = None, *, sort: list | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    try:
        cursor = db()[coll].find(flt, projection)
        if sort:
            cursor = cursor.sort(sort)
        return await asyncio.wait_for(cursor.limit(limit).to_list(length=limit), DB_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - retrieval degrades, never raises
        log.warning("auditor find on %s failed: %s", coll, e)
        return []


def _slim(e: dict[str, Any]) -> dict[str, Any]:
    refs = {k: v for k, v in (e.get("refs") or {}).items() if v is not None}
    out = {"seq": int(e.get("seq") or 0), "sim_ts": str(e.get("sim_ts") or ""),
           "type": str(e.get("type") or ""), "actor": str(e.get("actor") or ""),
           "acting_for": e.get("acting_for"), "summary": str(e.get("summary") or ""), "refs": refs}
    trust = (e.get("payload") or {}).get("trust")
    if trust == "untrusted" or out["type"] == "message_in":
        out["untrusted"] = True
    return out


def _date_prefix(entries: list[dict[str, Any]]) -> str | None:
    for e in entries:
        ts = str(e.get("sim_ts") or "")
        if "T" in ts:
            return ts.split("T")[0]
    return None


def _minutes(ts: str) -> int | None:
    try:
        d = parse_sim(ts)
        return d.hour * 60 + d.minute
    except (ValueError, TypeError):
        return None


async def retrieve(run_id: str, question: str, ids: dict[str, list[str]]) -> dict[str, Any]:
    """Ledger entries (scored), decisions, approvals and policies relevant to the question."""
    from ..memory.retrieval import ledger_search, policy_snippets

    score: dict[int, float] = {}
    entries: dict[int, dict[str, Any]] = {}
    origin: dict[int, set[str]] = {}
    paths: set[str] = set()

    def add(rows: list[dict[str, Any]], weight: float, path: str) -> None:
        for e in rows:
            try:
                seq = int(e.get("seq"))
            except (TypeError, ValueError):
                continue
            if e.get("type") == "audit_query":  # never answer from earlier answers
                continue
            entries.setdefault(seq, _slim(e))
            score[seq] = score.get(seq, 0.0) + weight
            origin.setdefault(seq, set()).add(path.split(":")[0])
            paths.add(path)

    # 1. hybrid search (never raises) and policy snippets, concurrently
    try:
        hits, snippets = await asyncio.gather(ledger_search(run_id, question, k=12),
                                              policy_snippets(question, k=3))
    except Exception as e:  # noqa: BLE001
        log.warning("auditor search failed: %s", e)
        hits, snippets = [], []
    for rank, h in enumerate(hits):
        add([h], 2.0 - 0.1 * rank, f"ledger_search:{h.get('retrieval', 'hybrid')}")

    # 2. exact id lookups
    ors: list[dict[str, Any]] = []
    for key, field in (("requests", "refs.request_id"), ("decisions", "refs.decision_id"),
                       ("approvals", "refs.approval_id"), ("executions", "refs.execution_id"),
                       ("messages", "refs.message_id")):
        if ids.get(key):
            ors.append({field: {"$in": ids[key]}})
    for term in ids.get("sections", []) + ids.get("households", []) + ids.get("names", []):
        ors.append({"summary": {"$regex": re.escape(term), "$options": "i"}})
    if ids.get("seqs"):
        ors.append({"seq": {"$in": [int(s) for s in ids["seqs"]]}})
    if ors:
        add(await _find(LEDGER, {"run_id": run_id, "$or": ors}, _LEDGER_FIELDS,
                        sort=[("seq", 1)], limit=80), 3.0, "id_match")

    # 3. time windows around times in the question
    times = [int(t[:2]) * 60 + int(t[3:5]) for t in ids.get("times", [])]
    date = _date_prefix(list(entries.values()))
    if times and not date:
        first = await _find(LEDGER, {"run_id": run_id}, {"sim_ts": 1}, sort=[("seq", 1)], limit=1)
        date = _date_prefix(first)
    if times and date:
        for t in times:
            lo, hi = max(0, t - TIME_WINDOW_MIN), min(24 * 60 - 1, t + TIME_WINDOW_MIN)
            rng = {"$gte": f"{date}T{lo // 60:02d}:{lo % 60:02d}",
                   "$lte": f"{date}T{hi // 60:02d}:{hi % 60:02d}:59~"}
            flt: dict[str, Any] = {"run_id": run_id, "sim_ts": rng, "type": {"$in": list(KEY_TYPES)}}
            add(await _find(LEDGER, flt, _LEDGER_FIELDS, sort=[("seq", 1)], limit=30),
                1.5 if not _has_ids(ids) else 0.5, "time_window")
        for seq, e in entries.items():  # proximity boost
            m = _minutes(e["sim_ts"])
            if m is not None and any(abs(m - t) <= TIME_WINDOW_MIN for t in times):
                score[seq] += 1.0

    # 4. expand to the full chain of the most relevant action requests: from exact id matches
    # when the question names ids, else from the top search hits and the time window
    precise = {"id_match"} if any(origin.get(s, set()) & {"id_match"} for s in entries) else \
        {"ledger_search", "time_window"}
    ranked = sorted((s for s in entries if origin.get(s, set()) & precise), key=lambda s: (-score[s], s))
    if precise != {"id_match"}:
        ranked = ranked[:3]
    req_ids: list[str] = list(ids.get("requests", []))
    for seq in ranked:
        rid = entries[seq]["refs"].get("request_id")
        if rid and rid not in req_ids:
            req_ids.append(rid)
        if len(req_ids) >= MAX_REQUESTS:
            break
    approvals: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    if req_ids or ids.get("approvals") or ids.get("decisions"):
        chain = await _find(LEDGER, {"run_id": run_id, "refs.request_id": {"$in": req_ids}},
                            _LEDGER_FIELDS, sort=[("seq", 1)], limit=120) if req_ids else []
        add(chain, 2.0, "request_chain")
        appr_or: list[dict[str, Any]] = [{"request_id": {"$in": req_ids}}]
        if ids.get("approvals"):
            appr_or.append({"_id": {"$in": ids["approvals"]}})
        dec_or: list[dict[str, Any]] = [{"request_id": {"$in": req_ids}}]
        if ids.get("decisions"):
            dec_or.append({"_id": {"$in": ids["decisions"]}})
        approvals, decisions = await asyncio.gather(
            _find(APPROVALS, {"run_id": run_id, "$or": appr_or},
                  {"rule_results": 0, "blast_radius": 0}, limit=20),
            _find(DECISIONS, {"run_id": run_id, "$or": dec_or},
                  {"allowed_household_ids": 0}, limit=20),
        )

    # 5. policy texts: semantic snippets + every rule the decisions or the question cite
    rule_ids = set(ids.get("rules", []))
    for d in decisions:
        for r in d.get("rule_results") or []:
            if r.get("applies") and (not r.get("passed") or r.get("effect") == "needs_approval"):
                rule_ids.add(str(r.get("rule_id")))
    policies = {str(p.get("rule_id")): p for p in snippets if p.get("rule_id")}
    missing = sorted(r for r in rule_ids if r not in policies)
    if missing:
        for p in await _find(POLICY_DOCS, {"rule_id": {"$in": missing}},
                             {"_id": 0, "rule_id": 1, "title": 1, "text": 1}, limit=16):
            policies[str(p.get("rule_id"))] = p

    return {"entries": entries, "score": score, "decisions": decisions, "approvals": approvals,
            "policies": list(policies.values()), "paths": sorted(paths), "request_ids": req_ids}


async def _display_names() -> dict[str, str]:
    rows = await _find(IDENTITIES, {"kind": "human"}, {"display_name": 1}, limit=20)
    names = dict(DISPLAY_FALLBACK)
    names.update({str(r["_id"]): str(r.get("display_name")) for r in rows if r.get("display_name")})
    return names


# ---------------------------------------------------------------- deterministic answer


def _t(ts: str | None) -> str:
    try:
        return hhmm(parse_sim(str(ts)))
    except (ValueError, TypeError):
        return str(ts or "")[:16]


def _who(identity: str | None, names: dict[str, str]) -> str:
    if not identity:
        return "unknown"
    return f"{identity} ({names[identity]})" if identity in names else identity


def pick_timeline(entries: dict[int, dict[str, Any]], score: dict[int, float],
                  n: int = MAX_TIMELINE) -> list[dict[str, Any]]:
    """The n most relevant entries (key types first), in seq order; weak matches are dropped."""
    def val(seq: int) -> float:
        return score.get(seq, 0) + (0.5 if entries[seq]["type"] in KEY_TYPES else 0)

    if not entries:
        return []
    floor = 0.4 * max(val(s) for s in entries)
    top = sorted((s for s in entries if val(s) >= floor), key=lambda s: (-val(s), s))[:n]
    return [entries[s] for s in sorted(top)]


def _sentence(e: dict[str, Any], names: dict[str, str]) -> str:
    t, actor, seq = _t(e["sim_ts"]), e["actor"], e["seq"]
    summary = common.clip_text(e["summary"], 150).rstrip(".")
    if e.get("untrusted"):
        summary = f"(untrusted) {summary}"
    af = f", acting for {_who(e.get('acting_for'), names)}" if e.get("acting_for") else ""
    kind = e["type"]
    if kind == "request_proposed":
        return f"At {t} {actor}{af} proposed: {summary} [seq {seq}]."
    if kind == "decision":
        return f"The Policy Gate (system:gate) ruled at {t}: {summary} [seq {seq}]."
    if kind == "approval_requested":
        return f"At {t} it was sent for human approval: {summary} [seq {seq}]."
    if kind == "approval_decided":
        return f"At {t} {_who(actor, names)} decided the approval: {summary} [seq {seq}]."
    if kind.startswith("execution_"):
        return f"At {t} {actor} reported: {summary} [seq {seq}]."
    return f"At {t} {actor}{af}: {summary} [seq {seq}]."


def _rule_names(decisions: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in decisions:
        for r in d.get("rule_results") or []:
            if r.get("applies") and (not r.get("passed") or r.get("effect") == "needs_approval"):
                rid = str(r.get("rule_id"))
                eff = "needs approval" if r.get("effect") == "needs_approval" else "denied"
                out[rid] = f"{rid} {r.get('name', '')} ({eff})".replace("  ", " ")
    return out


def fallback_answer(question: str, ids: dict[str, list[str]], ctx: dict[str, Any],
                    names: dict[str, str]) -> AuditAnswer:
    """Deterministic answer from the retrieved ledger entries (no LLM)."""
    entries, score = ctx.get("entries") or {}, ctx.get("score") or {}
    subject = ", ".join(ids.get("sections", []) + ids.get("requests", []) + ids.get("names", []))
    if not entries:
        about = f" about {subject}" if subject else ""
        return AuditAnswer(
            answer=(f"I found no ledger entries in this run{about} that answer the question. "
                    "Try naming a feeder section (TOR-S..), a request id (ar_...), a resident or a "
                    "time (HH:MM)."),
            timeline=[], rules_cited=ids.get("rules", []))
    timeline = pick_timeline(entries, score)
    lead = (f"From the ledger{' for ' + subject if subject else ''}: "
            f"{len(entries)} related entries, the key ones below.")
    sentences = [lead] + [_sentence(e, names) for e in timeline]
    rules = _rule_names(ctx.get("decisions") or [])
    if rules:
        sentences.append("Gate rules that applied: " + "; ".join(rules[r] for r in sorted(rules)) + ".")
    by_req_seq = {(e["refs"].get("approval_id") or e["refs"].get("request_id")): e["seq"]
                  for e in entries.values() if e["type"] == "approval_decided"}
    for a in ctx.get("approvals") or []:
        if a.get("status") in ("approved", "denied", "expired") and a.get("decided_by"):
            seq = by_req_seq.get(a.get("_id")) or by_req_seq.get(a.get("request_id"))
            cite = f" [seq {seq}]" if seq else ""
            note = f" Note: \"{common.clip_text(a['note'], 100)}\"." if a.get("note") else ""
            sentences.append(f"Approval {a.get('_id')} was {a['status']} by "
                             f"{_who(a['decided_by'], names)} at {_t(a.get('decided_sim_ts'))}{cite}.{note}")
    answer = " ".join(sentences)
    if len(answer) > ANSWER_MAX:
        answer = answer[: ANSWER_MAX - 1].rsplit(" ", 1)[0] + "…"
    cited = sorted(set(ids.get("rules", [])) | set(rules))
    return AuditAnswer(
        answer=answer,
        timeline=[TimelineItem(seq=e["seq"], sim_ts=e["sim_ts"], actor=e["actor"],
                               summary=common.clip_text(e["summary"], 240)) for e in timeline],
        rules_cited=cited)


# ---------------------------------------------------------------- LLM answer checks


def _llm_payload(question: str, ids: dict[str, list[str]], ctx: dict[str, Any]) -> dict[str, Any]:
    entries, score = ctx["entries"], ctx["score"]
    top = sorted(sorted(entries, key=lambda s: (-score.get(s, 0), s))[:MAX_LLM_ENTRIES])
    ledger = []
    for s in top:
        e = dict(entries[s])
        if e.pop("untrusted", False):
            e["summary"] = wrap_untrusted([{"_id": f"seq{s}", "from": e["actor"], "body": e["summary"]}])
        ledger.append(e)
    decisions = [{
        "decision_id": d.get("_id"), "request_id": d.get("request_id"), "verdict": d.get("verdict"),
        "sim_ts": d.get("sim_ts"), "policy_version": d.get("policy_version"),
        "failed_rules": [f"{r.get('rule_id')} {r.get('name', '')}: {r.get('detail', '')}"
                         for r in d.get("rule_results") or [] if r.get("applies") and not r.get("passed")],
        "flagged_rules": [f"{r.get('rule_id')} {r.get('name', '')}" for r in d.get("rule_results") or []
                          if r.get("applies") and r.get("effect") == "needs_approval"],
        "denied_reasons": d.get("denied_reasons") or {}, "blast_radius": d.get("blast_radius") or {},
        "untrusted_influence": bool(d.get("untrusted_influence")),
    } for d in ctx.get("decisions") or []]
    approvals = [{
        "approval_id": a.get("_id"), "request_id": a.get("request_id"), "status": a.get("status"),
        "summary": a.get("summary"), "requested_sim_ts": a.get("requested_sim_ts"),
        "decided_by": a.get("decided_by"), "decided_sim_ts": a.get("decided_sim_ts"), "note": a.get("note"),
    } for a in ctx.get("approvals") or []]
    policies = [{"rule_id": p.get("rule_id"), "title": p.get("title", ""),
                 "text": common.clip_text(p.get("text", ""), 600)} for p in ctx.get("policies") or []]
    return {"question": question, "ids_mentioned": {k: v for k, v in ids.items() if v},
            "ledger": ledger, "decisions": decisions, "approvals": approvals, "policies": policies}


def check_answer(out: AuditAnswer, ctx: dict[str, Any], fb: AuditAnswer) -> tuple[AuditAnswer, bool]:
    """Keep only real seqs in the timeline (with ledger text); reject answers citing unknown seqs.

    Returns (answer, used_fallback).
    """
    entries = ctx.get("entries") or {}
    cited = {int(n) for n in _CITE.findall(out.answer or "")}
    if not entries or not (out.answer or "").strip() or (cited - set(entries)):
        return fb, True
    seen: set[int] = set()
    timeline: list[TimelineItem] = []
    for item in sorted(out.timeline, key=lambda x: x.seq):
        e = entries.get(int(item.seq))
        if e is None or item.seq in seen:
            continue
        seen.add(item.seq)
        timeline.append(TimelineItem(seq=e["seq"], sim_ts=e["sim_ts"], actor=e["actor"],
                                     summary=common.clip_text(e["summary"], 240)))
    if not timeline:
        timeline = list(fb.timeline)
    answer = out.answer.strip()
    if not cited:
        answer += " Sources: " + ", ".join(f"[seq {t.seq}]" for t in timeline[:6]) + "."
    rules = sorted({f"R{int(m):02d}" for r in out.rules_cited for m in re.findall(r"R?(\d{1,2})", str(r))
                    if 1 <= int(m) <= 16})
    return AuditAnswer(answer=common.clip_text(answer, ANSWER_MAX + 300), timeline=timeline,
                       rules_cited=rules), False


# ---------------------------------------------------------------- entry point


@traced(AGENT, tags=["governed", "auditor"])
async def ask(run_id: str, question: str, by: str) -> AuditAnswer:
    """Answer a question about a run with ledger-seq citations; logs ledger audit_query. Never raises."""
    question = " ".join(str(question or "").split())[:1000]
    ids: dict[str, list[str]] = {}
    ctx: dict[str, Any] = {"entries": {}, "score": {}, "paths": []}
    names = dict(DISPLAY_FALLBACK)
    try:
        res_docs, biz_docs = await asyncio.gather(common.residents(), common.businesses())
        ids = extract_ids(question, res_docs, biz_docs)
        ctx, names = await asyncio.gather(retrieve(run_id, question, ids), _display_names())
    except Exception:  # noqa: BLE001 - answer from whatever was retrieved
        log.exception("auditor retrieval failed")
    fb = fallback_answer(question, ids, ctx, names)

    source, trace_id, used_fallback = "fallback", None, True
    answer = fb
    if ctx.get("entries"):
        try:
            res = await complete_json(
                agent=AGENT, model=get_settings().llm_model_smart, system=load_prompt("auditor"),
                payload=_llm_payload(question, ids, ctx), schema=AuditAnswer, fallback=lambda: fb,
                metadata={"run_id": run_id, "agent_id": AGENT, "asked_by": by,
                          "tags": ["governed", "auditor"], "effort": "low"},
            )
            source, trace_id = res.source, res.langsmith_run_id
            if res.source != "fallback":
                answer, used_fallback = check_answer(res.output, ctx, fb)  # type: ignore[arg-type]
                if used_fallback:
                    log.warning("auditor LLM answer cited unknown seqs; using deterministic answer")
                    source = "fallback"
        except Exception:  # noqa: BLE001
            log.exception("auditor LLM path failed; using deterministic answer")
    trace_id = trace_id or current_trace_id()

    sim_ts = max((e["sim_ts"] for e in ctx.get("entries", {}).values()), default=None)
    if not sim_ts:
        last = await _find(LEDGER, {"run_id": run_id}, {"sim_ts": 1}, sort=[("seq", -1)], limit=1)
        sim_ts = str(last[0]["sim_ts"]) if last else ""
    acting_for = by if str(by).startswith("human:") else "human:supervisor"
    await common.ledger_append(
        run_id, "audit_query", AGENT,
        f"Auditor answered {by}: \"{common.clip_text(question, 120)}\" citing "
        f"{len(answer.timeline)} ledger entries",
        sim_ts=sim_ts or "", acting_for=acting_for,
        payload={"question": question, "asked_by": by, "answer": answer.answer,
                 "timeline_seqs": [t.seq for t in answer.timeline], "rules_cited": answer.rules_cited,
                 "source": source, "retrieval": ctx.get("paths", []),
                 "request_ids": ctx.get("request_ids", []), "ids_mentioned": {k: v for k, v in ids.items() if v}},
        langsmith_run_id=trace_id,
    )
    return answer
