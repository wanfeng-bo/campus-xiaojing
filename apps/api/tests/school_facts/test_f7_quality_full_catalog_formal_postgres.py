"""Owned F7 catalog quality publication through the real Writer and Readers.

Governance/identity setup is an isolated test fixture, not an original-school
grant. Target facts must have real applied Writer lineage; this file never
manufactures school facts, publication batches or lineage with direct SQL.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_authority import create_education_authority
from app.composition.school_fact_education_execution import EducationDependencyFinalGuard
from app.composition.school_fact_education_quality import (
    EducationQualityBridge,
    SqlEducationDependencyProjection,
)
from app.composition.school_fact_education_reader import SqlEducationProjection
from app.composition.school_fact_education_settings import EducationConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_residence_authority import create_residence_authority
from app.composition.school_fact_residence_execution import ResidenceQualityFinalGuard
from app.composition.school_fact_residence_quality import ResidenceQualityBridge
from app.composition.school_fact_residence_reader import SqlResidenceProjection
from app.composition.school_fact_residence_settings import (
    ResidenceConfiguration,
    ResidenceScopeBinding,
)
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.domains.students.education_contracts import EDUCATION_FIELDS
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4, fact_content_fields
from app.platform.school_facts.f6_policy import F6FactScope
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_f7_education_quality_formal_postgres import _grant_education
from tests.school_facts.test_f7_family_quality_postgres import _directory, _grant_test_quality
from tests.school_facts.test_f7_quality_full_catalog_postgres import _approved_kinds

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def _owned_configuration():
    __tracebackhide__ = True
    path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    return path, config


def _foundation_entries(school, marker, at):
    """Finite complete input; all school-fact writes use Canonical Writer below."""
    keys = {
        kind: f"f7:catalog:{kind}:{marker}" for kind, owner in _approved_kinds() if owner != "F3"
    }
    keys.update(
        school=school.source_key,
        campus=f"f7:catalog:campus:{marker}",
        building=f"f7:catalog:building:{marker}",
        predecessor=f"f7:catalog:predecessor:{marker}",
        child_person=f"f7:catalog:child-person:{marker}",
        student=f"f7:catalog:student:{marker}",
        enrollment=f"f7:catalog:enrollment:{marker}",
    )
    k = keys
    rows = []

    def add(kind, content, *, source=None, expected=0):
        rows.append(
            dict(
                schema_version=4,
                operation="upsert",
                fact_kind=kind,
                source_key=source or k[kind],
                expected_version=expected,
                effective_at=at,
                content=content,
                origin={"sheet_code": "f7_catalog_foundation", "row_number": len(rows) + 1},
            )
        )

    add(
        "school",
        dict(
            business_code=school.school_code,
            display_name=school.display_name,
            timezone_name=school.timezone_name,
            lifecycle_status=school.status,
        ),
        expected=school.version,
    )
    for alias, kind, parent in (
        ("campus", "campus", None),
        ("building", "building", "campus"),
        ("location", "dorm_room", "building"),
    ):
        content = dict(
            kind=kind,
            display_name="隔离质量验收" + alias,
            display_path="/隔离质量验收/" + alias,
            status="active",
        )
        if parent:
            content["parent_source_key"] = k[parent]
        add("location", content, source=k[alias])
    add(
        "organization",
        dict(
            kind="department",
            display_name="隔离质量验收组织",
            display_path="/隔离质量验收组织",
            status="active",
        ),
    )
    for alias in ("person", "child_person"):
        add(
            "person",
            dict(
                display_name="隔离质量验收" + alias,
                person_kind="staff" if alias == "person" else "other",
                lifecycle_status="active",
            ),
            source=k[alias],
        )
    add(
        "assignment",
        dict(
            person_source_key=k["person"],
            organization_source_key=k["organization"],
            position_title="隔离任教",
            semantic_role_code="teacher",
            assignment_kind="substantive",
            appointment_source="manual",
            approval_reference="isolated-quality-fixture",
            status="confirmed",
        ),
    )
    add("student", dict(person_source_key=k["child_person"], status="active"))
    add(
        "student_school_relationship",
        dict(
            student_source_key=k["student"],
            student_number="F7-" + marker,
            enrollment_status="enrolled",
            residence_mode="boarding",
            primary_campus_source_key=k["campus"],
            administrative_campus_source_key=k["campus"],
        ),
        source=k["enrollment"],
    )
    add(
        "organization_campus_relationship",
        dict(organization_source_key=k["organization"], campus_source_key=k["campus"]),
    )
    add(
        "location_alias",
        dict(location_source_key=k["location"], alias="隔离房间简称", alias_kind="common"),
    )
    add(
        "person_contact",
        dict(
            person_source_key=k["person"],
            contact_kind="email",
            contact_value="fixture@example.invalid",
            is_primary=True,
            visibility_scope="private",
        ),
    )
    add(
        "official_contact",
        dict(
            owner_kind="organization",
            organization_source_key=k["organization"],
            contact_kind="email",
            contact_value="office@example.invalid",
            lifecycle_status="active",
            visibility_scope="public",
        ),
    )
    add("policy", {})
    add(
        "policy_scope",
        dict(policy_source_key=k["policy"], organization_source_key=k["organization"]),
    )
    add(
        "policy_version",
        dict(
            policy_source_key=k["policy"],
            business_code="F7-" + marker,
            display_name="隔离质量验收制度",
            category_code="general",
            classification="public",
            published_at=at,
            effective_from=at,
            content_text="只用于隔离工程质量验证。",
        ),
    )
    basis = "F7隔离正式测试依据"
    add(
        "organization_profile",
        dict(
            organization_source_key=k["organization"],
            organization_type="teaching_research_group",
            function_summary="隔离正式质量验证",
            basis_reference=basis,
        ),
    )
    add(
        "organization_membership",
        dict(
            person_source_key=k["person"],
            organization_source_key=k["organization"],
            membership_kind="member",
            basis_reference=basis,
        ),
    )
    add(
        "organization_location",
        dict(
            organization_source_key=k["organization"],
            location_source_key=k["location"],
            purpose="office",
            is_primary=True,
            basis_reference=basis,
        ),
    )
    add(
        "person_workplace",
        dict(
            person_source_key=k["person"],
            location_source_key=k["location"],
            purpose="office",
            is_primary=True,
            basis_reference=basis,
        ),
    )
    add(
        "school_service",
        dict(
            display_name="隔离服务",
            organization_source_key=k["organization"],
            audience_description="隔离验收者",
            service_summary="只说明渠道",
            basis_reference=basis,
            official_contact_source_key=k["official_contact"],
        ),
    )
    add("education_stage", dict(display_name="隔离学段"))
    add(
        "education_offering",
        dict(stage_source_key=k["education_stage"], campus_source_key=k["campus"]),
    )
    add(
        "education_grade",
        dict(display_name="隔离年级", stage_source_key=k["education_stage"], grade_order=1),
    )
    add(
        "education_calendar_rule",
        dict(
            display_name="隔离校历",
            stage_source_key=k["education_stage"],
            start_cycle=2026,
            month=9,
            day=1,
            timezone_name="Asia/Shanghai",
            grade_source_keys=[k["education_grade"]],
        ),
    )
    add(
        "education_term",
        dict(
            display_name="隔离学期",
            calendar_source_key=k["education_calendar_rule"],
            academic_year=2026,
            valid_to=at + timedelta(days=120),
        ),
    )
    add(
        "education_grade_group",
        dict(
            calendar_source_key=k["education_calendar_rule"],
            academic_year=2026,
            grade_source_key=k["education_grade"],
            campus_source_key=k["campus"],
        ),
    )
    add(
        "education_cohort",
        dict(
            display_name="隔离入学批次",
            stage_source_key=k["education_stage"],
            cohort_kind="original_intake",
            academic_year=2026,
        ),
    )
    add(
        "student_cohort_membership",
        dict(student_source_key=k["student"], cohort_source_key=k["education_cohort"]),
    )
    for alias in ("predecessor", "education_class"):
        add(
            "education_class",
            dict(
                display_name="隔离班" + alias,
                teacher_mode="single_primary",
                teacher_role_limits={"primary": 1},
                primary_classroom_limit=1,
            ),
            source=k[alias],
        )
    add(
        "class_placement",
        dict(
            class_source_key=k["education_class"],
            calendar_source_key=k["education_calendar_rule"],
            grade_source_key=k["education_grade"],
            campus_source_key=k["campus"],
            class_number=1,
        ),
    )
    add(
        "class_lineage",
        dict(
            class_source_key=k["education_class"],
            predecessor_class_source_key=k["predecessor"],
            lineage_kind="regrouped_from",
        ),
    )
    add(
        "student_grade_placement",
        dict(
            student_source_key=k["student"],
            calendar_source_key=k["education_calendar_rule"],
            grade_source_key=k["education_grade"],
        ),
    )
    add(
        "student_class_membership",
        dict(
            student_source_key=k["student"],
            school_relationship_source_key=k["enrollment"],
            membership_status="assigned",
            class_source_key=k["education_class"],
        ),
    )
    add(
        "class_teacher_assignment",
        dict(
            class_source_key=k["education_class"],
            assignment_source_key=k["assignment"],
            teacher_role="primary",
        ),
    )
    add(
        "class_location",
        dict(
            class_source_key=k["education_class"],
            location_source_key=k["location"],
            location_purpose="main_classroom",
        ),
    )
    add(
        "class_position_definition",
        dict(
            class_source_key=k["education_class"],
            display_name="班长",
            allow_multiple=False,
            allow_concurrent=True,
            max_holders=1,
        ),
    )
    add(
        "student_class_position",
        dict(
            class_source_key=k["education_class"],
            student_source_key=k["student"],
            position_source_key=k["class_position_definition"],
        ),
    )
    add("teaching_subject", dict(display_name="隔离学科", stage_source_keys=[k["education_stage"]]))
    add("teaching_course", dict(display_name="隔离课程"))
    add(
        "course_subject",
        dict(course_source_key=k["teaching_course"], subject_source_key=k["teaching_subject"]),
    )
    add("teaching_group", dict(organization_source_key=k["organization"], group_mode="shared"))
    add("teaching_offering", dict(course_source_key=k["teaching_course"], offering_status="active"))
    add(
        "offering_member_source",
        dict(
            offering_source_key=k["teaching_offering"],
            source_kind="independent_group",
            group_source_key=k["teaching_group"],
        ),
    )
    add(
        "teaching_group_member",
        dict(
            group_source_key=k["teaching_group"],
            student_source_key=k["student"],
            school_relationship_source_key=k["enrollment"],
        ),
    )
    add(
        "teaching_assignment",
        dict(
            offering_source_key=k["teaching_offering"],
            assignment_source_key=k["assignment"],
            teaching_role="lead",
        ),
    )
    add(
        "teaching_organization_scope",
        dict(
            organization_source_key=k["organization"],
            subject_source_key=k["teaching_subject"],
            scope_basis="stage",
            stage_source_key=k["education_stage"],
        ),
    )
    add(
        "dormitory_building_profile",
        dict(
            location_source_key=k["building"],
            usage_kind="building",
            display_name="隔离宿舍楼",
            is_available=True,
        ),
    )
    add(
        "dorm_room",
        dict(
            room_source_key=k["location"],
            authorized_bed_capacity=1,
            accommodation_zone="mixed",
            is_available=True,
        ),
    )
    add(
        "dorm_bed",
        dict(
            room_source_key=k["location"],
            business_code="B-" + marker,
            display_name="隔离床位",
            availability="available",
        ),
    )
    add(
        "student_bed_residence", dict(student_source_key=k["student"], bed_source_key=k["dorm_bed"])
    )
    return keys, rows


def _confirm(service, context, identity, changes):
    report = service.preflight(
        school_id=context.actor.school_id,
        change_set=changes,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert report.is_clean, [(i.row_number, i.column_code, i.error_code) for i in report.issues]
    prepared = service.prepare_confirmation(
        change_set=changes, context=context, session_binding=identity.session_nonce_hash
    )
    receipt = service.confirm(
        credential=prepared.credential,
        change_set=changes,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    replay = service.confirm(
        credential=prepared.credential,
        change_set=changes,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert replay.replayed and replay.batch_id == receipt.batch_id
    return receipt


@pytest.fixture(scope="module")
def catalog():
    path, config = _owned_configuration()
    engines = {
        role: safe_test_engine(path, role) for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with engines["migration"].connect() as connection:
            database, system = connection.execute(
                text("SELECT current_database(),system_identifier::text FROM pg_control_system()")
            ).one()
            assert (database, system) == (config["database"], config["system_identifier"])
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260924_55"
            )
            school = connection.scalar(text("SELECT school_id FROM school_database_anchor"))
            school_row = connection.execute(
                text("SELECT * FROM schools WHERE id=:school"), {"school": school}
            ).one()
        with pytest.MonkeyPatch.context() as patch:

            def safe_database_failure(error):
                cause = getattr(error, "orig", None)
                detail = getattr(cause, "diag", None)
                raise AssertionError(
                    "Owned fixture database rejection: "
                    + str(getattr(cause, "sqlstate", None))
                    + " / "
                    + str(getattr(detail, "constraint_name", None))
                ) from None

            patch.setattr(
                SqlAlchemySchoolFactPersistence,
                "_raise_db_error",
                staticmethod(safe_database_failure),
            )
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            actor, identity, _, service = existing_fixture_writer(engines)
            marker, at = uuid4().hex, datetime.now(UTC)
            keys, rows = _foundation_entries(school_row, marker, at)
            context = compose_command_context(actor, uuid4(), "f7-catalog-foundation:" + marker)
            with engines["reader"].connect() as connection:
                base = connection.scalar(
                    text("SELECT app.school_fact_current_version(:s)"), {"s": school}
                )
            changes = CanonicalFactChangeSetV4.model_validate_json(
                json.dumps(
                    dict(
                        schema_version=4,
                        base_version=base,
                        source_sha256=sha256(marker.encode()).hexdigest(),
                        changes=rows,
                    ),
                    default=str,
                )
            )
            receipt = _confirm(service, context, identity, changes)
            assert receipt.changed_count == len(rows)
            with engines["migration"].connect() as connection:
                actual = connection.execute(
                    text(
                        "SELECT fact_kind,fact_source_key FROM school_fact_change_lineage "
                        "WHERE school_id=:s AND batch_id=:b"
                    ),
                    {"s": school, "b": receipt.batch_id},
                ).all()
            assert {(c.fact_kind, c.source_key) for c in changes.changes} == set(actual)
            yield SimpleNamespace(
                scenario=scenario, engines=engines, school=school, config=config, keys=keys, at=at
            )
    finally:
        for engine in engines.values():
            engine.dispose()


def test_owned_catalog_formal_target_prerequisites(catalog):
    assert set(kind for kind, owner in _approved_kinds() if owner != "F3") <= set(catalog.keys)


def _grant_residence(scenario, actor, kind, key, field, action):
    """Exact disposable governance fixture; real F6 authorization reads it."""
    scope = F6FactScope(scenario.school, kind, (key,), (), (field,))
    at = datetime.now(UTC) - timedelta(seconds=1)
    duty = f"f6_{kind}_{'reader' if action == 'read' else 'maintainer'}"
    with scenario.owner.begin() as session:
        duty_id = session.scalar(
            text("SELECT id FROM governance_duties WHERE school_id=:s AND duty_code=:d"),
            {"s": scenario.school, "d": duty},
        )
        if duty_id is None:
            duty_id = uuid4()
            scenario._add(
                session,
                "governance_duties",
                id=duty_id,
                school_id=scenario.school,
                duty_code=duty,
                current_version=1,
            )
            scenario._add(
                session,
                "governance_duty_versions",
                school_id=scenario.school,
                duty_id=duty_id,
                display_name="F7隔离逐类质量职责",
                description="TEST FIXTURE ONLY",
                is_active=True,
                effective_from=at,
                version=1,
            )
        scope_id, grant_id = uuid4(), uuid4()
        scenario._add(
            session,
            "governance_scopes",
            id=scope_id,
            school_id=scenario.school,
            scope_key="f7-catalog:" + scope_id.hex,
            current_version=1,
        )
        scenario._add(
            session,
            "governance_scope_versions",
            school_id=scenario.school,
            scope_id=scope_id,
            schema_owner="school_facts.f6",
            schema_name="exact_residence_fact_set",
            schema_version=1,
            reference_document=scope.document,
            reference_sha256=scope.document_sha256,
            is_active=True,
            effective_from=at,
            version=1,
        )
        scenario._add(
            session,
            "governance_grants",
            id=grant_id,
            school_id=scenario.school,
            assignment_id=actor.assignment_id,
            duty_id=duty_id,
            current_version=1,
        )
        scenario._add(
            session,
            "governance_grant_versions",
            school_id=scenario.school,
            grant_id=grant_id,
            duty_id=duty_id,
            duty_version=1,
            scope_id=scope_id,
            scope_version=1,
            basis_authority="isolated_test_fixture",
            basis_reference="F7-owned-catalog",
            valid_from=at,
            effective_from=at,
            planned_valid_to=at + timedelta(hours=1),
            transition_kind="create",
            version=1,
        )
        session.execute(
            text(
                "INSERT INTO governance_assignment_epochs(school_id,assignment_id,epoch) "
                "VALUES(:s,:a,1) ON CONFLICT(school_id,assignment_id) DO UPDATE "
                "SET epoch=governance_assignment_epochs.epoch+1"
            ),
            {"s": scenario.school, "a": actor.assignment_id},
        )
    return ResidenceScopeBinding(
        assignment_id=actor.assignment_id,
        operation_code=f"school_facts.f6.{kind}.{action}",
        scope_id=scope_id,
    )


def _education_bridge(catalog, actor, directory, bindings, owner):
    authority = create_education_authority(
        sessions=catalog.scenario.sessions["reader"],
        configuration=EducationConfiguration(
            approval_reference="F7 isolated catalog fixture", scope_bindings=bindings
        ),
    )
    projection = SqlEducationProjection(
        F2ConnectionReadExecutor(
            catalog.scenario.sessions["reader"], authority.connection_authority
        ),
        current_sessions=authority.connection_authority.current_sessions,
    )
    return EducationQualityBridge(
        f2_guard=directory.guard,
        education_authority=authority,
        projection=SqlEducationDependencyProjection(projection, f5_enabled=owner == "F5"),
    )


def _residence_bridge(catalog, directory, bindings):
    authority = create_residence_authority(
        sessions=catalog.scenario.sessions["reader"],
        configuration=ResidenceConfiguration(
            approval_reference="F7 isolated catalog fixture", scope_bindings=bindings
        ),
    )
    return ResidenceQualityBridge(
        f2_guard=directory.guard,
        residence_authority=authority,
        projection=SqlResidenceProjection(catalog.scenario.sessions["reader"]),
    )


# Reuse the prior F3 six-type and these four formal cases. Exact residence
# remains here because its prior positive evidence concerned only a scope.
CASES = tuple(
    (kind, owner)
    for kind, owner in _approved_kinds()
    if owner != "F3"
    and kind
    not in {
        "organization",
        "education_grade_group",
        "class_teacher_assignment",
        "teaching_assignment",
    }
)


@pytest.mark.parametrize("kind,owner", CASES)
def test_missing_catalog_quality_formal_writer_current_reader_and_target_denial(
    catalog, kind, owner
):
    scenario, config = catalog.scenario, catalog.config
    identity = scenario.identity()
    actor = identity.staff()
    target = catalog.keys[kind]
    quality_key = "f7:catalog-quality:" + kind + ":" + uuid4().hex
    create = _grant_test_quality(scenario, actor, quality_key, purpose="maintain", creation=True)
    read = _grant_test_quality(scenario, actor, quality_key, purpose="read", creation=False)
    quality_bindings = (create, read)
    at = datetime.now(UTC)
    content = dict(
        quality_kind="source",
        source_status="unavailable",
        target_fact_kind=kind,
        target_source_key=target,
        owner_kind="school",
        basis_reference="隔离逐类正式质量核验",
        responsibility="隔离质量维护者",
        verified_at=at,
        valid_to=catalog.at + timedelta(days=1),
    )
    field = None
    if owner == "F2":
        targets = tuple(
            _grant_test_quality(
                scenario, actor, target, purpose=action, creation=False, fact_kind=kind
            )
            for action in ("maintain", "read")
        )
        school = (
            ()
            if kind == "school"
            else (
                _grant_test_quality(
                    scenario,
                    actor,
                    catalog.keys["school"],
                    purpose="read",
                    creation=False,
                    fact_kind="school",
                ),
            )
        )
        directory = _directory(scenario, config, (*quality_bindings, *targets, *school))
        guard = directory.guard
    else:
        directory = _directory(scenario, config, quality_bindings)
        if owner in {"F4", "F5"}:
            field = sorted(EDUCATION_FIELDS[kind][1])[0]
            content.update(
                owner_kind="education",
                education_target_kind=kind,
                education_target_source_key=target,
                field_name=field,
            )
            targets = tuple(
                _grant_education(scenario, actor, kind=kind, key=target, field=field, action=action)
                for action in ("maintain", "read")
            )
            bridge = _education_bridge(catalog, actor, directory, targets, owner)
            guard = EducationDependencyFinalGuard(
                quality=bridge,
                retained=lambda *_args: pytest.fail("Unexpected noneducation partition"),
            )
        else:
            field = sorted(fact_content_fields(kind, schema_version=4)[1])[0]
            content["field_name"] = field
            targets = tuple(
                _grant_residence(scenario, actor, kind, target, field, action)
                for action in ("maintain", "read")
            )
            bridge = _residence_bridge(catalog, directory, targets)
            guard = ResidenceQualityFinalGuard(bridge)
    context = compose_command_context(actor, uuid4(), quality_key)
    persistence = SqlAlchemySchoolFactPersistence(
        scenario.sessions["writer"],
        identity.envelope,
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
            config["context"].encode()
        ),
        final_authorization_guard=guard,
    )
    service = SchoolFactChangeService(reader=persistence, writer=persistence)
    base = persistence.for_request(context).current_school_fact_version(catalog.school)
    changes = CanonicalFactChangeSetV4.model_validate_json(
        json.dumps(
            dict(
                schema_version=4,
                source_sha256=sha256(quality_key.encode()).hexdigest(),
                base_version=base,
                changes=[
                    dict(
                        schema_version=4,
                        operation="upsert",
                        fact_kind="fact_quality",
                        source_key=quality_key,
                        expected_version=0,
                        effective_at=at,
                        origin={"sheet_code": "f7_formal_catalog", "row_number": 1},
                        content=content,
                    )
                ],
            ),
            default=str,
        )
    )
    if owner == "F2":
        assert guard.require_change_set(actor, changes)
    else:
        bridge.require_change_set(actor, changes).revalidate()
    receipt = _confirm(service, context, identity.session, changes)
    assert receipt.changed_count == 1
    with catalog.engines["migration"].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT import_batch_id FROM school_fact_qualities "
                    "WHERE school_id=:s AND source_key=:q"
                ),
                {"s": catalog.school, "q": quality_key},
            )
            == receipt.batch_id
        )
    if owner == "F2":
        reference = directory.query.reference_for(
            actor=actor, fact_kind="fact_quality", source_key=quality_key
        )
        result = directory.query.query(
            actor=actor, fact_kind="fact_quality", reference=reference, factual_at=at
        )
        assert result.status == "known" and len(result.records) == 1
        received = result.records[0].content
        denied = _directory(scenario, config, quality_bindings)
        denied_reference = denied.query.reference_for(
            actor=actor, fact_kind="fact_quality", source_key=quality_key
        )
        rejected = denied.query.query(
            actor=actor, fact_kind="fact_quality", reference=denied_reference, factual_at=at
        )
        assert rejected.status == "forbidden" and not rejected.records
    else:
        if owner in {"F4", "F5"}:
            result = bridge.read_for_source(actor, "fact_quality", quality_key, at)
            denied = _education_bridge(catalog, actor, directory, (targets[0],), owner)
            with pytest.raises((PermissionError, SchoolFactChangeRejected)):
                denied.read_for_source(actor, "fact_quality", quality_key, at)
        else:
            result = bridge.read_content(actor, quality_key)
            denied = _residence_bridge(catalog, directory, (targets[0],))
            with pytest.raises((PermissionError, SchoolFactChangeRejected)):
                denied.read_content(actor, quality_key)
        result["proof"].revalidate()
        received = result["content"]
        assert received["field_name"] == field
    assert received["source_status"] == "unavailable"
    assert received["target_fact_kind"] == kind
    if owner == "F2":
        assert (
            directory.query.source_for(
                actor=actor, fact_kind=kind, reference=received["target_ref"]
            )
            == target
        )
    else:
        assert received["target_source_key"] == target
