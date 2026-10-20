"""What the persona cannot change, stated only where something enforces it (ADR-0226 part D).

Each boundary names the fact that enforces it: a hardline rule, a policy
profile rule, a setting, or a code path. A claim whose fact is missing from
the loaded ruleset or composition is left out rather than softened, and a
gate fails the build when a shipped claim loses its fact. Text that depends
on configuration is chosen from the live settings, so the page states actual
behaviour, including the cases where Veetbot acts without asking.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

from agent_core.config import Settings
from agent_core.domain.persona import BoundaryEnforcement, PersonaBoundary, PersonaBoundaryFact
from agent_core.domain.policies import (
    LoadedRuleset,
    PolicyDecisionType,
    SideEffectClass,
)


@dataclass(frozen=True, slots=True)
class _Claim:
    id: str
    fact: PersonaBoundaryFact
    holds: Callable[[LoadedRuleset, Settings], bool]
    text: Callable[[Settings], str]
    active: Callable[[Settings], bool] = lambda _settings: True


def _hardline(rule_id: str, *classes: SideEffectClass) -> Callable[[LoadedRuleset, Settings], bool]:
    def holds(ruleset: LoadedRuleset, _settings: Settings) -> bool:
        return any(
            rule.id == rule_id and set(classes) <= set(rule.applies_to) for rule in ruleset.hardline
        )

    return holds


def _decision(
    side_effect: SideEffectClass, decision: PolicyDecisionType
) -> Callable[[LoadedRuleset, Settings], bool]:
    def holds(ruleset: LoadedRuleset, _settings: Settings) -> bool:
        return any(
            rule.side_effect is side_effect and rule.decision is decision and rule.condition is None
            for rule in ruleset.rules
        )

    return holds


def _setting(name: str) -> Callable[[LoadedRuleset, Settings], bool]:
    def holds(_ruleset: LoadedRuleset, settings: Settings) -> bool:
        return hasattr(settings, name)

    return holds


def _code(dotted: str) -> Callable[[LoadedRuleset, Settings], bool]:
    module_name, _, attribute = dotted.rpartition(".")

    def holds(_ruleset: LoadedRuleset, _settings: Settings) -> bool:
        try:
            return hasattr(importlib.import_module(module_name), attribute)
        except ImportError:
            return False

    return holds


def _messages(settings: Settings) -> str:
    text = (
        "Emails and messages ask you before they go. "
        "On iPhone, the compose sheet is where you confirm a text."
    )
    if settings.call_enabled and settings.call_owner_requests_enabled:
        text += " A call you ask for in Chat dials without a second confirmation."
    return text


_REQUIRE = PolicyDecisionType.REQUIRE_APPROVAL

CATALOG: tuple[_Claim, ...] = (
    _Claim(
        id="credentials",
        fact=PersonaBoundaryFact(kind=BoundaryEnforcement.HARDLINE_RULE, ref="credential_class"),
        holds=_hardline("credential_class", SideEffectClass.CREDENTIAL_ACCESS),
        text=lambda _s: "Veetbot never reads your passwords, keys or other credentials.",
    ),
    _Claim(
        id="secrets_stay_in",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.HARDLINE_RULE, ref="credential_to_egress"
        ),
        holds=_hardline(
            "credential_to_egress",
            SideEffectClass.EXTERNAL_MESSAGE,
            SideEffectClass.EXTERNAL_WRITE,
            SideEffectClass.PUBLICATION,
        ),
        text=lambda _s: (
            "Anything that looks like a password or key is never sent, written or published."
        ),
    ),
    _Claim(
        id="messages_ask",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.POLICY_RULE, ref="rules.external_message=require_approval"
        ),
        holds=_decision(SideEffectClass.EXTERNAL_MESSAGE, _REQUIRE),
        text=_messages,
    ),
    _Claim(
        id="deletes_ask",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.POLICY_RULE, ref="rules.external_delete=require_approval"
        ),
        holds=_decision(SideEffectClass.EXTERNAL_DELETE, _REQUIRE),
        text=lambda _s: "Deleting anything outside Veetbot asks you first.",
    ),
    _Claim(
        id="payments_ask",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.POLICY_RULE, ref="rules.financial=require_approval"
        ),
        holds=_decision(SideEffectClass.FINANCIAL, _REQUIRE),
        text=lambda _s: "Payments ask you first.",
    ),
    _Claim(
        id="publishing_asks",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.POLICY_RULE, ref="rules.publication=require_approval"
        ),
        holds=_decision(SideEffectClass.PUBLICATION, _REQUIRE),
        text=lambda _s: "Posting anything publicly asks you first.",
    ),
    _Claim(
        id="untrusted_content",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.POLICY_RULE,
            ref="trust_overlay.external_untrusted_requires_approval",
        ),
        holds=lambda ruleset, _s: ruleset.external_untrusted_requires_approval,
        text=lambda _s: (
            "Instructions found in emails, web pages and files never act on their own; "
            "acting on them asks you first."
        ),
    ),
    _Claim(
        id="unknown_tools",
        fact=PersonaBoundaryFact(kind=BoundaryEnforcement.POLICY_RULE, ref="unknown_tool=deny"),
        holds=lambda ruleset, _s: ruleset.unknown_tool_decision is PolicyDecisionType.DENY,
        text=lambda _s: "A tool Veetbot does not recognise is never run.",
    ),
    _Claim(
        id="website_task_grants",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.CONFIGURATION, ref="browser_task_grants_enabled"
        ),
        holds=_setting("browser_task_grants_enabled"),
        text=lambda _s: (
            "When you let Veetbot work on a website for a set time, it acts on that site "
            "without asking again until the time is up."
        ),
        active=lambda settings: settings.browser_task_grants_enabled,
    ),
    _Claim(
        id="tone_only",
        fact=PersonaBoundaryFact(
            kind=BoundaryEnforcement.CODE_PATH, ref="agent_core.domain.persona.PERSONA_FRAMING"
        ),
        holds=_code("agent_core.domain.persona.PERSONA_FRAMING"),
        text=lambda _s: (
            "Your voice and standing facts change how Veetbot talks, never what it may do."
        ),
    ),
)


def persona_boundaries(ruleset: LoadedRuleset, settings: Settings) -> tuple[PersonaBoundary, ...]:
    """The catalog as it applies to this composition; unenforced claims are left out."""
    return tuple(
        PersonaBoundary(
            id=claim.id,
            text=claim.text(settings),
            enforced_by=claim.fact,
            active=claim.active(settings),
        )
        for claim in CATALOG
        if claim.holds(ruleset, settings)
    )


def unenforced_claims(ruleset: LoadedRuleset, settings: Settings) -> list[str]:
    """Catalog entries whose fact is missing; the gate requires this to be empty."""
    return [claim.id for claim in CATALOG if not claim.holds(ruleset, settings)]
