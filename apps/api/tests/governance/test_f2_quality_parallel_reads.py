from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Barrier, Event, Lock
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import ReadAuthorization
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f2_read_authorization import F2ReadAuthorizationAdapter
from tests.test_secure_reader_projection import _actor


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
def test_independent_quality_reads_overlap_without_losing_identity_or_checks(phase):
    actor = _actor()
    identity = ContextVar("test_quality_identity", default=None)
    identity.set("request-one")
    lock, pair = Lock(), Event()
    started = []
    timed_out = []
    observed_contexts = []
    live = {"allowed": True}
    enabled = {"phase": phase if phase == "decide" else None}

    def checkpoint(kind, operation):
        if enabled["phase"] != operation or operation == "decide" and kind == "fact_quality":
            return
        with lock:
            started.append(kind)
            position = len(started)
            observed_contexts.append(identity.get())
            if position == 2:
                pair.set()
        if position == 1 and not pair.wait(0.5):
            timed_out.append(kind)

    class Base:
        def decide(self, *, operation, actor, as_of):
            kind = operation.code.split(".")[-2]
            checkpoint(kind, "decide")
            scope = F2FactScope(actor.school_id, kind, ("one",))
            return ReadAuthorization(
                True,
                kind,
                ScopePlan(uuid5(NAMESPACE_URL, kind), "school_facts.f2", scope.reference, 1),
                NamedProjection(operation.projection_name, 1),
                kind,
            )

        def revalidate(self, authorization, *, actor, as_of):
            checkpoint(authorization.basis, "revalidate")
            return live["allowed"]

    adapter = F2ReadAuthorizationAdapter(
        thin_service=object(),
        scope_id_resolver=lambda *args: uuid5(NAMESPACE_URL, "scope"),
        quality_target_kind="organization_profile",
    )
    adapter._base = cast(ThinAuthorizationReadAdapter, Base())
    operation = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    authorization = adapter.decide(operation=operation, actor=actor, as_of=datetime.now(UTC))
    assert authorization.allowed
    if phase == "revalidate":
        enabled["phase"] = phase
        assert adapter.revalidate(authorization, actor=actor, as_of=datetime.now(UTC))
    assert not timed_out, "Independent reads still serialize their IO waits"
    assert len(started) == (5 if phase == "decide" else 6)
    assert observed_contexts == ["request-one"] * len(started)
    identity.set("request-two")
    live["allowed"] = False
    assert not adapter.revalidate(authorization, actor=actor, as_of=datetime.now(UTC))


# Independent concurrency safety regression: exercise owner callbacks through the real adapter.
_TARGETS = ("location", "organization", "organization_profile", "person", "school")


def _quality_call(phase, check):
    actor = _actor()
    active = False

    class Base:
        def decide(self, *, operation, actor, as_of):
            kind = operation.code.split(".")[-2]
            allowed = check(kind) if active and phase == "decide" else True
            scope = F2FactScope(actor.school_id, kind, ("one",))
            return ReadAuthorization(
                allowed,
                kind,
                ScopePlan(uuid5(NAMESPACE_URL, kind), "school_facts.f2", scope.reference, 1),
                NamedProjection(operation.projection_name, 1),
                kind,
            )

        def revalidate(self, authorization, *, actor, as_of):
            return check(authorization.basis)

    adapter = F2ReadAuthorizationAdapter(
        thin_service=object(),
        scope_id_resolver=lambda *args: uuid5(NAMESPACE_URL, "scope"),
        quality_target_kind="organization_profile",
    )
    adapter._base = cast(ThinAuthorizationReadAdapter, Base())
    operation = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    authorization = adapter.decide(operation=operation, actor=actor, as_of=datetime.now(UTC))
    active = True

    def invoke():
        if phase == "decide":
            return adapter.decide(operation=operation, actor=actor, as_of=datetime.now(UTC)).allowed
        return adapter.revalidate(authorization, actor=actor, as_of=datetime.now(UTC))

    return invoke


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
def test_quality_exception_precedence_follows_dependency_order_not_completion(phase):
    later_finished = Event()
    first_error, second_error = RuntimeError("earlier"), ValueError("later")

    def check(kind):
        if kind == "location":
            assert later_finished.wait(2)
            raise first_error
        if kind == "organization":
            later_finished.set()
            raise second_error
        return True

    with pytest.raises(RuntimeError) as caught:
        _quality_call(phase, check)()
    assert caught.value is first_error


