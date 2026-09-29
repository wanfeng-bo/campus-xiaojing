"""Private F2 evidence; batching lasts one release check, never a model wait."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.contracts import StaffActorContext

if TYPE_CHECKING:
    from app.composition.school_fact_product_identity import ProductIdentityEvidence

_active: ContextVar[_ValidationBatch | None] = ContextVar("f2_read_validation", default=None)


def _key(backend: Any, kind: str, value: Any) -> object:
    method = getattr(backend, kind + "_key", None)
    return method(value) if method else value


def _decision_round(backend: Any, actor: StaffActorContext):
    method = getattr(backend, "decision_round", None)
    return method(actor) if method is not None else nullcontext()


def _decisions(backend: Any, requests: tuple[Any, ...]) -> tuple[Any, ...]:
    batch = getattr(backend, "decisions", None)
    if batch is None:
        return tuple(backend.decision(request) for request in requests)
    results: list[Any] = []
    for start in range(0, len(requests), 128):
        chunk = requests[start : start + 128]
        values = batch(chunk)
        if len(values) != len(chunk):
            raise AssistantRuntimeError("incomplete authority validation batch")
        results.extend(values)
    return tuple(results)


class _ValidationBatch:
    def __init__(self):
        self.root_rounds: dict[object, Any] = {}
        self.versions: dict[object, tuple[Any, StaffActorContext, int]] = {}
        self.decisions: dict[tuple[object, object], tuple[Any, Any, Any]] = {}
        self.probes: dict[object, Any] = {}
        self.proofs: list[ReadEvidence] = []
        self.probe_checks: dict[object, tuple[Any, Any, Any]] = {}
        self.own_receipts: dict[object, Any] = {}
        self.full_reads: dict[object, Callable[[], bool]] = {}
        self.content_rechecks: dict[object, Callable[[], None]] = {}
        self.rechecking_authority = False
        self.authority_items: dict[object, dict[Any, None]] = {}
        self.authority_rechecks: dict[object, Callable[[tuple[Any, ...]], None]] = {}
        self.identity_checks: dict[object, ProductIdentityEvidence] = {}
        self.time_windows: dict[
            object, tuple[Callable[[], datetime], datetime, datetime | None]
        ] = {}

    def decision_round(self, backend: Any, actor: StaffActorContext):
        factory = getattr(backend, "new_decision_round", None)
        if factory is None:
            return _decision_round(backend, actor)
        owner = (id(backend), actor)
        if owner not in self.root_rounds:
            self.root_rounds[owner] = factory(actor)
        return self.root_rounds[owner].activate()

    def check(self, proof: ReadEvidence) -> bool:
        backend = proof.backend
        now = backend.clock()
        if now < proof.observed_at or proof.valid_before is not None and now >= proof.valid_before:
            return False
        owner = (id(backend), proof.actor)
        if owner not in self.versions:
            self.versions[owner] = (backend, proof.actor, backend.version(proof.actor))
        if self.versions[owner][2] != proof.version:
            return False
        # Re-decide denied requests too: a new grant may change a quality scope.
        with self.decision_round(backend, proof.actor):
            pending: dict[tuple[object, object], Any] = {}
            for request, _ in proof.decisions:
                key = (owner, _key(backend, "decision", request))
                if key not in self.decisions:
                    pending[key] = request
            values = _decisions(backend, tuple(pending.values()))
            for (key, request), value in zip(pending.items(), values, strict=True):
                self.decisions[key] = (backend, request, value)
            for request, expected in proof.decisions:
                key = (owner, _key(backend, "decision", request))
                if self.decisions[key][2] != expected:
                    return False
        for probe, expected in proof.probes:
            key = (owner, _key(backend, "metadata", probe))
            if key not in self.probes:
                self.probes[key] = backend.metadata(probe)
            if self.probes[key] != expected:
                return False
            self.probe_checks[key] = (backend, probe, expected)
        self.proofs.append(proof)
        return True

    def check_authority(
        self,
        key: object,
        items: tuple[Any, ...],
        initial: Callable[[tuple[Any, ...]], None],
        fresh: Callable[[tuple[Any, ...]], None],
    ) -> None:
        known = self.authority_items.setdefault(key, {})
        pending = tuple(item for item in dict.fromkeys(items) if item not in known)
        if len(known) + len(pending) > 1024:
            raise AssistantRuntimeError("authority validation scope exceeded")
        if pending:
            # First use is immediate: owner exceptions still reach its gateway.
            initial(pending)
            known.update(dict.fromkeys(pending))
            self.authority_rechecks[key] = fresh

    def check_content(self, key: object, check: Callable[[], None]) -> None:
        # Exact content dependencies may share their initial IO within this
        # synchronous release. finish() still reads them fresh after authority.
        if key not in self.content_rechecks:
            check()
            self.content_rechecks[key] = check

    def finish(self) -> None:
        # The batch does not release anything before fresh post-read checks.
        for check in self.full_reads.values():
            if not check():
                raise AssistantRuntimeError("Authorized full-read evidence changed")
        for receipts in self.own_receipts.values():
            receipts.finish()
        for backend, probe, expected in self.probe_checks.values():
            if backend.metadata(probe) != expected:
                raise AssistantRuntimeError("history membership changed during validation")
        for owner, (backend, actor, _) in self.versions.items():
            with self.decision_round(backend, actor):
                pending = [
                    (request, expected)
                    for key, (_, request, expected) in self.decisions.items()
                    if key[0] == owner
                ]
                values = _decisions(backend, tuple(request for request, _ in pending))
                for (_, expected), value in zip(pending, values, strict=True):
                    if value != expected:
                        raise AssistantRuntimeError("history authority changed during validation")
        self.rechecking_authority = True
        try:
            for key, fresh in self.authority_rechecks.items():
                items = tuple(self.authority_items[key])
                try:
                    for offset in range(0, len(items), 128):
                        fresh(items[offset : offset + 128])
                except Exception:
                    raise AssistantRuntimeError(
                        "history authority changed during validation"
                    ) from None
        finally:
            self.rechecking_authority = False
        # Shared immutable roots are independently re-read after every fresh
        # Thin/Grant decision. No cached result authorizes release on its own.
        for round_ in self.root_rounds.values():
            round_.finish()
        for check in self.content_rechecks.values():
            try:
                check()
            except Exception:
                raise AssistantRuntimeError("history content changed during validation") from None
        for backend, actor, expected in self.versions.values():
            if backend.version(actor) != expected:
                raise AssistantRuntimeError("history fact version changed during validation")
        # Identity roots are fresh after every content/authority database check.
        # These callbacks bypass same-batch reuse; no remembered identity alone
        # authorizes release. Pure clock checks still run after this final IO.
        for check in self.identity_checks.values():
            try:
                check.revalidate()
            except Exception:
                raise AssistantRuntimeError("identity changed during validation") from None
        for proof in self.proofs:
            now = proof.backend.clock()
            if (
                now < proof.observed_at
                or proof.valid_before is not None
                and now >= proof.valid_before
            ):
                raise AssistantRuntimeError("history time boundary changed during validation")
        for clock, observed_at, valid_before in self.time_windows.values():
            _require_time_window(clock(), observed_at, valid_before)


@contextmanager
def validation_batch():
    existing = _active.get()
    if existing is not None:
        yield existing
        return
    batch = _ValidationBatch()
    token = _active.set(batch)
    try:
        yield batch
        batch.finish()
    finally:
        _active.reset(token)
        for round_ in batch.root_rounds.values():
            round_.clear()
        batch.root_rounds.clear()
        batch.versions.clear()
        batch.decisions.clear()
        batch.probes.clear()
        batch.proofs.clear()
        batch.probe_checks.clear()
        batch.own_receipts.clear()
        batch.full_reads.clear()
        batch.content_rechecks.clear()
        batch.authority_items.clear()
        batch.authority_rechecks.clear()
        batch.identity_checks.clear()
        batch.time_windows.clear()


def current_validation_batch() -> _ValidationBatch | None:
    """The caller's synchronous release scope, absent across model waits."""
    return _active.get()


