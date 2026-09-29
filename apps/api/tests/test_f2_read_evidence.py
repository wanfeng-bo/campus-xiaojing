from datetime import UTC, datetime, timedelta

import pytest

from app.composition.school_fact_read_evidence import ReadEvidence, validation_batch
from app.platform.assistant.runtime import AssistantRuntimeError
from tests.test_f2_directory_product import actor

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.mark.parametrize("combined", [False, True])
def test_self_authority_keeps_real_login_binding_through_release_recheck(combined):
    from types import SimpleNamespace

    from app.composition.school_fact_directory_context import current_directory_actor_matches
    from app.composition.school_fact_directory_gateway import (
        CombinedFactTurnGateway,
        _DirectoryTurn,
    )

    identity = SimpleNamespace(actor=actor())
    live = {"login": identity}

    def resolve(reference):
        return live[reference]

    class SelfBackend(Backend):
        def decision(self, request):
            return self.authority if current_directory_actor_matches(actor(), resolve) else "denied"

    backend = SelfBackend()
    proof = evidence(backend)
    turn = _DirectoryTurn(SimpleNamespace(resolve_actor=resolve, descriptors=()), "login")
    turn._checks.append(lambda identity: proof.check(identity.actor))
    gateway = CombinedFactTurnGateway(turn) if combined else turn
    with gateway.validation_scope():
        gateway.revalidate("login")
        assert proof.check(actor())
    assert not current_directory_actor_matches(actor(), resolve)

    # A new release must resolve the actual session again; no login survives logout.
    with pytest.raises(AssistantRuntimeError):
        with gateway.validation_scope():
            gateway.revalidate("login")
            live.clear()


def test_combined_gateway_does_not_merge_different_login_contexts():
    from types import SimpleNamespace

    from app.composition.school_fact_directory_gateway import (
        CombinedFactTurnGateway,
        _DirectoryTurn,
    )

    owner = SimpleNamespace(resolve_actor=lambda _: SimpleNamespace(actor=actor()), descriptors=())
    with pytest.raises(ValueError):
        CombinedFactTurnGateway(_DirectoryTurn(owner, "one"), _DirectoryTurn(owner, "two"))


class Backend:
    def __init__(self):
        self.now = NOW
        self.revision = 4
        self.authority = "allowed-scope-a"
        self.members = (("row-a", 1),)
        self.calls = []

    def clock(self):
        return self.now

    def version(self, who):
        self.calls.append("version")
        return self.revision

    def decision(self, request):
        self.calls.append("decision")
        return self.authority

    def metadata(self, probe):
        self.calls.append("metadata")
        return self.members


def evidence(backend, *, deadline=None):
    return ReadEvidence(
        backend=backend,
        actor=actor(),
        version=4,
        observed_at=NOW,
        valid_before=deadline,
        decisions=(("request-a", "allowed-scope-a"),),
        probes=(("query-a", (("row-a", 1),)),),
    )


def test_only_one_fresh_authority_pair_and_metadata_per_release_batch():
    backend = Backend()
    proof = evidence(backend)
    with validation_batch():
        assert proof.check(actor())
        assert proof.check(actor())
    assert backend.calls.count("decision") == 2
    assert backend.calls.count("metadata") == 2
    with validation_batch():
        assert proof.check(actor())
    assert backend.calls.count("decision") == 4


def test_release_batches_exact_decisions_and_rereads_after_metadata_change():
    from dataclasses import replace

    class BatchBackend(Backend):
        def decisions(self, requests):
            self.calls.append(("batch", requests))
            return tuple(self.authority for _ in requests)

    backend = BatchBackend()
    proof = replace(
        evidence(backend),
        decisions=tuple((request, backend.authority) for request in ("one", "two", "three")),
    )
    assert proof.check(actor())
    assert [call for call in backend.calls if isinstance(call, tuple)] == [
        ("batch", ("one", "two", "three")),
        ("batch", ("one", "two", "three")),
    ]
    metadata = backend.metadata

    def changed(probe):
        value = metadata(probe)
        backend.authority = "denied"
        return value

    backend.metadata = changed
    assert not proof.check(actor())


