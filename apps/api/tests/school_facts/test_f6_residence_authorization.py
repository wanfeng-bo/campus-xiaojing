"""F6 capabilities cannot be acquired from an F2/F3/F4/F5 scope."""

from importlib import import_module, util
from uuid import UUID

import pytest


def api():
    name = "app.platform.school_facts.f6_policy"
    assert util.find_spec(name), "F6 exact policy is not implemented"
    return import_module(name)


def test_f6_exact_scope_roundtrip_keeps_old_kinds_out():
    m = api()
    scope = m.F6FactScope(UUID(int=1), "student_bed_residence", ("r1",), (), ("bed_source_key",))
    assert m.F6FactScope.from_reference(scope.reference) == scope
    assert "teaching_assignment" not in m.F6_FACT_KINDS
    assert "person" not in m.F6_FACT_KINDS
    assert "student_name" not in m.F6_FACT_FIELDS["dorm_bed"]


def test_reserved_reads_are_explicit_finite_and_preserve_old_scope_hash():
    m = api()
    old = m.F6FactScope(UUID(int=1), "dorm_bed", ("bed-one",), (), ("display_name",))
    assert "reserved_read_keys" not in old.document
    scope = m.F6FactScope(
        UUID(int=1),
        "dorm_bed",
        ("bed-one",),
        (),
        ("display_name",),
        reserved_read_keys=("future-bed",),
    )
    assert m.F6FactScope.from_reference(scope.reference) == scope
    assert scope.readable_keys == ("bed-one", "future-bed")
    assert scope.document_sha256 != old.document_sha256
    for keys in (("*",), ("future", "future"), ("bed-one",), tuple(f"k{i}" for i in range(100))):
        with pytest.raises(ValueError):
            m.F6FactScope(
                UUID(int=1),
                "dorm_bed",
                ("bed-one",),
                (),
                ("display_name",),
                reserved_read_keys=keys,
            )


def test_reserved_read_does_not_authorize_maintenance_or_derive_from_creation():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority

    scope = api().F6FactScope(
        UUID(int=1), "dorm_bed", (), (), ("display_name",), reserved_read_keys=("future",)
    )
    ResidenceAuthority.require_object(
        SimpleNamespace(scope=scope, action="read"), "future", is_creation=False
    )
    for action, creation in (("maintain", False), ("maintain", True), ("read", True)):
        with pytest.raises(PermissionError):
            ResidenceAuthority.require_object(
                SimpleNamespace(scope=scope, action=action), "future", is_creation=creation
            )
    creation = api().F6FactScope(UUID(int=1), "dorm_bed", (), ("future",), ("display_name",))
    with pytest.raises(PermissionError):
        ResidenceAuthority.require_object(
            SimpleNamespace(scope=creation, action="read"), "future", is_creation=False
        )


def test_reserved_reference_reads_empty_then_same_exact_created_object():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.composition.school_fact_residence_reader import ResidenceRepository
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = api().F6FactScope(
        actor.school_id, "dorm_bed", (), (), ("display_name",), reserved_read_keys=("future",)
    )
    permit = SimpleNamespace(scope=scope, action="read")
    state = SimpleNamespace(created=False, version=1)

    def read(**kwargs):
        assert kwargs["source_keys"] == ("future",)
        return {
            "status": "available" if state.created else "no_current_records",
            "school_fact_version": state.version,
            "records": [
                {
                    "source_key": "future",
                    "fact_kind": "dorm_bed",
                    "content": {"display_name": "新床"},
                }
            ]
            if state.created
            else [],
            "next_boundary": None,
            "has_more": False,
        }

    repository = ResidenceRepository(
        authority=SimpleNamespace(
            require_scope=lambda *a, **k: permit,
            require_object=ResidenceAuthority.require_object,
            require_fields=ResidenceAuthority.require_fields,
            revalidate=lambda *a, **k: None,
        ),
        projection=SimpleNamespace(read=read, current_version=lambda *a: state.version),
        reference_key=b"reserved-independent-read-key",
        clock=lambda: NOW,
    )
    ref = repository.reference_for(actor, "dorm_bed", "future")
    assert repository.source_for_reference(actor, "dorm_bed", ref) == "future"
    empty = repository.read(actor, "dorm_bed")
    assert not empty.records
    state.created, state.version = True, 2
    with pytest.raises(RuntimeError):
        empty.proof.revalidate(actor=actor)
    assert repository.read(actor, "dorm_bed").records[0]["display_name"] == "新床"


