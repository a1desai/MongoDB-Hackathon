"""Shared Pydantic v2 models: one per collection plus every LLM output.

These are the frozen contract between workstreams (see docs/CONTRACTS.md). Add optional
fields if you need them; never rename or remove fields. Documents are stored with
`model.model_dump(by_alias=True)` so `id` becomes `_id` and `from_` becomes `from`.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal[
    "precool",
    "thermostat_setback",
    "ev_pause",
    "battery_discharge",
    "business_curtail_request",
    "send_sms",
    "feeder_outage",
    "feeder_restore",
    "thermostat_restore",
    "undo",
]
System = Literal["thermlink", "chargeflow", "powercell", "textline", "gridops"]
RequestStatus = Literal[
    "proposed",
    "denied",
    "needs_approval",
    "approved",
    "executing",
    "verified",
    "partial",
    "failed",
    "rolled_back",
    "cancelled",
]
Verdict = Literal["allow", "partial_allow", "deny", "needs_approval"]
Trust = Literal["system", "untrusted"]
LedgerType = Literal[
    "plan_created",
    "request_proposed",
    "decision",
    "approval_requested",
    "approval_decided",
    "execution_sent",
    "execution_verified",
    "execution_failed",
    "rollback_requested",
    "halt",
    "resume",
    "message_out",
    "message_in",
    "replan_triggered",
    "contingency",
    "world_event",
    "audit_query",
]

# Rung ladder, least harm first (PLAN §8.2). feeder_outage is the last resort.
RUNGS: dict[str, int] = {
    "precool": 1,
    "battery_discharge": 2,
    "ev_pause": 3,
    "business_curtail_request": 4,
    "thermostat_setback": 5,
    "send_sms": 6,
    "feeder_outage": 7,
}

ACTION_SYSTEM: dict[str, str] = {
    "precool": "thermlink",
    "thermostat_setback": "thermlink",
    "thermostat_restore": "thermlink",
    "ev_pause": "chargeflow",
    "battery_discharge": "powercell",
    "business_curtail_request": "textline",
    "send_sms": "textline",
    "feeder_outage": "gridops",
    "feeder_restore": "gridops",
}


class Doc(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------- planning

class Evidence(BaseModel):
    id: str  # fc_..., mem_..., msg_..., plan_..., or a ledger seq as "seq:42"
    kind: str  # forecast | memory | message | plan | policy | ledger | metric
    trust: Trust = "system"


class Selector(BaseModel):
    archetypes: list[str] = []
    sections: list[str] = []
    household_ids: list[str] = []
    business_ids: list[str] = []


class StepParams(BaseModel):
    offset_c: float | None = None
    amount_cad: float | None = None
    mw_target: float | None = None
    duration_min: int | None = None


class Window(BaseModel):
    start: str  # "HH:MM" simulated local time
    end: str


class PlanStep(BaseModel):
    step_id: str
    rung: int
    action_type: ActionType
    selector: Selector = Selector()
    params: StepParams = StepParams()
    window: Window
    expected_mw: float = 0
    evidence_ids: list[str] = []


class PlannerOutput(BaseModel):
    rationale: str = Field(max_length=600)
    expected_shortfall_mw: float = 0
    steps: list[PlanStep] = []


class SimilarEvent(BaseModel):
    id: str
    summary: str
    lesson: str = ""
    score: float = 0
    mw_by_action: dict[str, float] = {}


class Plan(Doc):
    id: str = Field(alias="_id")  # plan_...
    run_id: str
    version: int
    sim_ts: str
    created_by: str = "agent:planner"
    acting_for: str = "human:supervisor"
    source: Literal["llm", "cache", "fallback"]
    trigger: str = ""  # first_shortfall | watcher | hourly | contingency
    rationale: str
    expected_shortfall_mw: float = 0
    steps: list[PlanStep] = []
    evidence_ids: list[str] = []
    similar_events: list[SimilarEvent] = []
    langsmith_run_id: str | None = None


class HourlyPoint(BaseModel):
    hour: str  # "HH:00"
    demand_mw: float
    supply_mw: float
    shortfall_mw: float


class ShortfallWindow(BaseModel):
    start: str  # "HH:MM"
    end: str
    peak_short_mw: float


class Forecast(Doc):
    id: str = Field(alias="_id")  # fc_...
    run_id: str
    sim_ts: str
    hourly: list[HourlyPoint] = []
    windows: list[ShortfallWindow] = []
    explanation: str = ""
    source: Literal["llm", "cache", "fallback"] = "fallback"


# ---------------------------------------------------------------- governance

class Targets(BaseModel):
    household_ids: list[str] = []  # "hh_00042"; device batches (<= 2,000 per request)
    section_id: str | None = None  # feeder_outage / feeder_restore
    business_id: str | None = None  # business_curtail_request
    recipient_ids: list[str] = []  # send_sms: household or business ids


class OutboundMessage(BaseModel):
    to: str  # household id or business id
    body: str  # <= 320 chars, must end "Reply STOP to opt out"
    offer: dict[str, Any] | None = None  # {type, amount_cad, ask}


class ActionRequest(Doc):
    id: str = Field(alias="_id")  # ar_...; also the idempotency key
    run_id: str
    created_sim_ts: str
    sim_ts: str  # last status change
    action_type: ActionType
    system: System
    rung: int | None = None
    proposed_by: str  # ^agent:
    acting_for: str  # ^human:
    plan_id: str | None = None
    plan_version: int | None = None
    step_id: str | None = None
    summary: str  # "Pre-cool 5,200 homes in TOR-S04, TOR-S09 by -2.0 °C 14:30–16:30"
    targets: Targets = Targets()
    params: dict[str, Any] = {}  # offset_c, precool_offset_c, duration_min, kw, until, amount_cad
    window: Window | None = None
    messages: list[OutboundMessage] = []
    evidence: list[Evidence] = []
    expected_mw: float = 0
    status: RequestStatus = "proposed"
    idempotency_key: str
    undo_of: str | None = None  # execution id this request reverses
    decision_id: str | None = None
    approval_id: str | None = None
    execution_id: str | None = None
    langsmith_run_id: str | None = None


class RuleResult(BaseModel):
    rule_id: str  # "R01"
    name: str  # "SCOPE"
    applies: bool
    passed: bool
    effect: Literal["deny", "needs_approval", "none"] = "none"
    detail: str = ""
    counts: dict[str, int] = {}  # e.g. {"denied": 141}


class BlastRadius(BaseModel):
    homes_real: int = 0
    vulnerable_real: int = 0
    mw: float = 0


class Decision(Doc):
    id: str = Field(alias="_id")  # dec_...
    run_id: str
    request_id: str
    sim_ts: str
    verdict: Verdict
    rule_results: list[RuleResult]
    allowed_count: int = 0
    denied_count: int = 0
    denied_reasons: dict[str, int] = {}  # {"R04": 141, "R02": 45}
    allowed_household_ids: list[str] = []  # the executable subset for partial_allow
    blast_radius: BlastRadius = BlastRadius()
    policy_version: str
    decided_by: str = "system:gate"
    untrusted_influence: bool = False


class Approval(Doc):
    id: str = Field(alias="_id")  # apr_...
    run_id: str
    request_id: str
    decision_id: str
    status: Literal["pending", "approved", "denied", "expired"] = "pending"
    summary: str
    blast_radius: BlastRadius = BlastRadius()
    rule_results: list[RuleResult] = []
    untrusted_influence: bool = False
    requested_sim_ts: str
    expires_sim_ts: str
    decided_by: str | None = None
    decided_sim_ts: str | None = None
    note: str | None = None


class Verification(BaseModel):
    applied: int = 0
    pending: int = 0
    offline: int = 0
    overridden: int = 0
    failed: int = 0
    delivered_mw: float = 0


class UndoCommand(BaseModel):
    system: System
    method: Literal["POST"] = "POST"
    path: str  # "/mock/thermlink/v1/events/{batch_id}/end"
    body: dict[str, Any] = {}


class Execution(Doc):
    id: str = Field(alias="_id")  # exe_...
    run_id: str
    request_id: str
    executed_by: str = "agent:executor"
    system: System
    idempotency_key: str
    path: str
    body: dict[str, Any] = {}
    response: dict[str, Any] = {}
    pre_state: dict[str, Any] = {}
    undo: UndoCommand | None = None
    status: Literal["sent", "verified", "partial", "failed", "rolled_back"] = "sent"
    verification: Verification = Verification()
    attempts: int = 1
    sent_sim_ts: str
    sent_tick: int
    verified_sim_ts: str | None = None


class Message(Doc):
    id: str = Field(alias="_id")  # msg_...
    run_id: str
    thread_id: str  # the household or business id on the other side
    from_: str = Field(alias="from")  # "agent:negotiator" or "sim:resident:hh_00042"
    to: str
    body: str
    direction: Literal["out", "in"]
    trust: Trust
    sim_ts: str
    request_id: str | None = None
    offer: dict[str, Any] | None = None
    decision: str | None = None  # accept | decline | counter | none (replies)


class LedgerRefs(BaseModel):
    request_id: str | None = None
    decision_id: str | None = None
    approval_id: str | None = None
    execution_id: str | None = None
    plan_version: int | None = None
    message_id: str | None = None


class LedgerEntry(Doc):
    run_id: str
    seq: int
    sim_ts: str
    wall_ts: str
    type: LedgerType
    actor: str  # ^(agent|human|system|sim):
    acting_for: str | None = None
    summary: str
    refs: LedgerRefs = LedgerRefs()
    payload: dict[str, Any] = {}
    payload_digest: str
    prev_hash: str
    hash: str
    langsmith_run_id: str | None = None


class Scopes(BaseModel):
    propose: list[str] = []
    execute: list[str] = []
    read: list[str] = []


class Identity(Doc):
    id: str = Field(alias="_id")  # agent:planner, human:supervisor, system:gate
    kind: Literal["agent", "human", "system"]
    display_name: str
    acting_for: str | None = None
    scopes: Scopes = Scopes()
    status: Literal["active", "suspended", "revoked"] = "active"
    on_shift: bool = True
    expires_at: str | None = None


class Policy(Doc):
    id: str = Field(alias="_id")  # "R01"
    version: str  # policy-set version, e.g. "2031.06.1"
    name: str  # "SCOPE"
    description: str
    params: dict[str, Any] = {}
    applies_to: list[str] = []  # action types, or ["*"]
    effect: Literal["deny", "needs_approval"]
    active: bool = True


class AgentControl(Doc):
    id: str = Field(alias="_id")  # run_id
    halted: bool = False
    scope: str = "all"  # "all" or an agent id
    by: str | None = None
    reason: str | None = None
    sim_ts: str | None = None


class Run(Doc):
    id: str = Field(alias="_id")  # run_...
    scenario_id: str
    mode: Literal["governed", "baseline"]
    seed: int
    status: Literal["created", "running", "paused", "finished", "failed"] = "created"
    sim_ts: str
    speed: float = 1.0
    scale: float = 1.0  # real homes per simulated home
    households: int
    baseline_run_id: str | None = None
    policy_version: str | None = None
    created_wall_ts: str


# ---------------------------------------------------------------- LLM outputs (world + agents)

class ArchetypeOutput(BaseModel):
    override_rate_per_hour: float = Field(ge=0, le=0.8)
    offer_acceptance: float = Field(ge=0, le=1)
    ev_pause_acceptance: float = Field(ge=0, le=1)
    mood: str = ""
    one_line: str = ""


class ResidentReply(BaseModel):
    reply_text: str = Field(max_length=240)
    decision: Literal["accept", "decline", "counter", "none"]
    counter_amount_cad: float | None = None
    override_thermostat: bool = False


class BusinessReply(BaseModel):
    accept: bool
    mw_offered: float = 0
    start: str | None = None  # "HH:MM"
    end: str | None = None
    conditions: str = ""
    reply_text: str = ""


class NegotiatorMessage(BaseModel):
    recipient_id: str
    body: str = Field(max_length=320)
    offer: dict[str, Any] = {}  # {type, amount_cad, ask}


class TimelineItem(BaseModel):
    seq: int
    sim_ts: str
    actor: str
    summary: str


class AuditAnswer(BaseModel):
    answer: str
    timeline: list[TimelineItem] = []
    rules_cited: list[str] = []


class ForecastExplanation(BaseModel):
    explanation: str = Field(max_length=200)