@pytest.mark.parametrize(
    "change", ["revoke", "new_grant", "version", "membership", "clock", "expiry"]
)
def test_changed_authority_or_fact_or_time_cannot_reuse(change):
    backend = Backend()
    proof = evidence(backend, deadline=NOW + timedelta(seconds=5))
    if change == "revoke":
        backend.authority = "denied"
    if change == "new_grant":
        backend.authority = "allowed-wider"
    if change == "version":
        backend.revision += 1
    if change == "membership":
        backend.members = (("row-a", 1), ("row-b", 1))
    if change == "clock":
        backend.now -= timedelta(seconds=1)
    if change == "expiry":
        backend.now += timedelta(seconds=5)
    assert not proof.check(actor())


def test_authority_change_during_metadata_stops_release():
    backend = Backend()
    proof = evidence(backend)
    original = backend.metadata

    def metadata(probe):
        value = original(probe)
        backend.authority = "denied"
        return value

    backend.metadata = metadata
    assert not proof.check(actor())


def test_identity_is_not_reused_and_body_is_not_part_of_proof_repr():
    backend = Backend()
    proof = evidence(backend)
    assert not proof.check(
        __import__(
            "app.platform.contracts", fromlist=["_issue_staff_actor_context"]
        )._issue_staff_actor_context(
            **{**__import__("dataclasses").asdict(actor()), "account_user_id": 8}
        )
    )
    assert backend.calls == []
    assert repr(proof) == "ReadEvidence(<private>)"
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(proof)


@pytest.mark.parametrize(
    "kind", ["person", "policy_version", "location", "organization_campus_relationship"]
)
def test_metadata_reuses_scoped_relation_without_body_projection(kind):
    from sqlalchemy.dialects import postgresql

    from tests.test_f2_reader_temporal_metadata import relation

    compiled = relation(kind)
    metadata = compiled.metadata_statement
    assert metadata is not None
    assert set(metadata.selected_columns.keys()) == {
        "row_id",
        "content_row_id",
        "name_row_id",
        "name_version",
        "publication_id",
        "fact_version",
    }
    selected = str(metadata).split("FROM", 1)[0]
    assert "content_text" not in selected and "contact_value" not in selected
    assert metadata.get_final_froms() == compiled.data_statement.get_final_froms()
    assert metadata._where_criteria == compiled.data_statement._where_criteria
    sql = str(metadata.compile(dialect=postgresql.dialect()))
    if kind == "person":
        assert "school_person_names.version AS name_version" in sql


def test_partial_quality_or_access_source_failure_has_no_complete_proof():
    from app.composition.school_fact_directory_gateway import _evidence_complete

    assert not _evidence_complete(
        {"status": "known", "quality": {"evidence_status": "source_unavailable"}}
    )
    assert not _evidence_complete({"families": [{"status": "source_unavailable"}]})
    assert _evidence_complete({"status": "known", "quality": {"evidence_status": "forbidden"}})


def test_each_reader_marks_current_self_probe_inside_historical_capture(monkeypatch):
    from types import SimpleNamespace

    from app.composition.school_fact_read_capture import CapturingReader, _capture, _current_query
    from app.platform.authorization.secure_reader_service import SecureReader

    seen = []
    monkeypatch.setattr(SecureReader, "read", lambda self, req: seen.append(_current_query.get()))
    reader = object.__new__(CapturingReader)
    token = _capture.set(SimpleNamespace(current_time=False))
    try:
        reader.read(SimpleNamespace(factual_at=NOW))
        reader.read(SimpleNamespace(factual_at=None))
    finally:
        _capture.reset(token)
    assert seen == [False, True]


def test_natural_membership_change_during_batch_cannot_release():
    backend = Backend()
    proof = evidence(backend)
    from app.platform.assistant.runtime import AssistantRuntimeError

    with pytest.raises(AssistantRuntimeError, match="membership"):
        with validation_batch():
            assert proof.check(actor())
            backend.members = (("new-future-row", 1),)


def test_empty_membership_must_stay_empty_and_denied_dependency_must_stay_denied():
    backend = Backend()
    backend.members = ()
    backend.authority = "denied"
    proof = ReadEvidence(
        backend, actor(), 4, NOW, None, (("request-a", "denied"),), (("query-a", ()),)
    )
    assert proof.check(actor())
    backend.authority = "allowed-scope-a"
    assert not proof.check(actor())
    backend.authority = "denied"
    backend.members = (("new-row", 1),)
    assert not proof.check(actor())


