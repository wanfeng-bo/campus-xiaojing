"""F7 complete claims for education/residence require a current exact fact."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CoverageBasisV1
from app.platform.school_facts.f7_coverage_basis import mother_set_digest

AT = datetime(2026, 9, 24, tzinfo=UTC)


def claim(school, *, kind, target, owner_kind, owner_key, version=4):
    basis = CoverageBasisV1(
        school_id=school,
        owner_kind=owner_kind,
        owner_source_key=owner_key,
        target_fact_kind=kind,
        target_source_key=target,
        field_name=None,
        purpose=None,
        contact_kind=None,
        valid_from=AT,
        valid_to=None,
        school_fact_version=12,
        member_count=1,
        member_sha256=mother_set_digest(((target, version),))[1],
    )
    return SimpleNamespace(
        effective_at=AT,
        content=SimpleNamespace(
            quality_kind="coverage",
            coverage_status="complete",
            target_fact_kind=kind,
            target_source_key=target,
            owner_kind=owner_kind,
            coverage_basis=basis,
        ),
    )


@pytest.mark.parametrize("kind", ["class_teacher_assignment", "teaching_assignment"])
def test_writer_checks_f4_f5_exact_target_in_school_and_at_claim_time(monkeypatch, kind):
    school = uuid4()
    change = claim(
        school, kind=kind, target="education:target", owner_kind="education",
        owner_key="education:owner",
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence,
        "_current_global_fact_version",
        staticmethod(lambda _session, _school: 12),
    )

    class Session:
        def __init__(self, version):
            self.version = version
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((str(sql), params))
            return self

        def scalar_one_or_none(self):
            return self.version

    good = Session(4)
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        good, school, change, {"education_target_id": uuid4()}
    )
    assert len(good.calls) == 1
    sql, params = good.calls[0]
    assert "school_education_facts" in sql
    assert "school_education_versions" in sql
    assert params["school"] == school
    assert params["kind"] == kind
    assert params["target"] == "education:target"
    assert params["at"] == AT
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(5), school, change, {"education_target_id": uuid4()}
        )


def test_writer_does_not_accept_missing_education_target_as_complete(monkeypatch):
    school = uuid4()
    change = claim(
        school,
        kind="class_teacher_assignment",
        target="education:target",
        owner_kind="education",
        owner_key="education:owner",
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence,
        "_current_global_fact_version",
        staticmethod(lambda _session, _school: 12),
    )

    class Session:
        def execute(self, _sql, _params):
            return self

        def scalar_one_or_none(self):
            return None

    with pytest.raises(SchoolFactChangeRejected, match="时点不可核实"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(), school, change, {"education_target_id": uuid4()}
        )


@pytest.mark.parametrize(
    ("kind", "expected_table"),
    [
        ("dormitory_building_profile", "dormitory_building_profile_versions"),
        ("dorm_room", "dorm_room_profile_versions"),
        ("dorm_bed", "dorm_bed_versions"),
        ("student_bed_residence", "student_bed_residences"),
    ],
)
def test_writer_checks_exact_f6_target_at_claim_time(monkeypatch, kind, expected_table):
    school = uuid4()
    change = claim(
        school, kind=kind, target="f6:target", owner_kind="location",
        owner_key="location:owner",
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence,
        "_current_global_fact_version",
        staticmethod(lambda _session, _school: 12),
    )

    class Session:
        def __init__(self, version):
            self.version, self.calls = version, []

        def execute(self, sql, params):
            self.calls.append((str(sql), params))
            return self

        def scalar_one_or_none(self):
            return self.version

    good = Session(4)
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        good, school, change, {}
    )
    assert len(good.calls) == 1
    sql, params = good.calls[0]
    assert expected_table in sql
    assert "school_id" in sql and "valid_from" in sql
    assert params["school"] == school
    assert params["target"] == "f6:target"
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(5), school, change, {}
        )
    with pytest.raises(SchoolFactChangeRejected, match="时点不可核实"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(None), school, change, {}
        )
