from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.composition.school_fact_directory_product import FactChangeInput, FactQueryInput
from app.composition.school_fact_family_product_adapter import (
    FamilyMaintenanceTarget,
    FamilyStaffProductQuery,
    SchoolFactFamilyProductAdapter,
)
from app.platform import contracts as platform_contracts
from app.platform.school_facts.contracts import CanonicalFactContentV1, fact_content_enums
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS, F3_FACT_KINDS


class ProfileRepository:
    def __init__(self):
        self.calls = []
        self.blocked = False

    def reference_for(self, *, fact_kind, source_key, **_):
        return "f3:" + fact_kind + ":" + source_key

    def query(self, **kw):
        self.calls.append(kw)
        kind = kw["fact_kind"]
        suffix = str(kw.get("related_ref", "")).rsplit(":", 1)[-1]
        if kind == "person":
            contents = [(key, {"display_name": "同名学生"}) for key in ("a", "b")]
        elif kind == "student":
            contents = [(suffix, {"person_source_key": suffix})]
        elif kind == "student_school_relationship":
            if self.blocked and suffix == "a":
                return {
                    "status": "forbidden",
                    "records": [],
                    "truncated": False,
                    "coverage": "unknown",
                    "queried_at": NOW.isoformat(),
                }
            contents = [
                (
                    suffix,
                    {
                        "student_number": "N-" + suffix,
                        "student_source_key": suffix,
                        "primary_campus_source_key": "campus",
                        "residence_mode": "boarding",
                    },
                )
            ]
        elif kind == "person_region":
            contents = [
                (
                    suffix,
                    {
                        "person_source_key": suffix,
                        "district_name": "指定区",
                        "district_code": "must-not-read",
                    },
                )
            ]
        else:
            raise AssertionError(kind)
        fields = kw["requested_fields"]
        return {
            "status": "known",
            "truncated": False,
            "coverage": "unknown",
            "queried_at": NOW.isoformat(),
            "records": [
                {
                    "reference": "f3:" + kind + ":" + key,
                    "label": "同名学生",
                    "content": {name: value for name, value in content.items() if name in fields},
                    "version": 1,
                    "valid_from": NOW.isoformat(),
                    "valid_to": None,
                    "source_name": "正式学生资料",
                    "source_reference": "publication:fixture",
                    "source_published_at": NOW.isoformat(),
                }
                for key, content in contents
            ],
        }


def test_profile_composes_only_requested_domains_after_exact_student_selection():
    repository = ProfileRepository()
    query = FamilyStaffProductQuery(repository, None, _ExternalRefs())
    result = query.student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("region",),
        as_of=None,
    )
    assert result["status"] == "known"
    assert result["profile"]["region"]["value"] == "指定区"
    regions = [call for call in repository.calls if call["fact_kind"] == "person_region"]
    assert len(regions) == 1 and regions[0]["related_ref"].endswith(":b")
    assert regions[0]["requested_fields"] == ("district_name",)
    assert "residence_mode" not in str(result["profile"])
    assert "district_code" not in str(repository.calls)


class NumberProfileRepository(ProfileRepository):
    def __init__(self, *, duplicate=False, blocked=False):
        super().__init__()
        self.duplicate, self.blocked = duplicate, blocked

    def query(self, **kw):
        self.calls.append(kw)
        kind = kw["fact_kind"]
        assert kw.get("query_text") is None, "A number must never be turned into a person's name"
        if kw.get("student_number") is not None:
            assert kind == "student_school_relationship"
            ids = ("a", "b") if self.duplicate else ("a",)
            if self.blocked or kw["student_number"] != "N-a":
                ids = ()
        else:
            reference = kw.get("reference") or kw.get("related_ref")
            assert reference, "Number selection must not enumerate unrelated people"
            ids = (reference.rsplit(":", 1)[1],)
        fields = kw["requested_fields"]
        content = {
            "person": lambda key: {"display_name": "同号学生" + key},
            "student": lambda key: {"person_source_key": key},
            "student_school_relationship": lambda key: {
                "student_number": "N-a",
                "student_source_key": key,
                "residence_mode": "boarding",
            },
        }
        return {
            "status": "forbidden" if self.blocked else "known" if ids else "no_current_records",
            "truncated": False,
            "coverage": "unknown",
            "queried_at": NOW.isoformat(),
            "records": [
                {
                    "reference": "f3:" + kind + ":" + key,
                    "label": "合成候选",
                    "content": {k: v for k, v in content[kind](key).items() if k in fields},
                    "version": 1,
                    "valid_from": NOW.isoformat(),
                    "valid_to": None,
                    "source_name": "正式学生资料",
                    "source_reference": "publication:fixture",
                    "source_published_at": NOW.isoformat(),
                }
                for key in ids
            ],
        }


