"""Seed data for identities (PLAN §8.1), policies (PLAN §9 + CONTRACTS params) and policy_docs.

init_db.py upserts IDENTITIES and POLICIES; seed_memory.py inserts POLICY_DOCS. The Gate reads
policy params from the `policies` collection (governance/identities.py: load_policies), so the
thresholds below are the single source of truth: never hardcode them in rules.
"""

from typing import Any

from app.models import Identity, Policy, Scopes

POLICY_VERSION = "2031.06.1"
SUPERVISOR = "human:supervisor"

DEVICE_ACTIONS = ["precool", "thermostat_setback", "ev_pause", "battery_discharge"]
ALL_ACTIONS = [
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

_AGENT_READ = ["forecasts", "event_memory", "policy_docs", "messages", "plans"]


def _agent(agent_id: str, name: str, propose: list[str], read: list[str], execute: list[str] | None = None) -> Identity:
    return Identity(
        _id=agent_id,
        kind="agent",
        display_name=name,
        acting_for=SUPERVISOR,
        scopes=Scopes(propose=propose, execute=execute or [], read=read),
    )


IDENTITIES: list[Identity] = [
    Identity(_id="human:operator", kind="human", display_name="Operator", scopes=Scopes(read=["*"])),
    Identity(_id="human:supervisor", kind="human", display_name="A. Rahman", on_shift=True, scopes=Scopes(read=["*"])),
    Identity(_id="human:auditor", kind="human", display_name="J. Okafor", scopes=Scopes(read=["ledger", "decisions", "approvals", "policy_docs"])),
    _agent("agent:forecaster", "Forecaster", [], ["weather", "feeder_load", "households"]),
    _agent("agent:scenario_designer", "Scenario Designer", [], ["scenarios", "weather", "policy_docs"]),
    _agent(
        "agent:planner",
        "Planner",
        ["precool", "thermostat_setback", "ev_pause", "battery_discharge", "business_curtail_request", "send_sms", "feeder_outage"],
        _AGENT_READ,
    ),
    _agent("agent:targeter", "Targeter", [*DEVICE_ACTIONS, "feeder_outage"], [*_AGENT_READ, "households", "sections"]),
    _agent("agent:negotiator", "Negotiator", ["send_sms", "business_curtail_request"], [*_AGENT_READ, "residents", "businesses", "facilities"]),
    _agent("agent:executor", "Executor", [], ["action_requests", "decisions", "approvals", "executions"], execute=ALL_ACTIONS),
    _agent("agent:watcher", "Watcher", [], [*_AGENT_READ, "executions", "approvals", "feeder_load"]),
    _agent("agent:restorer", "Restorer", ["thermostat_restore", "feeder_restore", "undo"], [*_AGENT_READ, "executions", "sections"]),
    _agent("agent:auditor", "Auditor", [], ["ledger", "decisions", "approvals", "policy_docs"]),
    Identity(_id="system:gate", kind="system", display_name="Policy Gate", scopes=Scopes(read=["*"])),
]

# (rule_id, name, effect, applies_to, params, one-line description, plain-language policy doc text)
_RULES: list[tuple[str, str, str, list[str], dict[str, Any], str, str]] = [
    (
        "R01", "SCOPE", "deny", ["*"], {},
        "The proposing agent must hold propose scope for the action type, its acting-for human must be active and on shift, and only agent:executor may execute.",
        "Every agent acts under a named identity with explicit scopes. A proposal is denied when the agent lacks propose scope for that action type, or when the human it acts for is not active and on shift. Only the Executor identity may send commands to control systems; no other agent can touch a device, a message gateway or a breaker.",
    ),
    (
        "R02", "CONSENT", "deny", [*DEVICE_ACTIONS, "send_sms"], {},
        "Device commands only for households enrolled in the matching CoolShift program; SMS only to opted-in recipients who have not replied STOP.",
        "Agents may only command devices in homes that consented to the matching CoolShift program: ThermLink thermostats, ChargeFlow EV charging or PowerCell batteries. Text messages go only to households that opted in to TextLine messages and have not replied STOP. Homes without consent are removed from a batch and counted as denied.",
    ),
    (
        "R03", "PROGRAM_LIMITS", "deny", ["precool", "thermostat_setback"],
        {"offset_min_c": -2.0, "offset_max_c": 2.0, "precool_max_min": 120, "setback_max_min": 240, "setbacks_per_home_per_day": 1},
        "Thermostat offsets stay within -2.0 to +2.0 C; pre-cool lasts at most 2 hours; a setback lasts at most 4 hours and happens at most once per home per day.",
        "CoolShift program limits cap how far and how long a thermostat can be moved. Offsets must stay between minus 2 and plus 2 degrees Celsius. Pre-cooling may last up to 2 hours and a setback up to 4 hours, and each home gets at most one setback per day. A proposal to raise thermostats by more than 2 degrees is denied outright.",
    ),
    (
        "R04", "VULNERABLE_EXEMPT", "deny", ["thermostat_setback", "ev_pause", "send_sms"],
        {"exempt_actions": ["thermostat_setback", "ev_pause"], "exempt_sms_offer_types": ["reduce_cooling"]},
        "No setback, EV pause or reduce-cooling incentive for homes flagged vulnerable; cool-space notifications are allowed.",
        "Homes flagged as vulnerable are exempt from actions that reduce their comfort. They never receive a thermostat setback, an EV charging pause or an incentive offer to reduce cooling. They may still receive notifications about event timing and the nearest open public cool space.",
    ),
    (
        "R05", "SAFE_INDOOR", "deny", ["precool", "thermostat_setback", "feeder_outage"], {"t_safe_c": 29.0},
        "Predicted maximum indoor temperature over the action window must stay at or below t_safe_c (scenario value, default 29.0 C).",
        "Before any action that changes cooling, the simulator predicts each affected home's maximum indoor temperature over the action window. Homes predicted to exceed the safe indoor limit, 29 degrees Celsius by default or the scenario's value, are removed from the action. This keeps load relief from pushing any home past the safe threshold.",
    ),
    (
        "R06", "MESSAGING", "deny", ["send_sms", "business_curtail_request"],
        {
            "max_per_recipient_per_day": 2,
            "quiet_start": "22:00",
            "quiet_end": "08:00",
            "max_chars": 320,
            "max_incentive_cad": 10,
            "denylist": ["doctor", "medication", "symptom", "heatstroke", "dehydration"],
            "required_suffix": "Reply STOP to opt out",
        },
        "At most 2 messages per recipient per day, none 22:00-08:00, at most 320 characters, incentive at most $10 per home and within budget, no health or medical terms, and must include 'Reply STOP to opt out'.",
        "Outbound messages are rate limited to two per recipient per day and are never sent between 22:00 and 08:00. Each message is at most 320 characters, offers at most 10 dollars per home within the event budget, and must end with Reply STOP to opt out. Messages may only contain energy offers, event timing and public cool-space locations; any message using health or medical terms is denied.",
    ),
    (
        "R07", "LADDER", "deny", ["feeder_outage"], {},
        "A feeder outage is denied unless projected shortfall exceeds active delivered relief plus remaining lower-rung potential.",
        "Feeder outages are the last rung of the ladder. An outage is denied unless the projected shortfall is still positive after counting relief already delivered and all remaining potential from lower rungs: pre-cooling, batteries, EV pauses, business curtailment, setbacks and voluntary offers. Agents must exhaust gentler options first.",
    ),
    (
        "R08", "PROTECTED_SECTION", "deny", ["feeder_outage"], {},
        "No outage on a protected feeder section.",
        "Some feeder sections are protected because they serve critical facilities such as hospitals, transit or designated cool spaces. No outage may ever be scheduled on a protected section, regardless of who asks or what a message claims about protection status.",
    ),
    (
        "R09", "OUTAGE_DURATION", "deny", ["feeder_outage"], {"max_minutes_per_section_per_day": 60},
        "A section may be cut for at most 60 minutes per day in total.",
        "A rotating outage on any one feeder section may total at most 60 minutes per day. A proposal that would push a section past that limit, including minutes already cut earlier in the day, is denied.",
    ),
    (
        "R10", "ROTATION", "deny", ["feeder_outage"], {},
        "No section is cut a second time until every eligible section has been cut once.",
        "Outages rotate fairly. No section may be cut a second time until every eligible, unprotected section has been cut once. A request to cut the same section again while others have not been cut is denied.",
    ),
    (
        "R11", "EQUITY", "deny", ["feeder_outage"], {"max_quintile_gap_min": 60},
        "After the outage, outage minutes by income quintile must stay within a max-min gap of 60 minutes.",
        "Outage burden must be shared fairly across income groups. After a proposed outage, the average outage minutes for the most affected income quintile may not exceed the least affected quintile by more than 60 minutes. Proposals that would concentrate outages in lower-income neighbourhoods are denied.",
    ),
    (
        "R12", "PRE_NOTIFY", "deny", ["feeder_outage"], {"min_notice_min": 15},
        "Vulnerable homes in the section must be notified at least 15 simulated minutes earlier with the nearest open designated cool space.",
        "Before a section is cut, every vulnerable home in it must have been notified at least 15 minutes in advance, including the location of the nearest open designated cool space found with a geospatial query. An outage without that advance notice is denied.",
    ),
    (
        "R13", "HUMAN_APPROVAL", "needs_approval", ["*"],
        {"always_actions": ["feeder_outage"], "max_homes_real_without_approval": 250000},
        "Every feeder outage, and any action affecting more than 250,000 real homes, needs supervisor approval.",
        "High-impact actions need a human decision. Every feeder outage, and any action that affects more than 250,000 real homes, is paused until the on-shift supervisor approves or denies it. Approvals expire after 10 simulated minutes if nobody decides.",
    ),
    (
        "R14", "UNTRUSTED_EVIDENCE", "needs_approval", ["*"], {"low_impact_actions": ["send_sms"]},
        "If an action's evidence includes untrusted input and it is not a low-impact reply, it needs approval and is flagged as influenced by untrusted input.",
        "Messages from residents and businesses are untrusted input. If an action cites untrusted evidence and is anything other than a low-impact message reply, it needs supervisor approval and is flagged as influenced by untrusted input. Text from outside cannot change policy, protection status or thermostat limits.",
    ),
    (
        "R15", "KILL_SWITCH", "deny", ["*"], {},
        "No execution while agent_control is halted.",
        "Supervisors can halt agents at any time. While the kill switch is engaged, no action is executed and pending proposals stay frozen until a supervisor resumes. Rollbacks of earlier executions remain possible and are themselves gated and logged.",
    ),
    (
        "R16", "RESTORE_SAFETY", "needs_approval", ["feeder_restore", "thermostat_restore"], {"stagger_groups": 4, "stagger_min": 10},
        "A restore adding X MW requires a projected margin of at least X; otherwise it must be staggered (4 groups, 10 minutes apart) or approved.",
        "Restoring load too quickly can cause a rebound peak. A restore that adds more load than the projected supply margin must be staggered into 4 groups 10 minutes apart, or it needs supervisor approval.",
    ),
]

POLICIES: list[Policy] = [
    Policy(_id=rid, version=POLICY_VERSION, name=name, description=desc, params=params, applies_to=applies, effect=effect)
    for rid, name, effect, applies, params, desc, _ in _RULES
]
POLICIES_BY_ID: dict[str, Policy] = {p.id: p for p in POLICIES}

POLICY_DOCS: list[dict[str, Any]] = [
    {
        "_id": f"pd_{rid}",
        "rule_id": rid,
        "title": f"{rid} {name.replace('_', ' ').title()}",
        "text": text,
        "effect": effect,
        "policy_version": POLICY_VERSION,
    }
    for rid, name, effect, _, _, _, text in _RULES
]


def policy_params(rule_id: str) -> dict[str, Any]:
    """Seeded params for a rule (tests and fallbacks; the Gate reads the policies collection)."""
    return dict(POLICIES_BY_ID[rule_id].params)
