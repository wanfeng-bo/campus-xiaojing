"""F7 quality claims about F3 facts retain F3 ownership and no new negatives."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import CanonicalFactChangeV1, fact_content_fields
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

AT = datetime(2026, 9, 24, tzinfo=UTC)
FAMILY_TARGETS = frozenset(
    {
        "student",
        "student_school_relationship",
        "login_mapping",
        "student_contact",
        "guardian_relationship",
        "person_region",
    }
)


def quality(target_kind: str, **extra):
    return CanonicalFactChangeV1.model_validate(
        {
            "schema_version": 4,
            "operation": "upsert",
            "fact_kind": "fact_quality",
            "source_key": "f7:quality:test",
            "expected_version": 0,
            "effective_at": AT,
            "origin": {"sheet_code": "f7", "row_number": 2},
            "content": {
                "quality_kind": "source",
                "target_fact_kind": target_kind,
                "owner_kind": "person",
                "person_source_key": "person:student-a",
                "target_source_key": "f3:target-a",
                "basis_reference": "合成学校发布依据",
                "responsibility": "学籍维护者",
                "verified_at": AT,
                "source_status": "available",
                **extra,
            },
        }
    )


@pytest.mark.parametrize("target_kind", sorted(FAMILY_TARGETS))
def test_f3_quality_target_and_owned_field_are_canonical(target_kind):
    field = sorted(F3_FACT_FIELDS[target_kind])[0]
    change = quality(target_kind, field_name=field)
    assert change.content.target_fact_kind == target_kind
    assert change.content.field_name == field
    assert "field_name" in fact_content_fields("fact_quality", 4)[0]


def test_family_quality_rejects_an_unowned_field_and_a_formal_absence_claim():
    with pytest.raises(ValidationError):
        quality("student", field_name="external_subject")
    with pytest.raises(ValidationError):
        quality("guardian_relationship", quality_kind="negative", negative_code="no_guardian")


def test_family_complete_coverage_needs_a_verified_population_snapshot_not_free_text():
    with pytest.raises(ValidationError):
        quality(
            "student_school_relationship",
            quality_kind="coverage",
            coverage_status="complete",
            source_status=None,
            evidence_reference="管理员自行备注完整",
        )
    assert quality(
        "student_school_relationship", quality_kind="coverage",
        coverage_status="partial", source_status=None,
    ).content.coverage_status == "partial"


def test_prior_v2_quality_contract_does_not_silently_gain_f3_targets():
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(
            quality("student").model_dump(mode="python") | {"schema_version": 2}
        )


def test_model_and_append_only_revision_allow_exactly_the_six_family_targets():
    import ast
    from pathlib import Path

    from app.platform.school_facts.models import FactQuality

    check = next(
        constraint
        for constraint in FactQuality.__table__.constraints
        if constraint.name == "ck_school_fact_qualities_target"
    )
    expression = str(check.sqltext)
    for kind in FAMILY_TARGETS:
        assert f"'{kind}'" in expression
    migration = (
        Path(__file__).parents[2]
        / "alembic/versions/20260924_46_f7_family_quality_targets.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260922_45"' in migration
    assert "DROP CONSTRAINT ck_school_fact_qualities_target" in migration
    assert "ADD CONSTRAINT ck_school_fact_qualities_target" in migration
    revision = ast.parse(migration)
    targets = next(
        ast.literal_eval(statement.value)
        for statement in revision.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "QUALITY_TARGETS"
    )
    assert len(set(targets)) == len(targets)
    for kind in FAMILY_TARGETS:
        assert kind in targets
    assert "ck_school_fact_qualities_negative" not in migration


@pytest.mark.parametrize("target_kind", sorted(FAMILY_TARGETS))
def test_f3_quality_writer_checks_the_actual_student_or_person_owner(target_kind):
    from app.composition.school_fact_persistence import (
        SqlAlchemySchoolFactPersistence,
    )
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school, target, person, unrelated = (uuid4() for _ in range(4))
    calls = []

    class Session:
        def __init__(self, actual):
            self.actual = actual

        def execute(self, statement, parameters):
            calls.append((str(statement), dict(parameters)))
            return SimpleNamespace(scalar_one_or_none=lambda: self.actual)

    SqlAlchemySchoolFactPersistence._require_f3_quality_owner(
        Session(person), school, target_kind, target, person
    )
    assert len(calls) == 1
    assert calls[0][1] == {"school": school, "id": target}
    assert "school_id=:school" in calls[0][0]
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._require_f3_quality_owner(
            Session(unrelated), school, target_kind, target, person
        )


def test_f3_quality_writer_refuses_an_unlisted_target_kind_before_sql():
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    class Session:
        def execute(self, *_):
            raise AssertionError("Unlisted type reached SQL")

    with pytest.raises(ValueError):
        SqlAlchemySchoolFactPersistence._require_f3_quality_owner(
            Session(), uuid4(), "fact_quality", uuid4(), uuid4()
        )


def test_f3_correction_replacement_uses_formal_temporal_lineage_not_f2_only_content():
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school, target = uuid4(), uuid4()
    seen = []

    class Session:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, statement, parameters):
            seen.append((str(statement), dict(parameters)))
            return SimpleNamespace(all=lambda: self.rows)

    assert (
        SqlAlchemySchoolFactPersistence._f3_quality_target_at(
            Session([SimpleNamespace(target_id=target)]),
            school,
            "student_contact",
            "contact:student-a",
            AT,
        )
        == target
    )
    statement, params = seen[0]
    assert "school_fact_change_lineage" in statement
    assert "master_data_import_units" in statement
    assert "t.operation IN ('end','revoke')" in statement
    assert params["kind"] == "student_contact"
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._f3_quality_target_at(
            Session([]), school, "student_contact", "contact:student-a", AT
        )


def test_family_quality_reader_sql_requires_reader_role_anchor_and_exact_targets():
    from pathlib import Path

    migration = (
        Path(__file__).parents[2]
        / "alembic/versions/20260924_46_f7_family_quality_targets.py"
    ).read_text(encoding="utf-8")
    assert "CREATE FUNCTION app.school_fact_f7_family_quality" in migration
    assert "SESSION_USER<>'campus_ai_school_fact_reader'" in migration
    assert "app.require_school_database_anchor()" in migration
    assert "q.source_key=ANY(p_quality_sources)" in migration
    assert "q.target_fact_kind=p_target_kind" in migration
    assert "q.target_source_key=p_target_source" in migration
    assert "q.field_name=ANY(p_fields)" in migration
    assert "q.field_name IS NULL OR q.field_name=ANY(p_fields)" not in migration
    assert "b.mode='canonical_atomic_changes'" in migration
    assert "b.status='applied'" in migration
    # A later formal correction may describe an earlier factual period. The
    # publication clock must not be confused with the requested fact time.
    assert "q.verified_at<=statement_timestamp()" in migration
    assert "cardinality(p_quality_sources)>100" in migration
    assert "GRANT EXECUTE ON FUNCTION app.school_fact_f7_family_quality" in migration
    assert "CREATE FUNCTION app.school_fact_f7_family_quality_detail" in migration
    assert "GRANT EXECUTE ON FUNCTION app.school_fact_f7_family_quality_detail" in migration
    assert "TO campus_ai_school_fact_reader" in migration
    assert "q.target_fact_kind IN (" in migration


def test_current_family_quality_snapshot_needs_read_grants_and_current_period():
    from app.composition.school_fact_family_quality import FamilyQualityCurrentContent
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.test_secure_reader_projection import _actor

    actor = _actor()
    scope = F2FactScope(actor.school_id, "fact_quality", ("quality:a",), ())
    calls = []

    class Guard:
        def require_current(self, who, kind, *, purpose):
            calls.append(("quality", purpose))
            assert who == actor and kind == "fact_quality" and purpose == "read"
            return SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))

        def revalidate(self, permit, who):
            assert who == actor

    class Authority:
        def acquire(self, **kwargs):
            calls.append(("target", kwargs["purpose"]))
            assert kwargs["purpose"] == "read"
            assert kwargs["field_names"] == ("status",)
            return SimpleNamespace(revalidate=lambda who, at: calls.append(("revalidated", at)))

    current = {
        "quality_kind": "source", "target_fact_kind": "student",
        "target_source_key": "student:a", "field_name": "status",
        "person_source_key": "person:a", "source_status": "available",
    }

    class Projection:
        version = 12

        def current_version(self, school):
            assert school == actor.school_id
            return self.version

        def quality_coordinates(self, school, key):
            assert key == "quality:a"
            return {"version": 2, "content": current}

        def quality_detail(self, school, key):
            return {"version": 2, "content": current}

    class Reader:
        records = ({
            "source_key": "quality:a", "version": 2,
            "valid_from": AT.isoformat(), "valid_to": None,
        },)

        def read(self, **kwargs):
            calls.append(("formal", kwargs["quality_key"]))
            assert kwargs["target_kind"] == "student"
            return SimpleNamespace(
                records=self.records, school_fact_version=12,
                revalidate=lambda who: calls.append(("formal_revalidated", who)),
            )

    projection = Projection()
    reader = Reader()
    result = FamilyQualityCurrentContent(
        f2_guard=Guard(), authorizer=Authority(), projection=projection,
        reader=reader, clock=lambda: AT,
    )(actor, "quality:a")
    assert result["version"] == 2
    assert result["valid_from"] == AT.isoformat()
    assert result["school_fact_version"] == 12
    assert ("quality", "read") in calls and ("target", "read") in calls
    result["proof"].revalidate()
    projection.version = 13
    with pytest.raises(PermissionError):
        result["proof"].revalidate()
    reader.records = ()
    with pytest.raises(PermissionError):
        FamilyQualityCurrentContent(
            f2_guard=Guard(), authorizer=Authority(), projection=projection,
            reader=reader, clock=lambda: AT,
        )(actor, "quality:a")