def test_profile_number_only_filters_formal_relationship_before_exact_person_reads():
    repository = NumberProfileRepository()
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_profile(
        actor=actor(), name=None, student_number="N-a", fields=("name", "residence_mode"), as_of=NOW
    )
    assert result["status"] == "known"
    assert result["tool_context"]["student_ref"] == "f3:student:a"
    assert result["profile"]["name"]["value"] == "同号学生a"
    assert repository.calls[0]["fact_kind"] == "student_school_relationship"
    assert repository.calls[0]["student_number"] == "N-a"
    assert all(call["as_of"] == NOW for call in repository.calls)
    assert result["selection"]["name"] is None


@pytest.mark.parametrize("name,status", [("同号学生a", "known"), ("姓名不符", "object_unresolved")])
def test_f6_future_number_and_current_name_intersection_preserves_each_owner_time(name, status):
    from datetime import timedelta

    repository = NumberProfileRepository()
    future = datetime.now(UTC) + timedelta(days=1)
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_eligibility_profile(
        actor=actor(),
        name=name,
        student_number="N-a",
        fields=("name",),
        as_of=future,
    )
    assert result["status"] == status
    assert repository.calls[0]["as_of"] == future
    assert repository.calls[0]["_allow_formal_future"] is True
    assert all(call["as_of"] is None for call in repository.calls[1:])


@pytest.mark.parametrize(
    "number,duplicate,blocked,status",
    [
        ("missing", False, False, "object_unresolved"),
        ("N-a", True, False, "ambiguous"),
        ("N-a", False, True, "incomplete"),
    ],
)
def test_profile_number_only_never_guesses_missing_denied_or_duplicate_selection(
    number, duplicate, blocked, status
):
    repository = NumberProfileRepository(duplicate=duplicate, blocked=blocked)
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_profile(
        actor=actor(), name=None, student_number=number, fields=("name",), as_of=NOW
    )
    assert result["status"] == status
    assert "tool_context" not in result and "profile" not in result
    assert len(repository.calls) == (5 if duplicate else 1)


@pytest.mark.parametrize("supplied_name", ["许同学", "周老师", "同名学生"])
def test_profile_preserves_supplied_name_and_reports_actual_selection(supplied_name):
    repository = ProfileRepository()
    query = FamilyStaffProductQuery(repository, None, _ExternalRefs())
    result = query.student_profile(
        actor=actor(),
        name=supplied_name,
        student_number="N-b",
        fields=("region",),
        as_of=None,
    )
    assert repository.calls[0]["query_text"] == supplied_name
    assert result["selection"] == {
        "name": supplied_name,
        "student_number": "N-b",
        "fields": ["region"],
        "as_of": None,
    }


def test_profile_does_not_choose_visible_number_when_another_candidate_cannot_be_checked():
    repository = ProfileRepository()
    repository.blocked = True
    query = FamilyStaffProductQuery(repository, None, _ExternalRefs())
    result = query.student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("region",),
        as_of=None,
    )
    assert result["status"] == "incomplete"
    assert not any(call["fact_kind"] == "person_region" for call in repository.calls)
    assert "profile" not in result


def test_profile_returns_ambiguous_candidates_without_reading_their_regions():
    repository = ProfileRepository()
    query = FamilyStaffProductQuery(repository, None, _ExternalRefs())
    result = query.student_profile(
        actor=actor(),
        name="同名学生",
        student_number=None,
        fields=("region",),
        as_of=None,
    )
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert result["clarification"] == {"accepted_fields": ["name", "student_number"]}
    assert not any(call["fact_kind"] == "person_region" for call in repository.calls)