def test_owner_root_round_reuses_only_exact_owner_and_time_and_rechecks_exit(monkeypatch):
    from uuid import uuid4

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )

    calls = []
    state = ["version-1"]
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies, "_root", lambda self, **kw: (calls.append(kw), state[0])[1]
    )
    owner = object()
    dep = EvidenceGovernanceDependencies(None, owner=owner)
    fresh = ReadOnlyGovernanceDependencies(None)
    kw = dict(school_id=uuid4(), root_id=uuid4(), kind="person", as_of=NOW)
    with owner_root_round(owner, actor(), NOW, fresh, lambda: NOW):
        assert dep._root(**kw) == dep._root(**kw) == "version-1"
        assert len(calls) == 1
    assert len(calls) == 2
    dep._root(**kw)
    assert len(calls) == 3
    with pytest.raises(Exception, match="root changed"):
        with owner_root_round(owner, actor(), NOW, fresh, lambda: NOW + timedelta(seconds=1)):
            dep._root(**kw)
            state[0] = None


def test_root_round_does_not_reuse_across_backend_or_timestamp(monkeypatch):
    from uuid import uuid4

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )

    calls = []
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies, "_root", lambda self, **kw: (calls.append(kw), None)[1]
    )
    owner = object()
    same = EvidenceGovernanceDependencies(None, owner=owner)
    other = EvidenceGovernanceDependencies(None, owner=object())
    kw = dict(school_id=uuid4(), root_id=uuid4(), kind="person", as_of=NOW)
    with owner_root_round(owner, actor(), NOW, ReadOnlyGovernanceDependencies(None), lambda: NOW):
        same._root(**kw)
        other._root(**kw)
        other._root(**kw)
        same._root(**{**kw, "as_of": NOW + timedelta(seconds=1)})
        assert len(calls) == 4


def test_reader_post_compile_failure_cannot_seal_partial_evidence(monkeypatch):
    from types import SimpleNamespace

    from app.composition.school_fact_read_capture import CapturingReader, _capture, _read_progress
    from app.platform.authorization.secure_reader_service import SecureReader

    def failed(self, req):
        _read_progress.get()[0] = True
        raise RuntimeError("failed read")

    monkeypatch.setattr(SecureReader, "read", failed)
    capture = SimpleNamespace(current_time=False, supported=True)
    token = _capture.set(capture)
    try:
        with pytest.raises(RuntimeError):
            object.__new__(CapturingReader).read(SimpleNamespace(factual_at=NOW))
        assert not capture.supported
    finally:
        _capture.reset(token)


def test_governance_decisions_inside_root_round_keep_fresh_time(monkeypatch):
    from types import SimpleNamespace

    import app.composition.school_fact_read_capture as module

    seen = []
    backend = module.F2EvidenceBackend(
        sessions=None, thin=SimpleNamespace(decide=lambda request: seen.append(request.as_of))
    )
    request = SimpleNamespace(actor=actor(), as_of=NOW)
    monkeypatch.setattr(
        module, "replace", lambda value, **kw: SimpleNamespace(**{**vars(value), **kw})
    )
    monkeypatch.setattr(module, "decision_signature", lambda value: "ok")
    backend.clock = lambda: NOW + timedelta(seconds=5)
    with module.owner_root_round(
        backend.dependency_owner, actor(), NOW, SimpleNamespace(), backend.clock
    ):
        backend.decision(request)
    assert seen == [NOW + timedelta(seconds=5)]


def test_current_and_historical_original_keep_distinct_history_dependencies():
    from uuid import uuid4

    from app.composition.school_fact_directory_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
    from tests.test_f2_directory_gateway import _directory_read_turn

    class Query:
        def original(self, **kw):
            return {"kind": "policy_original", "reference": kw["reference"], "text": "same version"}

    _, turn, _ = _directory_read_turn(Query())
    context = ToolCallContext("private-actor", "dialogue", "turn", "run", 1, uuid4())
    results = []
    for index, args in enumerate(
        [
            {"reference": "f2:policy_version:abcdefgh"},
            {"reference": "f2:policy_version:abcdefgh", "as_of": NOW.isoformat()},
        ]
    ):
        results.append(
            turn.execute(
                ModelToolCall(call_id=str(index), name=MODULE + ".original", arguments=args),
                context,
            )
        )
    assert results[0].model_content == results[1].model_content
    assert results[0].history_check.key != results[1].history_check.key


