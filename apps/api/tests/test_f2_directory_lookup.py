from copy import deepcopy
from datetime import UTC, datetime
from importlib.util import find_spec

import pytest
from pydantic import ValidationError


def api():
    assert find_spec("app.composition.school_fact_directory_lookup"), "lookup capability missing"
    from app.composition.school_fact_directory_lookup import FactLookupInput, lookup

    return FactLookupInput, lookup


def record(ref, label, **fields):
    return {"reference": ref, "label": label, "fields": fields}


def view(kind, *records, status=None, **extra):
    return {
        "kind": "directory_facts",
        "fact_kind": kind,
        "status": status or ("known" if records else "no_current_records"),
        "records": list(records),
        "truncated": False,
        "matched_count": len(records),
        "coverage": {"status": "unknown"},
        **extra,
    }


class Adapter:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def query(self, *, actor, payload):
        self.calls.append(payload)
        assert actor == "trusted-actor"
        assert payload.scope == "selected"
        key = (payload.fact_kind, payload.query_text or payload.reference or payload.related_ref)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def execute(adapter, **kw):
    Input, lookup = api()
    return lookup(adapter, actor="trusted-actor", payload=Input(**kw))


@pytest.mark.parametrize("name,ref", [("青石校区", "location:one"), ("松林校区", "location:two")])
def test_exact_location_lookup_uses_only_direct_contact(name, ref):
    contact = view("official_contact", record("contact:one", "值班电话", service_hours="8–17"))
    adapter = Adapter(
        {
            ("location", name): view("location", record(ref, name)),
            ("official_contact", ref): contact,
        }
    )
    result = execute(
        adapter, subject_kind="location", query_text=name, include=("official_contact",)
    )
    assert result["kind"] == "directory_bundle"
    assert result["status"] == "known"
    assert result["subject_label"] == name
    assert result["results"][-1] == contact
    assert len(adapter.calls) == 2


def organization_adapter():
    candidates = view(
        "organization",
        record("organization:one", "研修组"),
        record("organization:two", "研修组"),
        status="ambiguous",
    )
    return Adapter(
        {
            ("organization", "研修组"): candidates,
            ("location", "松林校区"): view(
                "location", record("location:two", "松林校区", kind="campus")
            ),
            ("location", "青石校区"): view(
                "location", record("location:one", "青石校区", kind="campus")
            ),
            ("location", "location:one"): view(
                "location", record("location:one", "青石校区", kind="campus")
            ),
            ("location", "location:two"): view(
                "location", record("location:two", "松林校区", kind="campus")
            ),
            ("organization_campus_relationship", "organization:one"): view(
                "organization_campus_relationship",
                record(
                    "relation:one",
                    "关系",
                    organization_ref="organization:one",
                    campus_ref="location:one",
                ),
            ),
            ("organization_campus_relationship", "organization:two"): view(
                "organization_campus_relationship",
                record(
                    "relation:two",
                    "关系",
                    organization_ref="organization:two",
                    campus_ref="location:two",
                ),
            ),
            ("organization_profile", "organization:one"): view(
                "organization_profile", record("profile:one", "职能", function_summary="培训")
            ),
            ("organization_profile", "organization:two"): view(
                "organization_profile", record("profile:two", "职能", function_summary="研究")
            ),
        }
    )


def test_real_ambiguity_returns_authorized_campus_context_without_requested_facts():
    adapter = organization_adapter()
    result = execute(
        adapter, subject_kind="organization", query_text="研修组", include=("organization_profile",)
    )
    assert result["status"] == "ambiguous"
    assert {r["reference"] for r in result["candidates"]} == {
        "organization:one",
        "organization:two",
    }
    assert not any(p.fact_kind == "organization_profile" for p in adapter.calls)
    assert len(adapter.calls) == 5
    assert {r["campuses"][0]["label"] for r in result["candidates"]} == {"青石校区", "松林校区"}
    assert result["requested_fact_kinds"] == ["organization_profile"]


def test_campus_constraint_and_changed_target_use_formal_relations():
    adapter = organization_adapter()
    for campus, expected in [("松林校区", "研究"), ("青石校区", "培训")]:
        result = execute(
            adapter,
            subject_kind="organization",
            query_text="研修组",
            campus_name=campus,
            include=("organization_profile",),
        )
        assert result["status"] == "known"
        assert result["results"][-1]["records"][0]["fields"]["function_summary"] == expected


@pytest.mark.parametrize("status", ["source_unavailable", "denied", "no_current_records"])
def test_missing_or_unavailable_relations_never_guess_campus(status):
    adapter = organization_adapter()
    adapter.responses[("organization_campus_relationship", "organization:two")] = view(
        "organization_campus_relationship", status=status
    )
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile",),
    )
    assert result["status"] in {status, "partial"}
    assert not any(p.fact_kind == "organization_profile" for p in adapter.calls)


