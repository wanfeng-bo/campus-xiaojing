from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from tests.school_facts.test_f4_education_reader import actor

AT = datetime(2027, 9, 2, tzinfo=UTC)


def module():
    name = "app.composition.school_fact_education_query"
    assert find_spec(name) is not None, "Education query composition is absent"
    return import_module(name)


def record(kind, key, content, start="2026-09-01T00:00:00+08:00"):
    return {
        "fact_kind": kind,
        "source_key": key,
        "version": 1,
        "status": "available",
        "segments": [
            {
                "revision": 1,
                "effective_at": start,
                "valid_to": None,
                "recorded_at": "2026-08-01T00:00:00+08:00",
                "superseded_at": None,
                "content": content,
                "source": {"source_ref": "学校正式登记"},
            }
        ],
    }


class Repository:
    def __init__(self):
        self.clock = lambda: AT
        self.calls = []
        self.read_reference = lambda actor, kind, key: "opaque:" + key
        self.projection = SimpleNamespace(current_version=lambda school: 10)
        self.authority = SimpleNamespace(revalidate=lambda *a, **k: None)
        self.rows = {
            "education_class": [record("education_class", "class:a", {"display_name": "2023班"})],
            "class_placement": [
                record(
                    "class_placement",
                    "place:a",
                    {
                        "class_source_key": "class:a",
                        "calendar_source_key": "cal:a",
                        "grade_source_key": "grade:1",
                        "campus_source_key": "campus:a",
                        "class_number": 3,
                    },
                )
            ],
            "education_calendar_rule": [
                record(
                    "education_calendar_rule",
                    "cal:a",
                    {
                        "start_cycle": 2026,
                        "end_cycle": None,
                        "month": 9,
                        "day": 1,
                        "timezone_name": "Asia/Shanghai",
                        "grade_source_keys": ["grade:1", "grade:2", "grade:3"],
                    },
                )
            ],
            "education_grade": [
                record("education_grade", "grade:1", {"display_name": "初一"}),
                record("education_grade", "grade:2", {"display_name": "初二"}),
                record("education_grade", "grade:3", {"display_name": "初三"}),
            ],
            "class_teacher_assignment": [
                record(
                    "class_teacher_assignment",
                    "teacher:a",
                    {
                        "class_source_key": "class:a",
                        "assignment_source_key": "assignment:a",
                        "teacher_role": "primary",
                    },
                )
            ],
        }

    def source_for_reference(self, actor, fact_kind, reference, creation=False):
        return reference.removeprefix("opaque:")

    def reference_for(self, actor, fact_kind, source_key, creation=False):
        return "opaque:" + source_key

    def read_records(self, actor, kind, fields=None, source_keys=None, filter=None, **kwargs):
        from app.composition.school_fact_education_reader import (
            EducationReadProof,
            EducationReadResult,
        )

        self.calls.append((kind, filter, source_keys, fields))
        rows = self.rows.get(kind, [])
        if source_keys is not None:
            rows = [r for r in rows if r["source_key"] in source_keys]
        if filter:
            rows = [
                r
                for r in rows
                if any(
                    all(
                        s["content"].get(k) in (v if isinstance(v, list) else [v])
                        for k, v in filter.items()
                    )
                    for s in r["segments"]
                )
            ]
        return EducationReadResult(
            tuple(rows),
            10,
            kwargs.get("as_of") or AT,
            EducationReadProof(self, actor, (), 10, None),
            "available",
            tuple(
                (row["source_key"], self.read_reference(actor, kind, row["source_key"]))
                for row in rows
            ),
        )


class External:
    def read_records(self, actor, kind, source_keys, fields, as_of):
        content = {
            "assignment": {"person_source_key": "person:a"},
            "person": {"display_name": "陈老师"},
            "location": {"display_name": "合成甲校区"},
        }
        return {
            "records": [
                {"source_key": key, "content": content.get(kind, {})} for key in source_keys
            ],
            "proof": SimpleNamespace(revalidate=lambda: None, school_fact_version=10),
        }


