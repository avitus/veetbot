"""Consent, redaction, materialization, and expiry for trajectory exports."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from datetime import timedelta
from re import Pattern
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    ArtifactSweepError,
    ExportConsentError,
    ExportRedactionError,
    ExportRedactionPatternError,
    ExportStateError,
    NotFoundError,
)
from agent_core.domain.events import conversation_items
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import TERMINAL_RUN_STATUSES
from agent_core.domain.security import SECRET_RULES
from agent_core.domain.trajectory import (
    ArtifactRef,
    ExportConsent,
    RedactionSummary,
    TrajectoryExport,
)
from agent_core.ports.artifacts import TrajectoryArtifactStore
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.tools import ToolRegistry

BUILDER_VERSION = "trajectory@5"
RULESET_VERSION = "secrets@2"
RETENTION = timedelta(days=30)
logger = logging.getLogger(__name__)
SENSITIVE_KEY = re.compile(r"secret|token|password|api_?key|authorization", re.IGNORECASE)
QUANTIFIED_GROUP = re.compile(r"\)(?:[*+]|\{\d)")
UNSAFE_TENANT_PATTERN = re.compile(r"(?:\.\*|\.\+|\\[1-9]|\(\?P=|\(\?<?[=!]|\(\?\()")
BUILTIN_RULES: tuple[tuple[str, Pattern[str]], ...] = tuple(SECRET_RULES.items())


class TrajectoryRedactor:
    """Apply every mandatory rule, then verify the result with the same scanner."""

    def __init__(
        self,
        tenant_patterns: Iterable[tuple[str, str]] = (),
        *,
        replacement_enabled: bool = True,
    ) -> None:
        compiled: list[tuple[str, Pattern[str]]] = []
        for name, raw in tenant_patterns:
            if not name or len(raw) > 256:
                raise ExportRedactionPatternError(name, "name is empty or expression is too long")
            if QUANTIFIED_GROUP.search(raw) or UNSAFE_TENANT_PATTERN.search(raw):
                raise ExportRedactionPatternError(name, "expression is outside the linear subset")
            try:
                compiled.append((name, re.compile(raw)))
            except re.error as exc:
                raise ExportRedactionPatternError(name, str(exc)) from exc
        self._rules = (*BUILTIN_RULES, *compiled)
        self._replacement_enabled = replacement_enabled

    def redact(self, messages: list[dict[str, Any]]) -> RedactionSummary:
        counts: dict[str, int] = {}
        if self._replacement_enabled:
            for message in messages:
                self._replace_value(message, counts)
        self._verify(messages)
        return RedactionSummary(ruleset_version=RULESET_VERSION, replacements=counts)

    def _replace_value(self, value: Any, counts: dict[str, int]) -> Any:
        if isinstance(value, str):
            rendered = value
            for name, pattern in self._rules:
                rendered, count = pattern.subn(f"[redacted:{name}]", rendered)
                if count:
                    counts[name] = counts.get(name, 0) + count
            return rendered
        if isinstance(value, list):
            for index, nested in enumerate(value):
                value[index] = self._replace_value(nested, counts)
            return value
        if isinstance(value, dict):
            for key, nested in list(value.items()):
                if SENSITIVE_KEY.search(str(key)):
                    value[key] = "[redacted:sensitive_key]"
                    counts["sensitive_key"] = counts.get("sensitive_key", 0) + 1
                else:
                    value[key] = self._replace_value(nested, counts)
            return value
        return value

    def _verify(self, messages: list[dict[str, Any]]) -> None:
        for index, message in enumerate(messages):
            rendered = json.dumps(message, ensure_ascii=False, sort_keys=True)
            for name, pattern in self._rules:
                if pattern.search(rendered):
                    raise ExportRedactionError(name, index)


class TrajectoryExportService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        principal: Principal,
        clock: Clock,
        ids: IdFactory,
        tools: ToolRegistry,
        artifacts: TrajectoryArtifactStore,
        tenant_enabled: bool,
        redactor: TrajectoryRedactor | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._principal = principal
        self._clock = clock
        self._ids = ids
        self._tools = tools
        self._artifacts = artifacts
        self._tenant_enabled = tenant_enabled
        self._redactor = redactor or TrajectoryRedactor()

    async def grant_consent(self) -> ExportConsent:
        consent = ExportConsent(
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            granted_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            return await uow.export_consent.grant(consent)

    async def withdraw_consent(self) -> ExportConsent:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            consent = await uow.export_consent.withdraw(
                self._principal.tenant_id,
                self._principal.principal_id,
                now,
            )
            await uow.trajectory_exports.expire_for_principal(
                self._principal.tenant_id,
                self._principal.principal_id,
                now,
            )
            return consent

    async def export(self, run_id: UUID) -> ArtifactRef:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            run = await uow.runs.get(run_id, self._principal)
            if not self._tenant_enabled:
                raise ExportConsentError("trajectory export is disabled for this tenant")
            consent = await uow.export_consent.get(
                self._principal.tenant_id,
                self._principal.principal_id,
            )
            if consent is None or not consent.active:
                raise ExportConsentError("the principal has no active export consent")
            if not run.export_consent:
                raise ExportConsentError("the run was not consent-stamped when it started")
            if run.status not in TERMINAL_RUN_STATUSES:
                raise ExportStateError("only terminal runs can be exported")
            existing = await uow.trajectory_exports.get_for_run(run.id)
            if existing is not None:
                if (
                    existing.tenant_id != self._principal.tenant_id
                    or existing.principal_id != self._principal.principal_id
                ):
                    raise ExportConsentError("the run was exported by another principal")
                if existing.artifact.expires_at is None or existing.artifact.expires_at > now:
                    return existing.artifact
                raise ExportStateError("the prior export expired and awaits its sweep")
            projection = await uow.trajectory.catch_up(run.id)
            if projection is None or not projection.terminal:
                raise ExportStateError("the terminal trajectory projection is unavailable")
            events = await uow.events.list_after(
                run.session_id,
                projection.first_sequence - 1,
                self._principal,
            )
            invocations = await uow.invocations.list_for_run(run.id, self._principal)
        selected = [
            event
            for event in events
            if event.run_id == run.id and event.sequence <= projection.last_sequence
        ]
        messages = self._messages(selected)
        redaction = self._redactor.redact(messages)
        export_id = self._ids.new_id()
        document = {
            "schema_version": 1,
            "export_id": str(export_id),
            "run_id": str(run.id),
            "tenant_id": run.tenant_id,
            "agent_id": str(run.agent_id),
            "agent_version": run.agent_version,
            "outcome": run.status.value,
            "failure": (
                None
                if run.failure is None
                else {"kind": run.failure.reason.value, "at_step": run.failure.step_number}
            ),
            "recorded_on": now.date().isoformat(),
            "builder_version": BUILDER_VERSION,
            "redaction": redaction.model_dump(mode="json"),
            "messages": messages,
            "tools": self._tool_descriptors(invocations),
        }
        content = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        artifact = ArtifactRef(
            id=self._ids.new_id(),
            tenant_id=run.tenant_id,
            principal_id=self._principal.principal_id,
            session_id=run.session_id,
            run_id=run.id,
            name=f"trajectory-{run.id}.json",
            media_type="application/json",
            storage_uri="",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            expires_at=now + RETENTION,
            created_at=now,
            metadata={"export_id": str(export_id), "builder_version": BUILDER_VERSION},
        )
        stored = await self._artifacts.write(artifact, content)
        export = TrajectoryExport(
            export_id=export_id,
            tenant_id=run.tenant_id,
            principal_id=self._principal.principal_id,
            run_id=run.id,
            artifact=stored,
            builder_version=BUILDER_VERSION,
            ruleset_version=RULESET_VERSION,
            created_at=now,
        )
        try:
            async with self._uow_factory() as uow:
                consent = await uow.export_consent.get_for_update(
                    self._principal.tenant_id,
                    self._principal.principal_id,
                )
                if consent is None or not consent.active:
                    raise ExportConsentError("export consent was withdrawn before commit")
                created = await uow.trajectory_exports.create(export)
        except BaseException:
            await self._artifacts.delete(stored)
            raise
        if created.export_id != export.export_id:
            await self._artifacts.delete(stored)
        return created.artifact

    async def read(self, run_id: UUID) -> bytes:
        async with self._uow_factory() as uow:
            export = await uow.trajectory_exports.get_for_run(run_id)
        if export is None or (
            export.artifact.expires_at is not None
            and export.artifact.expires_at <= self._clock.now()
        ):
            raise NotFoundError("trajectory export not found")
        if (
            export.tenant_id != self._principal.tenant_id
            or export.principal_id != self._principal.principal_id
        ):
            raise NotFoundError("trajectory export not found")
        return await self._artifacts.read(export.artifact)

    async def sweep_once(self, *, limit: int = 100) -> int:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            expired = await uow.trajectory_exports.list_expired(now, limit=limit)
        deleted = 0
        failures = 0
        for artifact in expired:
            try:
                await self._artifacts.delete(artifact)
                async with self._uow_factory() as uow:
                    removed = await uow.trajectory_exports.delete_expired(
                        artifact.id,
                        now=now,
                    )
            except Exception:
                logger.exception(
                    "trajectory_artifact_sweep_failed",
                    extra={"artifact_id": str(artifact.id)},
                )
                failures += 1
            else:
                if removed:
                    deleted += 1
        if failures:
            raise ArtifactSweepError(deleted=deleted, failed=failures)
        return deleted

    @staticmethod
    def _messages(events: list[Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        seen_tool_calls: set[str] = set()
        seen_tool_results: set[str] = set()
        for event in events:
            for item in conversation_items(event):
                kind = item.kind
                call_id = getattr(item, "call_id", None)
                if kind == "provider_reasoning":
                    continue
                if kind == "tool_call" and isinstance(call_id, str):
                    if call_id in seen_tool_calls:
                        continue
                    seen_tool_calls.add(call_id)
                if kind == "tool_result" and isinstance(call_id, str):
                    if call_id in seen_tool_results:
                        continue
                    seen_tool_results.add(call_id)
                messages.append(
                    item.model_dump(
                        mode="json",
                        exclude={"principal_id", "source_event_sequence"},
                    )
                )
        return messages

    def _tool_descriptors(self, invocations: list[Any]) -> list[dict[str, str]]:
        """Render stable schema hashes for every exact tool version the run used."""

        result: dict[str, dict[str, str]] = {}
        for invocation in invocations:
            tool = self._tools.get(invocation.tool_name, invocation.tool_version)
            schema = json.dumps(
                tool.spec.input_schema,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            result[invocation.tool_name] = {
                "name": invocation.tool_name,
                "schema_sha256": hashlib.sha256(schema).hexdigest(),
            }
        return [result[name] for name in sorted(result)]