def test_reserved_read_is_an_independent_own_result_permission():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = api().F6FactScope(
        actor.school_id, "dorm_bed", (), (), ("display_name",), reserved_read_keys=("future",)
    )
    authority = ResidenceAuthority(
        thin=None, configuration=SimpleNamespace(scope_bindings=[]), clock=lambda: NOW
    )

    def require_scope(actor, *, action, **kwargs):
        if action != "read":
            raise PermissionError()
        return SimpleNamespace(actor=actor, action=action, scope=scope)

    authority.require_scope = require_scope
    authority.revalidate = lambda *a, **kw: None
    target = frozenset({("dorm_bed", "future")})
    proof = authority.own_result(SimpleNamespace(actor=actor), targets=target)
    assert proof.allowed_targets == target and not proof.creation_only_targets


def test_creation_only_f6_audit_reference_cannot_select_fact_or_creation():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.composition.school_fact_residence_reader import ResidenceRepository
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = api().F6FactScope(actor.school_id, "dorm_bed", (), ("new-bed",), ("display_name",))
    state = SimpleNamespace(active=True)

    def require_scope(actor, *, action, **kwargs):
        if not state.active:
            raise PermissionError("revoked")
        return SimpleNamespace(action=action, scope=scope)

    repo = ResidenceRepository(
        authority=SimpleNamespace(
            require_scope=require_scope, require_object=ResidenceAuthority.require_object
        ),
        projection=None,
        reference_key=b"f6-audit-independent-private-key",
        clock=lambda: NOW,
    )
    reference = repo.creation_receipt_reference_for(
        actor=actor, fact_kind="dorm_bed", source_key="new-bed"
    )
    assert reference.startswith("f6audit:dorm_bed:")
    for creation in (False, True):
        with pytest.raises(PermissionError):
            repo.source_for_reference(actor, "dorm_bed", reference, creation=creation)
    from unittest.mock import Mock

    from app.composition.school_fact_product import read_receipt_current_residence

    gateway = Mock()
    current = read_receipt_current_residence(
        gateway, None, SimpleNamespace(fact_kind="dorm_bed", reference=reference), None
    )
    assert current.model_content["status"] == "unsupported"
    gateway.execute.assert_not_called()
    state.active = False
    with pytest.raises(PermissionError):
        repo.creation_receipt_reference_for(actor=actor, fact_kind="dorm_bed", source_key="new-bed")


def test_quality_page_context_reads_no_body_and_tracks_all_exact_f6_scopes():
    from datetime import timedelta
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.composition.school_fact_residence_reader import ResidenceRepository
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = api().F6FactScope(
        actor.school_id, "dorm_bed", (), (), ("display_name",), reserved_read_keys=("future",)
    )
    permit = SimpleNamespace(
        scope=scope,
        action="read",
        basis=SimpleNamespace(governance=SimpleNamespace(valid_until=NOW + timedelta(seconds=1))),
    )
    state = SimpleNamespace(now=NOW)

    def require_scope(actor, *, fact_kind, **kwargs):
        if fact_kind != "dorm_bed":
            raise PermissionError()
        return permit

    def read(**kwargs):
        assert kwargs["source_keys"] == () and kwargs["fields"] == ("display_name",)
        return {
            "status": "no_current_records",
            "records": [],
            "school_fact_version": 1,
            "has_more": False,
            "next_boundary": (NOW + timedelta(seconds=2)).isoformat(),
        }

    repository = ResidenceRepository(
        authority=SimpleNamespace(
            require_scope=require_scope,
            require_fields=ResidenceAuthority.require_fields,
            revalidate=lambda *a, **k: None,
        ),
        projection=SimpleNamespace(read=read, current_version=lambda _: 1),
        reference_key=b"quality-page-context-unit-private",
        clock=lambda: state.now,
    )
    context = repository.read_page_context(actor)
    assert context.expires == NOW + timedelta(seconds=1)
    context.proof.revalidate()
    state.now += timedelta(seconds=1)
    with pytest.raises(RuntimeError):
        context.proof.revalidate()


def test_quality_owners_are_explicit_and_do_not_change_old_scope_hashes():
    m = api()
    scope = m.F6FactScope(UUID(int=1), "student_bed_residence", ("r1",), (), ("bed_source_key",))
    assert "quality_owners" not in scope.document
    owner = m.F6FactScope(
        UUID(int=1),
        "student_bed_residence",
        (),
        (),
        ("bed_source_key",),
        quality_owners=(("person", "person-one"),),
    )
    assert m.F6FactScope.from_reference(owner.reference) == owner
    with pytest.raises(ValueError):
        m.F6FactScope(
            UUID(int=1),
            "dorm_bed",
            (),
            (),
            ("display_name",),
            quality_owners=(("person", "person-one"),),
        )


@pytest.mark.parametrize(
    "keys,fields",
    [
        (("*",), ("bed_source_key",)),
        (("r1",), ("private_mobile",)),
        (("r1", "r1"), ("bed_source_key",)),
    ],
)
def test_f6_rejects_wildcard_ambiguous_or_unowned_fields(keys, fields):
    m = api()
    with pytest.raises(ValueError):
        m.F6FactScope(UUID(int=1), "student_bed_residence", keys, (), fields)