@pytest.mark.parametrize("status", ["forbidden", "source_unavailable", "conflict"])
def test_profile_retains_field_failure_and_partial_success_without_claiming_no_records(status):
    repository = ProfileRepository()
    original = repository.query

    def with_region_failure(**kw):
        view = original(**kw)
        if kw["fact_kind"] == "person_region":
            view.update(status=status, records=[])
        return view

    repository.query = with_region_failure
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("name", "region"),
        as_of=None,
    )
    assert result["status"] == "incomplete"
    assert result["profile"]["name"]["value"] == "同名学生"
    assert result["profile"]["region"]["status"] == status
    assert "no_current_records" not in str(result["profile"])


@pytest.mark.parametrize("kind", ["person_region", "student_school_relationship"])
def test_profile_multiple_legal_relationship_records_require_selection_not_invented_conflict(kind):
    repository = ProfileRepository()
    original = repository.query

    def with_multiple(**kw):
        view = original(**kw)
        if kw["fact_kind"] == kind:
            view["records"] = view["records"] * 2
        return view

    repository.query = with_multiple
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("region",),
        as_of=None,
    )
    assert result["status"] == "incomplete"
    assert "ambiguous" in str(result["limitations"])
    assert "conflict" not in str(result)


def test_profile_uses_location_owner_name_and_does_not_collect_unselected_contact_data():
    class Locations(_ExternalRefs):
        def __init__(self):
            self.calls = []

        def location_name(self, **kw):
            self.calls.append(kw)
            return {"status": "known", "name": "正式西校区", "source": {"label": "地点发布"}}

    locations = Locations()
    repository = ProfileRepository()
    result = FamilyStaffProductQuery(repository, None, locations).student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("campus",),
        as_of=NOW,
    )
    assert result["profile"] == {"campus": {"status": "known", "value": "正式西校区"}}
    assert len(locations.calls) == 1 and locations.calls[0]["as_of"] == NOW
    assert not any("contact" in call["fact_kind"] for call in repository.calls)
    schools = [
        call for call in repository.calls if call["fact_kind"] == "student_school_relationship"
    ]
    assert all(call["requested_fields"] == ("student_number",) for call in schools[:-1])
    assert schools[-1]["requested_fields"] == ("primary_campus_source_key",)
    assert schools[-1]["related_ref"].endswith(":b")


def test_profile_authorized_null_campus_is_not_confused_with_an_unreturned_field():
    repository = ProfileRepository()
    original = repository.query

    def nullable(**kw):
        view = original(**kw)
        if kw["fact_kind"] == "student_school_relationship":
            for row in view["records"]:
                if "primary_campus_source_key" in row["content"]:
                    row["content"]["primary_campus_source_key"] = None
        return view

    repository.query = nullable
    result = FamilyStaffProductQuery(repository, None, _ExternalRefs()).student_profile(
        actor=actor(),
        name="同名学生",
        student_number="N-b",
        fields=("campus",),
        as_of=None,
    )
    assert result["profile"]["campus"] == {"status": "not_recorded", "value": None}


NOW = datetime(2026, 9, 13, 9, tzinfo=UTC)
SCHOOL = UUID("00000000-0000-0000-0000-000000000001")


def actor():
    return platform_contracts._issue_staff_actor_context(
        account_user_id=9,
        school_id=platform_contracts.SchoolId(SCHOOL),
        person_id=platform_contracts.PersonId(UUID("00000000-0000-0000-0000-000000000002")),
        assignment_id=platform_contracts.AssignmentId(UUID("00000000-0000-0000-0000-000000000003")),
        assignment_version=2,
        authorization_version=5,
    )


class Repository:
    def __init__(self, target: FamilyMaintenanceTarget):
        self.target = target

    def prepare_target(self, **_kwargs):
        return self.target

    def resolve_field_references(self, **kwargs):
        return {
            key.removesuffix("_ref") + "_source_key" if key.endswith("_ref") else key: value
            for key, value in kwargs["fields"].items()
        }

    def public_field_references(self, **kwargs):
        return {
            key.removesuffix("_source_key") + "_ref" if key.endswith("_source_key") else key: (
                f"f3:{key.removesuffix('_source_key')}:{value}"
                if key.endswith("_source_key") and value is not None
                else value
            )
            for key, value in kwargs["fields"].items()
        }

    def creation_choices(self, **_kwargs):
        return ({"creation_ref": "create:guardian:1", "label": "新增家校关系"},)

    def reference_for(self, **kwargs):
        return f"f3:{kwargs['fact_kind']}:{kwargs['source_key']}"


