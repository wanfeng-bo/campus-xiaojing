from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from tests.school_facts.test_f4_education_query import AT, Repository, record
from tests.school_facts.test_f4_education_reader import actor


def service():
    name = "app.composition.school_fact_staff_teaching_query"
    assert find_spec(name) is not None, "Staff teaching query is missing"
    api = import_module(name)
    repo = Repository()
    repo.rows = {
        "teaching_assignment": [
            record(
                "teaching_assignment",
                "teaching:a",
                {
                    "assignment_source_key": "assignment:a",
                    "offering_source_key": "offering:a",
                    "teaching_role": "lead",
                    "subject_source_keys": None,
                    "duty_scope": None,
                },
            )
        ],
        "teaching_offering": [
            record(
                "teaching_offering",
                "offering:a",
                {
                    "course_source_key": "course:a",
                    "display_name": "基础数学",
                    "offering_status": "active",
                },
            )
        ],
        "teaching_course": [record("teaching_course", "course:a", {"display_name": "数学课程"})],
        "course_subject": [
            record(
                "course_subject",
                "link:a",
                {"course_source_key": "course:a", "subject_source_key": "subject:a"},
            )
        ],
        "teaching_subject": [record("teaching_subject", "subject:a", {"display_name": "数学"})],
        "offering_member_source": [
            record(
                "offering_member_source",
                "source:a",
                {
                    "offering_source_key": "offering:a",
                    "source_kind": "administrative_class",
                    "class_source_key": "class:a",
                    "group_source_key": None,
                },
            )
        ],
        "education_class": [record("education_class", "class:a", {"display_name": "合成一班"})],
    }
    checks = []

    class Coordinates:
        def resolve(self, a):
            assert a.assignment_id == actor().assignment_id
            return {
                "assignment_source_key": "assignment:a",
                "person_source_key": "person:a",
                "proof": SimpleNamespace(
                    school_fact_version=10, revalidate=lambda: checks.append("identity")
                ),
            }

    class External:
        def __init__(self):
            self.calls = []

        def read_records(self, a, kind, *, source_keys, fields, as_of, include_boundaries=True):
            self.calls.append((kind, source_keys, fields))
            rows = {
                "assignment": {"person_source_key": "person:a"},
                "person": {"display_name": "陈老师"},
                "organization": {
                    "display_name": "数学组",
                    "organization_kind": "teaching_research_group",
                },
            }
            return {
                "records": [
                    {"source_key": k, "content": {f: rows[kind].get(f) for f in fields}}
                    for k in source_keys
                ],
                "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
            }

    external = External()
    query = api.StaffTeachingQueryService(
        repo, external_reader=external, identity_coordinates=Coordinates()
    )
    return query, repo, external, checks


def test_self_teaching_uses_trusted_assignment_and_related_closure_only():
    query, repo, external, checks = service()
    result = query.query(actor(), view="my_teaching")
    assert result.public_result["items"][0]["course"] == "数学课程"
    assert result.public_result["items"][0]["teachers"][0]["name"] == "陈老师"
    assert result.public_result["items"][0]["audiences"][0]["name"] == "合成一班"
    assert repo.calls[0][0:2] == ("teaching_assignment", {"assignment_source_key": "assignment:a"})
    assert all(
        kind not in {"teaching_group_member", "teaching_organization_scope"}
        for kind, *_ in repo.calls
    )
    assert all(kind != "official_contact" for kind, *_ in external.calls)
    result.private_proof.revalidate(actor=actor())
    assert checks


def test_teaching_prefetches_only_the_four_independent_formal_projections():
    query, repo, _, _ = class_teaching_service()
    waves = []

    def read_many(a, selections, *, as_of):
        waves.append(selections)
        return tuple(
            repo.read_records(
                a, s.kind, fields=s.fields, source_keys=s.source_keys, filter=s.filter, as_of=as_of
            )
            for s in selections
        )

    repo.read_many = read_many
    result = query.query(actor(), view="teaching", class_reference="opaque:class:a")
    assert result.public_result["status"] == "available"
    assert len(waves) == 1
    assert [s.kind for s in waves[0]] == [
        "teaching_course",
        "course_subject",
        "teaching_assignment",
        "offering_member_source",
    ]
    assert waves[0][0].source_keys == ("course:a",)
    assert waves[0][1].filter == {"course_source_key": "course:a"}
    assert waves[0][2].filter == {"offering_source_key": "offering:a"}
    assert waves[0][3].filter == {"offering_source_key": "offering:a"}
    assert all(len(s.fields) <= 7 for s in waves[0])
    result.private_proof.revalidate(actor=actor())


def test_teaching_read_wave_denial_does_not_fallback_to_partial_reading():
    query, repo, external, _ = class_teaching_service()

    def denied(*args, **kwargs):
        raise PermissionError("current batch authority denied")

    repo.read_many = denied
    with pytest.raises(PermissionError):
        query.query(actor(), view="teaching", class_reference="opaque:class:a")
    assert not any(kind == "person" for kind, *_ in external.calls)


def test_empty_self_teaching_keeps_proof_and_does_not_read_other_families():
    query, repo, _, _ = service()
    repo.rows["teaching_assignment"] = []
    result = query.query(actor(), view="my_teaching")
    assert result.public_result["status"] == "no_current_records"
    assert [c[0] for c in repo.calls] == ["teaching_assignment"]
    repo.projection.current_version = lambda _: 11
    with pytest.raises(RuntimeError):
        result.private_proof.revalidate(actor=actor())


def test_subject_query_starts_with_named_subject_and_uses_scalar_links():
    query, repo, _, _ = service()
    result = query.query(actor(), view="teaching", subject_name="数学")
    assert result.public_result["observed_count"] == 1
    assert repo.calls[0][0:2] == ("teaching_subject", {"display_name": "数学"})
    assert any(
        kind == "course_subject" and filters == {"subject_source_key": "subject:a"}
        for kind, filters, *_ in repo.calls
    )


def test_unbounded_teaching_or_model_supplied_identity_is_rejected():
    query, _, _, _ = service()
    with pytest.raises(ValueError):
        query.query(actor(), view="teaching")
    with pytest.raises(TypeError):
        query.query(actor(), view="my_teaching", assignment_source_key="someone:else")


def test_class_query_reuses_education_resolution_and_its_proof():
    from app.composition.school_fact_education_query import (
        EducationQueryProof,
        EducationQueryResult,
    )
    from app.composition.school_fact_education_reader import EducationReadProof

    query, repo, _, _ = service()
    seen = []

    class Classes:
        def locate_class(self, a, **kw):
            seen.append(kw)
            return EducationQueryResult(
                {"status": "known", "reference": "opaque:class:a"},
                (),
                EducationQueryProof(EducationReadProof(repo, a, (), 10, None)),
            )

    query.class_query = Classes()
    result = query.query(actor(), view="teaching", grade_name="初二", class_number=1)
    assert result.public_result["observed_count"] == 1
    assert seen[0]["grade_name"] == "初二"
    assert repo.calls[0][1] == {"class_source_key": "class:a"}


@pytest.mark.parametrize("basis", ["fixed_grade", "academic_grade"])
def test_my_organizations_follows_actual_membership_and_formal_scope(basis):
    from contextlib import contextmanager

    query, repo, external, _ = service()
    calls = []

    class Directory:
        def reference_for(self, **kwargs):
            return "person:reference"

        def source_for(self, **kwargs):
            return "organization:a"

        def query(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                status="known",
                has_more=False,
                records=(
                    SimpleNamespace(
                        content={
                            "organization_ref": "organization:reference",
                            "membership_kind": "member",
                        }
                    ),
                ),
            )

    class Evidence:
        @contextmanager
        def capture(self, **kwargs):
            yield SimpleNamespace(
                seal=lambda: SimpleNamespace(version=10, check=lambda actor: True)
            )

    external.directory = Directory()
    external.evidence = Evidence()
    repo.rows["teaching_organization_scope"] = [
        record(
            "teaching_organization_scope",
            "scope:a",
            {
                "organization_source_key": "organization:a",
                "subject_source_key": "subject:a",
                "scope_basis": basis,
                "stage_source_key": None,
                "grade_source_key": "grade:a" if basis == "fixed_grade" else None,
                "grade_group_source_key": "group:year" if basis == "academic_grade" else None,
                "term_source_key": None,
            },
        )
    ]
    repo.rows["education_grade"] = [record("education_grade", "grade:a", {"display_name": "初二"})]
    repo.rows["education_grade_group"] = [
        record(
            "education_grade_group",
            "group:year",
            {"display_name": "任意组名", "academic_year": 2027, "grade_source_key": "grade:a"},
        )
    ]
    result = query.query(actor(), view="my_organizations")
    assert result.public_result["items"][0]["name"] == "数学组"
    assert result.public_result["items"][0]["membership"] == "成员"
    assert result.public_result["items"][0]["scopes"][0]["subject"] == "数学"
    if basis == "fixed_grade":
        assert result.public_result["items"][0]["scopes"][0]["grade"] == "初二"
    else:
        assert result.public_result["items"][0]["scopes"][0]["academic_year"] == 2027
        assert result.public_result["items"][0]["scopes"][0]["academic_grade"] == "初二"
    assert calls[0]["related_ref"] == "person:reference"
    assert calls[0]["fact_kind"] == "organization_membership"
    assert not any(c[0] == "teaching_group_member" for c in repo.calls)