@pytest.mark.parametrize(
    "status", ["source_unavailable", "denied", "formal_negative", "no_current_records"]
)
def test_contact_failure_or_absence_preserves_own_status(status):
    adapter = Adapter(
        {
            ("location", "location:one"): view("location", record("location:one", "青石校区")),
            ("official_contact", "location:one"): view("official_contact", status=status),
        }
    )
    result = execute(
        adapter, subject_kind="location", reference="location:one", include=("official_contact",)
    )
    assert result["results"][-1]["status"] == status
    assert result["status"] != "known"


def test_truncated_subject_does_not_resolve_unique_or_fetch_facts():
    adapter = Adapter(
        {
            ("location", "同名校区"): view(
                "location", record("location:one", "同名校区"), truncated=True, matched_count=2
            )
        }
    )
    result = execute(
        adapter, subject_kind="location", query_text="同名校区", include=("official_contact",)
    )
    assert result["status"] == "partial"
    assert len(adapter.calls) == 1


def test_as_of_is_propagated_and_exceptions_are_not_absence():
    adapter = Adapter({("location", "青石校区"): PermissionError("revoked")})
    result = execute(
        adapter,
        subject_kind="location",
        query_text="青石校区",
        include=("official_contact",),
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert result["status"] == "denied"
    assert adapter.calls[0].as_of == datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"query_text": "青石", "reference": "location:one"},
        {"query_text": "青石", "campus_name": "松林"},
        {"query_text": "青石", "include": ("organization_profile",)},
        {"query_text": "青石", "include": ("official_contact", "official_contact")},
    ],
)
def test_invalid_or_semantically_unbounded_requests_rejected(fields):
    Input, _ = api()
    with pytest.raises(ValidationError):
        Input(**{"subject_kind": "location", "include": ("official_contact",), **fields})


def test_auxiliary_quality_source_failure_is_not_complete_success():
    adapter = Adapter(
        {
            ("location", "location:one"): view("location", record("location:one", "青石")),
            ("official_contact", "location:one"): view(
                "official_contact",
                record("contact:one", "热线"),
                quality={"evidence_status": "source_unavailable", "coverage": "unknown"},
            ),
        }
    )
    result = execute(
        adapter, subject_kind="location", reference="location:one", include=("official_contact",)
    )
    assert result["status"] == "partial"
    assert result["results"][-1]["quality"]["evidence_status"] == "source_unavailable"


def test_organization_office_uses_only_authorized_linked_location_and_time():
    adapter = organization_adapter()
    adapter.responses[("organization_location", "organization:two")] = view(
        "organization_location",
        record(
            "office:one",
            "接待",
            organization_ref="organization:two",
            location_ref="location:office",
        ),
    )
    adapter.responses[("location", "location:office")] = view(
        "location", record("location:office", "松林接待室", kind="room")
    )
    when = datetime(2026, 9, 1, tzinfo=UTC)
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile", "organization_location"),
        as_of=when,
    )
    assert result["status"] == "known"
    assert result["results"][-1]["records"][0]["label"] == "松林接待室"
    assert all(call.as_of == when for call in adapter.calls)


def test_relationship_never_authorizes_campus_label():
    adapter = organization_adapter()
    adapter.responses[("organization", "研修组")] = view(
        "organization", record("organization:two", "研修组")
    )
    adapter.responses[("location", "location:two")] = view("location", status="denied")
    result = execute(
        adapter, subject_kind="organization", query_text="研修组", include=("organization_profile",)
    )
    assert result["status"] != "known"
    assert "松林校区" not in str(result)
    assert not any(p.fact_kind == "organization_profile" for p in adapter.calls)


def test_partial_campus_relation_page_cannot_prove_unique_match():
    adapter = organization_adapter()
    adapter.responses[("organization_campus_relationship", "organization:two")]["truncated"] = True
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile",),
    )
    assert result["status"] == "partial"
    assert not any(p.fact_kind == "organization_profile" for p in adapter.calls)


def test_constraint_does_not_fetch_unrelated_campus_labels():
    adapter = organization_adapter()
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile",),
    )
    assert result["status"] == "known"
    assert not any(p.fact_kind == "location" and p.reference for p in adapter.calls)


def test_ambiguous_campus_context_does_not_infer_unreadable_label():
    adapter = organization_adapter()
    adapter.responses[("location", "location:two")] = view("location", status="denied")
    result = execute(
        adapter, subject_kind="organization", query_text="研修组", include=("organization_profile",)
    )
    assert result["status"] == "partial"
    assert "松林校区" not in str(result)
    assert not any(p.fact_kind == "organization_profile" for p in adapter.calls)


