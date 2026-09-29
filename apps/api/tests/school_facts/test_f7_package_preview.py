"""A clean preflight alone cannot claim the maintainer saw all domain changes."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.platform.master_data_imports.school_facts_replacement import (
    PreparedReplacementEnd,
    ReplacementFactSnapshot,
    prepare_package_preview,
)
from app.platform.school_facts.contracts import CanonicalFactChangeV1

AT = datetime(2026, 9, 24, tzinfo=UTC)


def unit(kind="person", key="selected", operation="upsert", version=2):
    return CanonicalFactChangeV1.model_validate_json(
        __import__("json").dumps(
            {
                "schema_version": 4,
                "fact_kind": kind,
                "source_key": key,
                "expected_version": version,
                "operation": operation,
                "effective_at": AT.isoformat(),
                "origin": {"sheet_code": "input", "row_number": 2},
                "content": {
                    "display_name": "新名称",
                    "person_kind": "other",
                    "lifecycle_status": "active",
                }
                if operation == "upsert"
                else None,
            }
        )
    )


def snapshot(c):
    return ReplacementFactSnapshot(
        c.fact_kind,
        c.source_key,
        AT,
        c.expected_version,
        9,
        "active",
        {"display_name": "旧名称"},
        lambda: None,
    )


def review(c, extra=()):
    return PreparedReplacementEnd(
        (c, *extra),
        {"before": {"display_name": "旧名称"}, "after": {"display_name": "新名称"}},
        lambda: None,
    )


def preview(changes, resolve, read=snapshot):
    return prepare_package_preview(
        change_set=SimpleNamespace(changes=changes, base_version=9),
        read_snapshot=read,
        review_change=resolve,
    )


def test_all_actual_changes_have_exact_before_after_and_revalidation():
    changes = (unit(),)
    checks = []
    value = preview(
        changes,
        lambda c: review(c),
        lambda c: replace(snapshot(c), revalidate=lambda: checks.append(c.source_key)),
    )
    assert value.status == "complete"
    assert value.changes[0]["before"] == {"display_name": "旧名称"}
    assert value.changes[0]["after"]["display_name"] == "新名称"
    value.revalidate()
    assert checks == ["selected", "selected"]


@pytest.mark.parametrize(
    "kind", ["student_class_membership", "teaching_group_member", "student_bed_residence"]
)
def test_missing_class_officer_shared_group_or_residence_impact_blocks_confirmation(kind):
    primary = unit(kind, operation="end")
    linked = unit("person", "linked", operation="end")
    value = preview((primary,), lambda c: review(c, (linked,)))
    assert value.status == "incomplete"
    assert value.issues == ("PACKAGE_DOMAIN_IMPACT_MISSING",)


def test_changed_domain_primary_is_not_silently_called_complete():
    c = unit()
    modified = c.model_copy(update={"operation": "end", "content": None})
    value = preview((c,), lambda _: review(modified))
    assert value.status == "incomplete"
    assert "PACKAGE_DOMAIN_CHANGE_MISMATCH" in value.issues


def test_same_batch_actual_linked_change_is_included_in_preview():
    primary = unit("student_class_membership", operation="end")
    linked = unit("person", "linked", operation="end")
    value = preview(
        (primary, linked), lambda c: review(c, (linked,)) if c is primary else review(c)
    )
    assert value.status == "complete"
    assert {c["source_key"] for c in value.changes} == {"selected", "linked"}
    assert value.changes[1]["origin"] == "linked"


def test_read_revocation_is_not_downgraded_to_an_incomplete_public_preview():
    c = unit()

    def denied():
        raise PermissionError("current read revoked")

    with pytest.raises(PermissionError):
        preview((c,), lambda x: review(x), lambda x: replace(snapshot(x), revalidate=denied))


def test_history_correction_requires_authorized_full_prior_timeline():
    c = unit().model_copy(update={"operation": "correct", "replacement_segments": ()})
    value = preview((c,), lambda x: review(x))
    assert value.status == "incomplete"
    assert "PACKAGE_TIMELINE_PREVIEW_UNAVAILABLE" in value.issues


def owned_education_provider(coordinator):
    from app.composition.school_fact_package_replacement import PackageReplacementReader

    return PackageReplacementReader(
        partition=SimpleNamespace(group=lambda actor, changes: {"education": changes}),
        references=SimpleNamespace(require_coordinate=lambda *a, **kw: None),
        directory=None,
        family=None,
        residence=None,
        directory_adapter=None,
        family_adapter=None,
        residence_adapter=None,
        education=SimpleNamespace(
            maintenance=coordinator,
            repository=SimpleNamespace(
                reference_for=lambda actor, kind, key, **kw: coordinator.repository.reference_for(
                    actor=actor, fact_kind=kind, source_key=key, **kw
                )
            ),
        ),
        education_resolver=coordinator.resolve_change,
    )


def test_real_F4_transfer_owner_detects_missing_old_membership_and_officer_endings():
    from tests.school_facts.test_f4_education_maintenance import environment, record

    old = record(
        "student_class_membership",
        "old",
        student_source_key="student",
        class_source_key="old-class",
        school_relationship_source_key="relationship",
        membership_status="assigned",
    )
    role = record(
        "student_class_position",
        "officer",
        student_source_key="student",
        class_source_key="old-class",
        position_source_key="monitor",
    )
    coordinator, actor = environment([old, role], school_version=9)
    provider = owned_education_provider(coordinator)
    c = unit(version=0).model_copy(
        update={
            "fact_kind": "student_class_membership",
            "source_key": "new",
            "content": __import__(
                "app.platform.school_facts.contracts", fromlist=["CanonicalFactContentV3"]
            ).CanonicalFactContentV3(
                student_source_key="student",
                class_source_key="new-class",
                school_relationship_source_key="relationship",
                membership_status="assigned",
            ),
        }
    )
    value = preview((c,), lambda c: provider.review_change(actor, c))
    assert value.status == "incomplete"
    assert "PACKAGE_DOMAIN_IMPACT_MISSING" in value.issues
    impacts = value.changes[0]["domain_preview"]["impacts"]
    assert len(impacts) == 3


def test_real_F5_shared_group_owner_detects_both_offering_impacts():
    from tests.school_facts.test_f4_education_maintenance import environment, record

    rows = [record("teaching_group", "group", organization_source_key="org", group_mode="shared")]
    for suffix in ("a", "b"):
        rows += [
            record(
                "offering_member_source",
                "source-" + suffix,
                offering_source_key="offer-" + suffix,
                source_kind="independent_group",
                group_source_key="group",
            ),
            record(
                "teaching_offering",
                "offer-" + suffix,
                course_source_key="course",
                offering_status="active",
            ),
        ]
    coordinator, actor = environment(rows, school_version=9)
    provider = owned_education_provider(coordinator)
    c = unit("teaching_group", "group", operation="end", version=1)
    value = preview((c,), lambda c: provider.review_change(actor, c))
    assert value.status == "incomplete"
    assert "PACKAGE_DOMAIN_IMPACT_MISSING" in value.issues
    assert len(value.changes[0]["domain_preview"]["impacts"]) == 5


def test_real_F6_lifecycle_owner_exposes_residence_ending_before_confirmation():
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_residence_dependencies import ResidenceEligibilityAdapter
    from app.composition.school_fact_residence_maintenance import ResidenceLifecycleCoordinator
    from app.composition.school_fact_residence_product import ResidenceBatchInput
    from tests.school_facts.test_f6_dependency_product import lifecycle_environment

    coordinator, actor, relation, _, _ = lifecycle_environment()
    coordinator.family.adapter = SimpleNamespace(
        resolve_change=lambda **kw: pytest.fail("Lifecycle bypassed its existing owner")
    )
    family = coordinator.family.repository
    family.read_residence_eligibility = lambda **kw: dict(
        school_fact_version=3, timelines=[relation], revalidate=lambda actor: None
    )
    family.reference_for = lambda **kw: "f3:relationship:relation"
    repository, proof = Mock(), Mock()
    repository.read_student_impacts.return_value = SimpleNamespace(
        school_fact_version=3,
        proof=proof,
        records=[
            dict(
                source_key="residence",
                version=1,
                bed_source_key="bed",
                valid_from="2026-01-01T00:00:00Z",
                valid_to=None,
            )
        ],
    )
    repository.read.return_value = SimpleNamespace(
        school_fact_version=3,
        proof=proof,
        records=[dict(fields={"student_name": "合成学生", "bed_name": "1号床"})],
    )
    repository.reference_for.return_value = "f6:student_bed_residence:residence"
    adapter = ResidenceEligibilityAdapter(
        education=SimpleNamespace(maintenance=coordinator, family=coordinator.family),
        lifecycle=ResidenceLifecycleCoordinator(
            repository=repository,
            adapter=SimpleNamespace(public_fields=lambda actor, fields: fields),
            family_repository=family,
        ),
    )
    resolved = adapter.resolve_change(
        actor=actor,
        payload=ResidenceBatchInput(
            fact_kind="student_school_relationship",
            target_ref="f3:relationship:relation",
            operation="end",
            fields={},
            reason="正式结束在校关系",
        ),
        request_id=uuid4(),
        effective_at=AT,
    )
    primary = next(c for c in resolved.change_set.changes if c.source_key == "relation")
    value = prepare_package_preview(
        change_set=resolved.change_set.model_copy(update={"changes": (primary,)}),
        read_snapshot=lambda c: replace(snapshot(c), school_fact_version=3, segments=()),
        review_change=lambda c: PreparedReplacementEnd(
            resolved.change_set.changes,
            resolved.summary.model_dump(mode="json"),
            lambda: resolved.summary_revalidate(actor),
            3,
        ),
    )
    assert value.status == "incomplete"
    assert "PACKAGE_DOMAIN_IMPACT_MISSING" in value.issues
    assert len(value.changes[0]["domain_preview"]["impacts"]) == 5