@pytest.mark.parametrize("historical", (False, True))
def test_external_boundary_is_retained_and_only_expires_current_time_answer(historical):
    from datetime import timedelta

    repo = Repository()
    external = External()
    original = external.read_records
    calls = []
    boundary = AT + timedelta(minutes=1)

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        return {
            **result,
            "next_valid_boundary": boundary,
            "boundary_proof": SimpleNamespace(
                school_fact_version=10, revalidate=lambda: calls.append(True)
            ),
        }

    external.read_records = read
    result = (
        module()
        .EducationQueryService(repo, external_reader=external)
        .query(
            actor(),
            name="2023班",
            fields=("teachers",),
            as_of=AT if historical else None,
        )
    )
    assert calls
    repo.clock = lambda: boundary
    if historical:
        result.private_proof.revalidate(actor=actor())
    else:
        with pytest.raises(module().EducationReadStale):
            result.private_proof.revalidate(actor=actor())


def test_changed_external_boundary_proof_blocks_education_release():
    repo = Repository()
    external = External()
    original = external.read_records
    changed = False

    def validate():
        if changed:
            raise module().EducationReadStale("External metadata changed")

    def read(*args, **kwargs):
        return {
            **original(*args, **kwargs),
            "boundary_proof": SimpleNamespace(school_fact_version=10, revalidate=validate),
        }

    external.read_records = read
    result = (
        module()
        .EducationQueryService(repo, external_reader=external)
        .query(actor(), name="2023班", fields=("teachers",))
    )
    changed = True
    with pytest.raises(module().EducationReadStale):
        result.private_proof.revalidate(actor=actor())


def test_fixed_class_name_derives_grade_and_teacher_relation_is_filtered_in_sql():
    repo = Repository()
    result = (
        module()
        .EducationQueryService(repo, external_reader=External())
        .query(actor(), name="2023班", fields=("grade", "teachers"))
    )
    assert result.public_result["status"] == "known"
    assert "初二" in result.public_result["text"]
    assert "陈老师" in result.public_result["text"]
    teacher_calls = [c for c in repo.calls if c[0] == "class_teacher_assignment"]
    assert teacher_calls[0][1] == {"class_source_key": "class:a"}
    assert "source_key" not in str(result.public_result)


def test_grade_and_number_use_formal_placement_not_digits_in_class_name():
    repo = Repository()
    service = module().EducationQueryService(repo, external_reader=External())
    assert (
        service.query(actor(), grade_name="初二", class_number=3, fields=("grade",)).public_result[
            "status"
        ]
        == "known"
    )
    assert (
        service.query(
            actor(), grade_name="初二", class_number=2023, fields=("grade",)
        ).public_result["status"]
        == "not_recorded"
    )


def test_same_name_classes_stay_ambiguous_and_no_teacher_rows_are_loaded():
    repo = Repository()
    repo.rows["education_class"].append(
        record("education_class", "class:b", {"display_name": "2023班"})
    )
    result = (
        module().EducationQueryService(repo).query(actor(), name="2023班", fields=("teachers",))
    )
    assert result.public_result["status"] == "ambiguous"
    assert len(result.public_result["candidates"]) == 2
    assert not any(c[0] == "class_teacher_assignment" for c in repo.calls)


def test_absent_teacher_record_is_not_declared_formal_vacancy():
    repo = Repository()
    repo.rows["class_teacher_assignment"] = []
    result = (
        module().EducationQueryService(repo).query(actor(), name="2023班", fields=("teachers",))
    )
    assert result.public_result["teachers"]["status"] == "not_recorded"
    assert "空缺" not in result.public_result["text"]


def test_other_requested_facts_are_explicit_and_relation_queries_are_class_scoped():
    repo = Repository()
    result = (
        module()
        .EducationQueryService(repo)
        .query(
            actor(),
            name="2023班",
            fields=("classroom", "positions", "members", "cohorts", "official_contacts"),
        )
    )
    for field in ("classroom", "positions", "members", "cohorts", "official_contacts"):
        assert field in result.public_result, f"Requested {field} must not be silently omitted"
    for kind, filters, _keys, _fields in repo.calls:
        if kind in {"class_location", "student_class_position", "student_class_membership"}:
            assert filters["class_source_key"] == "class:a"


