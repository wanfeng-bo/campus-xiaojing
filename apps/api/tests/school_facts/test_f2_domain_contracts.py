from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.platform.school_facts import contracts as c


def change(kind, content, version=2):
    return c.CanonicalFactChangeV1.model_validate(
        {
            "schema_version": version,
            "operation": "upsert",
            "fact_kind": kind,
            "source_key": "f2:test",
            "expected_version": 0,
            "effective_at": datetime(2026, 9, 1, tzinfo=UTC),
            "origin": {"sheet_code": "f2", "row_number": 2},
            "content": content,
        }
    )


def test_f2_membership_requires_explicit_version_and_exact_fields():
    content = dict(
        person_source_key="person:one",
        organization_source_key="org:one",
        membership_kind="member",
        basis_reference="正式名册",
    )
    assert change("organization_membership", content).schema_version == 2
    with pytest.raises(ValidationError):
        change("organization_membership", content, 1)
    with pytest.raises(ValidationError):
        change("organization_membership", {**content, "position_title": "主任"})


def test_workplace_subject_is_exactly_one_and_interval_is_valid():
    content = dict(
        location_source_key="loc:office",
        purpose="office",
        is_primary=False,
        basis_reference="正式安排",
    )
    assert change("person_workplace", {**content, "assignment_source_key": "job:one"})
    for invalid in (
        content,
        {**content, "person_source_key": "person:one", "assignment_source_key": "job:one"},
        {
            **content,
            "person_source_key": "person:one",
            "valid_to": datetime(2026, 8, 1, tzinfo=UTC),
        },
    ):
        with pytest.raises(ValidationError):
            change("person_workplace", invalid)


def test_full_policy_text_survives_wire_and_digest_with_old_limit_intact():
    content = dict(
        policy_source_key="policy:one",
        business_code="P1",
        display_name="制度",
        category_code="general",
        classification="public",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        effective_from=datetime(2026, 9, 1, tzinfo=UTC),
        content_text=" 正文\n" * 10000,
    )
    item = change("policy_version", content)
    plan = c.CanonicalFactChangeSetV1(
        schema_version=2, source_sha256="a" * 64, base_version=1, changes=(item,)
    )
    restored = c.CanonicalFactChangeSetV1.model_validate_json(
        plan.model_dump_json(exclude_none=True)
    )
    assert restored.changes[0].content.content_text == content["content_text"]
    assert c.canonical_plan_sha256(restored) == c.canonical_plan_sha256(plan)
    from uuid import uuid4

    from app.platform.school_facts.executor_contracts import SchoolFactExecutorPreflightRequest

    request = SchoolFactExecutorPreflightRequest(
        school_id=uuid4(), change_set=plan, context_token="a" * 80, session_binding="f2-session"
    )
    transported = SchoolFactExecutorPreflightRequest.model_validate_json(
        request.model_dump_json(exclude_none=True)
    )
    assert transported.change_set.changes[0].content.content_text == content["content_text"]
    assert c.canonical_plan_sha256(transported.change_set) == c.canonical_plan_sha256(plan)

    with pytest.raises(ValidationError):
        change("policy_version", content, 1)
    with pytest.raises(ValidationError):
        c.CanonicalFactContentV1(content_text="长" * 1001)
    with pytest.raises(ValidationError):
        change("policy_version", {**content, "content_text": "长" * 100001})


def test_quality_is_typed_scoped_and_not_arbitrary_negative_assertion():
    content = dict(
        quality_kind="negative",
        target_fact_kind="organization_location",
        owner_kind="organization",
        organization_source_key="org:one",
        purpose="office",
        negative_code="no_office",
        basis_reference="正式核实",
        responsibility="总务处",
        verified_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert change("fact_quality", content)
    for invalid in (
        {**content, "target_fact_kind": "student"},
        {**content, "negative_code": "arbitrary_truth"},
        {**content, "owner_kind": "school"},
    ):
        with pytest.raises(ValidationError):
            change("fact_quality", invalid)


def test_f2_references_include_assignment_channel_and_typed_quality_target():
    from app.platform.school_facts.application import SchoolFactChangeService

    workplace = change(
        "person_workplace",
        dict(
            assignment_source_key="job:one",
            location_source_key="loc:one",
            purpose="office",
            is_primary=False,
            basis_reference="basis",
        ),
    )
    assert (
        "assignment_source_key",
        "assignment",
        "job:one",
    ) in SchoolFactChangeService._reference_bindings(workplace)
    service = change(
        "school_service",
        dict(
            display_name="Service",
            organization_source_key="org:one",
            audience_description="staff",
            service_summary="service",
            official_contact_source_key="contact:one",
            basis_reference="basis",
        ),
    )
    assert (
        "official_contact_source_key",
        "official_contact",
        "contact:one",
    ) in SchoolFactChangeService._reference_bindings(service)


def test_catalog_is_public_and_old_schema_keeps_normalization():
    allowed, required = c.fact_content_fields("organization_membership")
    assert "membership_kind" in required <= allowed
    assert c.fact_content_enums("organization_membership")["membership_kind"] == frozenset(
        {"member", "participant"}
    )
    with pytest.raises(ValueError):
        c.fact_content_fields("organization_membership", schema_version=1)
    old = change(
        "policy_version",
        dict(
            policy_source_key="policy:one",
            business_code="P",
            display_name="Policy",
            category_code="general",
            classification="public",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
            effective_from=datetime(2026, 9, 1, tzinfo=UTC),
            content_text="  original  ",
        ),
        1,
    )
    assert old.content.content_text == "original"