def test_batch_shares_roots_across_proofs_but_rechecks_each_live_decision(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    import app.composition.school_fact_read_capture as module
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    roots = []
    decisions = []
    owner = object()
    dependency = module.EvidenceGovernanceDependencies(None, owner=owner)
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies,
        "_root",
        lambda self, **kw: (roots.append(kw), "root-v1")[1],
    )

    def decide(request):
        decisions.append(request)
        assert (
            dependency._root(
                school_id=actor().school_id, root_id="person-a", kind="person", as_of=request.as_of
            )
            == "root-v1"
        )
        return None

    backend = module.F2EvidenceBackend(
        sessions=None, thin=SimpleNamespace(decide=decide), dependency_owner=owner
    )
    monkeypatch.setattr(backend, "clock", lambda: NOW)
    monkeypatch.setattr(backend, "version", lambda who: 4)
    monkeypatch.setattr(backend, "decision_key", lambda request: request.name)
    monkeypatch.setattr(module, "decision_signature", lambda value: "allow")
    monkeypatch.setattr(
        module, "replace", lambda value, **kw: SimpleNamespace(**{**vars(value), **kw})
    )
    proof = ReadEvidence(backend, actor(), 4, NOW, None, (), ())
    with validation_batch():
        for name in ("location", "contact", "quality"):
            request = SimpleNamespace(actor=actor(), name=name, as_of=NOW)
            assert replace(proof, decisions=((request, "allow"),)).check(actor())
    assert len(decisions) == 6  # Every distinct request at entry AND exit.
    assert len(roots) == 2  # One shared snapshot, one independent fresh exit.
    with validation_batch():
        assert replace(proof, decisions=((request, "allow"),)).check(actor())
    assert len(roots) == 4  # Nothing survives a release batch / model wait.

    from dataclasses import asdict

    from app.platform.contracts import _issue_staff_actor_context

    other_actor = _issue_staff_actor_context(**{**asdict(actor()), "account_user_id": 8})
    other_request = SimpleNamespace(actor=other_actor, name="location", as_of=NOW)
    with validation_batch():
        assert replace(proof, decisions=((request, "allow"),)).check(actor())
        assert replace(proof, actor=other_actor, decisions=((other_request, "allow"),)).check(
            other_actor
        )
    assert len(roots) == 8  # Full actor boundaries each need their own root pair.


@pytest.mark.parametrize(
    "change", ["root", "expiry", "source_failure", "grant", "projection", "denied"]
)
def test_shared_root_batch_never_releases_changed_authority(monkeypatch, change):
    from dataclasses import replace
    from types import SimpleNamespace

    import app.composition.school_fact_read_capture as module
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.platform.assistant.runtime import AssistantRuntimeError

    state = {"root": "v1", "now": NOW, "decision": "deny" if change == "denied" else "allow"}
    owner = object()
    dependency = module.EvidenceGovernanceDependencies(None, owner=owner)

    def root(self, **kw):
        if state["root"] == "error":
            raise RuntimeError("root source failed")
        if kw["as_of"] >= NOW + timedelta(seconds=1):
            return None
        return state["root"]

    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", root)

    def decide(request):
        dependency._root(
            school_id=actor().school_id, root_id="person-a", kind="person", as_of=request.as_of
        )
        return state["decision"]

    backend = module.F2EvidenceBackend(
        sessions=None, thin=SimpleNamespace(decide=decide), dependency_owner=owner
    )
    monkeypatch.setattr(backend, "clock", lambda: state["now"])
    monkeypatch.setattr(backend, "version", lambda who: 4)
    monkeypatch.setattr(backend, "decision_key", lambda request: request.name)
    monkeypatch.setattr(module, "decision_signature", lambda value: value)
    monkeypatch.setattr(
        module, "replace", lambda value, **kw: SimpleNamespace(**{**vars(value), **kw})
    )
    request = SimpleNamespace(actor=actor(), name="location", as_of=NOW)
    proof = ReadEvidence(backend, actor(), 4, NOW, None, ((request, state["decision"]),), ())
    with pytest.raises((AssistantRuntimeError, RuntimeError)):
        with validation_batch():
            assert proof.check(actor())
            if change == "root":
                state["root"] = "v2"
            elif change == "expiry":
                state["now"] = NOW + timedelta(seconds=1)
            elif change == "source_failure":
                state["root"] = "error"
            else:
                state["decision"] = {
                    "grant": "deny",
                    "projection": "allow-fewer-fields",
                    "denied": "allow",
                }[change]
            # Different proof consults the same root; final guard must still fail.
            next_request = SimpleNamespace(actor=actor(), name="quality", as_of=NOW)
            assert replace(proof, decisions=((next_request, state["decision"]),)).check(actor())
    assert module._root_round.get() is None
    state.update(root="v1", now=NOW, decision=proof.decisions[0][1])
    assert proof.check(actor())  # Failure left no active/shared snapshots behind.