@pytest.mark.parametrize("false_first", [False, True])
def test_revalidation_preserves_ordered_false_and_exception_precedence(false_first):
    error = RuntimeError("authority unavailable")
    decisions = {
        "fact_quality": False if false_first else error,
        "location": error if false_first else False,
    }

    def check(kind):
        result = decisions.get(kind, True)
        if isinstance(result, Exception):
            raise result
        return result

    invoke = _quality_call("revalidate", check)
    if false_first:
        assert invoke() is False
    else:
        with pytest.raises(RuntimeError) as caught:
            invoke()
        assert caught.value is error


def test_quality_target_denial_does_not_hide_later_owner_error():
    error = RuntimeError("school unavailable")

    def check(kind):
        if kind == "school":
            raise error
        return kind != "organization_profile"

    with pytest.raises(RuntimeError) as caught:
        _quality_call("decide", check)()
    assert caught.value is error


def test_quality_original_denial_submits_no_dependency_reads():
    seen = []

    def check(kind):
        seen.append(kind)
        return False

    assert not _quality_call("decide", check)()
    assert seen == ["fact_quality"]


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
@pytest.mark.parametrize("outcome", ["deny", "error"])
def test_quality_waits_for_all_submitted_reads_on_denial_or_error(phase, outcome):
    blocked, release, done = Event(), Event(), Event()
    lock = Lock()
    completed = []
    error = RuntimeError("owner unavailable")
    first = "location" if phase == "decide" else "fact_quality"

    def check(kind):
        try:
            if kind == "school":
                blocked.set()
                assert release.wait(3)
            if kind == first and outcome == "error":
                raise error
            return kind != ("organization_profile" if phase == "decide" else first)
        finally:
            with lock:
                completed.append(kind)

    invoke = _quality_call(phase, check)

    def run():
        try:
            return invoke()
        finally:
            done.set()

    with ThreadPoolExecutor(max_workers=1) as caller:
        pending = caller.submit(run)
        try:
            assert blocked.wait(2), "Every queued owner must run, including after early failure"
            assert not done.wait(0.05), "Caller returned while an owner was still executing"
        finally:
            release.set()
        if outcome == "error":
            with pytest.raises(RuntimeError) as caught:
                pending.result(timeout=2)
            assert caught.value is error
        else:
            assert pending.result(timeout=2) is False
    assert sorted(completed) == sorted(("fact_quality", *_TARGETS))


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
def test_quality_limits_concurrent_owner_reads_to_two(phase):
    pair, release, over_limit = Event(), Event(), Event()
    lock = Lock()
    active, maximum, completed = 0, 0, []

    def check(kind):
        nonlocal active, maximum
        if phase == "decide" and kind == "fact_quality":
            return True
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active > 2:
                over_limit.set()
            if active == 2:
                pair.set()
        try:
            assert release.wait(3)
            return True
        finally:
            with lock:
                active -= 1
                completed.append(kind)

    invoke = _quality_call(phase, check)
    with ThreadPoolExecutor(max_workers=1) as caller:
        pending = caller.submit(invoke)
        try:
            assert pair.wait(2), "Two independent owner reads should overlap"
            assert not over_limit.wait(0.05), "More than two owner reads ran concurrently"
        finally:
            release.set()
        assert pending.result(timeout=2)
    assert maximum == 2
    assert len(completed) == (5 if phase == "decide" else 6)


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
def test_quality_simultaneous_requests_keep_actual_identity_contexts_isolated(phase):
    from app.composition.school_fact_directory_context import (
        _PRIVATE_IDENTITY,
        bind_directory_identity,
    )

    caller_value = ContextVar("quality_request_test", default=None)
    rendezvous = Barrier(2)
    lock = Lock()
    observed = []

    def check(kind):
        request = _PRIVATE_IDENTITY.get()
        inherited = caller_value.get()
        if kind == "location":
            rendezvous.wait(timeout=3)
        with lock:
            observed.append((request, inherited, kind))
        if kind != "fact_quality" or phase == "revalidate":
            caller_value.set("worker-local")
        return True

    invoke = _quality_call(phase, check)

    def request(reference):
        with bind_directory_identity(reference):
            token = caller_value.set(reference)
            try:
                assert invoke()
                assert caller_value.get() == reference
                assert _PRIVATE_IDENTITY.get() == reference
            finally:
                caller_value.reset(token)
        assert _PRIVATE_IDENTITY.get() is None

    with ThreadPoolExecutor(max_workers=2) as callers:
        futures = [callers.submit(request, ref) for ref in ("request-one", "request-two")]
        for future in futures:
            future.result(timeout=5)
    for reference in ("request-one", "request-two"):
        assert sorted(
            (value, kind) for identity, value, kind in observed if identity == reference
        ) == [(reference, kind) for kind in sorted(("fact_quality", *_TARGETS))]


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
def test_quality_rechecks_specific_revoked_owner_on_every_call(phase):
    live = {"organization_profile": True}
    lock = Lock()
    calls = []

    def check(kind):
        with lock:
            calls.append(kind)
            return live.get(kind, True)

    invoke = _quality_call(phase, check)
    assert invoke()
    live["organization_profile"] = False
    assert not invoke()
    live["organization_profile"] = True
    assert invoke()
    assert sorted(calls) == sorted(["fact_quality", *_TARGETS] * 3)


