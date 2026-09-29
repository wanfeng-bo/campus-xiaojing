"""F2-owned capture and live probes over the existing Reader/Thin interfaces.

No public tool accepts these objects. They contain metadata, not cached business
rows, and are scoped to one private result and its original trusted identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, datetime
from threading import RLock
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_product_services import (
    ProductThinAuthorizationService,
    ReadOnlyGovernanceDependencies,
)
from app.composition.school_fact_read_evidence import ReadEvidence
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.authorization.secure_reader_adapters import (
    SqlAlchemyRepeatableReadExecutor,
    authorization_basis_fingerprint,
)
from app.platform.authorization.secure_reader_compiler import (
    CompiledReadRelation,
    ServerOwnedProjectionCompiler,
)
from app.platform.authorization.secure_reader_contracts import (
    SecureReadDenied,
    SecureReadPage,
    SecureReadRequest,
)
from app.platform.authorization.secure_reader_service import ReadTransaction, SecureReader
from app.platform.authorization.thin_contracts import (
    ThinAuthorizationDecision,
    ThinAuthorizationRequest,
)
from app.platform.contracts import StaffActorContext

_capture: ContextVar[ReadCapture | None] = ContextVar("f2_read_capture", default=None)
_current_query: ContextVar[bool] = ContextVar("f2_current_query", default=False)
_read_progress: ContextVar[list[bool] | None] = ContextVar("f2_read_progress", default=None)
_root_round: ContextVar[Any] = ContextVar("f2_owner_root_round", default=None)
_decision_clock: ContextVar[datetime | None] = ContextVar("f2_decision_clock", default=None)


class _OwnerRootRound:
    """Private snapshots retained only until one release validation exits."""

    def __init__(
        self,
        owner: object,
        actor: StaffActorContext,
        at: datetime,
        fresh: ReadOnlyGovernanceDependencies,
        clock: Callable[[], datetime],
        fresh_scope: Callable[[], AbstractContextManager[ReadOnlyGovernanceDependencies]]
        | None = None,
        decision_scope: Callable[[StaffActorContext], AbstractContextManager[None]] | None = None,
    ) -> None:
        self.decision_scope = decision_scope
        self.owner = owner
        self.actor = actor
        self.at = at
        self.fresh = fresh
        self.fresh_scope = fresh_scope
        self.clock = clock
        self.entries: dict[object, tuple[str, dict[str, Any], Any]] = {}
        self.lock = RLock()

    @contextmanager
    def activate(self, *, decision_at: datetime | None = None):
        token = _root_round.set((self.owner, self.actor, self.at, self.entries, self.lock))
        clock_token = _decision_clock.set(self.at if decision_at is None else decision_at)
        try:
            scope = self.decision_scope(self.actor) if self.decision_scope else nullcontext()
            with scope:
                yield
        finally:
            _decision_clock.reset(clock_token)
            _root_round.reset(token)

    def finish(self) -> None:
        if not self.entries:
            return
        # The optional connection scope exists only for this fresh exit pass.
        # Default evidence batches retain their original dependency/session behavior.
        scope = self.fresh_scope() if self.fresh_scope else nullcontext(self.fresh)
        with scope as fresh:
            for method, arguments, expected in self.entries.values():
                now = self.clock()
                if (
                    now < self.at
                    or getattr(fresh, method)(**{**arguments, "as_of": now}) != expected
                ):
                    raise AssistantRuntimeError("authority root changed during validation")

    def clear(self) -> None:
        self.entries.clear()


@contextmanager
def owner_root_round(
    owner: object,
    actor: StaffActorContext,
    at: datetime,
    fresh: ReadOnlyGovernanceDependencies,
    clock: Callable[[], datetime],
    *,
    fresh_scope: Callable[[], AbstractContextManager[ReadOnlyGovernanceDependencies]] | None = None,
):
    round_ = _OwnerRootRound(owner, actor, at, fresh, clock, fresh_scope)
    try:
        with round_.activate():
            yield
        round_.finish()
    finally:
        round_.clear()


class EvidenceGovernanceDependencies(ReadOnlyGovernanceDependencies):
    def __init__(self, sessions: sessionmaker[Session], *, owner: object) -> None:
        super().__init__(sessions)
        self.owner = owner

    def read_authority_roots(self, **kwargs: Any):
        # Existing owner rounds retain their exact per-root capture and fresh exit
        # probes. The bundle lives only inside an unbatched current decision.
        if _root_round.get() is not None:
            return None
        return super().read_authority_roots(**kwargs)

    def _once(self, method: str, arguments: dict[str, Any], read: Callable[..., Any]):
        state = _root_round.get()
        if (
            state is None
            or state[0] is not self.owner
            or arguments["as_of"] != _decision_clock.get()
        ):
            return read(**arguments)
        arguments = {**arguments, "as_of": state[2]}
        # A single round belongs to a full trusted actor; school and exact roots
        # remain part of each key, including negative owner snapshots.
        key = (state[1], method, tuple(sorted(arguments.items())))
        # Quality dependency workers share this round via copy_context. Serialize
        # only a root load; grants/scopes/epochs still run independently and fresh.
        with state[4]:
            if key not in state[3]:
                state[3][key] = (method, dict(arguments), read(**arguments))
            return state[3][key][2]

    def read_assignment(self, **kwargs: Any):
        parent = super().read_assignment
        return self._once("read_assignment", kwargs, parent)

    def _root(self, **kwargs: Any):
        parent = super()._root
        return self._once("_root", kwargs, parent)


def _hash(value: object) -> str:
    def encode(item: object):
        return asdict(item) if is_dataclass(item) and not isinstance(item, type) else str(item)

    return hashlib.sha256(json.dumps(value, sort_keys=True, default=encode).encode()).hexdigest()


def decision_signature(value: ThinAuthorizationDecision) -> str:
    fingerprint = authorization_basis_fingerprint(value.basis)
    if value.kind.value == "allow" and fingerprint is None:
        raise ValueError("Invalid read authority evidence")
    return _hash((value.kind, value.reason, value.scope_plan, value.projection, fingerprint))


class CapturingThinService(ProductThinAuthorizationService):
    def decide_many(
        self, requests: tuple[ThinAuthorizationRequest, ...]
    ) -> tuple[ThinAuthorizationDecision, ...]:
        results = super().decide_many(requests)
        capture = _capture.get()
        if capture is not None and capture.backend.thin is self:
            for request, result in zip(requests, results, strict=True):
                capture.record_decision(request, decision_signature(result))
        return results

    def decide(self, request: ThinAuthorizationRequest) -> ThinAuthorizationDecision:
        result = super().decide(request)
        capture = _capture.get()
        if capture is not None and capture.backend.thin is self:
            capture.record_decision(request, decision_signature(result))
        return result


@dataclass(frozen=True)
class _Probe:
    arguments: dict[str, Any]
    current_time: bool
    key: str


class CapturingCompiler(ServerOwnedProjectionCompiler):
    def compile(self, **kwargs: Any) -> CompiledReadRelation:
        progress = _read_progress.get()
        if progress is not None:
            progress[0] = True
        relation = super().compile(**kwargs)
        capture = _capture.get()
        if capture is not None:
            capture.relations[id(relation)] = (relation, dict(kwargs), _current_query.get())
        return relation


class CapturingReader(SecureReader):
    def read(self, request: SecureReadRequest) -> SecureReadPage:
        capture = _capture.get()
        # A historical self fallback may perform an additional CURRENT ownership
        # probe. Conversely quality inherits the outer current query's fact time.
        current = request.factual_at is None or capture is not None and capture.current_time
        token = _current_query.set(current)
        progress = [False]
        progress_token = _read_progress.set(progress)
        try:
            return super().read(request)
        except Exception as exc:
            if capture is not None and (progress[0] or not isinstance(exc, SecureReadDenied)):
                capture.supported = False
            raise
        finally:
            _read_progress.reset(progress_token)
            _current_query.reset(token)


class _CaptureTransaction:
    def __init__(self, transaction: Any, capture: ReadCapture) -> None:
        self.transaction = transaction
        self.capture = capture
        self.counts: dict[int, int] = {}

    def count(self, relation: CompiledReadRelation) -> int:
        result = self.transaction.count(relation)
        self.counts[id(relation)] = result
        return result

    def fetch(self, relation: CompiledReadRelation) -> Sequence[Mapping[str, object]]:
        rows = self.transaction.fetch(relation)
        capture = self.capture
        if relation.metadata_statement is None:
            capture.supported = False
        else:
            metadata = self.transaction.metadata(relation)
            capture.record_page(relation, self.counts[id(relation)], metadata, rows)
        return rows


class CapturingExecutor:
    def __init__(self, sessions: Callable[[], Session], *, delegate: Any = None) -> None:
        self.delegate = (
            delegate if delegate is not None else SqlAlchemyRepeatableReadExecutor(sessions)
        )

    @contextmanager
    def validated_read(
        self, before_read: Callable[[], None], after_read: Callable[[], None]
    ) -> Generator[ReadTransaction]:
        method = getattr(self.delegate, "validated_read", None)
        if not callable(method):
            with self.repeatable_read() as transaction:
                before_read()
                yield transaction
                after_read()
            return
        with cast(AbstractContextManager[Any], method(before_read, after_read)) as transaction:
            capture = _capture.get()
            yield transaction if capture is None else _CaptureTransaction(transaction, capture)

    @contextmanager
    def repeatable_read(self) -> Generator[ReadTransaction]:
        with self.delegate.repeatable_read() as transaction:
            capture = _capture.get()
            yield transaction if capture is None else _CaptureTransaction(transaction, capture)


class ReadCapture:
    def __init__(self, backend: F2EvidenceBackend, actor: StaffActorContext, current_time: bool):
        self.backend = backend
        self.actor = actor
        self.current_time = current_time
        self.observed_at = backend.clock()
        self.version = backend.version(actor)
        self.decisions: dict[str, tuple[Any, str]] = {}
        self.probes: list[tuple[_Probe, Any]] = []
        self.relations: dict[int, tuple[CompiledReadRelation, dict[str, Any], bool]] = {}
        self.deadlines: list[datetime] = []
        self.supported = True
        self.lock = RLock()

    def record_decision(self, request: ThinAuthorizationRequest, signature: str) -> None:
        with self.lock:
            key = self.backend.decision_key(request)
            old = self.decisions.get(key)
            if old is not None and old[1] != signature:
                self.supported = False
            self.decisions[key] = (request, signature)

    def record_page(
        self,
        relation: CompiledReadRelation,
        total: int,
        metadata: tuple[tuple[object, ...], ...],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        entry = self.relations.get(id(relation))
        if entry is None or entry[0] is not relation:
            self.supported = False
            return
        arguments = entry[1]
        current_time = entry[2]
        key_arguments = {
            **arguments,
            "as_of": "current" if current_time else arguments["as_of"],
        }
        self.probes.append(
            (_Probe(arguments, current_time, _hash(key_arguments)), (total, metadata))
        )
        if current_time:
            # Use every authorized quality row, including statements that are not
            # yet verified and therefore absent from the displayed quality claims.
            for row in rows:
                if row.get("fact_kind") == "fact_quality":
                    content = row["content"]
                    if not isinstance(content, dict):
                        self.supported = False
                        continue
                    for field in ("verified_at", "review_due_at"):
                        value = cast(dict[str, object], content).get(field)
                        if isinstance(value, str):
                            value = datetime.fromisoformat(value)
                        if isinstance(value, datetime) and value > self.observed_at:
                            self.deadlines.append(value)

    def seal(self) -> ReadEvidence | None:
        if (
            not self.supported
            or not self.decisions
            or self.backend.version(self.actor) != self.version
        ):
            return None
        return ReadEvidence(
            self.backend,
            self.actor,
            self.version,
            self.observed_at,
            min(self.deadlines) if self.deadlines else None,
            tuple(self.decisions.values()),
            tuple(self.probes),
        )


class F2EvidenceBackend:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        thin: ProductThinAuthorizationService,
        dependency_owner: object | None = None,
        decision_scope: Callable[[StaffActorContext], AbstractContextManager[None]] | None = None,
        fresh_scope: Callable[[], AbstractContextManager[ReadOnlyGovernanceDependencies]]
        | None = None,
    ) -> None:
        self.decision_scope = decision_scope
        self.fresh_scope = fresh_scope
        self.sessions = sessions
        self.thin = thin
        self.dependency_owner = dependency_owner or object()
        self.compiler = ServerOwnedProjectionCompiler()
        self.executor = SqlAlchemyRepeatableReadExecutor(sessions)

    @staticmethod
    def clock() -> datetime:
        return datetime.now(UTC)

    def version(self, actor: StaffActorContext) -> int:
        with self.sessions() as session:
            value = session.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
            )
        if type(value) is not int or value < 1:
            raise ValueError("Invalid fact revision")
        return value

    @contextmanager
    def capture(
        self, *, actor: StaffActorContext, current_time: bool = True
    ) -> Generator[ReadCapture]:
        capture = ReadCapture(self, actor, current_time)
        token = _capture.set(capture)
        try:
            yield capture
        finally:
            _capture.reset(token)
            capture.relations.clear()

    @staticmethod
    def decision_key(request: ThinAuthorizationRequest) -> str:
        return _hash({k: v for k, v in asdict(request).items() if k not in {"as_of", "request_id"}})

    def new_decision_round(self, actor: StaffActorContext) -> _OwnerRootRound:
        return _OwnerRootRound(
            self.dependency_owner,
            actor,
            self.clock(),
            ReadOnlyGovernanceDependencies(self.sessions),
            self.clock,
            fresh_scope=self.fresh_scope,
            decision_scope=self.decision_scope,
        )

    @contextmanager
    def decision_round(self, actor: StaffActorContext):
        with owner_root_round(
            self.dependency_owner,
            actor,
            self.clock(),
            ReadOnlyGovernanceDependencies(self.sessions),
            self.clock,
        ):
            yield

    def decision(self, request: ThinAuthorizationRequest) -> str:
        token = _capture.set(None)
        try:
            state = _root_round.get()
            if state is not None and state[0] is self.dependency_owner:
                if request.actor != state[1]:
                    raise AssistantRuntimeError("authority round identity mismatch")
            # Grants, scopes, competing candidates and Thin always use fresh time.
            # Only immutable owner roots are normalized inside the short round;
            # its exit independently re-reads their complete current snapshots.
            at = self.clock()
            clock_token = _decision_clock.set(at)
            try:
                return decision_signature(self.thin.decide(replace(request, as_of=at)))
            finally:
                _decision_clock.reset(clock_token)
        finally:
            _capture.reset(token)

    def decisions(self, requests: tuple[ThinAuthorizationRequest, ...]) -> tuple[str, ...]:
        batch = getattr(self.thin, "decide_many", None)
        if batch is None:
            return tuple(self.decision(request) for request in requests)
        if not requests:
            return ()
        if len(requests) > 128 or any(request.actor != requests[0].actor for request in requests):
            raise AssistantRuntimeError("authority batch identity mismatch")
        state = _root_round.get()
        if (
            state is not None
            and state[0] is self.dependency_owner
            and requests[0].actor != state[1]
        ):
            raise AssistantRuntimeError("authority round identity mismatch")
        token = _capture.set(None)
        at = self.clock()
        clock_token = _decision_clock.set(at)
        try:
            return tuple(
                decision_signature(result)
                for result in batch(tuple(replace(request, as_of=at) for request in requests))
            )
        finally:
            _decision_clock.reset(clock_token)
            _capture.reset(token)

    @staticmethod
    def metadata_key(probe: _Probe) -> str:
        return probe.key

    def metadata(self, probe: _Probe) -> tuple[int, tuple[tuple[object, ...], ...]]:
        arguments = dict(probe.arguments)
        if probe.current_time:
            arguments["as_of"] = self.clock()
        relation = self.compiler.compile(**arguments)
        if relation.metadata_statement is None:
            raise ValueError("Read evidence unavailable")
        with self.executor.repeatable_read() as transaction:
            return transaction.count_metadata(relation)