def test_candidate_context_follows_actual_relation_order_and_time():
    adapter = organization_adapter()
    adapter.responses[("organization", "研修组")]["records"].reverse()
    when = datetime(2026, 8, 20, tzinfo=UTC)
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        include=("organization_profile",),
        as_of=when,
    )
    assert result["candidates"][0]["reference"] == "organization:two"
    assert result["candidates"][0]["campuses"][0]["label"] == "松林校区"
    assert all(call.as_of == when for call in adapter.calls)


@pytest.mark.parametrize("fact_status", ["no_current_records", "formal_negative"])
def test_resolved_empty_query_is_completed_without_searching_other_subjects(fact_status):
    adapter = Adapter(
        {
            ("location", "location:one"): view("location", record("location:one", "青石")),
            ("official_contact", "location:one"): view("official_contact", status=fact_status),
        }
    )
    result = execute(
        adapter, subject_kind="location", reference="location:one", include=("official_contact",)
    )
    assert result["status"] == fact_status
    assert result["query_completion"] == "completed"
    assert len(adapter.calls) == 2


def test_multi_part_result_keeps_empty_part_without_claiming_unfinished_query():
    adapter = organization_adapter()
    adapter.responses[("organization_location", "organization:two")] = view("organization_location")
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile", "organization_location"),
    )
    assert result["status"] == "known"
    assert result["query_completion"] == "completed"
    assert [
        v["status"]
        for v in result["results"]
        if v["fact_kind"] in {"organization_profile", "organization_location"}
    ] == ["known", "no_current_records"]


@pytest.mark.parametrize("failure", ["denied", "source_unavailable"])
def test_failed_requested_part_is_not_completed(failure):
    adapter = organization_adapter()
    adapter.responses[("organization_profile", "organization:two")] = view(
        "organization_profile", status=failure
    )
    result = execute(
        adapter,
        subject_kind="organization",
        query_text="研修组",
        campus_name="松林校区",
        include=("organization_profile",),
    )
    assert result["status"] == failure
    assert result["query_completion"] == "incomplete"


def test_missing_subject_is_not_a_completed_subject_fact_query():
    adapter = Adapter({("location", "未知名称"): view("location")})
    result = execute(
        adapter, subject_kind="location", query_text="未知名称", include=("official_contact",)
    )
    assert result["query_completion"] == "subject_unresolved"
    assert len(adapter.calls) == 1


class ServiceAdapter(Adapter):
    def query(self, *, actor, payload):
        self.calls.append(payload)
        assert actor == "trusted-actor"
        if payload.scope == "all_authorized":
            assert payload.fact_kind == "school_service"
            assert not (payload.query_text or payload.reference or payload.related_ref)
        else:
            assert payload.scope == "selected"
        key = (payload.fact_kind, payload.query_text or payload.reference or payload.related_ref)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def service_adapter():
    service = record(
        "school_service:one",
        "资料服务",
        organization_ref="organization:one",
        official_contact_ref="official_contact:one",
    )
    return ServiceAdapter(
        {
            ("school_service", "资料服务"): view("school_service", service),
            ("school_service", None): view("school_service", service),
            ("official_contact", "official_contact:one"): view(
                "official_contact",
                record("official_contact:one", "公开电话", contact_value="021-00000001"),
            ),
            ("organization", "organization:one"): view(
                "organization",
                record("organization:one", "综合组", parent_ref="organization:parent"),
            ),
            ("organization_campus_relationship", "organization:one"): view(
                "organization_campus_relationship",
                record(
                    "relationship:one",
                    "正式关联",
                    organization_ref="organization:one",
                    campus_ref="location:one",
                ),
                record(
                    "relationship:two",
                    "正式关联",
                    organization_ref="organization:one",
                    campus_ref="location:two",
                ),
            ),
            ("location", "location:one"): view(
                "location", record("location:one", "山城校区", kind="campus")
            ),
            ("location", "location:two"): view(
                "location", record("location:two", "湖岸校区", kind="campus")
            ),
        }
    )