def payload(kind: str, **fields):
    return FactChangeInput(
        fact_kind=kind,
        target_ref="f3:target:0001",
        fields=fields,
        reason="按学校核验结果维护",
    )


def test_query_remains_f2_while_maintenance_accepts_every_f3_kind() -> None:
    kinds = {
        "person",
        "login_mapping",
        "assignment",
        "student",
        "student_school_relationship",
        "student_contact",
        "guardian_relationship",
        "person_region",
    }
    for kind in kinds:
        assert payload(kind).fact_kind == kind
        if kind in {
            "login_mapping",
            "student",
            "student_school_relationship",
            "student_contact",
            "guardian_relationship",
            "person_region",
        }:
            with pytest.raises(ValidationError):
                FactQueryInput(fact_kind=kind, scope="all_authorized")


def test_resolve_change_uses_current_version_and_rejects_unknown_fields() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="student_school_relationship",
        source_key="student-school:001",
        expected_version=7,
        school_fact_version=12,
        content={
            "student_source_key": "student:001",
            "student_number": "S001",
            "enrollment_status": "enrolled",
            "residence_mode": "day",
            "primary_campus_source_key": "location:main",
            "administrative_campus_source_key": "location:main",
        },
        label="同名学生 · S001",
        reference="f3:student:opaque",
    )
    adapter = SchoolFactFamilyProductAdapter(Repository(target))
    resolved = adapter.resolve_change(
        actor=actor(),
        payload=payload("student_school_relationship", residence_mode="boarding"),
        request_id=uuid4(),
        effective_at=NOW,
    )
    change = resolved.change_set.changes[0]
    assert change.expected_version == 7
    assert change.content is not None and change.content.residence_mode == "boarding"
    assert resolved.summary.before is not None
    assert "student_source_key" not in resolved.summary.before
    assert resolved.summary.before["student_ref"] == "f3:student:student:001"
    assert resolved.summary.after is not None
    assert set(resolved.summary.after) == set(resolved.summary.before)
    assert resolved.summary.after["residence_mode"] == "boarding"

    with pytest.raises(ValueError, match="字段"):
        adapter.resolve_change(
            actor=actor(),
            payload=payload("student_school_relationship", district_name="越权字段"),
            request_id=uuid4(),
            effective_at=NOW,
        )


def test_guardian_confirmation_keeps_pending_objects_and_two_step_status() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="guardian_relationship",
        source_key="family:001",
        expected_version=1,
        school_fact_version=4,
        content={
            "guardian_person_source_key": "person:parent",
            "student_source_key": "student:001",
            "relationship_kind": "verified_guardian",
            "access_purpose": "guardian_student_profile",
            "verification_source": "school_review",
            "verification_reference": "synthetic:review:001",
            "status": "pending_verification",
        },
        label="家校关系 · 待核验",
        reference="f3:guardian:opaque",
    )
    adapter = SchoolFactFamilyProductAdapter(Repository(target))
    resolved = adapter.resolve_change(
        actor=actor(),
        payload=payload("guardian_relationship", status="confirmed"),
        request_id=uuid4(),
        effective_at=NOW,
    )
    content = resolved.change_set.changes[0].content
    assert content is not None
    assert content.status == "confirmed"
    assert content.guardian_person_source_key == "person:parent"
    assert "verified_by_person_id" not in content.model_fields_set


def test_person_region_is_optional_but_never_accepts_detailed_address() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="person_region",
        source_key="region:001",
        expected_version=0,
        school_fact_version=2,
        content={},
        label="可选区县",
        reference="create:region:001",
    )
    adapter = SchoolFactFamilyProductAdapter(Repository(target))
    resolved = adapter.resolve_change(
        actor=actor(),
        payload=FactChangeInput(
            fact_kind="person_region",
            creation_ref="create:region:001",
            fields={
                "person_ref": "f3:person:opaque",
                "district_code": "330106",
                "district_name": "西湖区",
                "region_purpose": "student_profile",
            },
            reason="本人自愿提供区县",
        ),
        request_id=uuid4(),
        effective_at=NOW,
    )
    assert resolved.change_set.changes[0].content == CanonicalFactContentV1(
        person_source_key="f3:person:opaque",
        district_code="330106",
        district_name="西湖区",
        region_purpose="student_profile",
    )
    with pytest.raises(ValueError, match="字段"):
        adapter.resolve_change(
            actor=actor(),
            payload=FactChangeInput(
                fact_kind="person_region",
                creation_ref="create:region:001",
                fields={"street_address": "详细地址"},
                reason="不应接受",
            ),
            request_id=uuid4(),
            effective_at=NOW,
        )