def test_sql_self_coordinates_have_no_model_selectors_and_close_on_revalidation():
    from app.composition.school_fact_staff_teaching_query import SqlStaffTeachingCoordinates

    calls = []
    value = {
        "assignment_source_key": "assignment:a",
        "person_source_key": "person:a",
        "school_fact_version": 10,
    }

    class Session:
        def __enter__(self):
            calls.append("open")
            return self

        def __exit__(self, *_):
            calls.append("close")

        def scalar(self, statement, parameters):
            assert set(parameters) == {"school", "person", "assignment", "as_of"}
            assert parameters["assignment"] == actor().assignment_id
            assert parameters["person"] == actor().person_id
            assert "school_fact_f5_staff_coordinates" in str(statement)
            return dict(value)

    adapter = SqlStaffTeachingCoordinates(Session, clock=lambda: AT)
    result = adapter.resolve(actor())
    result["proof"].revalidate()
    assert calls == ["open", "close", "open", "close"]
    value["school_fact_version"] = 11
    with pytest.raises(RuntimeError):
        result["proof"].revalidate()
    assert calls[-1] == "close"


def test_same_named_offerings_keep_distinct_authorized_audiences():
    query, repo, _, _ = service()
    repo.rows["teaching_offering"].append(
        record(
            "teaching_offering",
            "offering:b",
            {
                "course_source_key": "course:a",
                "display_name": "基础数学",
                "offering_status": "active",
            },
        )
    )
    repo.rows["offering_member_source"].append(
        record(
            "offering_member_source",
            "source:b",
            {
                "offering_source_key": "offering:b",
                "source_kind": "administrative_class",
                "class_source_key": "class:b",
                "group_source_key": None,
            },
        )
    )
    repo.rows["education_class"].append(
        record("education_class", "class:b", {"display_name": "合成二班"})
    )
    result = query.query(actor(), view="teaching", subject_name="数学")
    assert len(result.public_result["items"]) == 2
    assert {i["audiences"][0]["name"] for i in result.public_result["items"]} == {
        "合成一班",
        "合成二班",
    }


def shared_group_service(*, separate_groups=False, separate_organizations=False):
    from copy import deepcopy

    query, repo, external, checks = service()
    for kind in ("teaching_assignment", "teaching_offering"):
        other = deepcopy(repo.rows[kind][0])
        other["source_key"] = "teaching:b" if kind == "teaching_assignment" else "offering:b"
        if kind == "teaching_assignment":
            other["segments"][0]["content"]["offering_source_key"] = "offering:b"
        repo.rows[kind].append(other)
    repo.rows["offering_member_source"] = []
    repo.rows["teaching_group"] = []
    for suffix in ("a", "b"):
        group = suffix if separate_groups else "a"
        repo.rows["offering_member_source"].append(
            record(
                "offering_member_source",
                "source:" + suffix,
                {
                    "offering_source_key": "offering:" + suffix,
                    "source_kind": "independent_group",
                    "class_source_key": None,
                    "group_source_key": "group:" + group,
                },
            )
        )
        if suffix == "a" or separate_groups:
            repo.rows["teaching_group"].append(
                record(
                    "teaching_group",
                    "group:" + group,
                    {
                        "organization_source_key": "organization:"
                        + (suffix if separate_organizations else "a"),
                        "group_mode": "independent",
                    },
                )
            )
    return query, repo, external, checks


@pytest.mark.parametrize("separate_groups", [False, True])
def test_shared_organization_is_read_once_per_point_with_both_audiences(separate_groups):
    query, _, external, _ = shared_group_service(separate_groups=separate_groups)
    result = query.query(actor(), view="my_teaching")
    assert len(result.public_result["items"]) == 2
    assert all(item["audiences"][0]["name"] == "数学组" for item in result.public_result["items"])
    assert [call for call in external.calls if call[0] == "organization"] == [
        ("organization", ("organization:a",), ("display_name",))
    ]


def test_distinct_organizations_remain_separate_exact_reads():
    query, _, external, _ = shared_group_service(separate_groups=True, separate_organizations=True)
    result = query.query(actor(), view="my_teaching")
    assert len(result.public_result["items"]) == 2
    assert [keys for kind, keys, _ in external.calls if kind == "organization"] == [
        ("organization:a",),
        ("organization:b",),
    ]


@pytest.mark.parametrize("empty", [False, True])
def test_shared_organization_retains_current_and_boundary_proofs_at_every_release(empty):
    from datetime import timedelta

    query, repo, external, _ = shared_group_service()
    original = external.read_records
    checks = []
    boundary = AT + timedelta(days=1)

    def check(label):
        checks.append(label)
        if repo.clock() >= boundary:
            raise PermissionError("boundary passed")

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "organization":
            if empty:
                result["records"] = []
            result["proof"] = SimpleNamespace(
                school_fact_version=10, revalidate=lambda: check("current")
            )
            result["boundary_proof"] = SimpleNamespace(
                school_fact_version=10, revalidate=lambda: check("boundary")
            )
            result["next_valid_boundary"] = boundary.isoformat()
        return result

    external.read_records = read
    trace = []
    result = query._query_point(actor(), as_of=AT, _trace=trace)
    assert sum(kind == "organization" for kind, *_ in external.calls) == 1
    assert len([row for row in trace if row["fact_kind"] == "organization"]) == 1
    assert checks == ["current", "boundary", "current", "boundary"]
    result.private_proof.revalidate(actor=actor())
    assert checks[-2:] == ["current", "boundary"]
    repo.clock = lambda: boundary
    with pytest.raises(PermissionError):
        result.private_proof.revalidate(actor=actor())


@pytest.mark.parametrize("change", ["revoked", "version", "boundary", "new_record"])
def test_shared_organization_is_rechecked_before_reuse_when_current_state_changes(
    change, monkeypatch
):
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_fact_education_external import ExternalReadProof
    from app.composition.school_fact_education_reader import EducationReadStale
    from tests.test_f2_read_evidence import Backend, evidence

    query, repo, external, _ = shared_group_service()
    original_read = external.read_records
    original_records = repo.read_records
    state = {"quality_called": False}
    backend = Backend()
    backend.now, backend.revision = AT, 10
    monkeypatch.setattr(backend, "members", ())
    deadline = AT + timedelta(seconds=1)
    proof = ExternalReadProof(
        actor(),
        (
            replace(
                evidence(backend, deadline=deadline),
                actor=actor(),
                version=10,
                observed_at=AT,
                probes=(("query-a", ()),),
            ),
        ),
    )

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if args[1] == "organization":
            proof.revalidate()
            result["proof"] = proof
            if change == "new_record":
                result["records"] = []
        return result

    def records(*args, **kwargs):
        result = original_records(*args, **kwargs)
        if args[1] == "offering_member_source" and kwargs.get("filter") == {
            "offering_source_key": "offering:b"
        }:
            if change == "revoked":
                backend.authority = "denied"
            elif change == "version":
                backend.revision = 11
            elif change == "boundary":
                backend.now = deadline
            else:
                backend.members = (("new-organization-row", 1),)
        return result

    def quality(*args, **kwargs):
        state["quality_called"] = True

    repo.read_records = records
    external.read_records = read
    monkeypatch.setattr(external, "quality_for", quality, raising=False)
    with pytest.raises(EducationReadStale):
        query.query(actor(), view="my_teaching")
    assert not state["quality_called"]


def test_shared_organization_is_acquired_again_for_new_query_actor_or_time():
    from datetime import timedelta

    query, _, external, _ = shared_group_service()
    seen = []
    original = external.read_records

    def read(a, kind, **kwargs):
        if kind == "organization":
            seen.append((a, kwargs["as_of"]))
        return original(a, kind, **kwargs)

    external.read_records = read
    selections = [
        (actor(), None),
        (actor(), None),
        (actor(77), AT),
        (actor(77), AT + timedelta(days=1)),
    ]
    for selected_actor, at in selections:
        query.query(selected_actor, view="teaching", subject_name="数学", as_of=at)
    assert seen == selections