def test_owner_root_round_serializes_parallel_load_of_the_same_snapshot(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from threading import Barrier
    from time import sleep

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )

    roots = []
    pair = Barrier(2)
    owner = object()
    dependency = EvidenceGovernanceDependencies(None, owner=owner)

    def root(self, **kw):
        roots.append(kw)
        sleep(0.03)  # Both workers try the same unloaded snapshot before IO completes.
        return "v1"

    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", root)

    def read():
        pair.wait(timeout=2)
        return dependency._root(
            school_id=actor().school_id, root_id=actor().person_id, kind="person", as_of=NOW
        )

    with owner_root_round(owner, actor(), NOW, ReadOnlyGovernanceDependencies(None), lambda: NOW):
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [workers.submit(copy_context().run, read) for _ in range(2)]
            assert [future.result(timeout=2) for future in pending] == ["v1", "v1"]
        assert len(roots) == 1
    assert len(roots) == 2


def test_scoped_root_finish_opens_only_at_exit_and_checks_every_root(monkeypatch):
    from contextlib import contextmanager

    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies
    from app.composition.school_fact_read_capture import (
        EvidenceGovernanceDependencies,
        owner_root_round,
    )

    calls = []
    fresh = ReadOnlyGovernanceDependencies(None)
    owner = object()
    dependency = EvidenceGovernanceDependencies(None, owner=owner)
    monkeypatch.setattr(
        ReadOnlyGovernanceDependencies,
        "_root",
        lambda self, **kw: (calls.append((self is fresh, kw["root_id"])), "v1")[1],
    )

    @contextmanager
    def finish_scope():
        calls.append("open")
        try:
            yield fresh
        finally:
            calls.append("close")

    for _ in range(2):
        before = len(calls)
        with owner_root_round(
            owner,
            actor(),
            NOW,
            ReadOnlyGovernanceDependencies(None),
            lambda: NOW,
            fresh_scope=finish_scope,
        ):
            for root in ("one", "two", "one"):
                dependency._root(
                    school_id=actor().school_id, root_id=root, kind="person", as_of=NOW
                )
            assert calls[before:] == [(False, "one"), (False, "two")]
        assert calls[before:] == [
            (False, "one"),
            (False, "two"),
            "open",
            (True, "one"),
            (True, "two"),
            "close",
        ]


@pytest.mark.parametrize("failure", ["root_change", "exception"])
def test_scoped_finish_closes_and_clears_on_failure(monkeypatch, failure):
    from contextlib import contextmanager

    import app.composition.school_fact_read_capture as module
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    owner = object()
    dependency = module.EvidenceGovernanceDependencies(None, owner=owner)
    state = ["v1"]
    events = []
    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", lambda *args, **kw: state[0])

    @contextmanager
    def finish_scope():
        events.append("open")
        try:
            if failure == "exception":
                raise RuntimeError("scope unavailable")
            yield ReadOnlyGovernanceDependencies(None)
        finally:
            events.append("close")

    with pytest.raises((module.AssistantRuntimeError, RuntimeError)):
        with module.owner_root_round(
            owner,
            actor(),
            NOW,
            ReadOnlyGovernanceDependencies(None),
            lambda: NOW,
            fresh_scope=finish_scope,
        ):
            dependency._root(school_id=actor().school_id, root_id="one", kind="person", as_of=NOW)
            state[0] = "v2"
    assert events == ["open", "close"]
    assert module._root_round.get() is None