def test_external_version_mismatch_is_not_composed():

    class Changed(External):
        def read_records(self, *args, **kwargs):
            result = super().read_records(*args, **kwargs)
            result["proof"].school_fact_version = 11
            return result

    with pytest.raises(module().EducationReadStale):
        module().EducationQueryService(Repository(), external_reader=Changed()).query(
            actor(), name="2023班", fields=("teachers",)
        )


def test_external_permission_failure_is_not_presented_as_missing_name():

    class Denied(External):
        def read_records(self, *args, **kwargs):
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        module().EducationQueryService(Repository(), external_reader=Denied()).query(
            actor(), name="2023班", fields=("teachers",)
        )


def test_explicit_class_quality_is_required_for_known_teacher_absence():
    class Quality(External):
        def quality_for(self, who, **kwargs):
            assert kwargs["owner_kind"] == "education_class"
            assert kwargs["owner_source_keys"] == ("class:a",)
            return {
                "statements": [
                    {
                        "content": {
                            "quality_kind": "negative",
                            "negative_code": "no_class_teacher",
                            "target_source_key": None,
                            "field_name": None,
                            "basis_reference": "学校核实当前未安排",
                        }
                    }
                ],
                "proof": SimpleNamespace(revalidate=lambda: None, school_fact_version=10),
            }

    repo = Repository()
    repo.rows["class_teacher_assignment"] = []
    result = (
        module()
        .EducationQueryService(repo, external_reader=Quality())
        .query(actor(), name="2023班", fields=("teachers",))
    )
    assert result.public_result["teachers"]["status"] == "known_negative"
    assert "学校核实当前未安排" in str(result.public_result["teachers"])


def test_reader_calendar_clips_explicit_end_and_ignores_empty_same_year_fragment():
    repo = Repository()
    calendar = repo.rows["education_calendar_rule"][0]
    segment = calendar["segments"][0]
    segment["content"]["end_cycle"] = 2030
    segment["valid_to"] = "2027-09-01T00:00:00+08:00"
    placement = repo.rows["class_placement"][0]
    resolve = module().resolve_grade_from_records
    value = resolve(
        at=AT, calendars=(calendar,), placement_record=placement, placement=placement["segments"][0]
    )
    assert value.status == "unknown"
    segment["valid_to"] = "2026-08-31T00:00:00+08:00"
    value = resolve(
        at=AT, calendars=(calendar,), placement_record=placement, placement=placement["segments"][0]
    )
    assert value.status == "unknown"


class StudentExternal(External):
    def __init__(self, status="enrolled", resumed=None):
        self.status, self.resumed = status, resumed

    def source_for_reference(self, actor, kind, reference):
        return "student:a"

    def grade_arrangement(self, actor, **kwargs):
        return {
            "status": self.status,
            "effective_at": "2027-09-01T00:00:00+08:00",
            "last_suspended_until": self.resumed,
            "proof": SimpleNamespace(revalidate=lambda: None, school_fact_version=10),
        }


def student_repository(*, assigned=True, origin=True):
    repo = Repository()
    repo.rows["student_class_membership"] = [
        record(
            "student_class_membership",
            "membership:a",
            {
                "student_source_key": "student:a",
                "school_relationship_source_key": "ssr:a",
                "membership_status": "assigned" if assigned else "unassigned",
                "class_source_key": "class:a" if assigned else None,
            },
        )
    ]
    if origin:
        repo.rows["student_grade_placement"] = [
            record(
                "student_grade_placement",
                "personal:a",
                {
                    "student_source_key": "student:a",
                    "calendar_source_key": "cal:a",
                    "grade_source_key": "grade:1",
                },
                start="2027-09-01T00:00:00+08:00",
            )
        ]
    return repo


