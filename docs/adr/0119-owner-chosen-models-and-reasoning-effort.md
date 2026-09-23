# ADR-0119: Owner-chosen models and reasoning effort

- Status: Accepted (authorized by the repository owner, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0012, ADR-0086, ADR-0087, ADR-0093, ADR-0101, ADR-0117;
  Sections 10 and 22 of the engineering plan
- Detailed design: `docs/plan/model-gateway.md`,
  `docs/plan/http-api-and-streaming.md`,
  `docs/plan/adaptive-memory-distillation.md`,
  `docs/plan/bootstrap-and-composition.md`

## Context

Every model request left reasoning effort to the provider's default. Chat ran
GPT-6 Astra at whatever OpenAI chose, and memory formation stayed on GPT-5.6
Sol because Astra, evaluated at its default effort, failed the formation@9
gates on 2026-09-11 (ADR-0093). The owner asked for chat at high effort,
for memory formation on Astra at medium effort once it passes the same
evaluation, and for all four choices to be adjustable from the client's
settings.

The plan's `ModelRequest` (Section 10.1) has no effort field. Chat models are
fixed per deployment, and memory formation's model is fixed at startup by
`formation.model_policy` and its evidence.

## Decisions

1. **Effort is a request field.** `ModelRequest.reasoning_effort` takes `low`,
   `medium`, `high`, `xhigh`, or `max`; null sends nothing and keeps the
   provider default. The OpenAI adapter sends `reasoning.effort`, the
   Anthropic adapter `output_config.effort`; neither sends it to a model
   without native reasoning, and the chat-completions adapter never does.
   Each provider-profile model lists the efforts it accepts, and a list on a
   profile without native reasoning is refused at load.
2. **The owner's settings are one versioned document.** `model_settings`
   stores, per principal, the chat model and effort and the memory model and
   effort, append-only under an expected-version precondition.
   `GET /v1/settings/models` returns what is in effect and every choice on
   offer; `PUT /v1/settings/models` saves. Two exact scopes, `settings.read`
   and `settings.write`, guard the routes. A save naming a choice not on
   offer is `malformed_request`; a stale version is `conflict`; saving the
   stored values again succeeds without a new version, so a retry is safe.
   Each save appends a content-free `settings.models.updated` event.
3. **What is on offer.** Chat offers the policies listed in
   `models/policies.yaml` under `selectable_chat_policies` whose provider
   holds a credential, and always the deployment default; a non-routed
   development composition offers only its own deterministic policy. A chat
   choice must name one of an offered model's efforts. Memory offers the
   tuple the composition selected and, on the People default path, every
   other (model policy, effort) tuple with a bundled formation@9 artifact
   that matches this tree exactly. A stored choice that stops being offered
   is ignored in favour of the default, never an error.
4. **When a choice applies.** A new app chat starts on the chosen chat model:
   the session binds a variant of the default agent that differs only in
   `model_policy`, under an id derived from the agent and the policy so it
   never becomes the deployed agent's latest version. Existing sessions,
   Telegram, WhatsApp, SMS, schedules, and email keep the deployment default.
   The chat effort applies to every agent run from its next message, when
   that run's model accepts it; typed email tasks keep their provider
   default. Memory formation reads the owner's memory choice at each
   extraction; People imports keep the configured tuple.
5. **The chat default effort is high.** With nothing saved, chat sends `high`
   to a model that accepts it.
6. **Evidence binds effort.** `MemoryDistillationEvidence` schema 8 records
   the effort the evaluation sent; schema 7 artifacts are provider-default
   evidence. Activation and the offered memory tuples compare effort as an
   eighth field, and the bundle test's uniqueness key includes it.
   `formation.reasoning_effort` in `memory/profiles.yaml` sets the default
   tuple's effort, and `agent eval memory-distillation --reasoning-effort`
   evaluates one. The formation@8 and formation@10 artifacts record no
   effort, so they match only a null effort.
7. **Astra becomes the memory default only on passing evidence.** The
   three-repeat formation@9 evaluation runs at Astra/medium on this tree.
   If it passes, its artifact is bundled and `formation.model_policy: astra`
   with `formation.reasoning_effort: medium` becomes the default, with Sol
   still offered through its own artifact. If it fails, Sol stays the default
   and only Sol is offered.
   **Outcome, 2026-09-23:** the evaluation at `35958ef` failed development
   precision (0.872), the rich conversation core, and holdout direct recall
   (0.892) and precision (0.700), at a recorded USD 10.24. No artifact was
   published; Sol stays the default and the only memory choice. The same
   evaluation at another effort, or after formation work, can add a choice
   without code changes.

## Consequences

- The owner principal must be granted `settings.read` and `settings.write`
  in `AUTH_SCOPES`; without them the client shows the settings as not
  authorized and nothing else changes.
- Every provider profile and the policy document change, so every
  `registry_version` changes; runs pinned to the previous registry must
  drain before deployment, as for any registry change.
- Higher effort costs more tokens per run. Run budgets are unchanged and
  still bound every run.
- A formation that ran on the owner's alternative records the delegate's
  name with the chosen policy and effort, so consolidation audits show which
  model formed it.
- No Python gate observes the Swift work; Swift model, view-model, transport,
  and fixture tests are its evidence under ADR-0049.

## Alternatives considered

- **A policy-level default effort in `policies.yaml`:** rejected because it
  would also raise effort for email, folders, and every other caller of the
  policy, where the owner asked only for chat.
- **Recording the chat model in session metadata:** rejected because the
  agent version, not metadata, defines a session's behaviour and pins.
- **Offering any memory tuple and falling back silently:** rejected by the
  owner; only evaluated tuples are selectable.