def test_service_lookup_reads_exact_channel_and_provider_campuses_without_claiming_service_scope():
    adapter = service_adapter()
    when = datetime(2026, 9, 7, tzinfo=UTC)
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("official_contact", "organization_campus_relationship"),
        as_of=when,
    )
    assert result["status"] == "known"
    assert result["query_completion"] == "completed"
    group = result["service_groups"][0]
    assert group["service_reference"] == "school_service:one"
    assert group["official_contact"] == {"reference": "official_contact:one", "status": "known"}
    assert group["provider"] == {"reference": "organization:one", "status": "known"}
    assert group["provider_campuses"]["relation_references"] == [
        "relationship:one",
        "relationship:two",
    ]
    assert group["provider_campuses"]["campuses"] == [
        {"reference": "location:one", "status": "known"},
        {"reference": "location:two", "status": "known"},
    ]
    assert group["relationship_scope"] == "provider_affiliation_only"
    assert "service_campus" not in group
    assert "unrelated" not in str(result)
    assert all(p.as_of == when for p in adapter.calls)
    assert not any(p.reference == "organization:parent" for p in adapter.calls)
    contact = next(v for v in result["results"] if v["fact_kind"] == "official_contact")
    assert contact["records"][0]["fields"]["contact_value"] == "021-00000001"


def test_service_list_is_legal_multivalue_and_shared_dependencies_are_read_once():
    adapter = service_adapter()
    service = deepcopy(adapter.responses[("school_service", None)]["records"][0])
    service.update(reference="school_service:two", label="借阅服务")
    adapter.responses[("school_service", None)]["records"].append(service)
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact", "organization_campus_relationship"),
    )
    assert result["status"] == "known"
    assert len(result["service_groups"]) == 2
    assert len(adapter.calls) == 6
    assert adapter.calls[0].scope == "all_authorized"
    assert all(p.scope == "selected" for p in adapter.calls[1:])


def test_selected_same_named_services_return_candidates_without_guessing_or_reading_children():
    adapter = service_adapter()
    service = deepcopy(adapter.responses[("school_service", "资料服务")]["records"][0])
    service["reference"] = "school_service:two"
    adapter.responses[("school_service", "资料服务")]["records"].append(service)
    result = execute(
        adapter, subject_kind="school_service", query_text="资料服务", include=("official_contact",)
    )
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert not result.get("service_groups")
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "failure", ["denied", "source_unavailable", "no_current_records", "formal_negative"]
)
def test_service_failed_channel_keeps_exact_status_and_other_requested_data(failure):
    adapter = service_adapter()
    adapter.responses[("official_contact", "official_contact:one")] = view(
        "official_contact", status=failure
    )
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("official_contact", "organization_campus_relationship"),
    )
    assert result["service_groups"][0]["official_contact"]["status"] == failure
    assert result["service_groups"][0]["provider_campuses"]["status"] == "known"
    assert result["status"] == (
        "partial" if failure in {"denied", "source_unavailable"} else "known"
    )
    assert result["query_completion"] == (
        "incomplete" if failure in {"denied", "source_unavailable"} else "completed"
    )


def test_service_link_does_not_authorize_provider_or_campus_name():
    adapter = service_adapter()
    adapter.responses[("organization", "organization:one")] = view("organization", status="denied")
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("organization_campus_relationship",),
    )
    assert result["status"] == "partial"
    assert result["service_groups"][0]["provider"]["status"] == "denied"
    assert not any(p.fact_kind == "organization_campus_relationship" for p in adapter.calls)
    assert "山城校区" not in str(result)
    adapter = service_adapter()
    adapter.responses[("location", "location:one")] = view("location", status="denied")
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("organization_campus_relationship",),
    )
    assert result["status"] == "partial"
    assert "山城校区" not in str(result)
    assert result["service_groups"][0]["provider_campuses"]["campuses"][0]["status"] == "denied"


def test_service_absent_reference_is_not_called_absent_target_or_substituted_by_owner_contacts():
    adapter = service_adapter()
    adapter.responses[("school_service", "资料服务")]["records"][0]["fields"].pop(
        "official_contact_ref"
    )
    result = execute(
        adapter, subject_kind="school_service", query_text="资料服务", include=("official_contact",)
    )
    assert result["service_groups"][0]["official_contact"]["status"] == "reference_not_provided"
    assert len(adapter.calls) == 1
    assert result["query_completion"] == "completed"
    assert result["status"] == "partial"


def test_service_short_list_truncation_keeps_visible_services_but_never_complete():
    adapter = service_adapter()
    adapter.responses[("school_service", None)].update(truncated=True, matched_count=12)
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact",),
    )
    assert result["status"] == "partial"
    assert result["query_completion"] == "incomplete"
    assert len(result["service_groups"]) == 1
    assert result["results"][0]["truncated"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"subject_kind": "location", "scope": "all_authorized"},
        {"scope": "all_authorized", "query_text": "资料服务"},
        {"scope": "all_authorized", "reference": "school_service:one"},
        {"query_text": "资料服务", "campus_name": "湖岸校区"},
        {"query_text": "资料服务", "include": ("organization_location",)},
        {},
    ],
)
def test_service_lookup_rejects_scope_substitution_or_unbounded_relations(fields):
    Input, _ = api()
    with pytest.raises(ValidationError):
        Input(**{"subject_kind": "school_service", "include": ("official_contact",), **fields})