def test_fresh_finish_scope_reuses_connection_with_new_sessions_and_no_identity_map(monkeypatch):
    from sqlalchemy import Column, Integer, create_engine, event, text
    from sqlalchemy.orm import declarative_base, sessionmaker

    import app.composition.school_fact_directory_services as module

    engine = create_engine("sqlite://")
    # In-memory SQLite proves lifecycle/identity-map isolation, not PG visibility.
    monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: "READ COMMITTED")
    base = declarative_base()

    class Root(base):
        __tablename__ = "root"
        id = Column(Integer, primary_key=True)
        version = Column(Integer)

    base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("insert into root values (1,1)"))
    counts = []
    event.listen(engine, "checkout", lambda *_: counts.append("out"))
    event.listen(engine, "checkin", lambda *_: counts.append("in"))
    sessions = sessionmaker(bind=engine)
    seen = []
    try:
        for _ in range(2):
            with module._fresh_root_scope(sessions) as fresh:
                with fresh._read_sessions.begin() as first:
                    seen.append(first)
                    original = first.get(Root, 1)
                    assert original.version == 1
                    first.connection().execute(text("update root set version=2 where id=1"))
                with fresh._read_sessions.begin() as second:
                    seen.append(second)
                    current = second.get(Root, 1)
                    assert current is not original and current.version == 2
            assert counts[-2:] == ["out", "in"]
        assert counts == ["out", "in", "out", "in"]
        assert len({id(session) for session in seen}) == 4
    finally:
        engine.dispose()


def test_fresh_finish_scope_rejects_wrong_isolation_and_broken_transaction(monkeypatch):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker

    import app.composition.school_fact_directory_services as module

    engine = create_engine("sqlite://")
    isolation = ["REPEATABLE READ"]
    monkeypatch.setattr(engine.dialect, "get_isolation_level", lambda _: isolation[0])
    returns = []
    event.listen(engine, "checkin", lambda *_: returns.append(True))
    sessions = sessionmaker(bind=engine)
    try:
        with pytest.raises(Exception, match="READ COMMITTED"):
            with module._fresh_root_scope(sessions):
                pytest.fail("invalid isolation must not yield")
        isolation[0] = "READ COMMITTED"
        with pytest.raises(Exception, match="transaction"):
            with module._fresh_root_scope(sessions) as fresh:
                with fresh._read_sessions() as reader:
                    reader.scalar(text("select 1"))
                    reader.rollback()
        assert len(returns) == 2
    finally:
        engine.dispose()


def test_evidence_round_opens_separate_short_decision_and_fresh_root_scopes(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import app.composition.school_fact_read_capture as module
    from app.composition.school_fact_product_services import ReadOnlyGovernanceDependencies

    events = []
    owner = object()
    dep = module.EvidenceGovernanceDependencies(None, owner=owner)
    monkeypatch.setattr(ReadOnlyGovernanceDependencies, "_root", lambda self, **kw: "v1")

    @contextmanager
    def decision_scope(who):
        assert who == actor()
        events.append("decision-open")
        try:
            yield
        finally:
            events.append("decision-close")

    @contextmanager
    def fresh_scope():
        events.append("root-open")
        try:
            yield ReadOnlyGovernanceDependencies(None)
        finally:
            events.append("root-close")

    def decide(request):
        assert events[-1] in ("decision-open", "decision")
        events.append("decision")
        dep._root(school_id=actor().school_id, root_id="person", kind="person", as_of=request.as_of)
        return "allow"

    backend = module.F2EvidenceBackend(
        sessions=None,
        thin=SimpleNamespace(decide=decide),
        dependency_owner=owner,
        decision_scope=decision_scope,
        fresh_scope=fresh_scope,
    )
    monkeypatch.setattr(backend, "clock", lambda: NOW)
    monkeypatch.setattr(backend, "version", lambda _: 4)
    monkeypatch.setattr(backend, "decision_key", lambda request: request.name)
    monkeypatch.setattr(module, "decision_signature", lambda value: value)
    monkeypatch.setattr(
        module, "replace", lambda value, **kw: SimpleNamespace(**{**vars(value), **kw})
    )
    requests = tuple(
        (SimpleNamespace(actor=actor(), name=name, as_of=NOW), "allow")
        for name in ("location", "quality")
    )
    proof = ReadEvidence(backend, actor(), 4, NOW, None, requests, ())
    with validation_batch():
        assert proof.check(actor())
        assert events == ["decision-open", "decision", "decision", "decision-close"]
    assert events == ["decision-open", "decision", "decision", "decision-close"] * 2 + [
        "root-open",
        "root-close",
    ]
    assert module._root_round.get() is None