def test_f6_settings_reject_duplicate_binding_or_previous_phase_operation():
    name = "app.composition.school_fact_residence_settings"
    assert util.find_spec(name), "F6 settings are not implemented"
    m = import_module(name)
    with pytest.raises(ValueError):
        m.ResidenceScopeBinding(
            assignment_id=UUID(int=1),
            scope_id=UUID(int=2),
            operation_code="school_facts.f5.teaching_assignment.read",
        )


def test_staff_repository_denies_before_projection_and_detects_stale_empty_result():
    from types import SimpleNamespace

    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    name = "app.composition.school_fact_residence_reader"
    assert util.find_spec(name), "F6 scoped repository is not implemented"
    module = import_module(name)
    m, actor = api(), _actor()
    state = SimpleNamespace(allowed=False, reads=0, version=8)
    permit = SimpleNamespace(
        actor=actor,
        scope=m.F6FactScope(actor.school_id, "dorm_bed", ("bed-one",), (), ("display_name",)),
    )

    class Authority:
        def require_scope(self, *args, **kwargs):
            if not state.allowed:
                raise PermissionError()
            return permit

        def revalidate(self, *args, **kwargs):
            if not state.allowed:
                raise PermissionError()

        def require_fields(self, p, fields):
            if not set(fields) <= set(p.scope.field_names):
                raise PermissionError()

        def require_object(self, p, key, *, is_creation):
            if key not in p.scope.object_keys:
                raise PermissionError()

    class Projection:
        def current_version(self, school):
            return state.version

        def read(self, **kwargs):
            state.reads += 1
            assert kwargs["source_keys"] == ("bed-one",)
            assert kwargs["fields"] == ("display_name",)
            return {
                "status": "no_current_records",
                "records": [],
                "school_fact_version": state.version,
                "as_of": NOW.isoformat(),
                "next_boundary": None,
                "has_more": False,
            }

    repository = module.ResidenceRepository(
        authority=Authority(),
        projection=Projection(),
        reference_key=b"f6-test-key-more-than-16-bytes",
        clock=lambda: NOW,
    )
    with pytest.raises(PermissionError):
        repository.read(actor, "dorm_bed")
    assert state.reads == 0
    state.allowed = True
    with pytest.raises(PermissionError):
        repository.read(actor, "dorm_bed", fields=("student_name",))
    assert state.reads == 0
    result = repository.read(actor, "dorm_bed")
    result.proof.revalidate(actor=actor)
    state.version += 1
    with pytest.raises(RuntimeError):
        result.proof.revalidate(actor=actor)


def test_staff_references_are_context_bound_and_never_accept_plain_keys():
    from types import SimpleNamespace

    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    name = "app.composition.school_fact_residence_reader"
    assert util.find_spec(name), "F6 references are not implemented"
    module, m, actor = import_module(name), api(), _actor()
    scope = m.F6FactScope(actor.school_id, "dorm_bed", ("bed-one",), (), ("display_name",))

    class Authority:
        def require_scope(self, *args, **kwargs):
            return SimpleNamespace(scope=scope)

        def require_object(self, permit, key, *, is_creation):
            if key not in scope.object_keys:
                raise PermissionError()

    repository = module.ResidenceRepository(
        authority=Authority(),
        projection=None,
        reference_key=b"f6-test-key-more-than-16-bytes",
        clock=lambda: NOW,
    )
    ref = repository.reference_for(actor, "dorm_bed", "bed-one")
    assert "bed-one" not in ref
    assert repository.source_for_reference(actor, "dorm_bed", ref) == "bed-one"
    with pytest.raises(PermissionError):
        repository.source_for_reference(actor, "dorm_bed", "bed-one")


def test_f6_reader_migration_is_additive_and_has_no_fact_write_api():
    from pathlib import Path

    file = Path(__file__).parents[2] / "alembic/versions/20260922_43_f6_residence_read.py"
    assert file.exists(), "F6 read SQL is not implemented"
    source = file.read_text(encoding="utf-8")
    assert 'down_revision = "20260922_42"' in source
    assert "SET search_path=pg_catalog" in source
    assert "family_relationship_is_authoritative" in source
    assert "superseded_at IS NULL" in source
    assert "REVOKE ALL ON FUNCTION" in source