def _require_time_window(
    now: datetime, observed_at: datetime, valid_before: datetime | None
) -> None:
    if now < observed_at or valid_before is not None and now >= valid_before:
        raise AssistantRuntimeError("history time boundary changed during validation")


def register_release_time_window(
    clock: Callable[[], datetime], *, observed_at: datetime, valid_before: datetime | None
) -> None:
    """Recheck an owner's known time window after all IO; this never runs an IO callback."""
    _require_time_window(clock(), observed_at, valid_before)
    batch = current_validation_batch()
    if batch is not None:
        batch.time_windows.setdefault(
            (id(clock), observed_at, valid_before), (clock, observed_at, valid_before)
        )


def validate_fallback_read(
    key: object,
    check: Callable[[], bool],
    *,
    time_window: tuple[Callable[[], datetime], datetime, datetime | None] | None = None,
) -> bool:
    """Run each exact owner fallback once before this release scope can complete."""
    with validation_batch() as batch:
        if time_window is not None:
            clock, observed_at, valid_before = time_window
            register_release_time_window(clock, observed_at=observed_at, valid_before=valid_before)
        batch.full_reads.setdefault(key, check)
    return True


@dataclass(frozen=True, repr=False)
class ReadEvidence:
    backend: Any
    actor: StaffActorContext
    version: int
    observed_at: datetime
    valid_before: datetime | None
    decisions: tuple[tuple[Any, Any], ...]
    probes: tuple[tuple[Any, Any], ...]

    def check(self, actor: StaffActorContext) -> bool:
        if actor != self.actor:
            return False
        try:
            with validation_batch() as batch:
                valid = batch.check(self)
            return valid
        except Exception:
            return False

    def __repr__(self) -> str:
        return "ReadEvidence(<private>)"

    def __reduce_ex__(self, protocol: object):
        raise TypeError("read evidence cannot be persisted")
