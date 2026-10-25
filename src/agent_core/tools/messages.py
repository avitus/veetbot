"""Fixed model-facing messages keyed only by stable reason code."""

from __future__ import annotations

TOOL_MESSAGES: dict[str, str] = {
    "tool.succeeded": "The tool completed successfully.",
    "tool.invalid_arguments.syntax": "The expression could not be parsed.",
    "tool.invalid_arguments.unknown_name": (
        "Unknown function or constant. Functions: abs, ceil, exp, floor, ln, "
        "log10, max, min, round, sqrt. Constants: pi, e."
    ),
    "tool.invalid_arguments.arity": "Wrong number of arguments for that function.",
    "tool.invalid_arguments.domain": "An argument is outside the function's domain.",
    "tool.invalid_arguments.division_by_zero": "Division by zero.",
    "tool.invalid_arguments.result_out_of_range": (
        "The result is too large or too small to represent."
    ),
    "tool.invalid_arguments.expression_too_long": (
        "The expression is longer than 1024 characters."
    ),
    "tool.invalid_arguments.expression_too_deep": (
        "The expression is nested more than 32 levels deep."
    ),
    "tool.invalid_arguments.unknown_timezone": (
        "Unknown timezone. Provide an IANA name such as UTC, America/New_York, or Europe/London."
    ),
    "tool.invalid_arguments.portability_ceiling": (
        "Portable memory is valid only for a preference, user_model_attr, or "
        "procedure_pointer. Use contextual or local portability for a fact or relationship."
    ),
    "tool.arguments_invalid": "The tool arguments were invalid.",
    "tool.skill.not_in_catalog": (
        "That skill is not available in this session. Choose only a name from the attached "
        "available-skill data."
    ),
    "tool.trust_rejected": (
        "The tool rejected content whose provenance was not trusted for this operation."
    ),
    "tool.memory.statement_not_owner_grounded": (
        "This save was refused because the statement carries words the owner did not say in "
        "this turn (web, document or other external content). Save only what the owner stated, "
        "in their words; derived plans and lookups belong in the reply or an artifact. Do not "
        "report this refusal unless the owner asked you to remember something."
    ),
    "tool.memory.manual_trust_rejected": (
        "This manual save failed because its provenance was not trusted. Background formation "
        "of owner-stated facts is separate and may run after the reply; its outcome is not yet "
        "known. Do not claim that no memories were formed. For owner-requested website learning, "
        "use memory.learn with exact source evidence when available."
    ),
    "tool.output_invalid": "The tool returned data outside its declared contract.",
    "tool.timeout": "The tool did not finish within its allowed time.",
    "tool.internal_error": "The tool could not complete because of an internal error.",
    "tool.outcome_unknown": "The outcome of this call is unknown. Do not repeat it.",
    "tool.server_unreachable": "The tool server is unavailable. Continue without this call.",
    "tool.server_unauthorized": "The tool server rejected its credential and is unavailable.",
    "tool.server_error": "The tool server reported that the call failed.",
    "tool.auth_failed": "The tool server rejected its configured credential.",
    "tool.auth_unsupported": "The tool server requires an unsupported authorization flow.",
    "tool.web.auth_failed": "The configured web provider rejected its credential.",
    "tool.web.provider_unavailable": "The configured web provider is temporarily unavailable.",
    "tool.web.quota_exceeded": (
        "The web provider's usage or billing limit has been reached; "
        "an operator must restore provider capacity."
    ),
    "tool.web.provider_rejected": (
        "The selected web provider rejected this request; the target or requested capability "
        "may be unsupported or blocked."
    ),
    "tool.web.output_invalid": "The web provider returned data outside its declared contract.",
    "tool.web.url_disallowed": "Only public HTTPS page URLs may be fetched.",
    "tool.media.permission": (
        "The media provider rejected its credential or model access. "
        "Check the configured key and model scopes."
    ),
    "tool.media.rejected": "The media provider rejected the generation request.",
    "tool.media.reference_unavailable": (
        "A reference image is unavailable. Use a sent attachment or generated image "
        "from this chat. No generation request was sent."
    ),
    "tool.media.reference_invalid": (
        "Reference images must contain PNG, JPEG or WebP bytes matching their recorded type. "
        "No generation request was sent."
    ),
    "tool.media.reference_too_large": (
        "Reference images exceed the count or byte limits in the tool definition. "
        "No generation request was sent."
    ),
    "tool.media.timeout": (
        "Media generation timed out and may have incurred a charge. "
        "Ask the user before trying again."
    ),
    "tool.media.transport": (
        "The media connection failed and may have incurred a charge. "
        "Ask the user before trying again."
    ),
    "tool.media.upstream": (
        "The media provider is unavailable or out of quota. Ask the user before trying again."
    ),
    "tool.media.invalid": (
        "The media provider returned invalid or incomplete media. "
        "A charge may have occurred; ask the user before trying again."
    ),
    "tool.media.too_large": (
        "Generated media exceeded the file size limit. "
        "A charge may have occurred; ask the user before trying again."
    ),
    "tool.browser.region_not_actionable": (
        "No action was sent. regions[].ref identifies a read-only region, not a control. "
        "For every action, including scroll, choose elements[].ref from the current observation. "
        "Use browser.observe with region_ref and expected_revision to read a region."
    ),
    "tool.browser.focus_required": (
        "No read was dispatched. Open the current region with region_ref and expected_revision, "
        "omitting text_offset or setting it to zero. Then copy the returned next_text request "
        "exactly to continue; do not guess offsets or reuse them after another operation."
    ),
    "tool.browser.element_not_found": (
        "The target control is missing, disabled or covered. Use the latest observation's "
        "elements[].ref, never regions[].ref, for actions including scroll. "
        "Inspect fresh evidence before proposing another action; do not substitute targets blindly."
    ),
    "tool.browser.page_changed": (
        "The observation or continuation is no longer valid. Use the latest fresh observation. "
        "Start a region read with region_ref and expected_revision without text_offset; "
        "continue only with its returned next_text. Never replay an uncertain action."
    ),
    "tool.browser.navigation_required": (
        "The browser has no open website page. Use browser.navigate with the intended allowed "
        "website URL, then use its fresh observation. Do not ask the user to sign in again "
        "unless the website then requires authentication. Never replay an uncertain action."
    ),
    "tool.browser.outcome_unknown": (
        "The action may already have happened. Do not repeat it. "
        "Use read-only evidence to reconcile the result, or report the uncertainty."
    ),
    # ADR-0129: names no grant, so the model cannot learn that one exists.
    "tool.browser.grant_not_applicable": (
        "Not performed. The page changed since it was observed; observe it again."
    ),
    "policy.scope.missing": "Not performed. The principal lacks a required scope.",
    "policy.milestone1.non_pure": (
        "Not performed. This milestone authorizes only side-effect-free tools."
    ),
    "policy.matrix.unknown_tool": "Not performed. The requested capability is unknown.",
    "policy.matrix.external_write": "Not performed. Approval is required.",
    "policy.matrix.workspace_read": "Not performed. The path is outside the workspace.",
    "policy.tool.device.sms.send": "Not performed. Approval is required.",
    "policy.unclassifiable_action": "Not performed. The action could not be classified.",
    "policy.advisory.escalated": "Not performed. Approval is required.",
    "policy.revalidation.changed": "Not performed. The approved action changed.",
    "policy.revalidation.escalated": "Not performed. Policy changed before execution.",
    "approval.denied": "Not performed. Approval was required and was denied.",
    "approval.expired": "Not performed. The approval expired.",
    "approval.cancelled": "Not performed. The approval was cancelled.",
    "tool.not_found.no_such_path": "No such path in the workspace.",
    "tool.not_found.not_offered": (
        "Not performed. No tool by that name is offered in this conversation."
    ),
    "tool.invalid_arguments.not_text": "Not a UTF-8 text file. This tool reads text only.",
    "tool.invalid_arguments.not_a_file": "That path is a directory.",
    "tool.invalid_arguments.not_a_directory": ("That path is a file. Use workspace.read_text."),
    "delegation.brief_invalid": "Not performed. A delegation brief failed validation.",
    "delegation.tools_not_subset": (
        "Not performed. A brief requested tools outside this run's advertised set."
    ),
    "delegation.depth_exceeded": "Not performed. A delegated child cannot delegate further.",
    "delegation.fanout_exceeded": "Not performed. The request exceeds a delegation fan-out cap.",
    "delegation.tenant_cap": (
        "Not performed. The tenant's live delegated-run cap would be exceeded."
    ),
    "delegation.budget_insufficient": (
        "Not performed. This run has no remaining budget to delegate."
    ),
    "delegation.child_failed": (
        "A delegated child run failed or was cancelled; its reason is in the result."
    ),
    "tool.device_offline": (
        "The target device is offline or unreachable; the action was not performed."
    ),
    "tool.device_send_failed": "The device reported that the message was not sent.",
    "tool.unavailable": "The tool is unavailable in this deployment.",
    "tool.run_cancelled": "Not performed. The run was cancelled.",
    "tool.budget_exhausted": (
        "Not performed. The run's tool-call budget is exhausted; "
        "answer from the evidence already gathered."
    ),
    "people.not_found": (
        "No visible person or source has that id. Use a person id returned by "
        "people.search and a source_id from a people.history item."
    ),
    "records.series_unknown": (
        "No such series. Call records.list to see the series, or records.create_series "
        "first (it needs the owner's approval)."
    ),
    "records.series_exists": (
        "A series with that name exists with a different declaration, which cannot "
        "change. Use another name or append to the existing series."
    ),
    "records.component_mismatch": (
        "The values must carry exactly the series' declared components, each once."
    ),
    "records.value_out_of_range": (
        "Each value must be a finite number with at most twelve integer digits and six "
        "fraction digits."
    ),
    "records.observed_in_future": (
        "The observed time is later than now. Use the time the owner measured it."
    ),
    "records.series_limit": "The owner already holds the maximum number of record series.",
    "records.entry_limit": "This series already holds the maximum number of entries.",
    "records.window_too_large": (
        "The bucketed window spans too many buckets. Narrow it or use a larger bucket."
    ),
    "records.secret_detected": (
        "Not recorded. A name, unit or note looked like a credential or secret."
    ),
    "records.name_rejected": (
        "Not recorded. A series, component or unit name looked like an instruction."
    ),
    "records.untrusted_turn": (
        "Not recorded. This turn read external content, so nothing is stored from it. "
        "Ask the owner to state the value in chat, then append it."
    ),
    "records.invalid_arguments": (
        "An argument has an invalid shape or value; check the observed time or window "
        "edge format and the component names."
    ),
    "cockpit.not_found": (
        "No such Cockpit widget or feed. Call cockpit.read with list_widgets or list_feeds."
    ),
    "cockpit.kit_unknown": (
        "The kit has no primitive by that name. Call cockpit.read describe_kit for the list."
    ),
    "cockpit.revision_changed": (
        "The widget is not at the revision named: it exists already or changed since it "
        "was read. Call cockpit.read read_widget and pass its head_revision_id as "
        "expected_revision to update it."
    ),
    "cockpit.expected_revision_required": (
        "An update names the widget's head_revision_id as expected_revision; call "
        "cockpit.read read_widget for it."
    ),
    "cockpit.widget_deleted": (
        "That widget is deleted. Restore it with the restore operation, or use another slug."
    ),
    "cockpit.widget_not_deleted": "Only a deleted widget can be restored.",
    "cockpit.layout_changed": (
        "The layout changed since it was read. Call cockpit.read get_layouts and pass its "
        "revision as expected_revision."
    ),
    "cockpit.conflict": "The Cockpit change conflicts with the widget's current state.",
    "cockpit.write_interactive_only": (
        "Widgets are written only in the owner's interactive chat, never from a scheduled, "
        "delegated or email task. Feeds can be published here instead."
    ),
    "cockpit.rate_limited": ("Not written. The hour's widget commits are used; try again later."),
    "cockpit.build_unavailable": (
        "The widget builder reached no verdict and nothing was recorded. Try again shortly."
    ),
    "cockpit.preview_unavailable": (
        "Preview images are not available in this deployment yet; use the validate operation."
    ),
    "cockpit.settings_invalid": (
        "The settings do not fit the widget's settings schema, or the widget is not live. "
        "Call cockpit.read read_widget for its settings_schema."
    ),
    "cockpit.layout_invalid": (
        "The layout names a widget that is not live, places one twice, or uses a size its "
        "manifest does not offer. Call cockpit.read read_widget for the widget's sizes."
    ),
    "cockpit.feed_schema_required": "A new feed's first publish carries its JSON Schema.",
    "cockpit.feed_schema_invalid": (
        "The schema is not a self-contained JSON Schema (draft 2020-12, referring to "
        "nothing outside itself)."
    ),
    "cockpit.feed_data_invalid": (
        "The data does not fit the feed's schema. Call cockpit.read read_feed for the schema, "
        "or publish with a new schema."
    ),
    "cockpit.feed_observed_in_future": (
        "The observed time is later than now; omit observed_at to use the current time."
    ),
    "cockpit.feed_invalid": (
        "The snapshot is out of bounds: the data holds at most 64 KiB and the schema 8 KiB."
    ),
    "cockpit.invalid_arguments": (
        "An argument is missing or has an invalid shape for that operation."
    ),
}


def message_for(reason_code: str) -> str:
    """Return narration written by the platform, never by an external system."""

    return TOOL_MESSAGES.get(
        reason_code, "The tool could not complete for a platform-defined reason."
    )