def test_maintenance_requires_read_and_maintain_before_reading_full_timeline():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_reader import ResidenceRepository
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor, calls = _actor(), []
    scope = api().F6FactScope(
        actor.school_id, "dorm_bed", ("bed-one",), ("new-bed",), ("display_name",)
    )

    class Authority:
        def require_scope(self, actor, *, action, **kwargs):
            calls.append(action)
            return SimpleNamespace(scope=scope)

        def require_fields(self, p, fields):
            assert set(fields) <= set(scope.field_names)

        def require_object(self, p, key, *, is_creation):
            assert key in (scope.creation_keys if is_creation else scope.object_keys)

        def revalidate(self, *args, **kwargs):
            pass

    class Projection:
        def current_version(self, school):
            return 8

        def read_for_maintenance(self, **kwargs):
            assert calls[:2] == ["maintain", "read"]
            assert kwargs["source_key"] == "bed-one"
            return {
                "school_fact_version": 8,
                "records": [
                    {
                        "source_key": "bed-one",
                        "fact_kind": "dorm_bed",
                        "version": 3,
                        "segments": [
                            {
                                "effective_at": NOW.isoformat(),
                                "valid_to": None,
                                "content": {"display_name": "一号床"},
                            }
                        ],
                    }
                ],
            }

    repository = ResidenceRepository(
        authority=Authority(),
        projection=Projection(),
        reference_key=b"f6-key-at-least-16-bytes",
        clock=lambda: NOW,
    )
    result = repository.read_for_maintenance(actor, "dorm_bed", "bed-one")
    assert result.records[0]["version"] == 3
    assert len(result.records[0]["segments"]) == 1


def test_writer_dispatch_partitions_by_kind_instead_of_schema_version():
    from types import SimpleNamespace

    module_name = "app.composition.school_fact_residence_execution"
    assert util.find_spec(module_name), "F6 Writer dispatch is not implemented"
    module = import_module(module_name)
    seen = []

    class ChangeSet:
        changes = (
            SimpleNamespace(fact_kind="dorm_room", schema_version=4),
            SimpleNamespace(fact_kind="student_school_relationship", schema_version=4),
        )

        def model_copy(self, *, update):
            return SimpleNamespace(**update)

    dispatch = module.ResidenceWriterDispatch(
        residence=lambda s, c, v: seen.append(("f6", v.changes)),
        retained=lambda s, c, v: seen.append(("old", v.changes)),
    )
    dispatch(None, None, ChangeSet())
    assert [(name, values[0].fact_kind) for name, values in seen] == [
        ("f6", "dorm_room"),
        ("old", "student_school_relationship"),
    ]


def test_own_receipt_rejects_retained_partial_target_proof():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer

    proof = SimpleNamespace(allowed_targets=frozenset({("person", "one")}), revalidate=lambda: None)
    retained = SimpleNamespace(for_targets=lambda c, t: proof)
    guard = ResidenceOwnResultAuthorizer(residence=None, retained=retained)
    with pytest.raises(PermissionError):
        guard.for_targets(None, frozenset({("person", "one"), ("person", "two")}))


def test_f2_quality_permission_does_not_grant_f6_target_permission():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_quality import ResidenceQualityBridge
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = F2FactScope(actor.school_id, "fact_quality", (), ("quality-new",))

    class Authority:
        clock = staticmethod(lambda: NOW)

        def require_scope(self, *args, **kwargs):
            raise PermissionError("F6 absent")

    f2 = SimpleNamespace(
        require_current=lambda *a, **k: SimpleNamespace(
            scope_plan=SimpleNamespace(reference=scope.reference)
        ),
        revalidate=lambda *a: None,
    )
    bridge = ResidenceQualityBridge(
        f2_guard=f2,
        residence_authority=Authority(),
        projection=SimpleNamespace(current_version=lambda s: 1),
    )
    content = SimpleNamespace(
        model_dump=lambda **k: {
            "target_fact_kind": "dorm_bed",
            "target_source_key": "bed-one",
            "owner_kind": "location",
            "owner_source_key": "room-one",
            "field_name": "display_name",
        }
    )
    change = SimpleNamespace(
        fact_kind="fact_quality", source_key="quality-new", expected_version=0, content=content
    )
    with pytest.raises(PermissionError):
        bridge.require_change_set(actor, SimpleNamespace(changes=(change,)))


def test_creation_only_receipt_never_authorizes_original_content():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer

    guard = ResidenceOwnResultAuthorizer(residence=None, retained=None, original_authority=None)
    with pytest.raises(PermissionError):
        guard.authorize_original(
            None,
            SimpleNamespace(fact_kind="dorm_bed", source_key="new", creation_receipt=True),
            ("display_name",),
        )


def test_feature_disabled_writer_guards_reject_before_any_execution_io():
    from app.composition.school_fact_residence_execution import (
        ResidenceFinalGuard,
        ResidenceQualityFinalGuard,
    )
    from app.platform.school_facts.application import SchoolFactChangeRejected

    for guard in (
        ResidenceFinalGuard(None, enabled=False),
        ResidenceQualityFinalGuard(None, enabled=False),
    ):
        with pytest.raises(SchoolFactChangeRejected):
            guard(None, None, None)


