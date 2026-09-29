"""A quality grant never substitutes for current exact F3 target authority."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f3_policy import F3FactScope
from tests.test_secure_reader_projection import _actor

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def fixture(*, quality=True, target=True, field=True, school=False):
    from app.composition.school_fact_family_quality import FamilyQualityAuthorizer

    actor = _actor()
    events = []
    alive = [True]
    quality_scope = F2FactScope(
        actor.school_id,
        "fact_quality",
        () if school else ("q:a",),
        coverage="school" if school else "exact",
    )
    target_scope = F3FactScope(
        actor.school_id,
        "student_school_relationship",
        () if school else (("student:school-a",) if target else ("student:school-b",)),
        (),
        ("enrollment_status",) if field else ("student_number",),
        coverage="school" if school else "exact",
    )

    class F2:
        def require_current(self, who, kind, *, purpose):
            events.append(("quality", kind, purpose))
            if not quality:
                raise PermissionError("no quality grant")
            return SimpleNamespace(scope_plan=SimpleNamespace(reference=quality_scope.reference))

        def revalidate(self, permit, who):
            events.append(("quality-revalidate",))
            if not alive[0]:
                raise PermissionError("revoked")

    class F3:
        def require_scope(self, who, action, kind, key, creation, checked_at):
            events.append(("target", kind, key, action))
            return SimpleNamespace(scope=target_scope, target=SimpleNamespace())

        def revalidate_permit(self, permit, who, target, checked_at):
            events.append(("target-revalidate",))
            if not alive[0]:
                raise PermissionError("revoked")

    return FamilyQualityAuthorizer(F2(), F3()), actor, events, alive


def test_read_requires_both_grants_and_revalidates_both_before_release():
    authorizer, actor, events, alive = fixture()
    proof = authorizer.acquire(
        actor=actor,
        quality_key="q:a",
        target_kind="student_school_relationship",
        target_key="student:school-a",
        field_names=("enrollment_status",),
        purpose="read",
        checked_at=NOW,
    )
    assert [event[0] for event in events] == ["quality", "target"]
    proof.revalidate(actor, NOW)
    assert [event[0] for event in events[-2:]] == ["quality-revalidate", "target-revalidate"]
    alive[0] = False
    with pytest.raises(PermissionError):
        proof.revalidate(actor, NOW)


def test_change_guard_requires_both_grants_for_exact_f3_field_and_replacement():
    from app.composition.school_fact_family_quality import FamilyQualityChangeGuard
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4

    actor = _actor()
    calls = []

    class Authorizer:
        def acquire(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(revalidate=lambda who, at: calls.append((who, at)))

    plan = CanonicalFactChangeSetV4.model_validate(
        {
            "schema_version": 4,
            "base_version": 1,
            "source_sha256": "a" * 64,
            "changes": (
                {
                    "schema_version": 4,
                    "operation": "upsert",
                    "fact_kind": "fact_quality",
                    "source_key": "q:a",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "f7", "row_number": 1},
                    "content": {
                        "quality_kind": "source",
                        "target_fact_kind": "student",
                        "owner_kind": "person",
                        "person_source_key": "person:a",
                        "target_source_key": "student:a",
                        "field_name": "status",
                        "basis_reference": "school publication",
                        "responsibility": "registrar",
                        "verified_at": NOW,
                        "source_status": "available",
                    },
                },
            ),
        }
    )
    guard = FamilyQualityChangeGuard(Authorizer(), clock=lambda: NOW)
    guard.require_change_set(actor, plan).revalidate()
    assert calls[0]["quality_key"] == "q:a"
    assert calls[0]["target_key"] == "student:a"
    assert calls[0]["field_names"] == ("status",)
    assert calls[0]["purpose"] == "maintain"
    assert calls[0]["quality_creation"] is True
    assert calls[1] == (actor, NOW)


def test_family_quality_final_guard_locks_epoch_and_checks_both_duties():
    from app.composition.school_fact_family_quality import FamilyQualityFinalGuard

    actor = _actor()
    checks = []

    def permit(epoch):
        return SimpleNamespace(
            basis=SimpleNamespace(governance=SimpleNamespace(assignment_epoch=epoch))
        )

    proof = SimpleNamespace(
        proofs=(SimpleNamespace(quality_permit=permit(8), target_permit=permit(8)),),
        revalidate=lambda: checks.append("revalidated"),
    )
    changes = SimpleNamespace(require_change_set=lambda *_: proof)
    session = SimpleNamespace(scalar=lambda statement, params: 8)
    FamilyQualityFinalGuard(changes)(session, SimpleNamespace(actor=actor), object())
    assert checks == ["revalidated"]
    bad_session = SimpleNamespace(scalar=lambda statement, params: 9)
    with pytest.raises(PermissionError):
        FamilyQualityFinalGuard(changes)(bad_session, SimpleNamespace(actor=actor), object())


def test_family_quality_writer_dispatch_routes_only_exact_f3_quality_changes():
    from app.composition.school_fact_family_quality import FamilyQualityWriterDispatch

    selected = []
    retained = []
    f3 = SimpleNamespace(
        fact_kind="fact_quality", content=SimpleNamespace(target_fact_kind="student")
    )
    f2 = SimpleNamespace(
        fact_kind="fact_quality", content=SimpleNamespace(target_fact_kind="official_contact")
    )
    plan = SimpleNamespace(
        changes=(f3, f2),
        model_copy=lambda *, update: SimpleNamespace(**update),
    )
    dispatch = FamilyQualityWriterDispatch(
        lambda session, context, p: selected.extend(p.changes),
        lambda session, context, p: retained.extend(p.changes),
    )
    dispatch(object(), object(), plan)
    assert selected == [f3]
    assert retained == [f2]


def test_existing_f3_quality_terminal_and_target_change_cannot_fall_through_retained_guard():
    from app.composition.school_fact_family_quality import FamilyQualityWriterDispatch

    selected = []
    retained = []
    old_f3 = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="quality:f3",
        expected_version=2,
        operation="end",
        content=None,
    )
    changed_owner = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="quality:f3",
        expected_version=2,
        operation="upsert",
        content=SimpleNamespace(target_fact_kind="official_contact"),
    )
    plan = SimpleNamespace(
        changes=(old_f3, changed_owner),
        model_copy=lambda *, update: SimpleNamespace(**update),
    )
    dispatch = FamilyQualityWriterDispatch(
        lambda session, context, p: selected.extend(p.changes),
        lambda session, context, p: retained.extend(p.changes),
        existing_target_kind=lambda actor, source: "student",
    )
    dispatch(object(), SimpleNamespace(actor=_actor()), plan)
    assert selected == [old_f3, changed_owner]
    assert retained == []


def test_existing_quality_without_verified_classification_fails_closed():
    from app.composition.school_fact_family_quality import FamilyQualityWriterDispatch

    terminal = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="quality:unknown",
        expected_version=1,
        operation="revoke",
        content=None,
    )
    plan = SimpleNamespace(
        changes=(terminal,), model_copy=lambda *, update: SimpleNamespace(**update)
    )
    dispatch = FamilyQualityWriterDispatch(
        lambda *_: pytest.fail("unverified F3 guard"),
        lambda *_: pytest.fail("unverified retained guard"),
    )
    with pytest.raises(PermissionError):
        dispatch(object(), SimpleNamespace(actor=_actor()), plan)


def test_existing_family_quality_requires_exact_old_target_and_current_version():
    from app.composition.school_fact_family_quality import FamilyQualityExistingContent

    actor = _actor()
    authorizer, _, events, alive = fixture()
    version = [12]
    projection = SimpleNamespace(
        quality_coordinates=lambda school, key: {
            "version": 2,
            "content": {
                "target_fact_kind": "student_school_relationship",
                "target_source_key": "student:school-a",
                "field_name": "enrollment_status",
                "owner_kind": "person",
            },
        },
        quality_detail=lambda school, key: {
            "version": 2,
            "content": {
                "target_fact_kind": "student_school_relationship",
                "target_source_key": "student:school-a",
                "field_name": "enrollment_status",
                "owner_kind": "person",
                "person_source_key": "person:a",
                "quality_kind": "source",
                "basis_reference": "正式来源",
            },
        },
        current_version=lambda school: version[0],
    )
    selected = FamilyQualityExistingContent(
        f2_guard=authorizer._f2_guard,
        authorizer=authorizer,
        projection=projection,
        clock=lambda: NOW,
    )
    assert selected.target_kind(actor, "q:a") == "student_school_relationship"
    previous = selected(actor, "q:a")
    assert previous["version"] == 2
    assert previous["content"]["person_source_key"] == "person:a"
    previous["proof"].revalidate()
    assert any(event[0] == "target-revalidate" for event in events)
    version[0] = 13
    with pytest.raises(PermissionError):
        previous["proof"].revalidate()
    version[0] = 12
    alive[0] = False
    with pytest.raises(PermissionError):
        selected(actor, "q:a")


@pytest.mark.parametrize("keys", [("q:a", "q:b"), (), None])
def test_family_target_quality_batches_only_authorized_keys_in_one_reader_call(keys):
    from contextlib import contextmanager

    from app.composition.school_fact_family_quality import (
        FamilyQualityAuthorizer,
        FamilyQualityReader,
    )

    actor = _actor()
    alive = [True]
    seen = []
    scope = (
        F2FactScope(actor.school_id, "fact_quality", (), coverage="school")
        if keys is None
        else F2FactScope(actor.school_id, "fact_quality", keys, () if keys else ("q:create",))
    )
    target_scope = F3FactScope(
        actor.school_id,
        "student_school_relationship",
        ("student:a",),
        (),
        ("enrollment_status",),
    )

    class F2:
        def require_current(self, who, kind, *, purpose):
            assert who == actor and kind == "fact_quality" and purpose == "read"
            return SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))

        def revalidate(self, permit, who):
            if not alive[0]:
                raise PermissionError("quality revoked")

    class F3:
        def require_scope(self, who, action, kind, key, creation, checked_at):
            assert action == "read" and kind == "student_school_relationship"
            assert key == "student:a" and not creation
            return SimpleNamespace(scope=target_scope, target=SimpleNamespace())

        def revalidate_permit(self, permit, who, target, checked_at):
            if not alive[0]:
                raise PermissionError("target revoked")

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield self
            after()

        def fetch(self, relation):
            seen.append(str(relation.data_statement))
            if "school_fact_scope_keys" in seen[-1]:
                page = sum("school_fact_scope_keys" in value for value in seen)
                return [
                    {
                        "payload": {
                            "school_fact_version": 4,
                            "keys": [f"q:{page}"],
                            "has_more": page == 1,
                            "next_after": "q:1" if page == 1 else None,
                        }
                    }
                ]
            return [{"payload": {"school_fact_version": 4, "records": []}}]

    reader = FamilyQualityReader(
        authorizer=FamilyQualityAuthorizer(F2(), F3()),
        executor=Executor(),
        current_version=lambda school: 4,
        clock=lambda: NOW,
    )
    result = reader.query_quality_for_target(
        actor=actor,
        target_kind="student_school_relationship",
        target_key="student:a",
        field_names=("enrollment_status",),
        as_of=NOW,
    )
    assert result.records == ()
    assert len(seen) == (4 if keys is None else (1 if keys else 0))
    result.revalidate(actor)
    alive[0] = False
    with pytest.raises(PermissionError):
        result.revalidate(actor)


def test_family_target_quality_rejects_cross_child_and_unowned_field_before_sql():
    authorizer, actor, _, _ = fixture()
    with pytest.raises(PermissionError):
        authorizer.acquire_batch(
            actor=actor,
            target_kind="student_school_relationship",
            target_key="student:other-child",
            field_names=("enrollment_status",),
            checked_at=NOW,
        )
    with pytest.raises(PermissionError):
        authorizer.acquire_batch(
            actor=actor,
            target_kind="student_school_relationship",
            target_key="student:school-a",
            field_names=("student_number",),
            checked_at=NOW,
        )


@pytest.mark.parametrize("failure", ["quality", "target", "field", "key", "kind"])
def test_no_quality_or_target_right_cannot_read_family_quality(failure):
    authorizer, actor, _, _ = fixture(
        quality=failure != "quality", target=failure != "target", field=failure != "field"
    )
    with pytest.raises((PermissionError, ValueError)):
        authorizer.acquire(
            actor=actor,
            quality_key="q:unowned" if failure == "key" else "q:a",
            target_kind="school" if failure == "kind" else "student_school_relationship",
            target_key="student:school-a",
            field_names=("enrollment_status",),
            purpose="read",
            checked_at=NOW,
        )


def test_guardian_quality_maintenance_requires_verifier_not_ordinary_maintainer():
    authorizer, actor, events, _ = fixture()
    with pytest.raises(PermissionError):
        authorizer.acquire(
            actor=actor,
            quality_key="q:a",
            target_kind="guardian_relationship",
            target_key="relation:a",
            field_names=("status",),
            purpose="maintain",
            checked_at=NOW,
        )
    assert any(event[0] == "target" and event[-1] == "verify" for event in events)


def test_family_quality_projection_is_exact_before_sql_and_revoked_before_release():
    from contextlib import contextmanager

    from app.composition.school_fact_family_quality import FamilyQualityReader

    authorizer, actor, events, alive = fixture()
    statements = []

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield self
            after()

        def fetch(self, relation):
            statements.append(str(relation.data_statement))
            return [
                {
                    "payload": {
                        "school_fact_version": 7,
                        "records": [
                            {
                                "quality_kind": "source",
                                "field_name": "enrollment_status",
                                "source_status": "available",
                                "coverage_status": None,
                                "dispute_status": None,
                                "review_due_at": None,
                                "verified_at": NOW.isoformat(),
                                "valid_from": NOW.isoformat(),
                                "valid_to": None,
                                "version": 1,
                            }
                        ],
                    }
                }
            ]

    reader = FamilyQualityReader(
        authorizer=authorizer,
        executor=Executor(),
        current_version=lambda school: 7,
        clock=lambda: NOW,
    )
    result = reader.read(
        actor=actor,
        quality_key="q:a",
        target_kind="student_school_relationship",
        target_key="student:school-a",
        field_names=("enrollment_status",),
        as_of=NOW,
    )
    assert result.records[0]["source_status"] == "available"
    assert "school_fact_f7_family_quality" in statements[0]
    assert [event[0] for event in events[:2]] == ["quality", "target"]
    result.revalidate(actor)
    alive[0] = False
    with pytest.raises(PermissionError):
        result.revalidate(actor)


def test_family_quality_reader_rejects_unrequested_column_from_projection():
    from contextlib import contextmanager

    from app.composition.school_fact_family_quality import FamilyQualityReader

    authorizer, actor, _, _ = fixture()

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield self
            after()

        def fetch(self, relation):
            return [
                {
                    "payload": {
                        "school_fact_version": 7,
                        "records": [{"field_name": "student_number", "quality_kind": "source"}],
                    }
                }
            ]

    reader = FamilyQualityReader(
        authorizer=authorizer,
        executor=Executor(),
        current_version=lambda school: 7,
        clock=lambda: NOW,
    )
    with pytest.raises(PermissionError):
        reader.read(
            actor=actor,
            quality_key="q:a",
            target_kind="student_school_relationship",
            target_key="student:school-a",
            field_names=("enrollment_status",),
            as_of=NOW,
        )


def test_school_quality_still_requires_target_fields_and_current_both_grants():
    authorizer, actor, _, alive = fixture(school=True)
    arguments = dict(
        actor=actor,
        quality_key="quality:future",
        target_kind="student_school_relationship",
        target_key="student:future",
        field_names=("enrollment_status",),
        purpose="read",
        checked_at=NOW,
    )
    proof = authorizer.acquire(**arguments)
    proof.revalidate(actor, NOW)
    with pytest.raises(PermissionError):
        authorizer.acquire(**{**arguments, "field_names": ("student_number",)})
    alive[0] = False
    with pytest.raises(PermissionError):
        proof.revalidate(actor, NOW)
    without_quality, _, _, _ = fixture(school=True, quality=False)
    with pytest.raises(PermissionError):
        without_quality.acquire(**arguments)