def test_personal_grade_is_not_replaced_by_class_grade_and_other_fields_continue():
    repo = student_repository()
    answer = (
        module()
        .EducationQueryService(repo, external_reader=StudentExternal())
        .query(actor(), student_ref="student-handle", fields=("grade", "teachers"))
        .public_result
    )
    assert answer["grade"]["name"] == "初一"
    assert answer["teachers"]["items"][0]["name"] == "陈老师"


def test_unassigned_student_keeps_explicit_grade_and_other_requested_results():
    repo = student_repository(assigned=False)
    repo.rows["student_cohort_membership"] = [
        record(
            "student_cohort_membership",
            "cohort-member:a",
            {"student_source_key": "student:a", "cohort_source_key": "cohort:a"},
        )
    ]
    repo.rows["education_cohort"] = [
        record(
            "education_cohort",
            "cohort:a",
            {"display_name": "原入学届", "cohort_kind": "original_intake"},
        )
    ]
    answer = (
        module()
        .EducationQueryService(repo, external_reader=StudentExternal())
        .query(actor(), student_ref="student-handle", fields=("grade", "teachers", "cohorts"))
        .public_result
    )
    assert answer["grade"]["name"] == "初一"
    assert answer["membership_status"] == "unassigned"
    assert answer["teachers"]["status"] == "not_recorded"
    assert answer["cohorts"]["items"][0]["name"] == "原入学届"


def test_suspension_or_return_without_post_boundary_personal_origin_never_copies_class():
    for status, resumed in (("suspended", None), ("enrolled", "2027-09-01T00:00:00+08:00")):
        repo = student_repository(origin=False)
        answer = (
            module()
            .EducationQueryService(repo, external_reader=StudentExternal(status, resumed))
            .query(actor(), student_ref="student-handle", fields=("grade", "teachers"))
            .public_result
        )
        assert answer["grade"]["status"] == "unknown"
        assert answer["grade"]["name"] is None
        assert answer["teachers"]["items"]


def test_student_and_explicit_class_reference_must_both_match():
    repo = student_repository()
    answer = (
        module()
        .EducationQueryService(repo, external_reader=StudentExternal())
        .query(
            actor(),
            student_ref="student-handle",
            class_reference="opaque:other-class",
            fields=("grade",),
        )
        .public_result
    )
    assert answer["status"] == "not_recorded"


def test_query_local_exact_read_cache_is_independent_of_candidate_count_and_never_crosses_queries():
    repo = Repository()
    for index in range(7):
        key = f"class:extra{index}"
        repo.rows["education_class"].append(
            record("education_class", key, {"display_name": f"Independent {index}"})
        )
        repo.rows["class_placement"].append(
            record(
                "class_placement",
                f"place:extra{index}",
                {
                    "class_source_key": key,
                    "calendar_source_key": "cal:a",
                    "grade_source_key": "grade:1",
                    "campus_source_key": "campus:a",
                    "class_number": index + 20,
                },
            )
        )
    service = module().EducationQueryService(repo, external_reader=External())
    for run in (1, 2):
        answer = service.query(
            actor(), grade_name="初二", class_number=3, fields=("grade",)
        ).public_result
        assert answer["name"] == "2023班"
        assert sum(kind == "education_calendar_rule" for kind, *_ in repo.calls) == run
        assert sum(kind == "education_grade" for kind, *_ in repo.calls) == run


@pytest.mark.parametrize(
    "status,label", [("suspended", "休学，保留原班关联"), ("unknown", "在校状态未明确")]
)
def test_member_list_keeps_explicit_school_status_separate_from_class_link(status, label):
    answer = (
        module()
        .EducationQueryService(student_repository(), external_reader=StudentExternal(status=status))
        .query(actor(), class_reference="opaque:class:a", fields=("members",))
        .public_result
    )
    assert answer["members"]["items"][0]["status_label"] == label
    assert answer["members"]["items"][0]["enrollment_status"] == (
        status if status != "unknown" else None
    )