def test_release_rejects_duty_expiring_during_final_checks():
    from datetime import timedelta
    from types import SimpleNamespace

    from app.composition.school_fact_read_evidence import validation_batch
    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.authorization.thin_contracts import ThinDecisionKind
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    state = SimpleNamespace(clock=NOW)
    actor = _actor()
    authority = ResidenceAuthority(
        thin=SimpleNamespace(
            revalidate=lambda *a, **kw: SimpleNamespace(kind=ThinDecisionKind.ALLOW)
        ),
        configuration=SimpleNamespace(scope_bindings=()),
        clock=lambda: state.clock,
    )
    permit = SimpleNamespace(
        actor=actor,
        basis=SimpleNamespace(
            governance=SimpleNamespace(issued_as_of=NOW, valid_until=NOW + timedelta(seconds=1))
        ),
    )
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            authority.revalidate(permit, actor=actor, checked_at=state.clock)
            state.clock += timedelta(seconds=1)


def test_v4_dependency_units_never_fall_back_to_old_maintenance():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceWriterDispatch
    from app.platform.school_facts.application import SchoolFactChangeRejected

    seen = []

    class Changes:
        schema_version = 4
        changes = (
            SimpleNamespace(fact_kind="student_school_relationship", source_key="enrollment"),
        )

        def model_copy(self, **kwargs):
            return self

    def old(*args):
        seen.append("old")

    def new(*args):
        seen.append("f6-dependency")

    ResidenceWriterDispatch(residence=None, retained=old, dependency=new)(None, None, Changes())
    assert seen == ["f6-dependency"]
    seen.clear()
    with pytest.raises(SchoolFactChangeRejected):
        ResidenceWriterDispatch(residence=None, retained=old)(None, None, Changes())
    assert not seen
    Changes.schema_version = 3
    ResidenceWriterDispatch(residence=None, retained=old, dependency=new)(None, None, Changes())
    assert seen == ["old"]


@pytest.mark.parametrize(
    "kind", ["fact_quality", "policy_scope", "education_grade_group", "teaching_assignment"]
)
def test_v4_education_does_not_become_a_residence_dependency(kind):
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceWriterDispatch

    calls = []

    class Batch:
        schema_version = 4
        changes = (SimpleNamespace(
            fact_kind=kind, content=SimpleNamespace(
                target_fact_kind="education_grade_group",
                education_target_kind="education_calendar_rule",
            ),
        ),)

        def model_copy(self, *, update):
            return SimpleNamespace(schema_version=4, **update)

    dispatch = ResidenceWriterDispatch(
        residence=lambda *_: pytest.fail("not a residence fact"),
        retained=lambda _s, _c, batch: calls.append(tuple(c.fact_kind for c in batch.changes)),
        dependency=lambda *_: pytest.fail("input schema is not residence authority"),
    )
    dispatch(None, SimpleNamespace(actor=None), Batch())
    assert calls == [(kind,)]


def test_v4_mixed_eligibility_and_education_quality_keep_both_current_guards():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceWriterDispatch

    calls = []

    class Batch:
        schema_version = 4
        changes = tuple(SimpleNamespace(
            fact_kind=kind, source_key=kind,
            cause_source_key="student_school_relationship", content=SimpleNamespace(
                target_fact_kind="education_grade_group",
                education_target_kind="education_calendar_rule",
            ),
        ) for kind in (
            "student_school_relationship", "student_class_position", "fact_quality",
        )) + (
            SimpleNamespace(fact_kind="student_class_membership", source_key="unrelated",
                cause_source_key="another-enrollment", content=None),
        )

        def model_copy(self, *, update):
            return SimpleNamespace(schema_version=4, **update)

    dispatch = ResidenceWriterDispatch(
        residence=lambda *_: pytest.fail("not a residence fact"),
        retained=lambda _s, _c, batch: calls.append(
            ("education", tuple(c.fact_kind for c in batch.changes))
        ),
        dependency=lambda _s, _c, batch: calls.append(
            ("eligibility", tuple(c.fact_kind for c in batch.changes))
        ),
    )
    dispatch(None, SimpleNamespace(actor=None), Batch())
    assert calls == [
        ("eligibility", ("student_school_relationship", "student_class_position")),
        ("education", ("fact_quality", "student_class_membership")),
    ]
    dispatch.retained = lambda *_: (_ for _ in ()).throw(PermissionError("education revoked"))
    with pytest.raises(PermissionError):
        dispatch(None, SimpleNamespace(actor=None), Batch())
    dispatch.dependency = lambda *_: (_ for _ in ()).throw(PermissionError("eligibility revoked"))
    with pytest.raises(PermissionError):
        dispatch(None, SimpleNamespace(actor=None), Batch())


