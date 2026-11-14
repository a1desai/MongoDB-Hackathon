# Cooldown — build contracts

PLAN.md is the product spec. This file pins what PLAN.md leaves open: who owns which files, and the
exact interfaces between them, so workstreams can build in parallel without drifting.

**Frozen (do not rename/remove, only add optional fields):** `backend/app/config.py`, `db.py`, `ids.py`,
`timeutil.py`, `runtime.py`, `models/__init__.py`, `sim/world_state.py`. If you truly need a change
there, add it and note it at the top of your final report.

## Decisions that override PLAN.md

1. **LangGraph is the orchestrator** (replaces PLAN's "small in-house orchestrator"). The governed
   pipeline is two compiled LangGraph graphs checkpointed in Atlas with
   `langgraph.checkpoint.mongodb.MongoDBSaver` (v0.5.1: takes a sync `pymongo.MongoClient`, which is the one allowed exception to the single-AsyncMongoClient rule; LangGraph calls its async `a*` methods; collections `langgraph_checkpoints`,
   `langgraph_checkpoint_writes`). Human approval uses LangGraph `interrupt()`; the approvals change
   stream resumes the thread with `Command(resume=...)`. LangSmith traces LangGraph natively.
2. **Embeddings default to Atlas Automated Embedding** (`EMBED_MODE=auto`): `autoEmbed` vector index
   (model `voyage-4`) and `$vectorSearch` with `query: {text}`. No Voyage key needed.
   `EMBED_MODE=client` (Voyage API + stored `embedding` vectors + `queryVector`) must also work.
3. **Auditor retrieval is hybrid search**: `$rankFusion` over an Atlas Search index on `ledger.summary`
   (`ledger_search`) and an `autoEmbed` vector index on `ledger.summary`, plus `$vectorSearch` on
   `policy_docs`. Fall back to a `$regex`/`$text` query if search indexes are not ready.
4. **LLM provider is OpenAI** (`LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `LLM_MODEL_SMART=gpt-5.5`,
   `LLM_MODEL_FAST=gpt-5.4-mini`), using the `openai` SDK with JSON-schema structured output and
   `langsmith.wrappers.wrap_openai`. `LLM_PROVIDER=anthropic` stays supported. With no key for the
   selected provider, every LLM call takes the deterministic fallback (`source: "fallback"`).
5. **No Cognee**: it would add SQLite/LanceDB/Kuzu stores, which breaks PLAN rule 2 (Atlas is the
   only database). Atlas Vector Search + Automated Embedding is the memory layer.
6. Python 3.12. Backend venv: `cd backend && uv venv --python 3.12 .venv && uv pip install -r requirements.txt`.
   Run everything from `backend/`: `.venv/bin/python -m scripts.init_db`, `.venv/bin/pytest -q`.
7. Ids come from `app.ids.new_id(prefix, run_id)`. Times: `app.timeutil` (`sim_iso`, `parse_sim`, `at`, `hhmm`).
8. Every PyMongo call is async (`AsyncMongoClient` via `app.db.db()`). Never construct another client.

## File ownership

| WS | Owns (create/edit only these) |
| --- | --- |
| W1 data | `scenarios/heatwave_2031.yaml`, `backend/scripts/fetch_data.py`, `backend/scripts/build_world.py`, `data/` |
| W2 atlas | `backend/scripts/init_db.py`, `backend/scripts/seed_memory.py`, `backend/app/embeddings/`, `backend/app/memory/`, `backend/app/governance/seed.py` (identities + policies + policy_docs seed data) |
| W3 sim | `backend/app/sim/` except `engine.py` and `world_state.py`; `backend/tests/test_thermal.py` |
| W4 gov | `backend/app/governance/` except `seed.py`; `backend/tests/test_gate_rules.py`, `backend/tests/test_ledger.py` |
| W5 exec | `backend/app/executor/`, `backend/app/mock_systems/` |
| W6 llm | `backend/app/llm/`, `backend/app/world_agents/`, `backend/app/agents/negotiator.py`, `backend/app/agents/auditor.py` |
| W7 agents | `backend/app/agents/` except negotiator/auditor; `backend/tests/test_fallback_planner.py` |
| W8 engine | `backend/app/sim/engine.py`, `backend/app/main.py`, `backend/app/api/`, `backend/tests/conftest.py`, `backend/tests/test_smoke_run.py` |
| W9 ui-core | `frontend/` except the files W10 owns |
| W10 ui-map | `frontend/components/CityMap.tsx`, `frontend/components/ResidentPanel.tsx`, `frontend/components/IntroMap.tsx`, `frontend/lib/mapLayers.ts` |

Every package directory needs an `__init__.py` (create the ones under the paths you own).

## Data files (W1 writes, W2 loads into Atlas)

`data/processed/` JSON arrays, one file per collection, shapes per PLAN §5.2:
`neighbourhoods.json`, `sections.json`, `facilities.json`, `households.json`, `archetypes.json`,
`residents.json`, `businesses.json`, `weather.json`, `event_memory_seed.json` (30 heat events, no embeddings).
- GeoJSON geometry everywhere (`{"type":"Polygon"|"MultiPolygon","coordinates":...}`, `{"type":"Point","coordinates":[lon,lat]}`). Valid for 2dsphere (closed rings, no self-intersections: `shapely.make_valid`, then `orient`).
- households: `{_id:"hh_00001", idx:0, location, neighbourhood_id, section_id, archetype_id, dwelling, ac_type, thermal:{k_env,k_ac,ac_kw,solar}, setpoint_base, comfort_max, base_kw_scale, devices:{thermostat_id?, ev_session_id?, battery_id?}, consent:{thermostat_program, ev_program, battery_program, sms_opt_in}, has_ev, has_battery, ev_arrival_h?, vulnerable, income_quintile, featured_resident_id?}`. `idx` is 0-based and dense; `_id` is `hh_{idx+1:05d}`.
- sections: `{_id:"TOR-S01", geometry, centroid, households, vulnerable, income_quintile, protected, protected_reasons[]}`.
- residents: `{_id:"res_01", household_id, name, age_bracket, household, job, attitudes, persona, injection_actor}` (exactly one `injection_actor: true`).
- businesses: `{_id:"biz_harbourfront_mall", name, max_mw, notice_min, protected, constraints, location, section_id}`.
- archetypes: `{_id, description, defaults:{override_rate_per_hour, offer_acceptance, ev_pause_acceptance}}`.
- weather: `{scenario_id, ts (ISO, America/Toronto), temp_c}` at 5-minute resolution 12:00–21:30 (interpolate hourly + scenario `delta_c`).
- `data/processed/meta.json`: `{households, seed, bbox, generated_at, sources_ok:{...}}`.
`build_world.py` honours `SIM_HOUSEHOLDS` and `SIM_SEED`, and must work offline (synthetic fallbacks).

## Atlas (W2)

`init_db.py` is idempotent: create/collMod every collection in `app.db` with `$jsonSchema` validators
(PLAN §6.2 for ledger, action_requests, decisions, policies, identities; sensible ones for the rest),
`feeder_load` as a **time series** collection (`timeField: ts`, `metaField: meta`, granularity minutes),
the PLAN §6.3 indexes, then loads `data/processed/*.json` (replace collections: households, sections,
facilities, neighbourhoods, archetypes, residents, businesses, weather, scenarios), seeds identities
and policies from `governance/seed.py`, and creates search indexes with
`await coll.create_search_index(SearchIndexModel(definition=..., name=..., type="vectorSearch"|"search"))`,
then polls `list_search_indexes()` until queryable (timeout 180 s, warn and continue).
- `event_memory_vec` on `event_memory`, `policy_docs_vec` on `policy_docs`, `ledger_vec` on `ledger`
  (`summary`, filter `run_id`): autoEmbed `{type:"autoEmbed", modality:"text", path:"summary"|"text", model:"voyage-4"}` + filter fields
  in auto mode; `{type:"vector", path:"embedding", numDimensions:1024, similarity:"cosine"}` in client mode.
- `ledger_search` (Atlas Search, `type:"search"`): `summary` as string, `run_id` as token.
- `seed_memory.py`: inserts event_memory (from the seed JSON, `_id: mem_0001..`) and policy_docs (one per rule, `{_id:"pd_R01", rule_id, title, text}`); embeds with Voyage only in client mode.

`app/memory/retrieval.py`:
```python
async def similar_events(query_text: str, k: int = 3) -> list[SimilarEvent]
async def policy_snippets(query_text: str, k: int = 3) -> list[dict]       # {rule_id, title, text, score}
async def ledger_search(run_id: str, query_text: str, k: int = 10) -> list[dict]  # hybrid $rankFusion; ledger docs
```
All three must return `[]` (never raise) when an index is missing or not ready.
`app/embeddings/voyage.py`: `async def embed(texts: list[str], input_type: str) -> list[list[float]]` (httpx, batches of 64).

## Simulation (W3)

```python
# sim/scenario.py
def load_scenario(scenario_id: str) -> dict                      # parses scenarios/<id>.yaml
# sim/loader.py
async def load_world(run_id: str, mode: str, scenario: dict, seed: int) -> World   # from Atlas collections
# sim/clock.py
def tick_minutes() -> int; def dt_hours() -> float
# sim/weather.py
def outdoor_c(world: World, t: datetime) -> float                 # from the weather collection, cached on world.scenario["_weather"]
# sim/thermal.py
def step_thermal(world: World, dt_h: float, t_out: float) -> None
def predict_max_tin(world: World, mask: np.ndarray, offset_c: float, start: datetime, end: datetime) -> np.ndarray   # per masked home
def kw_saved_estimate(world: World, mask: np.ndarray, offset_c: float) -> np.ndarray
# sim/behavior.py
def step_behavior(world: World, dt_h: float) -> list[dict]        # follower overrides, SMS acceptance effects, EV arrivals/charging, batteries, businesses
# sim/grid.py
def aggregate(world: World) -> dict    # sets world.demand_mw/supply_mw/shortfall_mw/section_demand_mw; returns {demand_mw, supply_mw, shortfall_mw, sections:{id: mw}}
def supply_mw(world: World, t: datetime) -> float                 # scenario supply minus contingencies that have fired
def check_contingencies(world: World) -> list[dict]               # fires scheduled contingencies at their time (world_event dicts)
def check_uncontrolled_trip(world: World) -> dict | None          # PLAN §7.6; cuts a random section for 90 min ignoring protection
def set_section_power(world: World, section_idx: int, on: bool, until: datetime | None) -> None
def release_expired_cuts(world: World) -> list[str]               # world-imposed cuts only (agent cuts are restored via feeder_restore)
# sim/baseline.py
class BaselineOperator:
    def step(self, world: World) -> list[dict]                    # applies status-quo actions directly to world; returns world_event dicts
# sim/metrics.py
def update(world: World, dt_h: float) -> None
def summary(world: World) -> dict                                 # PLAN §7.9 at real scale
def encode_map(world: World) -> dict                              # {"temps": b64 uint8 round(T_in*4), "flags": b64 bitfield}; bit0 power_off, bit1 overridden, bit2 precool, bit3 setback, bit4 vulnerable
def world_step(world: World, dt_h: float) -> list[dict]           # in sim/physics.py: weather -> thermal -> behaviour; returns world events
```
World events are dicts `{type: "world_event"|"contingency", summary, payload}` that the engine writes to the ledger as `actor="sim:world"`.

## Governance (W4)

```python
# governance/context.py
@dataclass
class GateContext:
    world: World; run: dict; policies: dict[str, Policy]; policy_version: str; identities: dict[str, Identity]
    sim_now: datetime; halted: bool; halted_scope: str; t_safe_c: float; budget_left_cad: float
    lower_rung_potential_mw: float; active_delivered_mw: float; projected_shortfall_mw: float
async def build_gate_context(world: World, run: dict) -> GateContext
# governance/gate.py — pure, deterministic, no I/O
def evaluate(request: ActionRequest, ctx: GateContext) -> Decision      # all 16 rules, vectorized for batches, precedence per PLAN §9
async def decide(request: ActionRequest, world: World, run: dict) -> Decision   # builds ctx, evaluates, stores decision, updates request status, appends ledger "decision"
# governance/ledger.py — the ONLY writer of the ledger collection
async def append(run_id: str, type: str, actor: str, summary: str, *, sim_ts: str, acting_for: str | None = None,
                 refs: dict | None = None, payload: dict | None = None, langsmith_run_id: str | None = None) -> LedgerEntry
async def verify_chain(run_id: str) -> dict   # {ok, checked, first_broken_seq}
def canonical_json(obj) -> str
# governance/approvals.py
async def request_approval(request: ActionRequest, decision: Decision, world: World) -> Approval   # 10 sim-minute expiry, ledger approval_requested
async def decide_approval(approval_id: str, decision: str, by: str, note: str | None) -> Approval  # requires human:supervisor; ledger approval_decided
async def expire_due(run_id: str, sim_now: datetime) -> list[Approval]
# governance/control.py
async def halt(run_id: str, scope: str, by: str, reason: str) -> AgentControl      # ledger halt
async def resume(run_id: str, by: str) -> AgentControl                            # ledger resume
async def is_halted(run_id: str, agent_id: str | None = None) -> bool
# governance/rollback.py
async def request_rollback(execution_id: str, by: str, reason: str) -> ActionRequest   # builds action_type "undo" from execution.undo; ledger rollback_requested
# governance/identities.py
async def load_identities() -> dict[str, Identity]; async def load_policies() -> tuple[dict[str, Policy], str]
```
Rules live in `governance/rules/r01_scope.py … r16_restore.py`, each exposing
`def check(request, ctx, mask) -> RuleResult` plus, for per-target rules, the denied mask.
`identities`/`policies` seed data (PLAN §8.1, §9 with params) are in `governance/seed.py` (owned by W2;
W4 reads policy params by rule id and must not hardcode thresholds that exist in params).

## Executor and mock systems (W5)

Mock systems are FastAPI routers mounted under `/mock/<system>/v1/...` (PLAN §7.7). Each request
carries headers `X-Api-Key` (only `agent:executor`'s key is accepted: `mock_systems.keys.EXECUTOR_KEY`),
`Idempotency-Key`, and `X-Run-Id` (selects `runtime.get_world(run_id)`). State lives in
`world.mock[...]` and is persisted to the `mock_*` collections.
```python
# mock_systems/__init__.py
routers: list[APIRouter]
def apply_pending(world: World) -> list[dict]       # per tick: latency/failure quirks, applies commands to world arrays; returns world events
async def persist(world: World) -> None             # bulk_write changed mock state
# executor/executor.py
class Executor:
    def __init__(self, http: httpx.AsyncClient): ...   # base_url = settings.api_base_url, or ASGITransport in tests
    async def execute(self, request: ActionRequest, decision: Decision, world: World) -> Execution
        # re-checks R15 + R01, reads pre_state, writes executions doc with undo, POSTs with Idempotency-Key=request.id,
        # retries 503 up to 3x, sets request.status "executing", ledger execution_sent
# executor/verifier.py
async def verify_pending(world: World) -> list[Execution]   # polls batch/device state; verified >=90% applied / partial / failed after 3 ticks; delivered_mw; ledger execution_verified|execution_failed
```

## Agents and LangGraph (W7), LLM layer (W6)

```python
# llm/client.py (W6)
@dataclass
class LLMResult: output: BaseModel; source: Literal["llm","cache","fallback"]; langsmith_run_id: str | None
async def complete_json(*, agent: str, model: str, system: str, payload: dict, schema: type[BaseModel],
                        fallback: Callable[[], BaseModel], metadata: dict | None = None) -> LLMResult
def wrap_untrusted(messages: list[dict]) -> str     # <untrusted id="msg_..">...</untrusted>
# agents/forecaster.py
async def update(world: World) -> Forecast            # copies state, simulates 6 h forward; stores forecasts doc; ledger world_event
# agents/fallback_planner.py
def fallback_plan(ctx: dict) -> PlannerOutput        # rungs in order until expected MW >= 1.15 x shortfall
# agents/planner.py
async def plan(world: World, forecast: Forecast, trigger: str) -> Plan   # context in code, $vectorSearch memory, LLM or fallback; stores plan; ledger plan_created
# agents/targeter.py
async def to_requests(plan: Plan, world: World) -> list[ActionRequest]   # ranks homes with thermal.kw_saved_estimate; <=2,000 per request; stores; ledger request_proposed
# agents/watcher.py
async def check(world: World, state: dict) -> list[str]   # replan reasons; expires approvals
# agents/restorer.py
async def propose(world: World) -> list[ActionRequest]
# agents/graph.py — LangGraph
class Governance:
    def __init__(self, world: World, run: dict, executor: Executor, checkpointer: MongoDBSaver): ...
    async def planning_cycle(self, trigger: str) -> Plan | None   # plan graph: context -> planner -> targeter -> fan out process_request
    async def process_request(self, request_id: str) -> str       # request graph (thread_id=request_id): gate -> [needs_approval: interrupt()] -> executor; returns final status
    async def resume_request(self, approval: Approval) -> str      # Command(resume={"status": approval.status, "by": approval.decided_by})
# agents/negotiator.py (W6)
async def compose(request: ActionRequest, world: World) -> list[OutboundMessage]
# agents/auditor.py (W6)
async def ask(run_id: str, question: str, by: str) -> AuditAnswer   # ledger audit_query
# world_agents/ (W6): archetype.react(world, event) / resident.on_message(world, message) / business.on_request(world, request)
```

## Engine and API (W8)

`sim/engine.py: class RunEngine` owns one run: loads the World, registers it in `runtime`, runs the tick
loop (PLAN §7.2) as an asyncio task, writes `feeder_load` (time series), map snapshots, metrics, and
drives `Governance` (governed mode) or `BaselineOperator` (baseline mode). Honour
`pause_on_planning` and `pause_on_approval`. `main.py` mounts the API routers and mock routers, and
on startup starts change-stream listeners: ledger inserts → WS, approvals updates →
`Governance.resume_request` + WS, agent_control updates → `world.halted` + WS.
REST and WS exactly as PLAN §13. `GET /world/static` returns
`{sections: FeatureCollection, facilities: FeatureCollection, neighbourhoods: FeatureCollection,
households: {ids: [...], lon: [...], lat: [...], vulnerable: [...], featured: {res_id: idx}}, residents: [...]}`.

WebSocket messages (`/ws?run_id=`), JSON:
```
{type:"tick", sim_ts, tick, outdoor_c, demand_mw, supply_mw, shortfall_mw, halted, status, paused_reason?}
{type:"ledger", entry}            # from the change stream
{type:"approval", approval}       # from the change stream
{type:"request", request}         # request status changes (ActionRequest + decision + execution summary)
{type:"plan", plan}
{type:"map", sim_ts, temps, flags}  # every 3 ticks, base64, fixed household order
{type:"sections", states:[{id, cut, cut_minutes_today, protected}]}
{type:"message", entry}
{type:"metrics", summary}         # end of run
```

## Frontend (W9, W10)

Next.js App Router + TypeScript + Tailwind + Zustand + Mapbox GL JS (`NEXT_PUBLIC_MAPBOX_TOKEN`).
`lib/api.ts`, `lib/ws.ts`, `store/useRunStore.ts` (W9) are the contract for W10: W10 reads
`useRunStore` (`staticWorld`, `mapBuffers {temps: Uint8Array, flags: Uint8Array, simTs}`, `sections`,
`selectedResidentId`, `selectedSectionId`, `setSelectedResident`, `setSelectedSection`, `threads`) and
must not add store slices except via W9's store file. If the token is missing, show a clear
placeholder instead of crashing.

## Policy parameters (W2 seeds them in `governance/seed.py`, W4 reads them; policy-set version `2031.06.1`)

| Rule | name | effect | params |
| --- | --- | --- | --- |
| R01 | SCOPE | deny | `{}` |
| R02 | CONSENT | deny | `{}` |
| R03 | PROGRAM_LIMITS | deny | `{offset_min_c:-2.0, offset_max_c:2.0, precool_max_min:120, setback_max_min:240, setbacks_per_home_per_day:1}` |
| R04 | VULNERABLE_EXEMPT | deny | `{exempt_actions:["thermostat_setback","ev_pause"], exempt_sms_offer_types:["reduce_cooling"]}` |
| R05 | SAFE_INDOOR | deny | `{t_safe_c:29.0}` (scenario `t_safe_c` overrides) |
| R06 | MESSAGING | deny | `{max_per_recipient_per_day:2, quiet_start:"22:00", quiet_end:"08:00", max_chars:320, max_incentive_cad:10, denylist:["doctor","medication","symptom","heatstroke","dehydration"], required_suffix:"Reply STOP to opt out"}` |
| R07 | LADDER | deny | `{}` |
| R08 | PROTECTED_SECTION | deny | `{}` |
| R09 | OUTAGE_DURATION | deny | `{max_minutes_per_section_per_day:60}` |
| R10 | ROTATION | deny | `{}` |
| R11 | EQUITY | deny | `{max_quintile_gap_min:60}` |
| R12 | PRE_NOTIFY | deny | `{min_notice_min:15}` |
| R13 | HUMAN_APPROVAL | needs_approval | `{always_actions:["feeder_outage"], max_homes_real_without_approval:250000}` (PLAN says 5,000, but device batches are 2,000 sim homes × scale, so 5,000 would put every batch behind approval and break the demo beat) |
| R14 | UNTRUSTED_EVIDENCE | needs_approval | `{low_impact_actions:["send_sms"]}` |
| R15 | KILL_SWITCH | deny | `{}` |
| R16 | RESTORE_SAFETY | needs_approval | `{stagger_groups:4, stagger_min:10}` |

Identities (PLAN §8.1): `human:operator`, `human:supervisor` ("A. Rahman", on shift), `human:auditor` ("J. Okafor"),
`agent:forecaster|planner|targeter|negotiator|executor|watcher|restorer|auditor` (all `acting_for: "human:supervisor"`),
`system:gate`. Only `agent:executor` has `execute` scope.

## Testing without Atlas

A local MongoDB 8.0 replica set runs in Docker at `mongodb://localhost:27019/?directConnection=true`
(no Atlas Search/Vector Search). Use your own database name (`MONGODB_DB=cooldown_<ws>`) so parallel
workstreams don't collide. Search-dependent code must degrade gracefully there.