def test_member_status_denial_never_turns_into_a_known_or_normal_school_status():
    class Denied(StudentExternal):
        def grade_arrangement(self, actor, **kwargs):
            raise PermissionError("exact SSR fields denied before SQL")

    with pytest.raises(PermissionError, match="before SQL"):
        module().EducationQueryService(student_repository(), external_reader=Denied()).query(
            actor(), class_reference="opaque:class:a", fields=("members",)
        )


@pytest.mark.parametrize("status", ["graduated", "transferred_out"])
def test_exited_student_has_no_current_derived_grade_but_past_enrolled_grade_is_kept(status):
    repo = student_repository()
    external = StudentExternal(status=status)
    service = module().EducationQueryService(repo, external_reader=external)
    assert (
        service.query(actor(), student_ref="student-handle", fields=("grade",)).public_result[
            "grade"
        ]["status"]
        == "unknown"
    )
    external.status = "enrolled"  # The same controlled reader's factual-time response.
    assert (
        service.query(actor(), student_ref="student-handle", fields=("grade",)).public_result[
            "grade"
        ]["status"]
        == "resolved"
    )


def test_ambiguous_candidates_include_authorized_location_grade_number_without_teachers():
    from copy import deepcopy

    repo = Repository()
    other = deepcopy(repo.rows["education_class"][0])
    other["source_key"] = "class:b"
    repo.rows["education_class"].append(other)
    placement = deepcopy(repo.rows["class_placement"][0])
    placement["source_key"] = "place:b"
    placement["segments"][0]["content"].update(class_source_key="class:b", class_number=4)
    repo.rows["class_placement"].append(placement)
    result = (
        module()
        .EducationQueryService(repo, external_reader=External())
        .query(actor(), name="2023班", fields=("teachers",))
    )
    assert result.public_result["status"] == "ambiguous"
    candidates = result.public_result["candidates"]
    assert [c["class_number"] for c in candidates] == [3, 4]
    assert all(c["grade_name"] == "初二" and c["campus_name"] for c in candidates)
    assert not any(call[0] == "class_teacher_assignment" for call in repo.calls)
    assert result.public_result["as_of"] == AT.isoformat()
    assert result.public_result["known_at"] is None


def test_ambiguous_name_only_scope_never_guesses_denied_coordinates_or_reads_teachers():
    from copy import deepcopy

    repo = Repository()
    other = deepcopy(repo.rows["education_class"][0])
    other["source_key"] = "class:b"
    repo.rows["education_class"].append(other)
    original = repo.read_records

    def narrow(actor, kind, *args, **kwargs):
        if kind == "class_placement":
            raise PermissionError("no locator grant")
        return original(actor, kind, *args, **kwargs)

    repo.read_records = narrow
    result = (
        module()
        .EducationQueryService(repo, external_reader=External())
        .query(actor(), name="2023班", fields=("teachers",))
    )
    assert result.public_result["status"] == "ambiguous"
    assert all(set(c) == {"reference", "name"} for c in result.public_result["candidates"])
    assert not any(call[0] == "class_teacher_assignment" for call in repo.calls)


def test_class_query_reuses_references_from_authorized_reads():
    repo = Repository()

    def unexpected_issue(*args, **kwargs):
        pytest.fail("query reissued a grant for an already-read class")

    repo.reference_for = unexpected_issue
    result = (
        module()
        .EducationQueryService(repo, external_reader=External())
        .query(actor(), name="2023班", fields=("grade",))
    )
    assert result.public_result["reference"] == "opaque:class:a"