def test_describe_matches_directory_workbench_field_contract() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="person_region",
        source_key="region:1",
        expected_version=1,
        school_fact_version=1,
        content={},
        label="地区",
        reference="f3:person_region:x",
    )
    view = SchoolFactFamilyProductAdapter(Repository(target)).describe(
        actor=actor(), fact_kind="person_region"
    )
    assert view["label"] == "人员地区"
    assert all(
        set(field) == {"name", "label", "type", "required", "reference_kind", "options"}
        for field in view["fields"]
    )
    assert (
        next(field for field in view["fields"] if field["name"] == "person_ref")["type"]
        == "reference"
    )


class _RawStaffQuery:
    def query(self, **kwargs):
        return {
            "kind": "f3_staff_facts",
            "status": "known",
            "fact_kind": "person_region",
            "records": [
                {
                    "reference": "f3:person_region:x",
                    "label": "现居地区",
                    "content": {
                        "person_source_key": "person:1",
                        "review_of_relationship_source_key": None,
                        "district_name": "西湖区",
                    },
                    "version": 2,
                    "factual_at": NOW.isoformat(),
                    "valid_from": NOW.isoformat(),
                    "valid_to": None,
                    "source_published_at": None,
                    "source_name": None,
                    "source_reference": "publication:00000000-0000-0000-0000-000000000001",
                    "source_trace_reference": "source:synthetic:sheet-1",
                }
            ],
            "coverage": "unknown",
            "has_more": False,
            "truncated": False,
            "queried_at": NOW.isoformat(),
        }

    def reference_for(self, **kwargs):
        return "f3:person:opaque"


class _ExternalRefs:
    def reference_for(self, **kwargs):
        return "f2:" + kwargs["fact_kind"] + ":opaque"

    def resolve(self, **kwargs):
        return "external:source"


def test_family_staff_product_query_matches_directory_workbench_record_contract() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="person_region",
        source_key="region:1",
        expected_version=1,
        school_fact_version=1,
        content={},
        label="地区",
        reference="f3:person_region:x",
    )
    adapter = SchoolFactFamilyProductAdapter(Repository(target))
    view = FamilyStaffProductQuery(_RawStaffQuery(), adapter, _ExternalRefs()).query(
        actor=actor(), fact_kind="person_region"
    )
    assert view["kind"] == "directory_facts"
    assert view["returned_count"] == len(view["records"]) == 1
    row = view["records"][0]
    assert row["fields"]["person_ref"] == "f3:person:opaque"
    assert row["fields"]["review_of_relationship_source_key"] is None
    assert row["source"]["label"] == "学校事实正式发布"
    assert row["source"]["reference"] == ("publication:00000000-0000-0000-0000-000000000001")
    assert "00000000" not in row["source"]["label"]
    assert row["source"]["trace_reference"] == "source:synthetic:sheet-1"
    assert set(row) == {
        "reference",
        "fact_kind",
        "label",
        "fields",
        "version",
        "valid_from",
        "valid_to",
        "source",
    }
    assert set(view["coverage"]) == {"status", "description"}


def test_describe_uses_canonical_required_types_and_localized_enum_options() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="assignment",
        source_key="assignment:1",
        expected_version=0,
        school_fact_version=1,
        content={},
        label="新增任职",
        reference="f3:assignment:x",
    )
    view = SchoolFactFamilyProductAdapter(Repository(target)).describe(
        actor=actor(), fact_kind="assignment"
    )
    fields = {field["name"]: field for field in view["fields"]}
    assert fields["person_ref"]["required"] is True
    assert fields["valid_to"]["required"] is False
    assert fields["valid_to"]["type"] == "datetime"
    assert fields["assignment_kind"]["options"] == [
        {"value": "acting", "label": "临时代理"},
        {"value": "part_time", "label": "兼职"},
        {"value": "substantive", "label": "正式任职"},
    ]


