"""World LLM agents (PLAN §11): the simulated environment, not governed agents.

- archetype.react(world, event): group behaviour parameters for sim/behavior.py.
- resident.on_message(world, message) / on_home_event / check_scripted / inject_scripted.
- business.on_request(world, request): curtailment replies.

Every reply is stored in `messages` with trust "untrusted" and logged as ledger `message_in`.
"""

from . import archetype, business, resident

__all__ = ["archetype", "business", "resident"]