@pytest.mark.parametrize("status", ["denied", "source_unavailable"])
def test_service_list_failure_is_incomplete_read_not_unresolved_named_subject(status):
    adapter = service_adapter()
    adapter.responses[("school_service", None)] = view("school_service", status=status)
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact",),
    )
    assert result["status"] == status
    assert result["query_completion"] == "incomplete"


def test_service_limit_bounds_total_dependency_rows_and_new_query_gets_fresh_data():
    adapter = service_adapter()
    rows = []
    for number in range(10):
        service = record(
            f"school_service:{number}",
            f"服务{number}",
            organization_ref="organization:one",
            official_contact_ref="official_contact:one",
        )
        rows.append(service)
    adapter.responses[("school_service", None)] = view("school_service", *rows)
    adapter.responses[("organization_campus_relationship", "organization:one")] = view(
        "organization_campus_relationship",
        *[
            record(
                f"relationship:{n}",
                "关联",
                organization_ref="organization:one",
                campus_ref=f"location:{n}",
            )
            for n in range(10)
        ],
    )
    for number in range(8):
        adapter.responses[("location", f"location:{number}")] = view(
            "location", record(f"location:{number}", f"校区{number}", kind="campus")
        )
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact", "organization_campus_relationship"),
    )
    assert result["status"] == "partial"
    assert len(result["service_groups"]) == 8
    for kind in {v["fact_kind"] for v in result["results"]}:
        assert sum(len(v["records"]) for v in result["results"] if v["fact_kind"] == kind) <= 8
        assert sum(p.fact_kind == kind for p in adapter.calls) <= 8
    assert len(adapter.calls) == 12
    adapter.responses[("official_contact", "official_contact:one")]["records"][0]["fields"][
        "contact_value"
    ] = "021-00000002"
    changed = execute(
        adapter, subject_kind="school_service", query_text="资料服务", include=("official_contact",)
    )
    assert (
        next(v for v in changed["results"] if v["fact_kind"] == "official_contact")["records"][0][
            "fields"
        ]["contact_value"]
        == "021-00000002"
    )


@pytest.mark.parametrize("status", ["no_current_records", "formal_negative"])
def test_service_empty_provider_relationship_keeps_bounded_evidence_not_service_exclusion(status):
    adapter = service_adapter()
    adapter.responses[("organization_campus_relationship", "organization:one")] = view(
        "organization_campus_relationship", status=status
    )
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("organization_campus_relationship",),
    )
    group = result["service_groups"][0]
    assert group["provider_campuses"] == {
        "status": status,
        "relation_references": [],
        "campuses": [],
    }
    assert group["relationship_scope"] == "provider_affiliation_only"
    assert result["status"] == "known"
    assert len(adapter.calls) == 3


def test_service_empty_authorized_list_is_completed_without_reading_unlinked_objects():
    adapter = service_adapter()
    adapter.responses[("school_service", None)] = view("school_service")
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact",),
    )
    assert result["status"] == "no_current_records"
    assert result["query_completion"] == "completed"
    assert result["service_groups"] == []
    assert len(adapter.calls) == 1


def test_service_quality_failure_preserved_and_channel_ref_does_not_authorize_any_owner_list():
    adapter = service_adapter()
    adapter.responses[("official_contact", "official_contact:one")]["quality"] = {
        "evidence_status": "source_unavailable"
    }
    result = execute(
        adapter, subject_kind="school_service", query_text="资料服务", include=("official_contact",)
    )
    assert result["status"] == "partial"
    assert result["query_completion"] == "incomplete"
    assert result["results"][1]["quality"]["evidence_status"] == "source_unavailable"
    assert adapter.calls[1].reference == "official_contact:one"
    assert adapter.calls[1].related_ref is None


