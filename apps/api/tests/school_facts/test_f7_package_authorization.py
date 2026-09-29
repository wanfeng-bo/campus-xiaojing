"""Package permissions follow exact owners, never the normalized input version."""

from types import SimpleNamespace

import pytest

from app.composition.school_fact_package_authorization import (
    PackageChangeAuthorization,
    PackageFactPartition,
    PackageReferenceAuthorization,
)
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_sf3_http import _context


def change(kind, key, *, content=None, version=0):
    return SimpleNamespace(
        fact_kind=kind,
        source_key=key,
        schema_version=4,
        expected_version=version,
        content=SimpleNamespace(**content) if content else None,
    )


def partition():
    return PackageFactPartition(
        directory_configuration=SimpleNamespace(
            source_catalog={"person": ("staff-person",)},
            creation_catalog={},
        ),
        family_configuration=SimpleNamespace(
            source_catalog={"person": ("child-person",)},
            creation_catalog={},
        ),
        education_configuration=SimpleNamespace(
            dependency_directory=SimpleNamespace(
                source_catalog={
                    "fact_quality": ("class-quality",),
                    "policy_scope": ("class-policy",),
                },
                creation_catalog={},
            ),
        ),
        existing_quality_kind=lambda _actor, key: {
            "family-quality": "student",
            "dorm-quality": "dorm_bed",
            "class-quality": "assignment",
            "directory-quality": "official_contact",
        }[key],
    )


def test_normalized_v4_directory_and_family_are_not_residence_dependencies():
    selected = partition().group(
        _context().actor,
        (
            change("organization", "office"),
            change("person", "child-person"),
            change("person", "staff-person"),
            change("education_class", "class"),
            change("teaching_assignment", "teacher"),
            change("dorm_bed", "bed"),
        ),
    )
    assert set(selected) == {"directory", "family", "education", "residence"}
    assert [v.source_key for v in selected["directory"]] == ["office", "staff-person"]
    assert [v.source_key for v in selected["family"]] == ["child-person"]


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_input_version_never_changes_ownership(version):
    candidate = change("organization", "office")
    candidate.schema_version = version
    assert tuple(partition().group(_context().actor, (candidate,))) == ("directory",)


def test_quality_and_policy_use_actual_current_target_and_configured_education_scope():
    selected = partition().group(
        _context().actor,
        (
            change("fact_quality", "family-quality", version=1),
            change("fact_quality", "dorm-quality", version=1),
            change("fact_quality", "class-quality", version=1),
            change("fact_quality", "directory-quality", version=1),
            change("policy_scope", "class-policy", version=1),
        ),
    )
    assert set(selected) == {
        "family_quality",
        "residence_quality",
        "education_quality",
        "directory",
    }


def test_quality_retargeting_cannot_hide_original_sensitive_owner():
    with pytest.raises(PermissionError):
        partition().group(
            _context().actor,
            (
                change(
                    "fact_quality",
                    "family-quality",
                    version=1,
                    content={"target_fact_kind": "official_contact"},
                ),
            ),
        )


def test_unknown_kind_and_untrusted_actor_fail_closed():
    with pytest.raises(PermissionError):
        partition().group(object(), (change("person", "child-person"),))
    with pytest.raises(PermissionError):
        partition().group(_context().actor, (change("unregistered", "x"),))
    with pytest.raises(PermissionError):
        partition().group(_context().actor, (change("person", "unknown-new-person"),))


def test_live_school_shared_directory_owner_still_requires_current_school_scope():
    from app.platform.school_facts.f2_policy import F2FactScope

    actor = _context().actor
    current = [F2FactScope(actor.school_id, "person", coverage="school")]
    guard = SimpleNamespace(require_current=lambda *_a, **_k: SimpleNamespace(
        scope_plan=SimpleNamespace(reference=current[0].reference)))
    selected = partition()
    selected.live_directory_guard = guard
    item = change("person", "new-formal-person")
    assert selected.group(actor, (item,)) == {"directory": (item,)}
    # This routing decision grants nothing and may not infer school-wide
    # ownership from an old finite grant or a different school's scope.
    current[0] = F2FactScope(actor.school_id, "person", creation_keys=(item.source_key,))
    with pytest.raises(PermissionError):
        selected.group(actor, (item,))


@pytest.mark.parametrize(
    "source,expected",
    [("package", "package"), ("admin", "legacy"), ("excel", "legacy"), ("adapted", "legacy")],
)
def test_writer_guard_selection_uses_validated_stored_source_not_schema(source, expected):
    persistence = object.__new__(SqlAlchemySchoolFactPersistence)
    calls = []
    persistence._final_authorization_guard = lambda *_args: calls.append("legacy")
    persistence._package_final_authorization_guard = lambda *_args: calls.append("package")
    persistence._authorize_confirmed_batch(
        object(), _context(), SimpleNamespace(schema_version=4), SimpleNamespace(source_kind=source)
    )
    assert calls == [expected]


def test_package_confirmation_without_registered_package_guard_fails_closed():
    persistence = object.__new__(SqlAlchemySchoolFactPersistence)
    persistence._final_authorization_guard = lambda *_args: pytest.fail("legacy fallback")
    persistence._package_final_authorization_guard = None
    with pytest.raises(SchoolFactChangeRejected):
        persistence._authorize_confirmed_batch(
            object(), _context(), object(), SimpleNamespace(source_kind="package")
        )


