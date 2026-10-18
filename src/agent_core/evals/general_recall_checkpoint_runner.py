"""Invoke the original runtime for a durably started continuation slot."""

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from agent_core.adapters.determinism import elapsed_time
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.config import PACKAGE_ROOT, Settings
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    _digest,
    _json,
    _read,
    _sync,
    _write,
    required_slots,
)
from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent, formation_identity
from agent_core.evals.general_recall_checkpoint_runtime import (
    ContinuationDeadline,
    bind_frozen_preflight,
    verify_worker_admissions,
)
from agent_core.evals.general_recall_composition import (
    local_embedding_provider,
    provider_adapters,
    system_clock,
)
from agent_core.evals.general_recall_confirmatory import (
    ConfirmationOptions,
    PreparedConfirmation,
    prepare_confirmation,
    verify_external_inputs,
)
from agent_core.evals.general_recall_continuation_budget import (
    ContinuationBudget,
    ContinuationHaltedError,
    ContinuationProvider,
    ParentLedger,
    _journal,
)
from agent_core.evals.general_recall_continuation_collector import (
    CollectionFailureError,
    collect_segment,
)
from agent_core.evals.general_recall_execution import evaluate_fixture
from agent_core.evals.general_recall_formation_bank import _Bank
from agent_core.evals.general_recall_observation import compile_runtime_observation
from agent_core.memory.general_recall_evidence import _unique_object
from agent_core.memory.recall_release_binding import capture_inputs, input_stamps
from agent_core.model.registry import ProviderRegistry
from agent_core.ports.embeddings import LocalEmbeddingProvider
from agent_core.ports.models import ModelProvider

_COLLECTOR_MODULES = frozenset(
    "agent_core.evals." + name
    for name in (
        "general_recall_checkpoint",
        "general_recall_checkpoint_runtime",
        "general_recall_checkpoint_parent",
        "general_recall_checkpoint_runner",
        "general_recall_continuation_budget",
        "general_recall_continuation_collector",
    )
)


def verify_frozen_module(root: Path) -> None:
    """Refuse mixed runtime imports; only the six separately pinned modules differ."""
    package = (root / "src/agent_core").resolve()
    for name, module in tuple(sys.modules.items()):
        if (
            not (name == "agent_core" or name.startswith("agent_core."))
            or name in _COLLECTOR_MODULES
        ):
            continue
        filename = getattr(module, "__file__", None)
        if filename is not None and not Path(filename).resolve().is_relative_to(package):
            raise ValueError("A measured runtime module was imported from another source root")
    if (
        Path(evaluate_fixture.__code__.co_filename).resolve()
        != package / "evals/general_recall_execution.py"
    ):
        raise ValueError("The evaluator is not the original frozen runtime")