def _root_phase_adapter(monkeypatch):
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )

    actor = _actor()
    now = datetime(2026, 9, 8, tzinfo=UTC)
    owner = object()
    state = {"root": "v1", "now": now, "hook": lambda *args: None, "owner": owner}
    roots, decisions = [], []
    dependency = EvidenceGovernanceDependencies(None, owner=owner)

    def root(self, **kwargs):
        roots.append(kwargs)
        value = state["root"]
        if state.get("expires") is not None and kwargs["as_of"] >= state["expires"]:
            return None
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", root)

    def check(kind, actor, as_of):
        decisions.append((kind, actor, as_of))
        value = dependency._root(
            school_id=actor.school_id, root_id=actor.person_id, kind="person", as_of=as_of
        )
        state["hook"](kind, actor, as_of)
        return value is not None

    class Base:
        def decide(self, *, operation, actor, as_of):
            kind = operation.code.split(".")[-2]
            allowed = check(kind, actor, as_of)
            scope = F2FactScope(actor.school_id, kind, ("one",))
            return ReadAuthorization(
                allowed,
                kind,
                ScopePlan(uuid5(NAMESPACE_URL, kind), "school_facts.f2", scope.reference, 1),
                NamedProjection(operation.projection_name, 1),
                kind,
            )

        def revalidate(self, authorization, *, actor, as_of):
            return check(authorization.basis, actor, as_of)

    adapter = F2ReadAuthorizationAdapter(
        thin_service=object(),
        scope_id_resolver=lambda *args: uuid5(NAMESPACE_URL, "scope"),
        quality_target_kind="organization_profile",
        authorization_round=lambda actor, at: owner_root_round(
            owner, actor, at, ReadOnlyGovernanceDependencies(None), lambda: state["now"]
        ),
    )
    adapter._base = cast(ThinAuthorizationReadAdapter, Base())
    operation = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    return adapter, operation, actor, state, roots, decisions


def test_quality_root_snapshots_are_shared_only_within_each_authorization_phase(monkeypatch):
    import app.composition.school_fact_read_capture as module

    adapter, operation, actor, state, roots, decisions = _root_phase_adapter(monkeypatch)
    auth = adapter.decide(operation=operation, actor=actor, as_of=state["now"])
    assert auth.allowed
    assert len(roots) == 2
    assert len(decisions) == 6
    assert module._root_round.get() is None
    for expected in (4, 6):
        assert adapter.revalidate(auth, actor=actor, as_of=state["now"])
        assert len(roots) == expected
        assert module._root_round.get() is None
    assert len(decisions) == 18
    state["root"] = None
    assert not adapter.revalidate(auth, actor=actor, as_of=state["now"])
    assert len(roots) == 8