def test_service_bundle_global_limit_stops_before_query_without_dropping_checked_results():
    adapter = ServiceAdapter({})
    services = []
    for number in range(8):
        organization = f"organization:{number}"
        contact = f"official_contact:{number}"
        campus = f"location:{number}"
        services.append(
            record(
                f"school_service:{number}",
                f"服务{number}",
                organization_ref=organization,
                official_contact_ref=contact,
            )
        )
        adapter.responses[("official_contact", contact)] = view(
            "official_contact", record(contact, f"渠道{number}", contact_value=f"0{number}")
        )
        adapter.responses[("organization", organization)] = view(
            "organization", record(organization, f"组织{number}")
        )
        adapter.responses[("organization_campus_relationship", organization)] = view(
            "organization_campus_relationship",
            record(
                f"relationship:{number}",
                "正式关系",
                organization_ref=organization,
                campus_ref=campus,
            ),
        )
        adapter.responses[("location", campus)] = view(
            "location", record(campus, f"校区{number}", kind="campus")
        )
    adapter.responses[("school_service", None)] = view("school_service", *services)
    result = execute(
        adapter,
        subject_kind="school_service",
        scope="all_authorized",
        include=("official_contact", "organization_campus_relationship"),
    )
    assert len(result["results"]) <= 32
    assert len(adapter.calls) == 32
    assert len(result["results"]) == len(adapter.calls)
    assert all(v["records"] for v in result["results"])
    assert result["status"] == "partial"
    assert result["service_groups"][-1]["status"] == "partial"
    assert result["service_groups"][-1]["provider_campuses"]["campuses"] == [
        {"reference": "location:7", "status": "partial"}
    ]
    assert not any(p.fact_kind == "location" and p.reference == "location:7" for p in adapter.calls)
    assert all(p.scope == "selected" for p in adapter.calls[1:])


def two_target_adapter(*, different_provider=False):
    adapter = service_adapter()
    first = adapter.responses[("school_service", "资料服务")]["records"][0]
    relations = adapter.responses[("organization_campus_relationship", "organization:one")]
    relations["records"] = relations["records"][:1]
    relations["matched_count"] = 1
    second = deepcopy(first)
    second["reference"] = "school_service:second"
    second["label"] = "另项服务"
    second["fields"].pop("official_contact_ref")
    if different_provider:
        second["fields"]["organization_ref"] = "organization:other"
        adapter.responses[("organization", "organization:other")] = view(
            "organization", record("organization:other", "另一个组")
        )
        adapter.responses[("organization_campus_relationship", "organization:other")] = view(
            "organization_campus_relationship",
            record(
                "relationship:other",
                "另条关系",
                organization_ref="organization:other",
                campus_ref="location:other",
            ),
        )
        adapter.responses[("location", "location:other")] = view(
            "location", record("location:other", "另一校区", kind="campus")
        )
    adapter.responses[("school_service", "另项服务")] = view("school_service", second)
    adapter.responses[("school_service", "school_service:second")] = view("school_service", second)
    return adapter


@pytest.mark.parametrize("different_provider,expected", [(False, 6), (True, 9)])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_selected_service_targets_share_only_equal_dependencies(
    different_provider, expected, reversed_order
):
    adapter = two_target_adapter(different_provider=different_provider)
    targets = [{"query_text": "资料服务"}, {"reference": "school_service:second"}]
    if reversed_order:
        targets.reverse()
    result = execute(
        adapter,
        subject_kind="school_service",
        targets=tuple(targets),
        include=("official_contact", "organization_campus_relationship"),
    )
    assert len(adapter.calls) == expected
    assert all(call.scope == "selected" for call in adapter.calls)
    assert [item["selection"] for item in result["target_results"]] == targets
    expected_refs = ["school_service:one", "school_service:second"]
    if reversed_order:
        expected_refs.reverse()
    assert [g["service_reference"] for g in result["service_groups"]] == expected_refs
    assert len(result["results"]) == expected
    assert all(g["provider_campuses"]["status"] == "known" for g in result["service_groups"])


@pytest.mark.parametrize(
    "unresolved", ["ambiguous", "no_current_records", "denied", "source_unavailable"]
)
def test_selected_targets_preserve_each_resolution_without_using_ambiguous_first(unresolved):
    adapter = two_target_adapter()
    if unresolved == "ambiguous":
        extra = deepcopy(adapter.responses[("school_service", "另项服务")]["records"][0])
        extra["reference"] = "school_service:third"
        adapter.responses[("school_service", "另项服务")]["records"].append(extra)
    else:
        adapter.responses[("school_service", "另项服务")] = view(
            "school_service", status=unresolved
        )
    result = execute(
        adapter,
        subject_kind="school_service",
        targets=({"query_text": "资料服务"}, {"query_text": "另项服务"}),
        include=("official_contact", "organization_campus_relationship"),
    )
    assert result["status"] == "partial"
    assert result["query_completion"] == (
        "completed" if unresolved == "no_current_records" else "incomplete"
    )
    assert result["target_results"][0]["status"] == "known"
    assert result["target_results"][1]["status"] == unresolved
    assert [g["service_reference"] for g in result["service_groups"]] == ["school_service:one"]
    if unresolved == "ambiguous":
        assert len(result["target_results"][1]["candidate_references"]) == 2
    assert len(adapter.calls) == 6