def test_relation_maintenance_does_not_authorize_an_unreadable_reference():
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
    from tests.school_facts.test_f7_full_scope_import import change

    relation = change(kind="student_class_membership", key="member", version=3).model_copy(
        update={"operation": "upsert", "expected_version": 0}
    )
    from app.platform.school_facts.contracts import CanonicalFactContentV4

    relation = relation.model_copy(
        update={
            "content": CanonicalFactContentV4(
                student_source_key="hidden-student",
                school_relationship_source_key="hidden-enrollment",
                membership_status="assigned",
                class_source_key="visible-class",
            )
        }
    )
    batch = CanonicalFactChangeSetV1.model_construct(
        schema_version=4, source_sha256="a" * 64, base_version=1, changes=(relation,)
    )
    calls = []
    family = SimpleNamespace(
        require_scope=lambda *args, **kwargs: (
            calls.append(args),
            (_ for _ in ()).throw(PermissionError("hidden reference")),
        )
    )
    references = PackageReferenceAuthorization(
        partition(),
        directory_guard=None,
        family_authority=family,
        education_authority=SimpleNamespace(
            require_scope=lambda *args, **kwargs: object(),
            require_object=lambda *args, **kwargs: None,
            revalidate=lambda *args, **kwargs: None,
        ),
        residence_authority=None,
    )
    authorize = PackageChangeAuthorization(
        partition(),
        {"education": lambda *_args: None},
        references=references,
    )
    with pytest.raises(PermissionError, match="hidden reference"):
        authorize(_context().actor, batch)
    assert calls


@pytest.mark.parametrize(
    "kind,owner",
    [
        ("official_contact", "directory"),
        ("student", "family_quality"),
        ("class_teacher_assignment", "education_quality"),
        ("teaching_assignment", "education_quality"),
        ("dorm_bed", "residence_quality"),
    ],
)
def test_actual_quality_coordinate_locator_accepts_every_owner_without_reading_its_body(
    kind, owner
):
    from uuid import uuid4

    from app.composition.school_fact_package_authorization import PackageExistingQualityKind
    from app.platform.school_facts.f2_policy import F2FactScope

    actor = _context().actor
    calls = []
    scope_id = uuid4()
    permit = SimpleNamespace(
        scope_plan=SimpleNamespace(
            reference=F2FactScope(actor.school_id, "fact_quality", ("quality",)).reference,
            scope_id=scope_id,
        )
    )
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, statement, parameters):
            assert "school_fact_f7_package_quality_kind" in str(statement)
            assert parameters == {
                "school": actor.school_id,
                "assignment": actor.assignment_id,
                "scope": scope_id,
                "key": "quality",
            }
            return kind

    locator = PackageExistingQualityKind(
        f2_guard=SimpleNamespace(
            require_current=lambda *a, **k: permit,
            revalidate=lambda *a: calls.append("checked"),
        ),
        sessions=Session,
    )
    selected = PackageFactPartition(
        directory_configuration=None,
        family_configuration=None,
        education_configuration=None,
        existing_quality_kind=locator.target_kind,
    ).group(actor, (change("fact_quality", "quality", version=1),))
    assert set(selected) == {owner}
    assert calls == ["checked"]


def test_writer_quality_dispatch_uses_generic_kind_before_f3_detail_guard():
    from app.composition.school_fact_family_quality import FamilyQualityWriterDispatch

    class Batch:
        def __init__(self, changes):
            self.changes = changes

        def model_copy(self, *, update):
            return Batch(update["changes"])

    routed = []
    dispatch = FamilyQualityWriterDispatch(
        lambda _session, _context, batch: routed.append(("family", batch.changes[0].source_key)),
        lambda _session, _context, batch: routed.append(("retained", batch.changes[0].source_key)),
        existing_target_kind=lambda _actor, key: {
            "organization-quality": "organization", "student-quality": "student"
        }[key],
    )
    batch = Batch((
        change("fact_quality", "organization-quality", version=2),
        change("fact_quality", "student-quality", version=2),
    ))
    dispatch(None, _context(), batch)
    assert routed == [
        ("retained", "organization-quality"), ("family", "student-quality")
    ]


def test_f7_writer_cannot_start_without_exact_package_final_authority():
    from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel

    with pytest.raises(ValueError, match="package"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None,
            settings=SimpleNamespace(schema_contract="school-facts-f7-v47"),
            final_authorization_guard=lambda *_: None,
            family_actor_validator=object(),
        )


def test_writer_still_requires_old_object_read_when_maintenance_is_retained():
    from app.composition.school_fact_package_authorization import (
        PackageReferenceAuthorization,
        PackageWriterAuthorization,
    )
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
    from tests.school_facts.test_sf3_http import NOW

    calls = []

    def denied(actor, kind, *, purpose):
        calls.append((kind, purpose))
        raise PermissionError("read revoked; maintenance remains")

    chosen = partition()
    gate = PackageWriterAuthorization(chosen, {"directory": lambda *args: None},
        references=PackageReferenceAuthorization(chosen,
            directory_guard=SimpleNamespace(require_current=denied), family_authority=None,
            education_authority=None, residence_authority=None))
    c = change("official_contact", "old-contact", version=2)
    c.effective_at = NOW
    batch = CanonicalFactChangeSetV1.model_construct(
        schema_version=4, source_sha256="a" * 64, base_version=1, changes=(c,))
    with pytest.raises(SchoolFactChangeRejected):
        gate(None, _context(), batch)
    assert calls == [("official_contact", "read")]