def test_receipt_exact_union_keeps_creation_only_until_independent_read():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer

    calls = []
    old, new = ("person", "old"), ("person", "new")

    def authorizer(allowed, creations=()):
        def exact(context, targets):
            calls.append(targets)
            subset = targets & frozenset(allowed)
            if not subset:
                raise PermissionError("not this scope")
            return SimpleNamespace(
                allowed_targets=subset,
                creation_only_targets=subset & frozenset(creations),
                revalidate=lambda: None,
            )

        return SimpleNamespace(for_targets=exact)

    guard = ResidenceOwnResultAuthorizer(
        residence=None, retained=authorizer((old,)), dependency=authorizer((new,), (new,))
    )
    result = guard.for_targets(None, frozenset({old, new}))
    assert result.allowed_targets == frozenset({old, new})
    assert result.creation_only_targets == frozenset({new})
    assert all(len(targets) == 1 for targets in calls)
    guard.dependency = authorizer((old, new))
    assert not guard.for_targets(None, frozenset({old, new})).creation_only_targets


@pytest.mark.parametrize("denial_name", (
    "PermissionError", "SchoolFactChangeRejected",
    "SchoolFactAuthorizationUnavailable", "RuntimeError",
))
def test_old_f2_read_can_classify_quality_but_cannot_authorize_f6_quality_body(denial_name):
    from types import SimpleNamespace

    from app.composition.school_fact_residence_quality import ResidenceQualityBridge
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.f2_policy import F2FactScope
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
    from tests.test_f1_official_contact_query import _actor

    actor = _actor()
    scope = F2FactScope(actor.school_id, "fact_quality", ("quality-one",), ())
    old = SimpleNamespace(
        require_current=lambda *a, **k: SimpleNamespace(
            scope_plan=SimpleNamespace(reference=scope.reference)
        ),
        revalidate=lambda *a: None,
    )

    denial = {
        "PermissionError": PermissionError,
        "SchoolFactChangeRejected": SchoolFactChangeRejected,
        "SchoolFactAuthorizationUnavailable": SchoolFactAuthorizationUnavailable,
        "RuntimeError": RuntimeError,
    }[denial_name]

    def denied(*args, **kwargs):
        raise denial("no f6 dependency")

    state = SimpleNamespace(kind="person", reads=0)

    def coordinates(*args):
        state.reads += 1
        return {"version": 1, "content": {"target_fact_kind": state.kind}}

    bridge = ResidenceQualityBridge(
        f2_guard=SimpleNamespace(require_current=denied),
        retained_f2_guard=old,
        residence_authority=None,
        projection=SimpleNamespace(quality_coordinates=coordinates),
    )
    change = SimpleNamespace(
        fact_kind="fact_quality", source_key="quality-one", expected_version=1, content=None
    )
    if denial_name in {"RuntimeError", "SchoolFactAuthorizationUnavailable"}:
        with pytest.raises(denial):
            bridge.require_read(actor, "quality-one")
        assert state.reads == 0
        return
    assert not bridge.recognizes_change(actor, change)
    assert bridge.require_read(actor, "quality-one") is None
    state.kind = "dorm_bed"
    assert bridge.recognizes_change(actor, change)
    with pytest.raises(denial):
        bridge.require_read(actor, "quality-one")


class _ReceiptScopeOwner:
    """Count owner decisions separately from actual current proof rechecks."""

    def __init__(self, targets, *, creations=()):
        self.targets, self.creations = frozenset(targets), frozenset(creations)
        self.enumerations, self.exact, self.checks = 0, 0, 0
        self.active = True
        self.version = 1

    def __call__(self, context, *, targets=None):
        if targets is not None:
            return self.for_targets(context, targets)
        self.enumerations += 1
        return self._proof(context, self.targets)

    def for_targets(self, context, targets):
        self.exact += 1
        return self._proof(context, targets & self.targets)

    def _proof(self, context, targets):
        from types import SimpleNamespace

        if not self.active or not targets:
            raise PermissionError("current receipt scope unavailable")
        version, actor = self.version, context.actor

        def revalidate():
            self.checks += 1
            if not self.active or self.version != version or context.actor != actor:
                raise PermissionError("receipt scope changed")

        return SimpleNamespace(allowed_targets=frozenset(targets),
                               creation_only_targets=self.creations & targets,
                               revalidate=revalidate)


@pytest.mark.parametrize("count", [1, 5, 30])
def test_own_receipt_list_reuses_scope_proofs_without_per_object_resigning(count):
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from tests.test_f1_official_contact_query import _actor

    dorm = frozenset(("dorm_bed", f"bed-{i}") for i in range(count))
    people = frozenset(("person", f"person-{i}") for i in range(count))
    classes = frozenset(("administrative_class", f"class-{i}") for i in range(count))
    owners = (_ReceiptScopeOwner(dorm), _ReceiptScopeOwner(people), _ReceiptScopeOwner(classes))
    context = SimpleNamespace(actor=_actor())
    guard = ResidenceOwnResultAuthorizer(
        residence=owners[0], retained=owners[1], dependency=owners[2],
    )
    proof = guard(context)
    assert proof.allowed_targets == dorm | people | classes
    proof.revalidate()  # Writer's current post-read boundary.
    assert [owner.enumerations for owner in owners] == [1, 1, 1]
    assert [owner.exact for owner in owners] == [0, 0, 0]
    assert [owner.checks for owner in owners] == [2, 2, 2]