@pytest.mark.parametrize(
    "extra",
    [
        {"query_text": "资料服务"},
        {"reference": "school_service:one"},
        {"scope": "all_authorized"},
        {"subject_kind": "organization"},
        {"targets": []},
        {"targets": [{"query_text": "资料服务"}] * 5},
        {"targets": [{"query_text": "资料服务", "reference": "school_service:one"}]},
        {"targets": [{}]},
    ],
)
def test_selected_service_target_contract_is_bounded_and_exclusive(extra):
    import json

    Input, _ = api()
    with pytest.raises(ValidationError):
        Input.model_validate_json(
            json.dumps(
                {
                    "subject_kind": "school_service",
                    "targets": [{"query_text": "资料服务"}],
                    "include": ["official_contact"],
                    **extra,
                }
            )
        )


def test_selected_targets_reduce_same_provider_split_reads_from_nine_to_six():
    adapter = two_target_adapter()
    include = ("official_contact", "organization_campus_relationship")
    for name in ("资料服务", "另项服务"):
        execute(adapter, subject_kind="school_service", query_text=name, include=include)
    assert len(adapter.calls) == 9
    adapter.calls.clear()
    result = execute(
        adapter,
        subject_kind="school_service",
        targets=({"query_text": "资料服务"}, {"query_text": "另项服务"}),
        include=include,
    )
    assert len(adapter.calls) == 6
    assert len(result["service_groups"]) == 2


@pytest.mark.parametrize("missing", ["official_contact_ref", "organization_ref"])
def test_service_missing_reference_completes_read_without_claiming_complete_facts(missing):
    adapter = service_adapter()
    adapter.responses[("school_service", "资料服务")]["records"][0]["fields"].pop(missing)
    result = execute(
        adapter,
        subject_kind="school_service",
        query_text="资料服务",
        include=("official_contact", "organization_campus_relationship"),
    )
    assert result["query_completion"] == "completed"
    assert result["status"] == "partial"
    component = "official_contact" if missing == "official_contact_ref" else "provider"
    assert result["service_groups"][0][component]["status"] == "reference_not_provided"


@pytest.mark.parametrize("failure", ["denied", "source_unavailable", "truncated", "quality_failed"])
def test_service_actual_read_failures_still_mean_incomplete_process(failure):
    adapter = service_adapter()
    contact = adapter.responses[("official_contact", "official_contact:one")]
    if failure == "truncated":
        contact["truncated"] = True
    elif failure == "quality_failed":
        contact["quality"] = {"evidence_status": "source_unavailable"}
    else:
        adapter.responses[("official_contact", "official_contact:one")] = view(
            "official_contact", status=failure
        )
    result = execute(
        adapter, subject_kind="school_service", query_text="资料服务", include=("official_contact",)
    )
    assert result["query_completion"] == "incomplete"
    assert result["status"] == "partial"


@pytest.mark.parametrize("selection", [{"query_text": "苏老师"}, {"reference": "person:su"}])
def test_person_assignment_lookup_returns_all_authorized_direct_candidates(selection):
    assignments = view(
        "assignment",
        record("assignment:math", "数学教师", person_ref="person:su"),
        record("assignment:research", "教研组成员", person_ref="person:su"),
        source={"label": "正式发布"},
    )
    adapter = Adapter(
        {
            ("person", next(iter(selection.values()))): view(
                "person", record("person:su", "苏老师")
            ),
            ("assignment", "person:su"): assignments,
        }
    )
    when = datetime(2027, 3, 2, tzinfo=UTC)
    result = execute(
        adapter, subject_kind="person", include=("assignment",), as_of=when, **selection
    )
    assert result["status"] == "known"
    assert result["subject_reference"] == "person:su"
    assert result["requested_fact_kinds"] == ["assignment"]
    assert result["query_completion"] == "completed"
    assert result["results"][-1] == assignments
    assert [(p.fact_kind, p.related_ref) for p in adapter.calls] == [
        ("person", None),
        ("assignment", "person:su"),
    ]
    assert all(p.as_of == when and p.scope == "selected" for p in adapter.calls)


@pytest.mark.parametrize(
    "incomplete", [{"truncated": True}, {"has_more": True}, {"next_cursor": "next"}]
)
def test_person_assignment_lookup_never_selects_first_page_as_unique(incomplete):
    adapter = Adapter(
        {("person", "苏老师"): view("person", record("person:su", "苏老师"), **incomplete)}
    )
    result = execute(adapter, subject_kind="person", query_text="苏老师", include=("assignment",))
    assert result["status"] == "partial"
    assert result["query_completion"] == "subject_unresolved"
    assert len(adapter.calls) == 1
    assert result["results"][0].items() >= incomplete.items()