@pytest.mark.parametrize("failure", ["exception", "proof", "object", "field"])
def test_shared_organization_failure_aborts_and_is_acquired_again_next_query(failure):
    query, _, external, _ = shared_group_service()
    original = external.read_records
    failed = False

    def read(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        if args[1] == "organization" and not failed:
            failed = True
            if failure == "exception":
                raise PermissionError("read denied")
            if failure == "proof":
                result.pop("proof")
            elif failure == "object":
                result["records"][0]["source_key"] = "organization:outside"
            else:
                result["records"][0]["content"]["private_phone"] = "must-not-return"
        return result

    external.read_records = read
    with pytest.raises(PermissionError):
        query.query(actor(), view="my_teaching")
    result = query.query(actor(), view="my_teaching")
    assert len(result.public_result["items"]) == 2
    assert sum(kind == "organization" for kind, *_ in external.calls) == 2
    assert "must-not-return" not in str(result.public_result)


def test_external_field_leak_is_rejected_before_answer():
    query, _, external, _ = service()
    old = external.read_records

    def leak(*args, **kwargs):
        result = old(*args, **kwargs)
        result["records"][0]["content"]["private_phone"] = "must-not-return"
        return result

    external.read_records = leak
    with pytest.raises(PermissionError):
        query.query(actor(), view="my_teaching")


def test_only_requested_official_contacts_are_read_and_their_proof_is_required():
    query, _, external, _ = service()
    called = []

    def contacts(a, **kwargs):
        called.append(kwargs["assignment_keys"])
        return {
            "records": [{"contact_value": "official-line"}],
            "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
        }

    external.official_contacts = contacts
    result = query.query(actor(), view="my_teaching", include_contacts=True)
    assert called == [("assignment:a",)]
    assert (
        result.public_result["items"][0]["teachers"][0]["official_contacts"][0]["contact_value"]
        == "official-line"
    )


def test_subject_division_excludes_unrelated_teacher_before_person_read():
    query, repo, external, _ = service()
    repo.rows["teaching_assignment"][0]["segments"][0]["content"]["subject_source_keys"] = [
        "subject:a"
    ]
    repo.rows["teaching_assignment"].append(
        record(
            "teaching_assignment",
            "teaching:b",
            {
                "assignment_source_key": "assignment:b",
                "offering_source_key": "offering:a",
                "teaching_role": "co_teacher",
                "subject_source_keys": ["subject:b"],
                "duty_scope": None,
            },
        )
    )
    repo.rows["course_subject"].append(
        record(
            "course_subject",
            "link:b",
            {"course_source_key": "course:a", "subject_source_key": "subject:b"},
        )
    )
    repo.rows["teaching_subject"].append(
        record("teaching_subject", "subject:b", {"display_name": "语文"})
    )
    result = query.query(actor(), view="teaching", subject_name="数学")
    assert len(result.public_result["items"][0]["teachers"]) == 1
    assert [keys for kind, keys, _ in external.calls if kind == "assignment"] == [("assignment:a",)]


def test_quality_is_queried_per_offering_role_and_retains_distinct_source():
    query, repo, external, _ = service()
    seen = []

    def quality(a, **kwargs):
        seen.append(kwargs)
        return {
            "records": [
                {
                    "effective_at": "2027-09-01T00:00:00Z",
                    "content": {
                        "quality_kind": "negative",
                        "negative_code": "no_teaching_assignment",
                        "teaching_role": "co_teacher",
                        "verified_at": "2027-09-01T00:00:00Z",
                        "coverage_status": None,
                    },
                    "source": {
                        "label": "正式核实记录",
                        "published_at": "2027-09-01T00:00:00Z",
                        "batch_id": "private",
                    },
                }
            ],
            "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
        }

    external.quality_for = quality
    result = query.query(actor(), view="my_teaching")
    assert len(seen) == 1
    assert seen[0]["owner_source_keys"] == ("offering:a",)
    assert seen[0]["target_source_keys"] == ("teaching:a",)
    assert seen[0]["teaching_roles"] == ("lead", "co_teacher")
    item = result.public_result["items"][0]
    assert item["teachers"][0]["name"] == "陈老师"
    assert item["role_status"]["共同任教"] == "学校明确未安排"
    assert "private" not in str(item["quality"])


@pytest.mark.parametrize("state", ["available", "unavailable"])
def test_teaching_relationship_quality_reads_own_target_and_stays_on_exact_teacher(state):
    query, repo, external, _ = service()
    repo.rows["teaching_assignment"].append(
        record(
            "teaching_assignment",
            "teaching:b",
            {
                "assignment_source_key": "assignment:a",
                "offering_source_key": "offering:a",
                "teaching_role": "co_teacher",
                "subject_source_keys": None,
                "duty_scope": None,
            },
        )
    )
    calls, checks = [], []

    def quality(_actor, **kwargs):
        calls.append(kwargs)
        return {
            "records": [
                {
                    "applies_to_source_key": "teaching:a",
                    "effective_at": "2026-09-01T00:00:00+00:00",
                    "content": {
                        "education_target_kind": "teaching_assignment",
                        "education_target_source_key": "teaching:a",
                        "target_source_key": None,
                        "field_name": "assignment_source_key",
                        "quality_kind": "source",
                        "source_status": state,
                    },
                    "source": {"label": "学校独立核查", "published_at": "2026-09-01T00:00:00Z"},
                }
            ],
            "school_fact_version": 10,
            "proof": SimpleNamespace(
                school_fact_version=10, revalidate=lambda: checks.append("quality")
            ),
        }

    external.quality_for = quality
    result = query.query(actor(), view="my_teaching")
    assert calls[0]["include_target_owner"] is True
    item = result.public_result["items"][0]
    assert item["teachers"][0]["quality_declarations"][0]["status"] == (
        "来源可用" if state == "available" else "来源暂不可用"
    )
    assert "quality_declarations" not in item["teachers"][1]
    assert "quality" not in item and "notices" not in item
    assert "teaching:a" not in str(item["teachers"][0]["quality_declarations"])
    result.private_proof.revalidate(actor=actor())
    assert checks


def test_teaching_relationship_exact_basis_uses_actual_quality_owner():
    from app.platform.school_facts.f7_coverage_basis import mother_set_digest

    query, _, external, _ = service()
    basis = {
        "schema_version": 1,
        "school_id": str(actor().school_id),
        "owner_kind": "education",
        "owner_source_key": "teaching:a",
        "target_fact_kind": "teaching_assignment",
        "target_source_key": "teaching:a",
        "field_name": "teaching_role",
        "purpose": None,
        "contact_kind": None,
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": None,
        "school_fact_version": 9,
        "member_count": 1,
        "member_sha256": mother_set_digest((("teaching:a", 1),))[1],
    }

    def quality(_actor, **kwargs):
        return {
            "records": [
                {
                    "applies_to_source_key": "teaching:a",
                    "effective_at": "2026-09-01T00:00:00Z",
                    "content": {
                        "education_target_source_key": "teaching:a",
                        "target_source_key": "teaching:a",
                        "field_name": "teaching_role",
                        "quality_kind": "coverage",
                        "coverage_status": "complete",
                        "coverage_basis": basis,
                    },
                }
            ],
            "school_fact_version": 10,
            "proof": SimpleNamespace(school_fact_version=10, revalidate=lambda: None),
        }

    external.quality_for = quality
    item = query.query(actor(), view="my_teaching").public_result["items"][0]
    assert item["teachers"][0]["quality"] == {"record_coverage": "complete"}
    assert "quality" not in item
    basis["owner_source_key"] = "offering:a"
    stale = query.query(actor(), view="my_teaching").public_result["items"][0]
    assert "quality" not in stale["teachers"][0]


def test_term_query_uses_offering_calendar_and_splits_later_assignment_change():
    from copy import deepcopy

    query, repo, external, _ = service()
    offering = repo.rows["teaching_offering"][0]["segments"][0]["content"]
    offering["term_source_key"] = "term:current"
    term = record(
        "education_term",
        "term:current",
        {"display_name": "本学期", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    assignment = repo.rows["teaching_assignment"][0]
    later = deepcopy(assignment["segments"][0])
    later["effective_at"] = "2027-11-01T00:00:00Z"
    later["content"]["teaching_role"] = "co_teacher"
    assignment["segments"][0]["valid_to"] = "2027-11-01T00:00:00Z"
    assignment["segments"].append(later)
    result = query.query(actor(), view="my_teaching", period="current_term", as_of=None)
    items = result.public_result["items"]
    assert {item["teachers"][0]["role"] for item in items} == {"主讲", "共同任教"}
    assert all("period_start" in item and "period_end" in item for item in items)
    assert all(
        filters or keys for kind, filters, keys, *_ in repo.calls if kind == "education_term"
    )


def test_unregistered_next_term_is_unknown_and_never_reads_all_terms():
    query, repo, _, _ = service()
    repo.rows["teaching_offering"][0]["segments"][0]["content"]["term_source_key"] = "term:current"
    term = record(
        "education_term",
        "term:current",
        {"display_name": "current", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    result = query.query(actor(), period="next_term")
    assert result.public_result["status"] == "period_not_recorded"
    assert result.public_result["items"] == []
    assert all(
        filters or keys for kind, filters, keys, *_ in repo.calls if kind == "education_term"
    )


def test_period_uses_administrative_class_calendar_without_student_roster():
    query, repo, _, _ = service()
    repo.rows["class_placement"] = [
        record(
            "class_placement",
            "placement:a",
            {"class_source_key": "class:a", "calendar_source_key": "calendar:a"},
        )
    ]
    term = record(
        "education_term",
        "term:current",
        {"display_name": "current", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    result = query.query(actor(), period="current_term")
    assert result.public_result["status"] == "available"
    assert any(
        kind == "class_placement" and filters == {"class_source_key": "class:a"}
        for kind, filters, *_ in repo.calls
    )
    assert not any(
        kind in {"student_class_membership", "teaching_group_member"} for kind, *_ in repo.calls
    )


def test_period_reference_current_term_expiry_invalidates_released_answer():
    from datetime import UTC, datetime

    query, repo, _, _ = service()
    repo.rows["teaching_offering"][0]["segments"][0]["content"]["term_source_key"] = "term:current"
    term = record(
        "education_term",
        "term:current",
        {"display_name": "current", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    result = query.query(actor(), period="current_term")
    repo.clock = lambda: datetime(2028, 1, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError):
        result.private_proof.revalidate(actor=actor())


def test_period_external_name_change_creates_another_segment_without_education_change():
    from datetime import UTC, datetime

    query, repo, external, _ = service()
    repo.rows["teaching_offering"][0]["segments"][0]["content"]["term_source_key"] = "term:current"
    term = record(
        "education_term",
        "term:current",
        {"display_name": "current", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    old = external.read_records
    boundary = datetime(2027, 10, 1, tzinfo=UTC)

    def changed(a, kind, **kwargs):
        result = old(a, kind, **kwargs)
        at = kwargs.get("as_of") or AT
        if kind == "person":
            result["records"][0]["content"]["display_name"] = "old" if at < boundary else "new"
            result["next_valid_boundary"] = boundary.isoformat() if at < boundary else None
            result["boundary_proof"] = SimpleNamespace(
                school_fact_version=10, revalidate=lambda: None
            )
        return result

    external.read_records = changed
    result = query.query(actor(), period="current_term")
    assert {item["teachers"][0]["name"] for item in result.public_result["items"]} == {"old", "new"}


def test_promoted_class_outside_course_grade_keeps_offering_and_reports_mismatch():
    query, repo, _, _ = service()
    repo.rows["teaching_course"][0]["segments"][0]["content"].update(
        grade_source_keys=["grade:1"], stage_source_keys=["stage:a"]
    )
    repo.rows["class_placement"] = [
        record(
            "class_placement",
            "placement:a",
            {
                "class_source_key": "class:a",
                "calendar_source_key": "calendar:a",
                "grade_source_key": "grade:1",
            },
        )
    ]
    repo.rows["education_calendar_rule"] = [
        record(
            "education_calendar_rule",
            "calendar:a",
            {
                "start_cycle": 2026,
                "end_cycle": None,
                "month": 9,
                "day": 1,
                "timezone_name": "Asia/Shanghai",
                "grade_source_keys": ["grade:1", "grade:2", "grade:3"],
                "stage_source_key": "stage:a",
            },
        )
    ]
    result = query.query(actor())
    assert result.public_result["items"][0]["course"] == "数学课程"
    assert any("适用年级" in notice for notice in result.public_result["items"][0]["notices"])
    assert not any(
        kind in {"student_class_membership", "teaching_group_member"} for kind, *_ in repo.calls
    )


def exact_service():
    query, repo, external, checks = service()

    def source(actor, kind, reference):
        prefix = f"f4:{kind}:"
        if not reference.startswith(prefix):
            raise PermissionError("wrong signed reference family")
        key = reference[len(prefix) :]
        if not any(row["source_key"] == key for row in repo.rows.get(kind, [])):
            raise PermissionError("unrecognized reference")
        return key

    repo.source_for_reference = source
    repo.reference_for = lambda a, kind, key: f"f4:{kind}:{key}"
    repo.read_reference = repo.reference_for
    return query, repo, external, checks


def test_same_named_offerings_keep_distinct_authorized_objects_and_selection():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    query, repo, source, _, _ = signed_service()
    for kind in (
        "teaching_assignment",
        "teaching_offering",
        "offering_member_source",
        "education_class",
    ):
        other = deepcopy(source.rows[kind][0])
        other["source_key"] = other["source_key"].replace(":a", ":b")
        for key, value in other["segments"][0]["content"].items():
            if key in {"offering_source_key", "class_source_key", "assignment_source_key"}:
                other["segments"][0]["content"][key] = value.replace(":a", ":b")
        source.rows[kind].append(other)
    issued = []

    original_reference = repo.reference_for

    def reference(a, kind, key):
        issued.append((kind, key))
        return original_reference(a, kind, key)

    repo.reference_for = reference
    result = query.query(actor(), view="teaching", subject_name="数学")
    material = education_model_material(result.public_result, teaching_answer=True)
    first, second = material["items"]
    assert first["course"] == second["course"]
    assert first["audiences"][0]["name"] == second["audiences"][0]["name"]
    for row in (first, second):
        assert row["tool_context"]["fact_kind"] == "teaching_offering"
        assert row["teachers"][0]["tool_context"]["fact_kind"] == "teaching_assignment"
        assert row["audiences"][0]["tool_context"]["fact_kind"] == "education_class"
        assert "reference" not in row["audiences"][0]["tool_context"]
    assert first["tool_context"]["reference"] != second["tool_context"]["reference"]
    assert (
        first["teachers"][0]["tool_context"]["reference"]
        != second["teachers"][0]["tool_context"]["reference"]
    )
    assert first["tool_context"]["object_identity"] != second["tool_context"]["object_identity"]
    assert (
        first["teachers"][0]["tool_context"]["object_identity"]
        != second["teachers"][0]["tool_context"]["object_identity"]
    )
    assert (
        first["audiences"][0]["tool_context"]["object_identity"]
        != second["audiences"][0]["tool_context"]["object_identity"]
    )
    assert material["selection"] == {
        "view": "teaching",
        "subject_name": "数学",
        "period": "current",
    }
    assert issued == []
    assert all(
        kind not in {"class_placement", "education_calendar_rule"} for kind, *_ in source.calls
    )


def test_exact_target_selection_survives_empty_result_and_model_projection():
    from app.composition.school_fact_education_gateway import education_model_material

    query, repo, _, _ = exact_service()
    reference = "f4:teaching_assignment:teaching:a"
    repo.rows["teaching_assignment"][0]["segments"][0]["valid_to"] = "2027-01-01T00:00:00+00:00"
    result = query.query(actor(), view="teaching", target_reference=reference)
    material = education_model_material(result.public_result, teaching_answer=True)
    assert material["items"] == []
    assert material["selection"]["tool_context"]["target_reference"] == reference
    assert material["selection"]["view"] == "teaching"


@pytest.mark.parametrize("ended", [False, True])
def test_exact_selection_resolution_is_independent_of_current_records(ended):
    query, repo, _, _ = exact_service()
    if ended:
        repo.rows["teaching_assignment"][0]["segments"][0]["valid_to"] = "2027-01-01T00:00:00Z"
    result = query.query(
        actor(), view="teaching", target_reference="f4:teaching_assignment:teaching:a"
    )
    assert result.public_result["selection_scope"]["resolution"] == "resolved"
    assert result.public_result["status"] == ("no_current_records" if ended else "available")
    assert result.public_result["coverage"] == "unknown"


def test_unlocated_period_keeps_actual_object_resolution_separate_from_period_failure():
    query, repo, _, _ = exact_service()
    repo.rows["education_term"] = []
    resolved = []
    original = repo.source_for_reference

    def resolve(*args):
        value = original(*args)
        resolved.append(value)
        return value

    repo.source_for_reference = resolve
    result = query.query(
        actor(),
        view="teaching",
        target_reference="f4:teaching_assignment:teaching:a",
        period="next_term",
    )
    assert result.public_result["status"] == "period_not_recorded"
    assert resolved == ["teaching:a"]
    assert result.public_result["selection_scope"]["resolution"] == "resolved"


def test_point_period_failure_before_reference_lookup_does_not_claim_resolution():
    query, repo, _, _ = exact_service()
    repo.rows["education_term"] = []

    def unexpected_lookup(*args):
        raise AssertionError("period lookup has not reached object resolution")

    repo.source_for_reference = unexpected_lookup
    result = query._query_point(
        actor(),
        view="teaching",
        target_reference="f4:teaching_assignment:teaching:a",
        period="next_term",
    )
    assert result.public_result["status"] == "period_not_recorded"
    assert result.public_result["selection_scope"]["resolution"] == "not_resolved"


def test_exact_result_reuses_validated_target_and_related_read_reference():
    query, repo, _, _ = exact_service()
    issued = []
    original = repo.reference_for

    def reference_for(a, kind, key):
        issued.append((kind, key))
        return original(a, kind, key)

    repo.reference_for = reference_for
    reference = "f4:teaching_assignment:teaching:a"
    result = query.query(actor(), view="teaching", target_reference=reference)
    assert result.public_result["items"][0]["teachers"][0]["reference"] == reference
    assert issued == []


def test_object_identity_is_stable_across_selection_but_bound_to_current_actor():
    query, repo, _, _ = exact_service()
    broad = query.query(actor(), view="teaching", subject_name="数学")
    exact = query.query(
        actor(), view="teaching", target_reference="f4:teaching_assignment:teaching:a"
    )

    def identity(result):
        return result.public_result["items"][0]["teachers"][0]["object_identity"]

    assert identity(broad) == identity(exact)
    other = actor(assignment=4)
    changed = query.query(other, view="teaching", subject_name="数学")
    assert identity(changed) != identity(broad)
    reference = broad.public_result["items"][0]["teachers"][0]["reference"]
    continued = query.query(actor(), view="teaching", target_reference=reference)
    assert identity(continued) == identity(broad)


def test_staff_timezone_reuses_only_already_authorized_calendar_material():
    query, repo, _, _ = service()
    result = query.query(actor())
    assert "fact_timezone_name" not in result.public_result
    repo.rows["teaching_course"][0]["segments"][0]["content"]["grade_source_keys"] = ["grade:2"]
    repo.rows["class_placement"] = [
        record(
            "class_placement",
            "placement:a",
            {
                "class_source_key": "class:a",
                "calendar_source_key": "calendar:a",
                "grade_source_key": "grade:1",
            },
        )
    ]
    repo.rows["education_calendar_rule"] = [
        record(
            "education_calendar_rule",
            "calendar:a",
            {
                "start_cycle": 2026,
                "end_cycle": None,
                "month": 9,
                "day": 1,
                "timezone_name": "Asia/Shanghai",
                "grade_source_keys": ["grade:1", "grade:2", "grade:3"],
                "stage_source_key": "stage:a",
            },
        )
    ]
    result = query.query(actor())
    assert result.public_result["fact_timezone_name"] == "Asia/Shanghai"
    assert sum(kind == "education_calendar_rule" for kind, *_ in repo.calls) == 1


@pytest.mark.parametrize(
    "kind,key,expected",
    [("teaching_assignment", "teaching:a", 1), ("teaching_offering", "offering:a", 2)],
)
def test_exact_teaching_reference_preserves_target_or_offering_set(kind, key, expected):
    from copy import deepcopy

    query, repo, external, _ = exact_service()
    other = deepcopy(repo.rows["teaching_assignment"][0])
    other["source_key"] = "teaching:b"
    other["segments"][0]["content"]["assignment_source_key"] = "assignment:b"
    repo.rows["teaching_assignment"].append(other)
    result = query.query(actor(), view="teaching", target_reference=f"f4:{kind}:{key}")
    assert result.public_result["items"][0]["course"] == "数学课程"
    assert len(result.public_result["items"][0]["teachers"]) == expected
    assert result.public_result["selection_scope"]["kind"] == kind
    if kind == "teaching_assignment":
        assert all("assignment:b" not in keys for _, keys, _ in external.calls)
        assert not any(k == "teaching_assignment" and f for k, f, *_ in repo.calls)
    result.private_proof.revalidate(actor=actor())
    repo.projection.current_version = lambda _: 11
    with pytest.raises(RuntimeError):
        result.private_proof.revalidate(actor=actor())


def test_exact_ended_assignment_does_not_substitute_other_current_teacher():
    from datetime import UTC, datetime

    query, repo, external, _ = exact_service()
    repo.rows["teaching_assignment"][0]["segments"][0]["valid_to"] = "2027-01-01T00:00:00Z"
    result = query.query(
        actor(), view="teaching", target_reference="f4:teaching_assignment:teaching:a"
    )
    assert result.public_result["status"] == "no_current_records"
    assert result.public_result["items"] == [] and external.calls == []
    old = query.query(
        actor(),
        view="teaching",
        target_reference="f4:teaching_assignment:teaching:a",
        as_of=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert old.public_result["items"][0]["teachers"][0]["name"] == "陈老师"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"view": "my_teaching"},
        {"view": "my_organizations"},
        {"view": "teaching", "subject_name": "数学"},
        {"view": "teaching", "class_reference": "f4:education_class:class:a"},
        {"view": "teaching", "course_name": "数学课程"},
    ],
)
def test_exact_target_cannot_mix_selection_semantics(kwargs):
    query, repo, _, _ = exact_service()
    with pytest.raises(ValueError):
        query.query(actor(), target_reference="f4:teaching_assignment:teaching:a", **kwargs)
    assert repo.calls == []


def test_exact_target_still_requires_reference_and_dependency_field_permission():
    query, repo, external, _ = exact_service()

    def denied(*args, **kwargs):
        raise PermissionError("current exact scope denied")

    old = repo.source_for_reference
    repo.source_for_reference = denied
    with pytest.raises(PermissionError, match="exact scope"):
        query.query(actor(), view="teaching", target_reference="f4:teaching_assignment:teaching:a")
    assert repo.calls == []
    repo.source_for_reference = old
    external.read_records = denied
    with pytest.raises(PermissionError, match="exact scope"):
        query.query(actor(), view="teaching", target_reference="f4:teaching_assignment:teaching:a")


def signed_service():
    """Real signed-reference repository and proof; only its SQL projection is in memory."""
    from copy import deepcopy

    from app.composition.school_fact_education_reader import EducationRepository
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS

    query, source, external, _ = service()
    state = SimpleNamespace(
        allowed=True, deny_field=None, after_read=lambda: None, version=10, read_waves=[]
    )

    class Authority:
        def require_scope(self, a, *, fact_kind, **kwargs):
            if not state.allowed:
                raise PermissionError("revoked")
            return SimpleNamespace(
                actor=a,
                action="read",
                scope=SimpleNamespace(
                    fact_kind=fact_kind,
                    reference="scope:" + fact_kind,
                    object_keys=tuple(r["source_key"] for r in source.rows.get(fact_kind, ())),
                    creation_keys=(),
                    is_school_wide=False,
                    field_names=tuple(EDUCATION_CONTENT_FIELDS[fact_kind]),
                ),
            )

        def require_scopes(self, a, *, requests, checked_at):
            return tuple(
                self.require_scope(a, fact_kind=kind, action=action, checked_at=checked_at)
                for kind, action in requests
            )

        def revalidate_read_wave(self, permits, *, actor, checked_at):
            for permit in permits:
                self.revalidate(permit, actor=actor, checked_at=checked_at)

        def require_fields(self, permit, fields):
            if state.deny_field in fields or not set(fields) <= set(permit.scope.field_names):
                raise PermissionError("field denied")

        def require_object(self, permit, key, **kwargs):
            if key not in permit.scope.object_keys:
                raise PermissionError("object denied")

        def revalidate(self, permit, *, actor, **kwargs):
            if not state.allowed or actor != permit.actor:
                raise PermissionError("revoked")

    class Projection:
        def current_version(self, _):
            return state.version

        def read_many(self, *, school_id, selections, as_of, before_read, after_read):
            before_read()
            selected = selections()
            state.read_waves.append(
                tuple((kind, fields, filters) for kind, _, fields, filters in selected)
            )
            results = []
            for kind, keys, fields, filters in selected:
                rows = deepcopy(
                    source.read_records(
                        actor(),
                        kind,
                        source_keys=keys,
                        fields=fields,
                        filter=filters,
                        as_of=as_of,
                    ).records
                )
                for row in rows:
                    for segment in row.get("segments", ()):
                        segment["content"] = {
                            name: value
                            for name, value in segment["content"].items()
                            if name in fields
                        }
                results.append({"records": list(rows), "school_fact_version": state.version})
            state.after_read()
            answer = tuple(results)
            after_read(answer)
            return answer

        def read(self, **kwargs):
            kwargs["before_read"]()
            keys, fields = kwargs["source_keys"](), kwargs["field_names"]()
            rows = deepcopy(
                source.read_records(
                    actor(),
                    kwargs["fact_kind"],
                    source_keys=keys,
                    fields=fields,
                    filter=kwargs["filter"],
                ).records
            )
            for row in rows:
                for segment in row.get("segments", ()):
                    segment["content"] = {
                        k: v for k, v in segment["content"].items() if k in fields
                    }
            result = {"records": list(rows), "school_fact_version": state.version}
            state.after_read()
            kwargs["after_read"](result)
            return result

    repo = EducationRepository(
        projection=Projection(),
        authority=Authority(),
        reference_key=b"test-reference-key" * 2,
        clock=lambda: AT,
    )
    query.repository = repo
    return query, repo, source, external, state


def test_school_teaching_reference_passes_gateway_shape_after_current_scope():
    from app.composition.school_fact_teaching_gateway import TeachingQueryInput
    _, repo, _, _, _ = signed_service()
    original = repo.authority.require_scope

    def school_scope(a, *, fact_kind, action, checked_at):
        permit = original(a, fact_kind=fact_kind, action=action, checked_at=checked_at)
        permit.scope.is_school_wide = True
        permit.scope.allows_key = lambda key, *, is_creation=False: bool(key)
        return permit

    repo.authority.require_scope = school_scope
    repo.authority.require_object = lambda permit, key, *, is_creation: (
        None if permit.scope.allows_key(key, is_creation=is_creation)
        else (_ for _ in ()).throw(PermissionError("object denied"))
    )
    reference = repo.reference_for(actor(), "teaching_offering", "offering:later")
    assert reference.startswith("schoolv1:f5:teaching_offering:o:")
    assert (
        TeachingQueryInput(view="teaching", target_reference=reference).target_reference
        == reference
    )


@pytest.mark.parametrize("change", ["revoked", "field", "during_read", "tampered", "other_actor"])
def test_broad_signed_target_consumption_uses_current_repository_guards(change):
    query, repo, source, _, state = signed_service()
    broad = query.query(actor(), view="teaching", subject_name="数学")
    reference = broad.public_result["items"][0]["teachers"][0]["reference"]
    source.calls.clear()
    selected_actor = actor()
    if change == "revoked":
        state.allowed = False
    elif change == "field":
        state.deny_field = "assignment_source_key"
    elif change == "during_read":
        state.after_read = lambda: setattr(state, "allowed", False)
    elif change == "tampered":
        reference = reference[:-1] + ("0" if reference[-1] != "0" else "1")
    else:
        selected_actor = actor(assignment=77)
    with pytest.raises(PermissionError):
        query.query(selected_actor, view="teaching", target_reference=reference)
    if change in {"revoked", "tampered", "other_actor", "field"}:
        assert source.calls == []


@pytest.mark.parametrize("selection", ["broad", "exact"])
def test_signed_gateway_result_rechecks_session_and_current_authority(selection):
    from uuid import uuid4

    from app.composition.school_fact_teaching_gateway import MODULE, StaffTeachingGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    query, repo, _, _, state = signed_service()
    identity = SimpleNamespace(actor=actor(), session_binding="one")
    gateway = StaffTeachingGateway(resolve_actor=lambda _: identity, query=query).for_turn("actor")
    reference = repo.reference_for(actor(), "teaching_assignment", "teaching:a")
    result = gateway.execute(
        ModelToolCall(
            call_id="one",
            name=MODULE + ".query",
            arguments={
                "view": "teaching",
                **(
                    {"target_reference": reference}
                    if selection == "exact"
                    else {"subject_name": "数学"}
                ),
            },
        ),
        ToolCallContext("actor", "conv", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == "available"
    assert "陈老师" in str(result.model_content)
    teacher = result.model_content["items"][0]["teachers"][0]
    assert teacher["tool_context"]["reference"] == reference
    assert "reference" not in teacher and "source_key" not in str(result.model_content)
    assert result.history_check.valid()
    state.version = 11
    assert not result.history_check.valid()
    state.version = 10
    assert result.history_check.valid()
    identity.session_binding = "two"
    assert not result.history_check.valid()
    identity.session_binding = "one"
    state.allowed = False
    assert not result.history_check.valid()


@pytest.mark.parametrize("selection", ["broad", "exact"])
def test_signed_result_expires_at_natural_boundary(selection):
    from datetime import timedelta

    query, repo, source, _, _ = signed_service()
    boundary = AT + timedelta(days=1)
    source.rows["teaching_assignment"][0]["next_valid_boundary"] = boundary.isoformat()
    reference = repo.reference_for(actor(), "teaching_assignment", "teaching:a")
    result = query.query(
        actor(),
        view="teaching",
        **({"target_reference": reference} if selection == "exact" else {"subject_name": "数学"}),
    )
    assert result.public_result["items"][0]["teachers"][0]["reference"] == reference
    result.private_proof.revalidate(actor=actor())
    repo.clock = lambda: boundary
    with pytest.raises(RuntimeError):
        result.private_proof.revalidate(actor=actor())


def test_exact_assignment_period_keeps_selected_identity_and_scope():
    from copy import deepcopy

    query, repo, external, _ = exact_service()
    other = deepcopy(repo.rows["teaching_assignment"][0])
    other["source_key"] = "teaching:b"
    other["segments"][0]["content"]["assignment_source_key"] = "assignment:b"
    repo.rows["teaching_assignment"].append(other)
    repo.rows["teaching_offering"][0]["segments"][0]["content"]["term_source_key"] = "term:current"
    term = record(
        "education_term",
        "term:current",
        {"display_name": "current", "calendar_source_key": "calendar:a"},
        start="2027-09-01T00:00:00Z",
    )
    term["segments"][0]["valid_to"] = "2028-01-01T00:00:00Z"
    repo.rows["education_term"] = [term]
    result = query.query(
        actor(),
        view="teaching",
        target_reference="f4:teaching_assignment:teaching:a",
        period="current_term",
    )
    assert result.public_result["status"] == "available"
    assert result.public_result["selection_scope"]["kind"] == "teaching_assignment"
    assert all(len(row["teachers"]) == 1 for row in result.public_result["items"])
    assert all("assignment:b" not in keys for _, keys, _ in external.calls)


@pytest.mark.parametrize("kind", ["teaching_assignment", "teaching_offering"])
def test_broad_signed_target_reaches_exact_read_and_existing_formal_prepare(kind):
    from datetime import timedelta
    from uuid import uuid4

    from app.composition.school_fact_directory_product import DirectoryFactProductService
    from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
    from app.composition.school_fact_education_product import (
        EducationBatchInput,
        EducationProductAdapter,
    )
    from app.composition.school_fact_teaching_gateway import TeachingQueryInput
    from app.composition.school_facts import InMemorySchoolFactPlanStore
    from tests.test_f2_directory_product import Boundary

    query, repo, source, external, state = signed_service()
    broad = query.query(actor(), view="teaching", subject_name="数学")
    item = broad.public_result["items"][0]
    selected = item["teachers"][0] if kind == "teaching_assignment" else item
    reference = selected["reference"]
    selection = TeachingQueryInput(view="teaching", target_reference=reference)
    exact = query.query(actor(), **selection.model_dump())
    assert exact.public_result["selection_scope"]["resolution"] == "resolved"
    assert len(exact.public_result["items"]) == 1

    # External staff references are fixed synthetic inputs; the changed target
    # uses the real signed education repository through the maintenance owner.
    external.reference_for = lambda *, fact_kind, source_key, **kw: f"f2:{fact_kind}:{source_key}"
    external.source_for_reference = lambda *, reference, **kw: reference.split(":", 2)[2]
    coordinator = EducationMaintenanceCoordinator(
        EducationProductAdapter(repository=repo, external=external), family=None
    )

    class DraftBoundary(Boundary):
        def prepare_confirmation(self, **kw):
            return (
                super()
                .prepare_confirmation(**kw)
                .model_copy(update={"expires_at": AT + timedelta(minutes=5)})
            )

    boundary = DraftBoundary()
    maintenance_allowed = [False]

    def require_maintenance(selected_actor, _change=None):
        if selected_actor != actor() or not maintenance_allowed[0]:
            raise PermissionError("separate maintenance permission required")

    product = DirectoryFactProductService(
        boundary=boundary,
        resolve_change=coordinator.resolve_change,
        require_maintenance=require_maintenance,
        require_receipt=require_maintenance,
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: AT),
        clock=lambda: AT,
    )
    fields = (
        {"assignment_ref": "f2:assignment:assignment:b"}
        if kind == "teaching_assignment"
        else {"display_name": "合成开课新名称"}
    )
    payload = EducationBatchInput(
        fact_kind=kind, target_ref=reference, fields=fields, reason="合成工程验收"
    )
    with pytest.raises(PermissionError, match="prepare_maintenance_denied"):
        product.prepare(actor=actor(), session_binding="one", payload=payload, request_id=uuid4())
    assert boundary.calls == []
    maintenance_allowed[0] = True
    proposal = product.prepare(
        actor=actor(), session_binding="one", payload=payload, request_id=uuid4()
    )
    assert [name for name, _ in boundary.calls] == ["preflight", "prepare"]
    assert proposal.summary.reference == reference
    assert proposal.summary.after == {**proposal.summary.before, **fields}
    if kind == "teaching_assignment":
        assert set(payload.fields) == {"assignment_ref"}
        assert len(proposal.summary.after) > len(payload.fields)
        assert proposal.summary.after["offering_ref"] == proposal.summary.before["offering_ref"]
        assert proposal.summary.after["teaching_role"] == proposal.summary.before["teaching_role"]
        assert proposal.summary.reference_labels_as_of == AT
        assert proposal.summary.impacts[0]["reference_labels_as_of"] == AT.isoformat().replace(
            "+00:00", "Z"
        )
        assert proposal.summary.reference_labels[fields["assignment_ref"]] == "陈老师"
        assert (
            proposal.summary.reference_labels[proposal.summary.before["assignment_ref"]] == "陈老师"
        )
        assert proposal.summary.impacts[0]["reference_labels"] == proposal.summary.reference_labels
        assert (
            proposal.summary.before_segments[0]["fields"]["assignment_ref"]
            in proposal.summary.reference_labels
        )
    changes = boundary.calls[0][1]["change_set"].changes
    assert len(changes) == 1
    assert changes[0].source_key == (
        "teaching:a" if kind == "teaching_assignment" else "offering:a"
    )
    assert changes[0].expected_version == 1
    assert source.rows[kind][0]["version"] == 1  # Preparing did not write.
    state.allowed = False
    with pytest.raises(PermissionError):
        product.prepare(actor=actor(), session_binding="one", payload=payload, request_id=uuid4())
    assert len(boundary.calls) == 2


def test_broad_read_reference_still_requires_fresh_scope_before_release():
    from app.composition.school_fact_education_gateway import education_model_material

    query, repo, source, _, state = signed_service()
    calls = []
    original = repo._reference

    def reference_for(a, kind, key, creation, school_wide=False):
        calls.append((kind, key))
        assert any(row["source_key"] == key for row in source.rows[kind])
        state.allowed = False
        return original(a, kind, key, creation, school_wide)

    repo._reference = reference_for
    with pytest.raises(PermissionError, match="revoked"):
        query.query(actor(), view="teaching", subject_name="数学")
    assert len(calls) == 1
    state.allowed = True
    repo._reference = original
    source.calls.clear()
    source.rows["teaching_assignment"][0]["segments"][0]["content"]["basis_reference"] = (
        "not requested"
    )
    result = query.query(actor(), view="teaching", subject_name="数学")
    material = education_model_material(result.public_result, teaching_answer=True)
    assert "not requested" not in str(material) and "source_key" not in str(material)
    assert "reference" in material["items"][0]["teachers"][0]["tool_context"]
    assert len([call for call in source.calls if call[0] == "teaching_assignment"]) == 1


def original_teaching_projection_setup():
    from importlib import import_module, util
    from types import SimpleNamespace

    name = "app.composition.school_fact_receipt_teaching"
    assert util.find_spec(name) is not None, "Original teaching name projection is missing"
    module = import_module(name)
    state = SimpleNamespace(
        allowed=True,
        revision=1,
        missing=None,
        overbroad=False,
        source_failure=False,
        calls=[],
        now=AT,
    )

    class External:
        def read_records(
            self, selected, kind, *, source_keys, fields, as_of, include_boundaries=True
        ):
            assert include_boundaries is False
            assert selected == actor()
            assert as_of <= state.now
            state.calls.append((kind, source_keys, fields, as_of))
            if not state.allowed:
                raise PermissionError("private scope diagnostic")
            if state.source_failure:
                raise RuntimeError("private source diagnostic")
            revision = state.revision

            def revalidate():
                if not state.allowed or state.revision != revision:
                    raise PermissionError("Current association evidence changed")

            key = "assignment:original" if kind == "assignment" else "person:original"
            content = (
                {"person_source_key": "person:original"}
                if kind == "assignment"
                else {"display_name": "陈老师"}
            )
            if state.overbroad:
                key = "unrelated:private"
            return {
                "records": []
                if state.missing == kind
                else [{"source_key": key, "content": content}],
                "proof": SimpleNamespace(revalidate=revalidate),
            }

    return module, External(), state


def original_teaching_segment(*, start=None, end=None):
    from datetime import timedelta

    from app.platform.school_facts.ports import SchoolFactOwnOriginalSegment

    return SchoolFactOwnOriginalSegment(
        effective_at=start or AT - timedelta(days=2),
        valid_to=end,
        content={
            "assignment_source_key": "assignment:original",
            "offering_source_key": "offering:original",
            "teaching_role": "lead",
        },
    )


def test_original_teaching_names_follow_original_assignment_at_its_effective_time():
    module, external, state = original_teaching_projection_setup()
    segment = original_teaching_segment()
    result = module.project_original_teaching(
        actor(), (segment,), external_reader=external, clock=lambda: state.now
    )
    item = result.material["items"][0]
    assert item["teacher_name"] == "陈老师" and item["teaching_role"] == "主讲"
    assert item["effective_at"] == segment.effective_at.isoformat() and item["valid_to"] is None
    assert item["name_as_of"] == segment.effective_at.isoformat()
    assert item["name_status"] == "verified"
    assert "name_unavailable_reason" not in item
    assert state.calls == [
        ("assignment", ("assignment:original",), ("person_source_key",), segment.effective_at),
        ("person", ("person:original",), ("display_name",), segment.effective_at),
    ]
    assert "source_key" not in str(result.material) and "original" not in str(item)
    result.proof.revalidate(actor=actor())


@pytest.mark.parametrize(
    "condition",
    ["future", "ended", "missing_assignment", "missing_person", "denied", "overbroad", "source"],
)
def test_original_teaching_name_unknown_never_erases_the_original_record(condition):
    from datetime import timedelta

    module, external, state = original_teaching_projection_setup()
    segment = original_teaching_segment(
        start=AT + timedelta(days=1) if condition == "future" else None,
        end=AT - timedelta(days=1) if condition == "ended" else None,
    )
    if condition.startswith("missing_"):
        state.missing = condition.removeprefix("missing_")
    state.allowed = condition != "denied"
    state.overbroad = condition == "overbroad"
    state.source_failure = condition == "source"
    result = module.project_original_teaching(
        actor(), (segment,), external_reader=external, clock=lambda: state.now
    )
    item = result.material["items"][0]
    assert item["effective_at"] == segment.effective_at.isoformat()
    assert item["valid_to"] == (segment.valid_to.isoformat() if segment.valid_to else None)
    assert item["teaching_role"] == "主讲"
    assert item["teacher_name"] == ("陈老师" if condition == "ended" else None)
    assert item["name_status"] == ("verified" if condition == "ended" else "unverified")
    assert (
        item.get("name_unavailable_reason")
        == {
            "ended": None,
            "future": "future_not_queried",
            "missing_assignment": "historical_read_empty",
            "missing_person": "historical_read_empty",
            "denied": "permission_denied",
            "overbroad": "source_unavailable",
            "source": "source_unavailable",
        }[condition]
    )
    assert "private" not in str(result.material)
    if condition == "future":
        assert not state.calls
    if condition == "missing_assignment":
        assert len(state.calls) == 1


@pytest.mark.parametrize("change", ["authority", "content", "actor", "clock_rollback"])
def test_original_teaching_name_proof_revalidates_after_wait(change):
    from datetime import timedelta

    module, external, state = original_teaching_projection_setup()
    result = module.project_original_teaching(
        actor(), (original_teaching_segment(),), external_reader=external, clock=lambda: state.now
    )
    selected = actor()
    if change == "authority":
        state.allowed = False
    elif change == "content":
        state.revision += 1
    elif change == "actor":
        selected = actor(assignment=77)
    else:
        state.now -= timedelta(seconds=1)
    with pytest.raises((PermissionError, RuntimeError)):
        result.proof.revalidate(actor=selected)


def test_original_teaching_empty_revision_has_no_guessed_teacher_or_broad_read():
    module, external, state = original_teaching_projection_setup()
    result = module.project_original_teaching(
        actor(), (), external_reader=external, clock=lambda: state.now
    )
    assert result.material["items"] == [] and not state.calls
    result.proof.revalidate(actor=actor())


def test_original_teaching_uses_external_reader_historical_reference_recovery():
    from contextlib import contextmanager
    from datetime import timedelta
    from types import SimpleNamespace

    from app.composition.school_fact_education_external import EducationExternalReader
    from app.composition.school_fact_education_reader import EducationReadStale
    from app.composition.school_fact_receipt_teaching import project_original_teaching

    segment = original_teaching_segment(end=AT - timedelta(days=1))
    calls = []
    reads = []
    state = SimpleNamespace(allowed=True, now=AT)

    class Evidence:
        def check(self, selected):
            assert selected == actor()
            return state.allowed

        @contextmanager
        def capture(self, *, actor, current_time):
            assert current_time is False
            yield SimpleNamespace(seal=lambda: self)

    class Directory:
        def reference_for(self, *, actor, fact_kind, source_key):
            reads.append((fact_kind, source_key))
            return f"f2:{fact_kind}:{source_key}"

        def source_for(self, *, actor, fact_kind, reference):
            assert fact_kind == "person" and reference == "f2:person:person:original"
            return "person:original"

        def query(self, **kwargs):
            calls.append(kwargs)
            historical = kwargs["factual_at"] == segment.effective_at
            content = (
                {"person_ref": "f2:person:person:original"}
                if kwargs["fact_kind"] == "assignment"
                else {"display_name": "原任教陈老师" if historical else "当前王老师"}
            )
            return SimpleNamespace(
                status="known", has_more=False, records=(SimpleNamespace(content=content),)
            )

    def unused_boundary(*args):
        pytest.fail("Original names must not fetch unused future boundary metadata")

    result = project_original_teaching(
        actor(),
        (segment,),
        external_reader=EducationExternalReader(
            directory=Directory(),
            evidence=Evidence(),
            boundary_reader=SimpleNamespace(read=unused_boundary),
        ),
        clock=lambda: state.now,
    )
    assert reads == [("assignment", "assignment:original"), ("person", "person:original")]
    assert [call["content_fields"] for call in calls] == [("person_source_key",), ("display_name",)]
    assert all(call["factual_at"] == segment.effective_at for call in calls)
    assert all(call["page_size"] == 2 and call["reference"] for call in calls)
    assert result.material["items"][0]["teacher_name"] == "原任教陈老师"
    state.now += timedelta(days=7)
    result.proof.revalidate(actor=actor())
    state.allowed = False
    with pytest.raises(EducationReadStale):
        result.proof.revalidate(actor=actor())


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("no_current_records", "historical_read_empty"), ("conflict", "conflict")],
)
def test_original_teaching_keeps_original_fact_when_joined_name_is_missing_or_conflicts(
    outcome, reason
):
    from app.composition.school_fact_receipt_teaching import project_original_teaching
    from tests.school_facts.test_f4_education_gateway import related_family_reader

    external, _ = related_family_reader(outcome=outcome)
    result = project_original_teaching(
        actor(),
        (original_teaching_segment(),),
        external_reader=external,
        clock=lambda: AT,
    )
    item = result.material["items"][0]
    assert item["teacher_name"] is None
    assert item["name_unavailable_reason"] == reason
    assert item["teaching_role"] == "主讲"
    assert item["effective_at"] == original_teaching_segment().effective_at.isoformat()


def test_original_teaching_name_drift_remains_distinct_from_source_outage():
    from app.composition.school_fact_receipt_teaching import project_original_teaching
    from app.platform.authorization.secure_reader_contracts import SecureReadAuthorizationDrift
    from tests.school_facts.test_f4_education_gateway import related_family_reader

    external, _ = related_family_reader(error=SecureReadAuthorizationDrift)
    result = project_original_teaching(
        actor(),
        (original_teaching_segment(),),
        external_reader=external,
        clock=lambda: AT,
    )
    assert result.material["items"][0]["name_unavailable_reason"] == "stale"


def test_original_teaching_preserves_every_supplied_changed_segment():
    from dataclasses import replace
    from datetime import timedelta

    module, external, state = original_teaching_projection_setup()
    first = original_teaching_segment(end=AT - timedelta(days=1))
    second = replace(
        original_teaching_segment(start=first.valid_to),
        content={**first.content, "teaching_role": "co_teacher"},
    )
    result = module.project_original_teaching(
        actor(), (first, second), external_reader=external, clock=lambda: state.now
    )
    assert [item["teaching_role"] for item in result.material["items"]] == ["主讲", "共同任教"]
    assert [item["effective_at"] for item in result.material["items"]] == [
        first.effective_at.isoformat(),
        second.effective_at.isoformat(),
    ]
    assert [call[3] for call in state.calls] == [
        first.effective_at,
        first.effective_at,
        second.effective_at,
        second.effective_at,
    ]


def test_teaching_query_reuses_references_from_authorized_reads():
    query, repo, *_ = service()

    def unexpected_issue(*args, **kwargs):
        pytest.fail("query reissued a grant for an already-read teaching object")

    repo.reference_for = unexpected_issue
    result = query.query(actor(), view="my_teaching")
    assert result.public_result["status"] == "available"


def class_teaching_service():
    query, repo, external, checks = service()
    original = Repository()
    for kind, rows in original.rows.items():
        if kind not in repo.rows:
            repo.rows[kind] = rows
    repo.rows["teaching_course"][0]["segments"][0]["content"]["grade_source_keys"] = ["grade:2"]
    return query, repo, external, checks


def test_class_teaching_reuses_exact_class_resolution_rows_and_fields():
    query, repo, _, _ = class_teaching_service()
    result = query.query(actor(), view="teaching", class_reference="opaque:class:a")
    assert result.public_result["status"] == "available"
    assert len([c for c in repo.calls if c[0] == "education_class"]) == 1
    assert len([c for c in repo.calls if c[0] == "class_placement"]) == 1
    # Class location no longer reads a display grade before teaching scope checks.
    assert len([c for c in repo.calls if c[0] == "education_calendar_rule"]) == 1
    assert not any(c[0] == "education_grade" for c in repo.calls)


def test_class_resolution_private_rows_are_not_reused_for_different_fact_time():
    from dataclasses import replace
    from datetime import timedelta

    query, repo, _, _ = class_teaching_service()
    original = query.class_query.locate_class

    def different_time(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(
            result,
            private_reads=tuple(
                replace(read, as_of=AT - timedelta(days=1)) for read in result.private_reads
            ),
        )

    query.class_query.locate_class = different_time
    result = query.query(
        actor(), view="teaching", class_reference="opaque:class:a", grade_name="初二"
    )
    assert result.public_result["status"] == "available"
    assert len([c for c in repo.calls if c[0] == "education_class"]) == 2
    assert len([c for c in repo.calls if c[0] == "class_placement"]) == 2


@pytest.mark.parametrize("change", ("known_at", "empty", "keys", "filter", "fields", "actor"))
def test_class_resolution_reuse_cannot_change_scope_or_projection(change):
    from dataclasses import replace

    query, repo, _, _ = class_teaching_service()
    original = query.class_query.locate_class

    def modified(*args, **kwargs):
        result = original(*args, **kwargs)
        rows = []
        for read in result.private_reads:
            if read.kind == "class_placement":
                if change == "known_at":
                    read = replace(read, known_at=AT)
                elif change == "empty":
                    read = replace(read, result=replace(read.result, records=()))
                elif change == "keys":
                    read = replace(read, source_keys=("place:a",))
                elif change == "filter":
                    read = replace(read, filters=(("class_source_key", "another"),))
                elif change == "fields":
                    read = replace(read, fields=("class_source_key",))
                else:
                    read = replace(
                        read,
                        result=replace(
                            read.result, proof=replace(read.result.proof, actor=actor(4))
                        ),
                    )
            rows.append(read)
        return replace(result, private_reads=tuple(rows))

    query.class_query.locate_class = modified
    result = query.query(
        actor(), view="teaching", class_reference="opaque:class:a", grade_name="初二"
    )
    assert result.public_result["status"] == "available"
    assert len([c for c in repo.calls if c[0] == "class_placement"]) == 2


def test_class_resolution_reuse_revalidates_time_before_consuming_rows():
    from dataclasses import replace

    from app.composition.school_fact_education_reader import EducationReadStale

    query, repo, _, _ = class_teaching_service()
    original = query.class_query.locate_class

    def expired(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(
            result,
            private_reads=tuple(
                replace(
                    read,
                    result=replace(read.result, proof=replace(read.result.proof, next_boundary=AT)),
                )
                for read in result.private_reads
            ),
        )

    query.class_query.locate_class = expired
    with pytest.raises(EducationReadStale):
        query.query(actor(), view="teaching", class_reference="opaque:class:a")
    assert len([c for c in repo.calls if c[0] == "education_class"]) == 1


def test_teaching_class_locator_does_not_resolve_just_issued_reference_again():
    query, repo, _, _ = class_teaching_service()
    calls = []
    original = repo.source_for_reference

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    repo.source_for_reference = resolve
    result = query.query(actor(), view="teaching", class_reference="opaque:class:a")
    assert result.public_result["status"] == "available"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "selection",
    [
        {"view": "teaching", "subject_name": "数学"},
        {"view": "teaching", "course_name": "数学课程"},
    ],
)
def test_signed_teaching_wave_matches_serial_result_and_keeps_every_proof(selection):
    from collections import Counter

    query, repo, source, _, state = signed_service()
    batched = query.query(actor(), **selection)
    batch_calls = Counter(
        (kind, tuple(sorted((filters or {}).items())), keys, fields)
        for kind, filters, keys, fields in source.calls
    )
    assert len(state.read_waves) == 1
    assert [kind for kind, _, _ in state.read_waves[0]] == [
        "teaching_course",
        "course_subject",
        "teaching_assignment",
        "offering_member_source",
    ]
    assert {permit.scope.fact_kind for permit in batched.private_proof.education.permits} >= {
        "teaching_course",
        "course_subject",
        "teaching_assignment",
        "offering_member_source",
    }
    source.calls.clear()
    repo.read_many = None
    serial = query.query(actor(), **selection)
    assert batched.public_result == serial.public_result
    assert batched.evidence == serial.evidence
    assert batch_calls == Counter(
        (kind, tuple(sorted((filters or {}).items())), keys, fields)
        for kind, filters, keys, fields in source.calls
    )


def test_signed_teaching_wave_permission_failure_precedes_all_wave_data():
    query, _, source, _, state = signed_service()
    state.deny_field = "duty_scope"
    with pytest.raises(PermissionError, match="field denied"):
        query.query(actor(), view="teaching", subject_name="数学")
    assert state.read_waves == []
    assert not any(
        kind in {"teaching_course", "teaching_assignment", "offering_member_source"}
        for kind, *_ in source.calls
    )


def test_signed_teaching_wave_revocation_prevents_answer_release():
    query, repo, _, _, state = signed_service()
    original = repo.projection.read_many

    def revoked(**kwargs):
        state.after_read = lambda: setattr(state, "allowed", False)
        return original(**kwargs)

    repo.projection.read_many = revoked
    with pytest.raises(PermissionError, match="revoked"):
        query.query(actor(), view="teaching", subject_name="数学")
    assert len(state.read_waves) == 1


def test_signed_teaching_multiple_offerings_chunk_without_extra_reads_or_lost_evidence():
    from collections import Counter
    from copy import deepcopy

    query, repo, source, _, state = signed_service()
    for kind, key in (
        ("teaching_offering", "offering:b"),
        ("teaching_assignment", "teaching:b"),
        ("offering_member_source", "source:b"),
    ):
        row = deepcopy(source.rows[kind][0])
        row["source_key"] = key
        if kind != "teaching_offering":
            row["segments"][0]["content"]["offering_source_key"] = "offering:b"
        source.rows[kind].append(row)
    batched = query.query(actor(), view="teaching", subject_name="数学")
    batch_calls = Counter(
        (kind, tuple(sorted((filters or {}).items())), keys, fields)
        for kind, filters, keys, fields in source.calls
    )
    assert [len(wave) for wave in state.read_waves] == [4, 2]
    assert len(batched.public_result["items"]) == 2
    source.calls.clear()
    repo.read_many = None
    serial = query.query(actor(), view="teaching", subject_name="数学")
    assert batched.public_result == serial.public_result
    assert batched.evidence == serial.evidence
    assert batch_calls == Counter(
        (kind, tuple(sorted((filters or {}).items())), keys, fields)
        for kind, filters, keys, fields in source.calls
    )
def test_school_external_owner_is_fixed_by_kind_and_legacy_catalog():
    from types import SimpleNamespace

    from app.composition.school_fact_education_external import EducationExternalReader

    calls = []

    class Owner:
        def __init__(self, label):
            self.label = label

        def reference_for(self, *, actor, fact_kind, source_key):
            calls.append((self.label, fact_kind, source_key))
            if self.label == "f3" and source_key == "person:legacy":
                raise PermissionError("revoked F3 catalog owner")
            return f"{self.label}:{fact_kind}:opaque"

    f3 = Owner("f3")
    family = SimpleNamespace(
        repository=f3,
        configuration=SimpleNamespace(source_catalog={"person": ("person:legacy",)}),
    )
    external = EducationExternalReader(directory=Owner("f2"), evidence=None, family=family)
    selected = actor()
    assert external.reference_for(
        actor=selected, fact_kind="student", source_key="student:new"
    ).startswith("f3:")
    assert external.reference_for(
        actor=selected, fact_kind="person", source_key="person:new"
    ).startswith("f2:")
    with pytest.raises(PermissionError, match="revoked F3"):
        external.reference_for(actor=selected, fact_kind="person", source_key="person:legacy")
    assert calls[-1] == ("f3", "person", "person:legacy")
