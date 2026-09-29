"""Actual SSR/education Writer and Reader regression; authorization fixture is explicit."""

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_external import EducationExternalReader
from app.composition.school_fact_education_query import EducationQueryService
from app.composition.school_fact_education_reader import EducationReadProof, EducationReadResult
from tests.school_facts.test_f4_reader_postgres import query
from tests.school_facts.test_f4_temporal_cohort_postgres import fact, setup_school
from tests.school_facts.test_f4_writer_postgres import apply, writer  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit owned F4 PG target required"
)


def test_real_suspension_return_and_unassigned_personal_grade_do_not_inherit_class(writer):  # noqa: F811
    k, _rule, now = setup_school(writer)
    keys = {
        "education_class": (k["class"],),
        "class_placement": (k["placement"],),
        "student_class_membership": (k["member"],),
        "student_grade_placement": (k["student"] + ":grade",),
    }
    clock = [now]

    def version(*_args):
        with writer[0]["reader"].connect() as c:
            return c.scalar(
                text("SELECT app.school_fact_current_version(:s)"), {"s": writer[1].school_id}
            )

    class Repository:
        clock = staticmethod(lambda: clock[0])
        projection = SimpleNamespace(current_version=version)
        authority = SimpleNamespace(revalidate=lambda *args, **kwargs: None)

        def reference_for(self, actor, kind, key):
            return "f4:" + kind + ":" + key

        def read_records(
            self, actor, kind, fields, source_keys=None, filter=None, as_of=None, known_at=None
        ):
            selected = source_keys if source_keys is not None else keys.get(kind, ())
            raw = (
                query(
                    writer, kind, list(selected), list(fields), as_of or clock[0], known_at, filter
                )
                if selected
                else {"records": [], "school_fact_version": version()}
            )
            return EducationReadResult(
                tuple(raw["records"]),
                raw["school_fact_version"],
                as_of or clock[0],
                EducationReadProof(self, actor, (), raw["school_fact_version"], None),
                "available",
            )

    def timeline(**kwargs):
        kwargs["before_read"]()
        with writer[0]["reader"].connect() as c:
            raw = c.scalar(
                text("SELECT app.school_fact_f4_ssr_timeline(:s,:key,:fields)"),
                {
                    "s": writer[1].school_id,
                    "key": kwargs["source_key"],
                    "fields": list(kwargs["field_names"]()),
                },
            )
        kwargs["after_read"]()
        return raw

    family_repo = SimpleNamespace(
        _clock=lambda: clock[0],
        evidence=SimpleNamespace(version=version),
        source_for_reference=lambda actor, ref: (
            ("student", k["student"])
            if ref == "f3:student:fixture"
            else ("student_school_relationship", k["ssr"])
        ),
        reference_for=lambda **kwargs: "f3:student:fixture",
        query=lambda **kwargs: {"status": "known", "records": [{"reference": "f3:ssr:fixture"}]},
        _authorize_scope=lambda *args: SimpleNamespace(
            scope=SimpleNamespace(
                field_names=("student_source_key", "enrollment_status", "valid_to")
            ),
            target=k["ssr"],
        ),
        _revalidate=lambda *args: None,
        _projection=SimpleNamespace(education_timeline=timeline),
    )
    external = EducationExternalReader(
        directory=None, evidence=None, family=SimpleNamespace(repository=family_repo)
    )
    service = EducationQueryService(Repository(), external_reader=external)

    def grade():
        return service.query(
            writer[1], student_ref="f3:student:fixture", fields=("grade",), as_of=clock[0]
        ).public_result

    assert grade()["grade"]["name"] == "Level 1"
    suspended = now + timedelta(seconds=1)
    # Preserve the original SSR content; a school status is not a duplicate F4 fact.
    with writer[0]["reader"].connect() as c:
        raw = c.scalar(
            text("SELECT app.school_fact_f4_ssr_timeline(:s,:key,:fields)"),
            {
                "s": writer[1].school_id,
                "key": k["ssr"],
                "fields": [
                    "student_source_key",
                    "student_number",
                    "enrollment_status",
                    "residence_mode",
                    "primary_campus_source_key",
                    "administrative_campus_source_key",
                ],
            },
        )
    content = raw["segments"][0]["content"]
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                k["ssr"],
                dict(content, enrollment_status="suspended"),
                suspended,
                version=1,
            )
        ],
    )
    clock[0] = suspended
    assert grade()["grade"]["status"] == "unknown"
    resumed = now + timedelta(seconds=2)
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                k["ssr"],
                dict(content, enrollment_status="enrolled"),
                resumed,
                version=2,
            )
        ],
    )
    clock[0] = resumed
    assert grade()["grade"]["status"] == "unknown"
    apply(
        writer,
        [
            fact(
                "student_grade_placement",
                keys["student_grade_placement"][0],
                dict(
                    student_source_key=k["student"],
                    calendar_source_key=k["calendar"],
                    grade_source_key=k["g2"],
                ),
                resumed,
            )
        ],
    )
    assert grade()["grade"]["name"] == "Level 2"
    unassigned = now + timedelta(seconds=3)
    apply(
        writer,
        [
            fact(
                "student_class_membership",
                k["member"],
                dict(
                    student_source_key=k["student"],
                    school_relationship_source_key=k["ssr"],
                    membership_status="unassigned",
                ),
                unassigned,
                version=1,
            ),
            fact(
                "student_class_position", k["holder"], None, unassigned, version=1, operation="end"
            ),
        ],
    )
    clock[0] = unassigned
    value = grade()
    assert value["grade"]["name"] == "Level 2" and value["membership_status"] == "unassigned"
    graduated = now + timedelta(seconds=4)
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                k["ssr"],
                dict(content, enrollment_status="graduated"),
                graduated,
                version=3,
            ),
            fact(
                "student_class_membership", k["member"], None, graduated, version=2, operation="end"
            ),
        ],
    )
    clock[0] = graduated
    assert grade()["grade"]["status"] == "unknown"
    clock[0] = resumed
    assert grade()["grade"]["name"] == "Level 2"