def test_person_assignment_lookup_ambiguity_does_not_read_either_persons_assignments():
    people = view("person", record("person:one", "同名老师"), record("person:two", "同名老师"))
    adapter = Adapter({("person", "同名老师"): people})
    result = execute(adapter, subject_kind="person", query_text="同名老师", include=("assignment",))
    assert result["status"] == "ambiguous"
    assert result["candidates"] == people["records"]
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "status", ["denied", "source_unavailable", "no_current_records", "formal_negative"]
)
def test_person_assignment_lookup_preserves_component_failure_and_absence(status):
    adapter = Adapter(
        {
            ("person", "苏老师"): view("person", record("person:su", "苏老师")),
            ("assignment", "person:su"): view("assignment", status=status),
        }
    )
    result = execute(adapter, subject_kind="person", query_text="苏老师", include=("assignment",))
    assert result["status"] == status
    assert result["results"][-1]["status"] == status
    assert result["results"][-1]["coverage"] == {"status": "unknown"}
    assert result["query_completion"] == (
        "completed" if status in {"no_current_records", "formal_negative"} else "incomplete"
    )


def test_person_assignment_lookup_preserves_assignment_page_and_continuation():
    adapter = Adapter(
        {
            ("person", "苏老师"): view("person", record("person:su", "苏老师")),
            ("assignment", "person:su"): view(
                "assignment",
                record("assignment:one", "教师"),
                truncated=True,
                next_cursor="original-page-token",
            ),
        }
    )
    result = execute(adapter, subject_kind="person", query_text="苏老师", include=("assignment",))
    assert result["status"] == "partial"
    assert result["query_completion"] == "incomplete"
    assert result["results"][-1]["next_cursor"] == "original-page-token"


@pytest.mark.parametrize(
    "fields",
    [
        {"scope": "all_authorized", "query_text": None},
        {"campus_name": "甲校区"},
        {"include": ("person_contact",)},
        {"include": ("official_contact",)},
        {"include": ("assignment", "official_contact")},
        {"subject_kind": "organization"},
        {"subject_kind": "location"},
    ],
)
def test_person_assignment_lookup_rejects_unbounded_or_unrelated_requests(fields):
    Input, _ = api()
    with pytest.raises(ValidationError):
        Input(
            **{
                "subject_kind": "person",
                "query_text": "苏老师",
                "include": ("assignment",),
                **fields,
            }
        )


@pytest.mark.parametrize("count", [9, 20])
def test_person_lookup_preserves_complete_authorized_name_candidates(count):
    people = [record(f"person:same{index}", "同名老师") for index in range(count)]
    adapter = Adapter({("person", "同名老师"): view("person", *people)})
    result = execute(adapter, subject_kind="person", query_text="同名老师", include=("assignment",))
    assert result["status"] == "ambiguous"
    assert result["candidates"] == people
    assert result["results"][0]["records"] == people
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("count", [9, 20])
def test_person_lookup_complete_assignment_page_is_not_limited_to_eight(count):
    assignments = [
        record(f"assignment:role{index}", f"岗位{index}", person_ref="person:su")
        for index in range(count)
    ]
    adapter = Adapter(
        {
            ("person", "苏老师"): view("person", record("person:su", "苏老师")),
            ("assignment", "person:su"): view("assignment", *assignments),
        }
    )
    result = execute(adapter, subject_kind="person", query_text="苏老师", include=("assignment",))
    assert result["status"] == "known"
    assert result["query_completion"] == "completed"
    assert result["results"][-1]["records"] == assignments
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("paged_kind", ["person", "assignment"])
def test_person_lookup_twenty_one_matches_require_actual_continuation(paged_kind):
    paged = view(
        paged_kind,
        *(
            record(f"{paged_kind}:item{index}", "同名老师" if paged_kind == "person" else "教师")
            for index in range(20)
        ),
        matched_count=21,
        truncated=True,
        next_cursor="next-authorized-page",
    )
    adapter = Adapter(
        {
            ("person", "苏老师"): paged
            if paged_kind == "person"
            else view("person", record("person:su", "苏老师")),
            ("assignment", "person:su"): paged,
        }
    )
    result = execute(adapter, subject_kind="person", query_text="苏老师", include=("assignment",))
    assert result["status"] == "partial"
    assert result["results"][-1]["matched_count"] == 21
    assert result["results"][-1]["next_cursor"] == "next-authorized-page"
    assert len(result["results"][-1]["records"]) == 20
    if paged_kind == "person":
        assert len(result["candidates"]) == 20
        assert len(adapter.calls) == 1
    else:
        assert len(adapter.calls) == 2
        assert result["query_completion"] == "incomplete"