@pytest.mark.parametrize(
    "field,kind,content,applies_to",
    [
        (
            "teachers",
            "class_teacher_assignment",
            {"assignment_source_key": "assignment:a", "teacher_role": "primary"},
            "班主任任职关系",
        ),
        (
            "classroom",
            "class_location",
            {"location_source_key": "location:a", "location_purpose": "main_classroom"},
            "班级固定地点安排",
        ),
        (
            "positions",
            "student_class_position",
            {"student_source_key": "student:a", "position_source_key": "position:a"},
            "班内学生职务关系",
        ),
        (
            "members",
            "student_class_membership",
            {
                "student_source_key": "student:a",
                "school_relationship_source_key": "ssr:a",
                "membership_status": "assigned",
            },
            "学生与行政班关系",
        ),
    ],
)
def test_class_query_publication_is_bound_to_each_returned_relationship(
    field, kind, content, applies_to
):
    from copy import deepcopy

    repo = Repository()
    first = record(kind, "link:a", {"class_source_key": "class:a", **content})
    second = deepcopy(first)
    second["source_key"] = "link:b"
    for key in ("assignment_source_key", "location_source_key", "student_source_key"):
        if key in second["segments"][0]["content"]:
            second["segments"][0]["content"][key] = content[key].replace(":a", ":b")
    for row, label in ((first, "第一项正式依据"), (second, "第二项正式依据")):
        row["segments"][0]["source"] = {
            "summary": label,
            "published_at": "2026-08-02T00:00:00Z",
            "source_ref": "internal-source",
            "publication_reference": "internal-publication",
        }
    unrelated = deepcopy(first)
    unrelated["source_key"] = "other-class"
    unrelated["segments"][0]["content"]["class_source_key"] = "class:other"
    unrelated["segments"][0]["source"]["summary"] = "另一班不得混入的来源"
    repo.rows[kind] = [first, second, unrelated]
    repo.rows["class_position_definition"] = [
        record(
            "class_position_definition",
            "position:a",
            {"class_source_key": "class:a", "display_name": "班长"},
        )
    ]

    class Quality(StudentExternal):
        def quality_for(self, who, **kwargs):
            return {
                "statements": [],
                "proof": SimpleNamespace(revalidate=lambda: None, school_fact_version=10),
            }

    result = (
        module()
        .EducationQueryService(repo, external_reader=Quality())
        .query(actor(), class_reference="opaque:class:a", fields=(field,))
        .public_result[field]
    )
    assert len(result["items"]) == 2
    assert result["quality_basis"] == []
    assert result["coverage"] == "unknown"
    for item, label in zip(result["items"], ("第一项正式依据", "第二项正式依据"), strict=True):
        assert item["publication_evidence"] == [
            {
                "applies_to": applies_to,
                "basis": {
                    "label": label,
                    "published_at": "2026-08-02T00:00:00Z",
                    "recorded_at": "2026-08-01T00:00:00+08:00",
                },
                "effective_at": "2026-09-01T00:00:00+08:00",
                "valid_to": None,
            }
        ]
    assert "internal-source" not in str(result)
    assert "internal-publication" not in str(result)
    assert "另一班不得混入" not in str(result)
    assert len([call for call in repo.calls if call[0] == kind]) == 1


def test_member_publication_uses_the_requested_period_not_future_or_ended_links():
    from copy import deepcopy

    repo = student_repository()
    row = repo.rows["student_class_membership"][0]
    old = row["segments"][0]
    old["valid_to"] = "2027-09-01T00:00:00Z"
    old["source"] = {"label": "原归班依据"}
    current = deepcopy(old)
    current.update(effective_at=old["valid_to"], valid_to=None, source={"label": "当前归班依据"})
    row["segments"].append(current)
    query = module().EducationQueryService(repo, external_reader=StudentExternal())
    for at, expected in ((datetime(2027, 8, 30, tzinfo=UTC), "原归班依据"), (AT, "当前归班依据")):
        result = query.query(
            actor(), class_reference="opaque:class:a", fields=("members",), as_of=at
        )
        assert (
            result.public_result["members"]["items"][0]["publication_evidence"][0]["basis"]["label"]
            == expected
        )


def test_empty_member_result_does_not_invent_publication_or_borrow_the_class_source():
    result = (
        module()
        .EducationQueryService(Repository(), external_reader=StudentExternal())
        .query(actor(), class_reference="opaque:class:a", fields=("members",))
        .public_result["members"]
    )
    assert result["items"] == []
    assert result["coverage"] == "unknown"
    assert "publication_evidence" not in result


