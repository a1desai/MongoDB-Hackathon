"""The single entry point for every LLM call: complete_json().

Pipeline per call: no key -> fallback | cache lookup (LLM_MODE) -> semaphore -> provider call
(30 s timeout) -> JSON parse + Pydantic validation -> one retry with the error -> fallback.
It never raises for LLM, network, cache or validation problems, so the sim can never crash or
stall on an LLM.

Providers (LLM_PROVIDER):
- openai (default): `openai.AsyncOpenAI().responses.create` with `text.format` = JSON schema
  (structured output, non-strict because our Pydantic models carry defaults and free-form dicts).
- anthropic: `anthropic.AsyncAnthropic().messages.create`.
Either way the JSON schema is also appended to the system prompt and the reply is validated
with Pydantic. With no API key for the selected provider every call takes the fallback at once,
without any network or database access.

LangSmith: when LANGSMITH_API_KEY is set (and LANGSMITH_TRACING is true) the provider client is
wrapped with `langsmith.wrappers.wrap_openai` / `wrap_anthropic`, each call runs inside
`@traceable(name=agent)`, and the run id from `get_current_run_tree()` is returned so ledger
entries can link to the trace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..db import LLM_CACHE, db
from ..ids import run_tag

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TIMEOUT_S = 30.0
CACHE_IO_TIMEOUT_S = 3.0
MAX_TOKENS = 8192
RUNTAG = "RUNTAG"
PROVIDERS = ("openai", "anthropic")
OPENAI_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

Source = Literal["llm", "cache", "fallback"]


@dataclass
class LLMResult:
    output: BaseModel
    source: Source
    langsmith_run_id: str | None = None


# ---------------------------------------------------------------- helpers


def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, str() for datetimes etc."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@lru_cache
def load_prompt(name: str) -> str:
    """Text of llm/prompts/<name>.md ("planner", "negotiator", ...). Empty string if missing."""
    path = PROMPTS_DIR / f"{name.removesuffix('.md')}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("prompt %s not found", path)
        return ""


_TAG_BREAK = re.compile(r"<\s*/?\s*untrusted", re.IGNORECASE)


def _neutralize(text: str) -> str:
    """Stop untrusted text from opening or closing its own <untrusted> tag."""
    return _TAG_BREAK.sub(lambda m: m.group(0).replace("<", "&lt;"), text)


def wrap_untrusted(messages: list[dict]) -> str:
    """Render message docs as <untrusted id="msg_..">body</untrusted> blocks, one per line.

    Accepts Message.to_mongo() dicts (`_id`, `from`, `body`) or plain {id, from, body} dicts.
    """
    out = []
    for m in messages:
        mid = str(m.get("_id") or m.get("id") or "unknown")
        mid = re.sub(r"[^A-Za-z0-9_:\-.]", "", mid)
        sender = str(m.get("from") or m.get("from_") or "")
        sender = re.sub(r"[^A-Za-z0-9_:\-.]", "", sender)
        body = _neutralize(str(m.get("body", "")))
        attrs = f'id="{mid}"' + (f' from="{sender}"' if sender else "")
        out.append(f"<untrusted {attrs}>{body}</untrusted>")
    return "\n".join(out)


def _first_word(value: str | None, default: str) -> str:
    """Settings values may carry inline .env comments on some loaders; keep the first token."""
    raw = (value or "").strip().lower()
    return raw.split()[0] if raw else default


def _mode() -> str:
    mode = _first_word(get_settings().llm_mode, "live")
    return mode if mode in ("live", "cache_first", "cache_only") else "live"


def provider() -> str:
    """The selected LLM provider: "openai" (default) or "anthropic"."""
    p = _first_word(get_settings().llm_provider, "openai")
    return p if p in PROVIDERS else "openai"


def provider_key() -> str:
    s = get_settings()
    return (s.anthropic_api_key if provider() == "anthropic" else s.openai_api_key) or ""


def llm_available() -> bool:
    """True when the selected provider has an API key (otherwise every call is a fallback)."""
    return bool(provider_key().strip())


def _run_id_of(payload: dict, metadata: dict | None) -> str | None:
    rid = (metadata or {}).get("run_id") or payload.get("run_id")
    return str(rid) if rid else None


def cache_key(model: str, system: str, payload: dict, run_id: str | None) -> str:
    """sha256(model + system + canonical_json(payload)) with the run tag replaced by RUNTAG."""
    src = model + system + canonical_json(payload)
    if run_id:
        tag = run_tag(run_id)
        if tag:
            src = src.replace(tag, RUNTAG)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def _schema_dict(schema: type[BaseModel]) -> dict[str, Any]:
    try:
        return schema.model_json_schema()
    except Exception:  # noqa: BLE001 - schema generation must never break a call
        return {"type": "object"}


def _schema_name(schema: type[BaseModel]) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", schema.__name__)[:64] or "output"


def _full_system(system: str, schema: type[BaseModel]) -> str:
    return (
        f"{system.strip()}\n\n"
        "Respond with exactly one JSON object and nothing else (no prose, no code fences). "
        f"It must validate against this JSON Schema:\n"
        f"{json.dumps(_schema_dict(schema), separators=(',', ':'))}"
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates code fences and stray prose)."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(t[start : end + 1])


def _safe_fallback(fallback: Callable[[], BaseModel], schema: type[BaseModel]) -> BaseModel:
    try:
        return fallback()
    except Exception:  # noqa: BLE001 - last line of defence
        log.exception("fallback for %s raised; using schema defaults", schema.__name__)
        return schema.model_construct()


# ---------------------------------------------------------------- clients, semaphore, tracing

_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    """One Semaphore(LLM_CONCURRENCY) per event loop (tests may run several loops)."""
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(id(loop))
    if sem is None:
        sem = asyncio.Semaphore(max(1, get_settings().llm_concurrency))
        _semaphores[id(loop)] = sem
    return sem


def langsmith_enabled() -> bool:
    s = get_settings()
    return bool(s.langsmith_api_key) and bool(s.langsmith_tracing)


@lru_cache
def _configure_langsmith() -> bool:
    """pydantic-settings reads .env without exporting it; LangSmith reads os.environ."""
    if not langsmith_enabled():
        return False
    s = get_settings()
    os.environ.setdefault("LANGSMITH_API_KEY", s.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", s.langsmith_project)
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    return True


@lru_cache
def _client(which: str) -> Any:
    """The provider SDK client (one per process), LangSmith-wrapped when tracing is on."""
    s = get_settings()
    if which == "anthropic":
        import anthropic

        client: Any = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key, timeout=TIMEOUT_S,
                                               max_retries=1)
        wrapper = "wrap_anthropic"
    else:
        import openai

        client = openai.AsyncOpenAI(api_key=s.openai_api_key, timeout=TIMEOUT_S, max_retries=1)
        wrapper = "wrap_openai"
    if _configure_langsmith():
        try:
            from langsmith import wrappers

            client = getattr(wrappers, wrapper)(client)
        except Exception:  # noqa: BLE001
            log.warning("langsmith %s failed; tracing LLM calls disabled", wrapper)
    return client


def _current_run_id() -> str | None:
    try:
        from langsmith import get_current_run_tree

        rt = get_current_run_tree()
        return str(rt.id) if rt is not None else None
    except Exception:  # noqa: BLE001
        return None


def traced(name: str, *, tags: list[str] | None = None, metadata: dict | None = None,
           process_inputs: Callable[[dict], dict] | None = None):
    """Decorator: langsmith @traceable(run_type="chain") when LangSmith is on, identity otherwise.

    Pass `process_inputs` for functions that take a World (numpy arrays must not be uploaded).
    """

    def deco(fn):
        if not _configure_langsmith():
            return fn
        try:
            from langsmith import traceable

            kw: dict[str, Any] = {"name": name, "run_type": "chain", "tags": tags or [],
                                  "metadata": metadata or {}}
            if process_inputs is not None:
                kw["process_inputs"] = _safe_inputs(process_inputs)
            return traceable(**kw)(fn)
        except Exception:  # noqa: BLE001
            return fn

    return deco


def _safe_inputs(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
    def wrapper(inputs: dict) -> dict:
        try:
            return fn(inputs)
        except Exception:  # noqa: BLE001
            return {}

    return wrapper


def _complete_inputs(inputs: dict) -> dict:
    """What a complete_json trace shows: payload and agent, not the fallback callable."""
    schema = inputs.get("schema")
    return {"agent": inputs.get("agent"), "model": inputs.get("model"),
            "schema": getattr(schema, "__name__", str(schema)), "payload": inputs.get("payload")}


def current_trace_id() -> str | None:
    """LangSmith run id of the enclosing traced call, or None."""
    return _current_run_id() if langsmith_enabled() else None


# ---------------------------------------------------------------- cache


async def _cache_get(key: str, run_id: str | None) -> Any | None:
    try:
        doc = await asyncio.wait_for(db()[LLM_CACHE].find_one({"_id": key}), CACHE_IO_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.warning("llm_cache read failed: %s", e)
        return None
    if not doc or "response" not in doc:
        return None
    resp = doc["response"]
    if run_id and isinstance(resp, str):
        resp = resp.replace(RUNTAG, run_tag(run_id))
    return resp


async def _cache_put(key: str, model: str, agent: str, text: str, run_id: str | None) -> None:
    if run_id and run_tag(run_id):
        text = text.replace(run_tag(run_id), RUNTAG)
    doc = {"_id": key, "model": model, "agent": agent, "provider": provider(), "response": text,
           "created_at": datetime.now(UTC)}
    try:
        await asyncio.wait_for(
            db()[LLM_CACHE].replace_one({"_id": key}, doc, upsert=True), CACHE_IO_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001
        log.warning("llm_cache write failed: %s", e)


# ---------------------------------------------------------------- provider calls


def _effort(which: str, metadata: dict | None) -> str | None:
    """Reasoning effort from metadata["effort"]; OpenAI defaults to "low" to fit the 30 s budget."""
    md = metadata or {}
    if which == "anthropic":
        effort = md.get("effort", "medium")
        return effort if effort in ANTHROPIC_EFFORTS else None
    effort = md.get("effort", "low")
    return effort if effort in OPENAI_EFFORTS else None


def _openai_text(resp: Any) -> str:
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                raise RuntimeError(f"model refused: {str(getattr(part, 'refusal', ''))[:200]}")
    text = (getattr(resp, "output_text", "") or "").strip()
    if not text:
        details = getattr(resp, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details else None
        raise RuntimeError(f"empty reply (status={getattr(resp, 'status', None)}, reason={reason})")
    return text


def _anthropic_text(resp: Any) -> str:
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("model refused")
    parts = [getattr(b, "text", "") for b in getattr(resp, "content", []) or []
             if getattr(b, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError(f"empty reply (stop_reason={getattr(resp, 'stop_reason', None)})")
    return text


async def _call_openai(model: str, system: str, messages: list[dict], schema: type[BaseModel],
                       opts: dict[str, Any]) -> str:
    kwargs: dict[str, Any] = {"model": model, "instructions": system, "input": messages,
                              "max_output_tokens": MAX_TOKENS, "store": False}
    if opts.get("structured", True):
        kwargs["text"] = {"format": {"type": "json_schema", "name": _schema_name(schema),
                                     "schema": _schema_dict(schema), "strict": False}}
    if opts.get("effort"):
        kwargs["reasoning"] = {"effort": opts["effort"]}
    resp = await _client("openai").responses.create(**kwargs)
    return _openai_text(resp)


async def _call_anthropic(model: str, system: str, messages: list[dict], opts: dict[str, Any]) -> str:
    kwargs: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                              "messages": messages}
    if opts.get("effort") and not model.startswith(("claude-haiku-4", "claude-3")):
        kwargs["output_config"] = {"effort": opts["effort"]}
    resp = await _client("anthropic").messages.create(**kwargs)
    return _anthropic_text(resp)


async def _call_llm(which: str, model: str, system: str, messages: list[dict],
                    schema: type[BaseModel], opts: dict[str, Any]) -> str:
    async with _semaphore():
        if which == "anthropic":
            coro = _call_anthropic(model, system, messages, opts)
        else:
            coro = _call_openai(model, system, messages, schema, opts)
        return await asyncio.wait_for(coro, TIMEOUT_S)


async def _attempt_llm(
    *, agent: str, model: str, system: str, user: str, schema: type[BaseModel],
    metadata: dict | None,
) -> tuple[BaseModel | None, str | None]:
    """Up to two tries (the second carries the error). Returns (validated output, raw text)."""
    which = provider()
    messages: list[dict] = [{"role": "user", "content": user}]
    opts: dict[str, Any] = {"structured": True, "effort": _effort(which, metadata)}
    for attempt in (1, 2):
        text: str | None = None
        try:
            text = await _call_llm(which, model, system, messages, schema, opts)
            return schema.model_validate(extract_json(text)), text
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            err = f"{type(e).__name__}: {str(e)[:800]}"
        except (TimeoutError, asyncio.TimeoutError):
            err = f"timeout after {TIMEOUT_S:.0f}s"
        except Exception as e:  # noqa: BLE001 - nothing may escape complete_json
            err = f"{type(e).__name__}: {str(e)[:300]}"
            if type(e).__name__ == "BadRequestError":
                # The model may reject optional params (schema format, reasoning effort): the
                # schema is in the system prompt, so retry without them.
                opts = {"structured": False, "effort": None}
        log.warning("LLM %s (%s %s) attempt %d failed: %s", agent, which, model, attempt, err)
        if attempt == 1 and text:
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"That output was invalid: {err}\n"
                    "Reply again with only the corrected JSON object.")},
            ]
    return None, None


async def _complete(
    *, agent: str, model: str, system: str, payload: dict, schema: type[BaseModel],
    fallback: Callable[[], BaseModel], metadata: dict | None,
) -> LLMResult:
    run_id = _run_id_of(payload, metadata)
    trace_id = current_trace_id()

    if not llm_available():
        return LLMResult(_safe_fallback(fallback, schema), "fallback", trace_id)

    mode = _mode()
    full_system = _full_system(system, schema)
    key = cache_key(model, system, payload, run_id)

    if mode in ("cache_first", "cache_only"):
        cached = await _cache_get(key, run_id)
        if cached is not None:
            try:
                out = schema.model_validate(extract_json(cached) if isinstance(cached, str) else cached)
                return LLMResult(out, "cache", trace_id)
            except (ValidationError, ValueError) as e:
                log.warning("cached %s response invalid, ignoring: %s", agent, e)
        if mode == "cache_only":
            return LLMResult(_safe_fallback(fallback, schema), "fallback", trace_id)

    user = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    out, _text = await _attempt_llm(agent=agent, model=model, system=full_system, user=user,
                                    schema=schema, metadata=metadata)
    if out is None:
        return LLMResult(_safe_fallback(fallback, schema), "fallback", trace_id)
    await _cache_put(key, model, agent, out.model_dump_json(), run_id)
    return LLMResult(out, "llm", trace_id)


async def complete_json(
    *,
    agent: str,
    model: str,
    system: str,
    payload: dict,
    schema: type[BaseModel],
    fallback: Callable[[], BaseModel],
    metadata: dict | None = None,
) -> LLMResult:
    """One validated JSON completion, or the deterministic fallback (source "fallback").

    `agent` is the identity making the call ("agent:planner", "sim:resident:hh_00042").
    `metadata` may carry run_id, sim_ts, request_ids, tags and an optional reasoning "effort".
    """
    md = dict(metadata or {})
    try:
        if not _configure_langsmith():
            return await _complete(agent=agent, model=model, system=system, payload=payload,
                                   schema=schema, fallback=fallback, metadata=md)
        tags = list(md.pop("tags", None) or [])
        tags.append("governed" if agent.startswith(("agent:", "system:")) else "world")
        if md.get("run_id"):
            tags.append(str(md["run_id"]))
        ls_meta = {k: v for k, v in md.items() if k != "effort"}
        ls_meta.setdefault("agent_id", agent)
        ls_meta.setdefault("provider", provider())

        traced_fn = traced(agent, tags=tags, metadata=ls_meta,
                           process_inputs=_complete_inputs)(_complete)
        return await traced_fn(agent=agent, model=model, system=system, payload=payload,
                               schema=schema, fallback=fallback, metadata=md)
    except Exception:  # noqa: BLE001 - the sim must never crash on an LLM call
        log.exception("complete_json(%s) failed unexpectedly; using fallback", agent)
        return LLMResult(_safe_fallback(fallback, schema), "fallback", None)