@pytest.mark.parametrize("phase", ["decide", "revalidate"])
@pytest.mark.parametrize(
    "change", ["version", "expiry", "clock_rollback", "source_failure", "exception"]
)
def test_quality_phase_rejects_changed_roots_and_clears_context(monkeypatch, phase, change):
    from datetime import timedelta

    import app.composition.school_fact_read_capture as module

    adapter, operation, actor, state, roots, _ = _root_phase_adapter(monkeypatch)
    auth = adapter.decide(operation=operation, actor=actor, as_of=state["now"])
    start = state["now"]

    def change_root(kind, *_):
        if kind != "school":
            return
        if change == "exception":
            raise RuntimeError("dependency failed")
        if change == "expiry":
            state["expires"] = start + timedelta(seconds=1)
            state["now"] += timedelta(seconds=1)
        elif change == "clock_rollback":
            state["now"] -= timedelta(seconds=1)
        elif change == "source_failure":
            state["root"] = RuntimeError("root source failed")
        else:
            state["root"] = "v2"

    state["hook"] = change_root
    with pytest.raises((module.AssistantRuntimeError, RuntimeError)):
        if phase == "decide":
            adapter.decide(operation=operation, actor=actor, as_of=start)
        else:
            adapter.revalidate(auth, actor=actor, as_of=start)
    assert module._root_round.get() is None
    assert module._decision_clock.get() is None
    state.update(root="v3", expires=None, hook=lambda *args: None)
    before = len(roots)
    assert adapter.decide(operation=operation, actor=actor, as_of=state["now"]).allowed
    assert len(roots) == before + 2


def test_quality_phase_isolated_from_outer_release_round(monkeypatch):
    import app.composition.school_fact_read_capture as module
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    adapter, operation, actor, state, roots, _ = _root_phase_adapter(monkeypatch)
    dependency = module.EvidenceGovernanceDependencies(None, owner=state["owner"])
    with module.owner_root_round(
        state["owner"],
        actor,
        state["now"],
        ReadOnlyGovernanceDependencies(None),
        lambda: state["now"],
    ):
        previous = module._root_round.get()
        dependency._root(
            school_id=actor.school_id, root_id=actor.person_id, kind="person", as_of=state["now"]
        )
        assert len(roots) == 1
        state["root"] = "v2"
        assert adapter.decide(operation=operation, actor=actor, as_of=state["now"]).allowed
        assert len(roots) == 3  # Inner phase does not reuse the outer snapshot.
        assert module._root_round.get() is previous
        state["root"] = "v1"
    assert len(roots) == 4
    assert module._root_round.get() is None


def test_quality_root_phases_keep_simultaneous_actors_independent(monkeypatch):
    from dataclasses import asdict

    import app.composition.school_fact_read_capture as module
    from app.platform.contracts import _issue_staff_actor_context

    adapter, operation, actor, state, roots, decisions = _root_phase_adapter(monkeypatch)
    other_actor = _issue_staff_actor_context(**{**asdict(actor), "account_user_id": 8})
    pair = Barrier(2)

    def meet(kind, *_):
        if kind == "school":
            pair.wait(timeout=3)

    state["hook"] = meet

    def call(who):
        assert adapter.decide(operation=operation, actor=who, as_of=state["now"]).allowed
        assert module._root_round.get() is None
        assert module._decision_clock.get() is None

    with ThreadPoolExecutor(max_workers=2) as callers:
        pending = [callers.submit(call, who) for who in (actor, other_actor)]
        for future in pending:
            future.result(timeout=5)
    assert len(roots) == 4
    assert len(decisions) == 12
    assert sum(who == actor for _, who, _ in decisions) == 6
    assert sum(who == other_actor for _, who, _ in decisions) == 6