def test_family_describe_declares_name_and_explicit_related_field_support() -> None:
    target = FamilyMaintenanceTarget(
        fact_kind="student_school_relationship",
        source_key="enrollment:1",
        expected_version=1,
        school_fact_version=1,
        content={},
        label="在校关系",
        reference="f3:student_school_relationship:x",
    )
    adapter = SchoolFactFamilyProductAdapter(Repository(target))
    student = adapter.describe(actor=actor(), fact_kind="student")
    assert student["query_support"]["name_fields"] == []
    assert "不支持名称条件" in student["query_support"]["guidance"]
    view = adapter.describe(actor=actor(), fact_kind="student_school_relationship")
    assert view["query_support"]["student_number"] is True
    assert "学号" in view["query_support"]["guidance"]
    assert view["query_support"]["related_fields"] == [
        {"field": "administrative_campus_ref", "kind": "location", "label": "行政校区"},
        {"field": "primary_campus_ref", "kind": "location", "label": "主要校区"},
        {"field": "student_ref", "kind": "student", "label": "学生"},
    ]
    assert view["creation_choices"] == [
        {"creation_ref": "create:guardian:1", "label": "新增家校关系"}
    ]


class _EnabledStaffQuery(_RawStaffQuery):
    def query(self, **kwargs):
        kind = kwargs["fact_kind"]
        enums = fact_content_enums(kind)
        content = {}
        for name in F3_FACT_FIELDS[kind]:
            if name.endswith("_source_key"):
                content[name] = "source:synthetic:linked-object"
            elif name in enums:
                content[name] = sorted(enums[name])[0]
            else:
                content[name] = {
                    "residence_mode": "boarding",
                    "priority": 1,
                    "is_emergency": True,
                    "valid_to": None,
                    "district_code": "330106",
                    "display_name": "林同学",
                }.get(name, "正式资料")
        view = super().query(**kwargs)
        view["fact_kind"] = kind
        view["records"][0]["content"] = content
        view["records"][0]["reference"] = f"f3:{kind}:opaque"
        return view

    def creation_choices(self, **_kwargs):
        return ()

    def reference_for(self, **kwargs):
        return f"f3:{kwargs['fact_kind']}:opaque"


@pytest.mark.parametrize("fact_kind", sorted(F3_FACT_KINDS))
def test_every_enabled_product_query_shape_has_complete_model_field_descriptors(fact_kind):
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_gateway import MODULE, FamilyStaffToolGateway
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    repository = _EnabledStaffQuery()
    adapter = SchoolFactFamilyProductAdapter(repository)
    query = FamilyStaffProductQuery(repository, adapter, _ExternalRefs())
    gateway = FamilyStaffToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor(), session_binding="session"),
        query=query,
        product=SimpleNamespace(),
    )
    result = gateway.execute(
        ModelToolCall(
            call_id="read",
            name=MODULE + ".read",
            arguments={
                "fact_kind": fact_kind,
                "fields": [
                    name.removesuffix("_source_key") + "_ref"
                    if name.endswith("_source_key")
                    else name
                    for name in sorted(F3_FACT_FIELDS[fact_kind])
                ],
            },
        ),
        ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.fact_card["kind"] == "directory_facts"
    fields = result.fact_card["records"][0]["fields"]
    expected_names = {
        name.removesuffix("_source_key") + "_ref" if name.endswith("_source_key") else name
        for name in F3_FACT_FIELDS[fact_kind]
    }
    assert set(fields) == expected_names
    model_fields = result.model_content["records"][0]["fields"]
    assert set(model_fields["field_labels"]) == expected_names
    descriptors = {
        item["name"]: item for item in query.describe(actor=actor(), fact_kind=fact_kind)["fields"]
    }
    assert set(descriptors) == expected_names
    for name in expected_names:
        descriptor = descriptors[name]
        if descriptor["type"] == "reference":
            assert model_fields["tool_context"][name] == fields[name]
            assert name not in model_fields
        elif descriptor["type"] == "select":
            labels = {option["value"]: option["label"] for option in descriptor["options"]}
            assert labels and all(value != label for value, label in labels.items())
            assert model_fields[name] == labels[fields[name]]
            assert model_fields["tool_context"]["field_values"][name] == fields[name]
        else:
            assert model_fields[name] == fields[name]
    if fact_kind == "person":
        assert fields["display_name"] == model_fields["display_name"] == "林同学"
        assert "name" not in fields
        assert descriptors["person_kind"]["options"] == [
            {"value": "other", "label": "其他"},
            {"value": "staff", "label": "教职工"},
            {"value": "student_contact", "label": "学生联系人"},
        ]