class FrozenSlotEvaluator:
    """Lazy paid adapters after durable slot admission; never retry an answer."""

    def __init__(
        self,
        *,
        root: Path,
        parent: CheckpointParent,
        prepared: PreparedConfirmation,
        options: ConfirmationOptions,
        budget: ContinuationBudget,
        deadline: ContinuationDeadline,
        verify_protocol: Callable[[], None],
        forbidden_databases: tuple[str, ...],
    ) -> None:
        self.root, self.parent, self.prepared = root, parent, prepared
        self.options, self.budget, self.deadline = options, budget, deadline
        self.verify_protocol = verify_protocol
        self._providers: dict[str, ModelProvider] = {}
        self._encoders: dict[int, LocalEmbeddingProvider] = {}
        self._closed = False
        verify_protocol()
        verify_frozen_module(root)
        self.prepared = bind_frozen_preflight(
            parent_identity=parent.identity.model_dump(mode="json"),
            parent_manifest=parent.manifest.model_dump(mode="json"),
            parent_preregistration=parent.preregistration.model_dump(mode="json"),
            parent_inputs=parent.inputs,
            parent_options=parent.options.model_dump(mode="json"),
            prepared=prepared,
            options=options,
            root=root,
            forbidden_databases=forbidden_databases,
        )
        store = budget.store
        store._owned()
        receipt = json.loads(
            _read(store.directory / "parent-import.json"), object_pairs_hook=_unique_object
        )
        if (
            prepared.confirmation != parent.confirmation
            or prepared.preregistration != parent.preregistration
            or prepared.cases != parent.cases
            or prepared.snapshots != parent.inputs
            or len(prepared.worker_settings) != 2
            or prepared.settings != prepared.worker_settings[0]
            or store.plan.identity_sha256 != parent.identity_sha256
            or receipt["plan_sha256"] != _digest(store.plan.encoded())
            or receipt["parent_archive_sha256"] != parent.archive.sha256
            or receipt["selected_rows"] != len(parent.rows)
            or not set(parent.rows) <= set(store.completed())
            or capture_inputs(root, parent.identity.model_dump(mode="json")).digest
            != store.plan.evaluated_inputs_sha256
        ):
            raise ValueError("The original runtime and imported measurements are not admitted")
        self._stamps = input_stamps(root)
        deadline.remaining()

    def admit(self, key: Slot) -> None:
        self.budget.store._owned()
        if self._closed or self.budget._segment_lock is None:
            raise ValueError("The collection segment is closed")
        if self.budget.failed:
            raise ContinuationHaltedError(self.budget.halt_reason)
        self.verify_protocol()
        self.deadline.remaining()
        if (
            key not in required_slots(self.budget.store.plan.scenarios)
            or input_stamps(self.root) != self._stamps
        ):
            raise ValueError("The slot or evaluated source changed")
        self.parent.bank_path(self.budget.store, key[0])

    async def evaluate(self, key: Slot, worker: int) -> bytes:
        self.admit(key)
        self.budget.store.require_started(key)
        if worker not in {1, 2}:
            raise ValueError("A continuation requires exactly two registered workers")
        bank = self.parent.bank_path(self.budget.store, key[0])
        if not self._providers:
            registry = ProviderRegistry.load(
                PACKAGE_ROOT / "models",
                adapters=ADAPTER_DEFINITIONS,
                overlay_root=self.prepared.settings.config_dir,
            )
            self._providers = provider_adapters(self.prepared.settings, registry)
        if worker not in self._encoders:
            self._encoders[worker] = local_embedding_provider(
                self.prepared.model_directory, self.prepared.local
            )
        variant = next(
            v for v in self.parent.manifest.variants if v.key == (key[2], key[1], key[3])
        )
        reader = ContinuationProvider(
            self._providers[self.prepared.resolved.provider], self.budget, slot=key
        )
        try:
            async with asyncio.timeout(self.deadline.remaining()):
                row = await evaluate_fixture(
                    case=self.parent.by_id[key[0]],
                    settings=self.prepared.worker_settings[worker - 1],
                    principal=self.parent.manifest.principal.principal(),
                    pipeline=variant.pipeline,
                    path=variant.path,
                    channel=variant.channel,
                    reader=reader,
                    resolved=self.prepared.resolved,
                    local=self.prepared.local,
                    embeddings=self._encoders[worker],
                    noise=self.options.noise,
                    storage="postgres",
                    disposable_database=self.options.disposable_database,
                    recover_sources=self.options.source_recovery,
                    semantic_fusion_weight=self.options.semantic_fusion_weight,
                    formation_policy=self.options.formation_policy,
                    reranking=self.options.reranking,
                    query_planning=self.options.query_planning,
                    search_planning=self.options.search_planning,
                    historical_indexing=self.options.historical_indexing,
                    production_reader=self.options.production_reader,
                    confirmation=self.parent.confirmation,
                    formation_bank_path=bank,
                )
        except (Exception, asyncio.CancelledError) as error:
            stage = "answer" if bank.exists() else "formation"
            cause = error
            # The original bank may surface its safe secondary sealing error after
            # a cancelled provider. Recover only an exactly slot-bound journal fact.
            if (
                stage == "formation"
                and type(error) is ValueError
                and str(error) == "The formation bank failed or was already sealed"
            ):
                records = [
                    json.loads(line, object_pairs_hook=_unique_object)
                    for line in _journal(self.budget.journal).splitlines()
                ]
                failed = [
                    r
                    for r in records
                    if r.get("slot") == list(key) and r.get("outcome") == "uncertain"
                ]
                if len(failed) == 1 and failed[0].get("error_class") in {
                    "CancelledError",
                    "TimeoutError",
                }:
                    cause = (
                        asyncio.CancelledError()
                        if failed[0]["error_class"] == "CancelledError"
                        else TimeoutError()
                    )
            raise CollectionFailureError(stage, cause) from error
        row["observed_at"] = system_clock().now().isoformat()
        return (json.dumps(row) + "\n").encode()

    def validate(self, value: dict[str, Any]) -> None:
        self.parent.confirmation.validate_observation(value)
        key = self.parent._key(value)
        if key not in required_slots(self.budget.store.plan.scenarios):
            raise ValueError("The result is outside the amended matrix")
        case = self.parent.by_id[key[0]]
        path = self.parent.bank_path(self.budget.store, key[0])
        bank_bytes = _read(path)
        bank = _Bank.model_validate_json(bank_bytes)
        if _digest(bank_bytes) != value.get(
            "formation_bank_sha256"
        ) or bank.identity != formation_identity(
            case,
            settings=self.prepared.settings,
            options=self.options,
            implementation_sha256=self.parent.identity.implementation_sha256,
        ):
            raise ValueError("The completed observation does not match its original formation bank")
        observed_at = datetime.fromisoformat(str(value.get("observed_at")))
        if (
            observed_at.tzinfo is None
            or not self.parent.preregistration.registered_at <= observed_at <= system_clock().now()
        ):
            raise ValueError("The measured observation has an invalid timestamp")
        compile_runtime_observation(
            case,
            value,
            identity_sha256=self.parent.identity_sha256,
            formation_bank=path,
            principal=self.parent.manifest.principal.principal(),
        )

    async def close(self) -> None:
        if self.budget.active_streams:
            raise ValueError("Admitted provider requests must settle before cleanup")
        self._closed = True
        results = await asyncio.gather(
            *(p.close() for p in self._encoders.values()),
            *(p.close() for p in self._providers.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def run_checkpoint_segment(
    *,
    root: Path,
    store: CheckpointStore,
    parent: CheckpointParent,
    parents: tuple[ParentLedger, ...],
    options: ConfirmationOptions,
    settings: Settings,
    scratch: Settings,
    protocol_sha256: str,
    verify_protocol: Callable[[], None],
    forbidden_databases: tuple[str, ...],
    create_deadline: bool,
    preparation_approval: tuple[bytes, str, bytes] | None = None,
    monotonic: Callable[[], float] = elapsed_time,
) -> dict[str, Any]:
    """Run one explicitly admitted segment, never automatically launch a successor."""
    store._owned()
    verify_protocol()
    verify_frozen_module(root)
    parent.archive.verify()
    parent.import_into(store)
    if preparation_approval is not None:
        proof, digest, diagnostic = preparation_approval
        parent.admit_preparation_replacement(
            store, proof, expected_sha256=digest, diagnostic=diagnostic
        )
    if len(store.pending()) + len(store.completed()) != 1600:
        raise ValueError("An unresolved slot requires offline reconciliation before continuation")
    if len(store.completed()) == 1600:
        raise ValueError("The matrix is already collected; do not launch another segment")
    previous_workers = verify_worker_admissions(
        store, protocol_sha256=protocol_sha256, forbidden_databases=forbidden_databases
    )
    forbidden = forbidden_databases + previous_workers
    prepared = await prepare_confirmation(root, options, settings, scratch)
    prepared = bind_frozen_preflight(
        parent_identity=parent.identity.model_dump(mode="json"),
        parent_manifest=parent.manifest.model_dump(mode="json"),
        parent_preregistration=parent.preregistration.model_dump(mode="json"),
        parent_inputs=parent.inputs,
        parent_options=parent.options.model_dump(mode="json"),
        prepared=prepared,
        options=options,
        root=root,
        forbidden_databases=forbidden,
    )
    await verify_external_inputs(root, options, settings, scratch, prepared)
    verify_protocol()
    deadline = ContinuationDeadline(
        store,
        protocol_sha256=protocol_sha256,
        duration_seconds=options.deadline_seconds,
        create=create_deadline,
        now=store.clock.now,
        monotonic=monotonic,
    )
    budget = ContinuationBudget(store, parents)
    evaluator = None
    try:
        directory = store.directory / "operator-segments"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _sync(store.directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe worker admission directory")
        names = tuple(make_url(worker.database_url).database for worker in prepared.worker_settings)
        _write(
            directory / budget.journal.with_suffix(".json").name,
            _json(
                {
                    "version": "recall-checkpoint-worker-admission@1",
                    "plan_sha256": _digest(store.plan.encoded()),
                    "protocol_sha256": protocol_sha256,
                    "budget_admission_sha256": _digest(
                        _read(budget.journal.with_suffix(".admission.json"))
                    ),
                    "databases": names,
                    "forbidden_databases": forbidden,
                    "options_sha256": _digest(options.model_dump_json().encode()),
                    "effective_inputs_sha256": _digest(_json(prepared.snapshots)),
                }
            ),
        )
        verify_worker_admissions(
            store, protocol_sha256=protocol_sha256, forbidden_databases=forbidden_databases
        )
        evaluator = FrozenSlotEvaluator(
            root=root,
            parent=parent,
            prepared=prepared,
            options=options,
            budget=budget,
            deadline=deadline,
            verify_protocol=verify_protocol,
            forbidden_databases=forbidden,
        )

        def seal() -> str:
            budget.seal()
            return _digest(_journal(budget.journal))

        return await collect_segment(
            store,
            evaluate=evaluator.evaluate,
            validate=evaluator.validate,
            admit=evaluator.admit,
            seal_ledger=seal,
            monotonic=monotonic,
        )
    finally:
        # The collector settles admitted peers even on supervisor cancellation.
        try:
            if evaluator is not None:
                await evaluator.close()
        finally:
            if budget._segment_lock is not None:
                budget.seal()
