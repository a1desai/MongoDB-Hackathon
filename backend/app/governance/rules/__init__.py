"""The 16 Policy Gate rules (PLAN §9), one module each, in rule-id order.

Each module exposes RULE_ID, NAME, EFFECT, `evaluate(request, ctx, mask) -> (RuleResult, denied_mask)`
and `check(request, ctx, mask=None) -> RuleResult`. Thresholds come from policy params, never literals.
"""

from types import ModuleType

from . import (
    r01_scope,
    r02_consent,
    r03_program_limits,
    r04_vulnerable,
    r05_safe_indoor,
    r06_messaging,
    r07_ladder,
    r08_protected,
    r09_outage_duration,
    r10_rotation,
    r11_equity,
    r12_prenotify,
    r13_approval,
    r14_untrusted,
    r15_kill_switch,
    r16_restore,
)

ALL_RULES: list[ModuleType] = [
    r01_scope,
    r02_consent,
    r03_program_limits,
    r04_vulnerable,
    r05_safe_indoor,
    r06_messaging,
    r07_ladder,
    r08_protected,
    r09_outage_duration,
    r10_rotation,
    r11_equity,
    r12_prenotify,
    r13_approval,
    r14_untrusted,
    r15_kill_switch,
    r16_restore,
]
RULES_BY_ID: dict[str, ModuleType] = {m.RULE_ID: m for m in ALL_RULES}
# Evaluation order for deny rules: the kill switch first (an absolute stop is the headline reason),
# then R01..R12. Decisions still list rule_results in rule-id order.
DENY_RULES: list[ModuleType] = [r15_kill_switch] + [m for m in ALL_RULES if m.EFFECT == "deny" and m is not r15_kill_switch]
APPROVAL_RULES: list[ModuleType] = [m for m in ALL_RULES if m.EFFECT == "needs_approval"]

__all__ = ["ALL_RULES", "APPROVAL_RULES", "DENY_RULES", "RULES_BY_ID"]
