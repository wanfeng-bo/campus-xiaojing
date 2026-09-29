"""Owned fixture authority, formal SSR Writer revisions and actual family Reader.

The guardian fixture is cloned only for the new isolated student; this test does
not claim to exercise the separate two-person guardian approval workflow.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_education_maintenance import snapshot_at
from app.db.base import Base
from app.domains.students.education_change_planner import (
    EducationChangeCommand,
    plan_education_change,
)
from app.domains.students.teaching_change_planner import plan_teaching_change
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    change,
    graph,  # noqa: F401
    read_rows,
    ssr_snapshot,
)
from tests.school_facts.test_f5_writer_reader_postgres import writer as owned_writer  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="owned PG only")


@pytest.fixture
def selected(graph):  # noqa: F811
    writer, k, now, at, ssr = graph
    engines = writer[0]
    table = Base.metadata.tables["guardian_relationships"]
    relationship_id = uuid4()
    with engines["migration"].begin() as c:
        child = c.scalar(
            text("SELECT id FROM students WHERE source_key=:key"), {"key": k("student")}
        )
        values = dict(
            c.execute(
                text(
                    "SELECT * FROM guardian_relationships "
                    "WHERE relationship_kind='verified_guardian' "
                    "AND authority_version=1 AND status='confirmed' AND revoked_at IS NULL "
                    "AND valid_from<=now() AND (valid_to IS NULL OR now()<valid_to) LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
        values.update(id=relationship_id, student_id=child, valid_from=at, valid_to=None)
        c.execute(insert(table).values(**values))
    params = dict(
        s=values["school_id"],
        a=values["guardian_person_id"],
        r=relationship_id,
        v=values["version"],
    )
    try:
        yield writer, k, now, ssr, params
    finally:
        with engines["migration"].begin() as c:
            c.execute(
                update(table)
                .where(table.c.id == relationship_id, table.c.student_id == child)
                .values(status="revoked", revoked_at=datetime.now(UTC))
            )


def read(writer, params, at=None):
    with writer[0]["reader"].connect() as c:
        return c.scalar(
            text(
                "SELECT app.school_fact_family_teaching(:s,:a,:r,:v,:at,'current',NULL,NULL,false)"
            ),
            {**params, "at": at},
        )


def offering_keys(raw):
    return {r["source_key"] for r in raw["records"] if r["fact_kind"] == "teaching_offering"}


def test_formal_ssr_revision_preserves_A_B_until_formal_departure(selected):
    writer, k, now, ssr, params = selected
    expected = {k("offer-a"), k("offer-b")}
    assert offering_keys(read(writer, params)) == expected
    for version, status in ((1, "suspended"), (2, "enrolled")):
        at = datetime.now(UTC)
        apply(
            writer,
            [
                change(
                    "student_school_relationship",
                    k("ssr"),
                    at,
                    {**ssr, "enrollment_status": status},
                    expected=version,
                )
            ],
        )
        raw = read(writer, params)
        assert raw["enrollment_status"] == status
        assert offering_keys(raw) == expected, (
            "current RLS must not hide the stable SSR source join"
        )
        assert offering_keys(read(writer, params, now)) == expected
        assert read_rows(writer, "teaching_group_member", [k("member")], at)[0]["version"] == 1
        assert (
            read_rows(writer, "student_class_membership", [k("class-member")], at)[0]["version"]
            == 1
        )
    leave = datetime.now(UTC)
    member = snapshot_at(
        read_rows(writer, "teaching_group_member", [k("member")], leave)[0],
        kind="teaching_group_member",
        at=leave,
    )
    administrative = snapshot_at(
        read_rows(writer, "student_class_membership", [k("class-member")], leave)[0],
        kind="student_class_membership",
        at=leave,
    )
    snapshots = (member, administrative, ssr_snapshot(writer, k("ssr"), leave))
    keys = frozenset({k("ssr"), k("member"), k("class-member")})
    primary = change(
        "student_school_relationship",
        k("ssr"),
        leave,
        {**ssr, "enrollment_status": "transferred_out"},
        expected=3,
    )
    education = plan_education_change(
        EducationChangeCommand(
            purpose="graduation",
            effective_at=leave,
            cause_source_key=k("ssr"),
            cause_version=4,
            primary_changes=(primary,),
            student_source_key=k("student"),
        ),
        snapshots,
        authorized_keys=keys,
        complete_snapshot=True,
    )
    assert not education.issues
    planned = plan_teaching_change(
        education.changes, snapshots, now=now, authorized_keys=keys, complete_snapshot=True
    )
    assert not planned.issues
    result = apply(writer, list(planned.changes))
    assert result.changed_count == 3
    assert not offering_keys(read(writer, params))
    apply(
        writer,
        [change("student_school_relationship", k("ssr"), datetime.now(UTC), ssr, expected=4)],
    )
    assert not offering_keys(read(writer, params)), (
        "returning enrollment must not resurrect independently ended memberships"
    )
    for altered in ({**params, "s": uuid4()}, {**params, "a": uuid4()}):
        with pytest.raises(DBAPIError):
            read(writer, altered)


def test_member_cannot_rebind_to_another_students_formal_relationship(selected):
    writer, k, _, ssr, params = selected
    at = datetime.now(UTC)
    apply(
        writer,
        [
            change(
                "person",
                k("other-person"),
                at,
                {
                    "display_name": "另一隔离学生",
                    "person_kind": "other",
                    "lifecycle_status": "active",
                },
            ),
            change(
                "student",
                k("other-student"),
                at,
                {"person_source_key": k("other-person"), "status": "active"},
            ),
            change(
                "student_school_relationship",
                k("other-ssr"),
                at,
                {
                    **ssr,
                    "student_source_key": k("other-student"),
                    "student_number": uuid4().hex[:12],
                },
            ),
        ],
    )
    baseline = offering_keys(read(writer, params))
    for kind, name in (
        ("teaching_group_member", "member"),
        ("student_class_membership", "class-member"),
    ):
        content = snapshot_at(read_rows(writer, kind, [k(name)], at)[0], kind=kind, at=at).content
        with pytest.raises(SchoolFactChangeRejected):
            apply(
                writer,
                [
                    change(
                        kind,
                        k(name),
                        datetime.now(UTC),
                        {**content, "school_relationship_source_key": k("other-ssr")},
                        expected=1,
                    )
                ],
            )
        assert offering_keys(read(writer, params)) == baseline
        assert read_rows(writer, kind, [k(name)], datetime.now(UTC))[0]["version"] == 1