@pytest.mark.parametrize("individual", (False, True))
def test_cohort_definition_and_membership_keep_separate_publication_scope(individual):
    repo = student_repository()
    membership = record(
        "student_cohort_membership",
        "cohort-member:a",
        {
            "student_source_key": "student:a",
            "cohort_source_key": "cohort:a",
        },
    )
    membership["segments"][0]["source"] = {"label": "学生所属批次依据"}
    definition = record(
        "education_cohort",
        "cohort:a",
        {
            "display_name": "2026入学批次",
            "cohort_kind": "original_intake",
            "academic_year": 2026,
        },
    )
    definition["segments"][0]["source"] = {"label": "学校批次定义依据"}
    repo.rows["student_cohort_membership"] = [membership]
    repo.rows["education_cohort"] = [definition]
    selection = (
        {"student_ref": "student-handle"} if individual else {"class_reference": "opaque:class:a"}
    )
    result = (
        module()
        .EducationQueryService(repo, external_reader=StudentExternal())
        .query(actor(), fields=("cohorts",), **selection)
        .public_result["cohorts"]
    )
    evidence = result["items"][0]["publication_evidence"]
    assert {(item["applies_to"], item["basis"]["label"]) for item in evidence} == {
        ("届别或批次定义", "学校批次定义依据"),
        ("学生与届别批次关系", "学生所属批次依据"),
    }


def test_relation_without_source_metadata_does_not_invent_a_publication():
    repo = student_repository()
    repo.rows["student_class_membership"][0]["segments"][0]["source"] = {}
    item = (
        module()
        .EducationQueryService(repo, external_reader=StudentExternal())
        .query(actor(), class_reference="opaque:class:a", fields=("members",))
        .public_result["members"]["items"][0]
    )
    evidence = item["publication_evidence"][0]
    assert evidence["applies_to"] == "学生与行政班关系"
    assert evidence["basis"]["label"] is None
    assert evidence["basis"]["published_at"] is None


@pytest.mark.parametrize(
    "selection",
    [
        {"name": "2023班"},
        {"name": "2023班", "campus_name": "合成甲校区"},
        {"name": "2023班", "class_number": 3},
    ],
)
def test_internal_class_locator_omits_unrequested_grade_reads(selection):
    repo = Repository()
    query = module().EducationQueryService(repo, external_reader=External())
    result = query.locate_class(actor(), **selection)
    assert result.public_result["status"] == "known"
    assert "grade" not in result.public_result
    assert not {"education_calendar_rule", "education_grade"} & {c[0] for c in repo.calls}
    with pytest.raises(ValueError):
        query.query(actor(), fields=())


def test_internal_class_locator_keeps_grade_filter_and_real_ambiguity():
    repo = Repository()
    query = module().EducationQueryService(repo, external_reader=External())
    result = query.locate_class(actor(), name="2023班", grade_name="初二")
    assert result.public_result["status"] == "known"
    assert any(c[0] == "education_grade" for c in repo.calls)
    repo.rows["education_class"].append(
        record("education_class", "class:b", {"display_name": "2023班"})
    )
    result = query.locate_class(actor(), name="2023班")
    assert result.public_result["status"] == "ambiguous"
    assert result.public_result["candidates"][0]["grade_name"] == "初二"


@pytest.mark.parametrize(
    "at,expected", [(AT, "known"), (datetime(2028, 1, 1, tzinfo=UTC), "not_recorded")]
)
def test_internal_class_locator_respects_requested_fact_time_and_class_end(at, expected):
    repo = Repository()
    repo.rows["education_class"][0]["segments"][0]["valid_to"] = "2027-12-01T00:00:00Z"
    result = module().EducationQueryService(repo).locate_class(actor(), name="2023班", as_of=at)
    assert result.public_result["status"] == expected
    assert result.public_result["as_of"] == at.isoformat()
    assert {call[0] for call in repo.calls} == {"education_class"}


def test_internal_class_locator_does_not_bypass_class_read_permission():
    repo = Repository()

    def denied(*args, **kwargs):
        raise PermissionError("class name read revoked")

    repo.read_records = denied
    with pytest.raises(PermissionError):
        module().EducationQueryService(repo).locate_class(actor(), name="2023班")