def test_receipt_list_owner_partition_and_creation_metadata_are_not_broadened():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from tests.test_f1_official_contact_query import _actor

    bed, person, new, forbidden = (
        ("dorm_bed", "bed"), ("person", "one"), ("person", "new"), ("dorm_room", "unowned")
    )
    guard = ResidenceOwnResultAuthorizer(
        residence=_ReceiptScopeOwner((bed, ("person", "not-f6"))),
        retained=_ReceiptScopeOwner((person, forbidden)),
        dependency=_ReceiptScopeOwner((new,), creations=(new,)),
    )
    proof = guard(SimpleNamespace(actor=_actor()))
    assert proof.allowed_targets == frozenset({bed, person, new})
    assert proof.creation_only_targets == frozenset({new})
    guard.retained = _ReceiptScopeOwner((person, new))
    assert not guard(SimpleNamespace(actor=_actor())).creation_only_targets


def test_receipt_list_keeps_quality_owner_checks_and_drops_only_denied_targets():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from tests.test_f1_official_contact_query import _actor

    keys = {("fact_quality", key) for key in ("permitted", "denied", "creation")}
    people = ("person", "one")
    scoped = _ReceiptScopeOwner(keys | {people}, creations=(("fact_quality", "creation"),))
    seen, rechecks = [], []

    def quality(actor, key):
        seen.append(key)
        if key == "denied":
            raise PermissionError("target fields unavailable")
        return SimpleNamespace(
            allowed_targets=frozenset({("fact_quality", key)}),
            revalidate=lambda: rechecks.append(key),
        )

    guard = ResidenceOwnResultAuthorizer(
        residence=None, retained=scoped, quality=SimpleNamespace(require_read=quality),
    )
    proof = guard(SimpleNamespace(actor=_actor()))
    assert proof.allowed_targets == frozenset({people}) | (keys - {("fact_quality", "denied")})
    assert proof.creation_only_targets == frozenset({("fact_quality", "creation")})
    assert set(seen) == {"permitted", "denied"}  # No body read for creation-only metadata.
    proof.revalidate()
    assert rechecks == ["permitted", "permitted"]
    assert scoped.exact == 0


@pytest.mark.parametrize("change", ["revoked", "fields", "logout"])
def test_receipt_list_reused_proof_is_current_after_read_and_connection_is_released(
    change, monkeypatch,
):
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.pool import QueuePool

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
    from app.platform.school_facts.ports import SchoolFactOwnResultPage
    from tests.school_facts.test_sf5b_writer_executor import _context, _writer_settings

    context = _context()
    scoped = _ReceiptScopeOwner({("dorm_bed", "bed")})
    guard = ResidenceOwnResultAuthorizer(residence=scoped, retained=None)
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0)
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(
        engine=engine, settings=_writer_settings(), own_result_authorizer=guard,
    )
    seen = []

    def read(**kwargs):
        assert kwargs["allowed_targets"] == frozenset({("dorm_bed", "bed")})
        assert kwargs["limit"] == 5
        seen.append("bounded metadata read")
        if change == "fields":
            scoped.version += 1
        else:
            scoped.active = False
        return SchoolFactOwnResultPage((), False)

    monkeypatch.setattr(kernel, "_service", lambda **kwargs: SimpleNamespace(list_own_results=read))
    try:
        with pytest.raises(PermissionError):
            kernel.list_own_results(
                context=context, session_binding="same-current-session", limit=5,
            )
        assert seen == ["bounded metadata read"]
        assert engine.pool.checkedout() == 0
        assert scoped.exact == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("count", [1, 8, 32])
def test_actual_f6_receipt_scope_decisions_stay_constant_as_object_count_grows(count):
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor = _actor()
    scope = api().F6FactScope(
        actor.school_id, "dorm_bed", tuple(f"bed-{n}" for n in range(count)), (), ("display_name",),
    )
    decisions, checks = [], []
    authority = ResidenceAuthority(
        thin=None, configuration=SimpleNamespace(scope_bindings=()), clock=lambda: NOW,
    )

    def current(actor, *, fact_kind, action, **kwargs):
        if (fact_kind, action) != ("dorm_bed", "read"):
            raise PermissionError("no configured scope")
        decisions.append((fact_kind, action))
        return SimpleNamespace(actor=actor, action=action, scope=scope)

    authority.require_scope = current
    authority.revalidate = lambda *args, **kwargs: checks.append("current scope")
    guard = ResidenceOwnResultAuthorizer(residence=authority.own_result, retained=None)
    result = guard(SimpleNamespace(actor=actor))
    result.revalidate()  # Existing Writer post-query check.
    assert len(result.allowed_targets) == count
    assert decisions == [("dorm_bed", "read")]
    assert len(checks) == 3  # Owner construction, list read-before, Writer read-after.


def test_receipt_list_rechecks_each_request_and_old_proof_never_survives_revocation():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from tests.test_f1_official_contact_query import _actor

    owner = _ReceiptScopeOwner({("dorm_bed", "bed")})
    guard = ResidenceOwnResultAuthorizer(residence=owner, retained=None)
    context = SimpleNamespace(actor=_actor())
    first, second = guard(context), guard(context)
    assert first is not second and owner.enumerations == 2
    owner.active = False
    with pytest.raises(PermissionError):
        first.revalidate()
    with pytest.raises(PermissionError):
        guard(context)
    assert owner.enumerations == 3


def test_receipt_list_quality_read_still_enforces_exact_target_field_projection():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_authority import ResidenceAuthority
    from app.composition.school_fact_residence_execution import ResidenceOwnResultAuthorizer
    from app.composition.school_fact_residence_quality import ResidenceQualityBridge
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.test_f1_official_contact_query import _actor
    from tests.test_f3_family_read import NOW

    actor, target = _actor(), ("fact_quality", "quality-one")
    f2_scope = F2FactScope(actor.school_id, "fact_quality", ("quality-one",), ())
    f6_scope = api().F6FactScope(actor.school_id, "dorm_bed", ("bed",), (), ("display_name",))
    content = {"target_fact_kind": "dorm_bed", "target_source_key": "bed",
               "field_name": "availability"}
    authority = SimpleNamespace(
        clock=lambda: NOW,
        require_scope=lambda *a, **k: SimpleNamespace(scope=f6_scope, action="read"),
        require_fields=ResidenceAuthority.require_fields,
        require_object=ResidenceAuthority.require_object,
        revalidate=lambda *a, **k: None,
    )
    bridge = ResidenceQualityBridge(
        f2_guard=SimpleNamespace(
            require_current=lambda *a, **k: SimpleNamespace(
                scope_plan=SimpleNamespace(reference=f2_scope.reference),
            ), revalidate=lambda *a: None,
        ), residence_authority=authority,
        projection=SimpleNamespace(
            quality_coordinates=lambda *a: {"version": 1, "content": content},
            current_version=lambda *a: 1,
        ),
    )
    guard = ResidenceOwnResultAuthorizer(
        residence=None, retained=_ReceiptScopeOwner({target}), quality=bridge,
    )
    context = SimpleNamespace(actor=actor)
    with pytest.raises(PermissionError):
        guard(context)
    content["field_name"] = "display_name"
    allowed = guard(context)
    assert allowed.allowed_targets == frozenset({target})
    allowed.revalidate()


@pytest.mark.parametrize("count,depth", [(1, 1), (4, 3), (8, 4)])
def test_nested_receipt_creation_metadata_does_not_multiply_per_target(count, depth):
    from app.composition.school_fact_family_execution import PartitionedOwnResultAuthorization

    class Owner:
        def __init__(self):
            self.allowed_targets = frozenset(("dorm_bed", f"bed-{n}") for n in range(count))
            self.creations = frozenset({("dorm_bed", "bed-0")})
            self.reads = self.checks = 0

        @property
        def creation_only_targets(self):
            self.reads += 1
            return self.creations

        def revalidate(self):
            self.checks += 1

    owner = Owner()
    proof = owner
    for _ in range(depth):
        proof = PartitionedOwnResultAuthorization(owner.allowed_targets, (proof,))
    assert proof.creation_only_targets == owner.creations
    assert owner.reads == 1  # One traversal, not (object_count + 1) ** depth.
    assert owner.checks == 0  # Metadata evaluation is separate from current-authority checks.
    owner.creations = frozenset()
    assert not proof.creation_only_targets
    assert owner.reads == 2  # No result cache survives the next evaluation.
    proof.revalidate()
    assert owner.checks == 1


def test_nested_receipt_creation_metadata_keeps_readable_precedence_and_exact_scope():
    from types import SimpleNamespace

    from app.composition.school_fact_family_execution import PartitionedOwnResultAuthorization

    new, both, outside = (("dorm_bed", label) for label in ("new", "both", "outside"))
    creator = SimpleNamespace(
        allowed_targets=frozenset({new, both, outside}),
        creation_only_targets=frozenset({new, both, outside}), revalidate=lambda: None,
    )
    reader = SimpleNamespace(allowed_targets=frozenset({both}), revalidate=lambda: None)
    combined = PartitionedOwnResultAuthorization(frozenset({new, both}), (creator, reader))
    outer = PartitionedOwnResultAuthorization(frozenset({new, both}), (combined,))
    assert outer.creation_only_targets == frozenset({new})
    assert outer.allowed_targets == frozenset({new, both})
