from datetime import UTC, datetime, timedelta
from importlib import import_module, util

import pytest

NOW = datetime(2026, 9, 6, tzinfo=UTC)
OWNER = "f2:organization:" + "a" * 64
OTHER = "f2:organization:" + "b" * 64
TARGET = "f2:organization_location:" + "c" * 64


def api():
    name = "app.platform.school_facts.f2_quality"
    assert util.find_spec(name) is not None, "F2 query must consume authorized quality evidence"
    return import_module(name)


def fact(ref=TARGET, purpose="office"):
    return api().QualityFact(
        ref,
        "organization_location",
        {
            "organization_ref": OWNER,
            "purpose": purpose,
        },
        NOW - timedelta(days=10),
        None,
    )


def statement(kind, **fields):
    valid_from = fields.pop("valid_from", NOW - timedelta(days=1))
    valid_to = fields.pop("valid_to", None)
    return api().QualityFact(
        "f2:fact_quality:" + kind,
        "fact_quality",
        {
            "quality_kind": kind,
            "target_fact_kind": "organization_location",
            "owner_kind": "organization",
            "organization_ref": OWNER,
            "verified_at": NOW - timedelta(days=1),
            "responsibility": "正式维护者",
            "basis_reference": "正式核实依据",
            **fields,
        },
        valid_from,
        valid_to,
    )


def assess(
    *statements,
    records=(),
    related_ref=OWNER,
    reference=None,
    as_of=NOW,
    truncated=False,
    status="known",
    name=None,
):
    return api().evaluate_quality(
        fact_kind="organization_location",
        reference=reference,
        related_ref=related_ref,
        name=name,
        factual_at=as_of,
        records=records,
        statements=statements,
        query_status=status,
        evidence_status="known",
        evidence_truncated=truncated,
    )


def test_empty_results_are_unknown_and_legal_multiple_offices_are_not_conflict():
    empty = assess(status="no_current_records")
    assert empty["existence"] == "unknown"
    assert empty["coverage"] == "unknown"
    multiple = assess(records=(fact(), fact(TARGET + "2")))
    assert multiple["existence"] == "known"
    assert multiple["conflict"] == "unknown"


def test_owner_scoped_null_target_negative_is_consumed_without_global_absence():
    result = assess(
        statement("negative", negative_code="no_office", purpose="office"),
        status="no_current_records",
    )
    assert result["existence"] == "unknown"
    assert result["statements"][0]["negative_code"] == "no_office"
    assert result["statements"][0]["purpose"] == "office"
    assert result["statements"][0]["owner_ref"] == OWNER
    assert result["statements"][0]["applies_to_query"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"organization_ref": OTHER},
        {"target_fact_kind": "person_workplace"},
        {"target_ref": TARGET + "other"},
        {"valid_from": NOW + timedelta(seconds=1)},
        {"valid_to": NOW},
        {"purpose": "reception"},
    ],
)
def test_statement_cannot_cross_owner_target_family_time_or_purpose(change):
    result = assess(
        statement(
            "source",
            target_ref=TARGET,
            source_status="unavailable",
            **{k: v for k, v in change.items() if k != "target_ref"},
        )
        if "target_ref" not in change
        else statement("source", source_status="unavailable", **change),
        records=(fact(),),
        reference=TARGET,
        related_ref=None,
    )
    assert result["statements"] == []
    assert result["source_availability"] == "unknown"


def test_source_outage_staleness_dispute_and_correction_remain_composable():
    rows = (
        statement("source", target_ref=TARGET, source_status="unavailable"),
        statement("review", target_ref=TARGET, review_due_at=NOW),
        statement("dispute", target_ref=TARGET, dispute_status="open", evidence_reference="核实单"),
        statement(
            "correction",
            target_ref=TARGET,
            replacement_ref=TARGET + "replacement",
            evidence_reference="纠正单",
        ),
    )
    result = assess(*rows, records=(fact(),), reference=TARGET, related_ref=None)
    assert result["existence"] == "known"
    assert result["source_availability"] == "unavailable"
    assert result["freshness"] == "review_overdue"
    assert result["conflict"] == "open"
    assert {s["kind"] for s in result["statements"]} == {
        "source",
        "review",
        "dispute",
        "correction",
    }


def test_complete_coverage_does_not_prove_partial_or_unqueried_collection():
    coverage = statement("coverage", target_ref=TARGET, coverage_status="complete")
    assert assess(coverage, records=(fact(),))["coverage"] == "unknown"
    assert (
        assess(coverage, records=(fact(),), reference=TARGET, related_ref=None)["coverage"]
        == "complete"
    )
    assert (
        assess(coverage, records=(fact(),), reference=TARGET, related_ref=None, truncated=True)[
            "coverage"
        ]
        == "unknown"
    )
    assert (
        assess(statement("coverage", coverage_status="complete"), records=(fact(),))["coverage"]
        == "unknown"
    )


def test_conflicting_declarations_are_not_arbitrarily_overwritten():
    result = assess(
        statement("source", target_ref=TARGET, source_status="available"),
        statement("source", target_ref=TARGET, source_status="unavailable"),
        records=(fact(),),
        reference=TARGET,
        related_ref=None,
    )
    assert result["source_availability"] == "conflicting_statements"


def test_negative_versus_positive_fact_marks_conflict_only_for_exact_dimension():
    negative = statement("negative", negative_code="no_office", purpose="office")
    assert assess(negative, records=(fact(),))["conflict"] == "contradictory_evidence"
    assert assess(negative, records=(fact(purpose="reception"),))["conflict"] == "unknown"


def test_selection_or_denied_query_never_consumes_quality():
    for status in ("forbidden", "ambiguous", "selection_required", "source_unavailable"):
        result = assess(statement("source", source_status="unavailable"), status=status)
        assert result["statements"] == []
        assert result["existence"] == "unknown"


def test_name_only_empty_query_does_not_apply_owner_statement():
    result = assess(
        statement("negative", negative_code="no_office", purpose="office"),
        related_ref=None,
        name="不存在的名字",
        status="no_current_records",
    )
    assert result["statements"] == []


def adapter_setup(*, empty=False, deny_quality=False, deny_fact=False, outage=False):
    from uuid import UUID

    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.f2_reader_contracts import F2FactRow
    from app.platform.authorization.secure_reader_contracts import SecureReadDenied, SecureReadPage
    from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
    from tests.test_f2_secure_reader import _guard, actor

    calls = []
    guard, _, _ = _guard({"f2_organization_reader": ("owner.private",)})

    def row(kind, key, fields):
        return F2FactRow(
            UUID(int=31),
            kind,
            key,
            fields,
            1,
            2,
            NOW - timedelta(days=2),
            None,
            "formal_publication",
            UUID(int=32),
            NOW,
        )

    class Reader:
        def read(self, request):
            calls.append(request)
            if request.operation_code == "school_facts.f2.fact_quality.read":
                if deny_quality:
                    raise SecureReadDenied("not permitted")
                if outage:
                    raise SchoolFactAuthorizationUnavailable("outage")
                quality = row(
                    "fact_quality",
                    "quality.private",
                    {
                        "quality_kind": "negative" if empty else "source",
                        "target_fact_kind": "organization_location",
                        "owner_kind": "organization",
                        "organization_source_key": "owner.private",
                        "verified_at": NOW - timedelta(days=1),
                        **(
                            {"negative_code": "no_office", "purpose": "office"}
                            if empty
                            else {
                                "target_source_key": "office.private",
                                "source_status": "unavailable",
                            }
                        ),
                    },
                )
                return SecureReadPage((quality,), 1, NOW, None)
            if deny_fact:
                raise SecureReadDenied("not permitted")
            records = (
                ()
                if empty
                else (
                    row(
                        "organization_location",
                        "office.private",
                        {
                            "organization_source_key": "owner.private",
                            "purpose": "office",
                        },
                    ),
                )
            )
            return SecureReadPage(records, len(records), NOW, None)

    repository = F2FactQueryRepository(
        reader=Reader(),
        quality_readers={"organization_location": Reader()},
        reference_key=b"private-test-key-only",
        maintenance_guard=guard,
        clock=lambda: NOW,
    )
    reference = repository.reference_for(
        actor=actor(), fact_kind="organization", source_key="owner.private"
    )
    return DirectoryProductAdapter(repository), actor(), reference, calls


def test_adapter_uses_authorized_quality_at_same_fact_time_and_only_public_references():
    import json

    from app.composition.school_fact_directory_product import FactQueryInput

    adapter, actor, related, calls = adapter_setup()
    result = adapter.query(
        actor=actor,
        payload=FactQueryInput(fact_kind="organization_location", related_ref=related, as_of=NOW),
    )
    assert "quality" in result, "adapter currently discards formal quality evidence"
    assert result["quality"]["source_availability"] == "unavailable"
    assert result["status"] == "known" and len(result["records"]) == 1
    assert calls[-1].operation_code == "school_facts.f2.fact_quality.read"
    assert calls[-1].factual_at == NOW
    encoded = json.dumps(result)
    assert "source_key" not in encoded
    assert not any(key in encoded for key in ("quality.private", "owner.private", "office.private"))


def test_adapter_consumes_empty_owner_negative():
    from app.composition.school_fact_directory_product import FactQueryInput

    adapter, actor, related, _ = adapter_setup(empty=True)
    result = adapter.query(
        actor=actor, payload=FactQueryInput(fact_kind="organization_location", related_ref=related)
    )
    assert "quality" in result
    assert result["quality"]["statements"][0]["negative_code"] == "no_office"
    assert result["quality"]["existence"] == "unknown"
    assert result["status"] == "no_current_records"


@pytest.mark.parametrize(
    "flags, expected",
    [
        ({"deny_quality": True}, "forbidden"),
        ({"outage": True}, "source_unavailable"),
    ],
)
def test_quality_denial_or_outage_retains_facts_without_quality_inference(flags, expected):
    from app.composition.school_fact_directory_product import FactQueryInput

    adapter, actor, related, _ = adapter_setup(**flags)
    result = adapter.query(
        actor=actor, payload=FactQueryInput(fact_kind="organization_location", related_ref=related)
    )
    assert "quality" in result
    assert result["status"] == "known" and len(result["records"]) == 1
    assert result["quality"]["evidence_status"] == expected
    assert result["quality"]["statements"] == []
    assert result["quality"]["source_availability"] == "unknown"


def test_denied_fact_query_does_not_read_quality_or_crash_on_missing_fact_time():
    from app.composition.school_fact_directory_product import FactQueryInput

    adapter, actor, related, calls = adapter_setup(deny_fact=True)
    result = adapter.query(
        actor=actor, payload=FactQueryInput(fact_kind="organization_location", related_ref=related)
    )
    assert result["status"] == "denied"
    assert len(calls) == 1
    assert result["quality"]["statements"] == []


def test_whole_owner_service_negative_is_distinct_from_unknown():
    result = api().evaluate_quality(
        fact_kind="school_service",
        reference=None,
        related_ref=OWNER,
        name=None,
        factual_at=NOW,
        records=(),
        statements=(
            statement("negative", target_fact_kind="school_service", negative_code="no_service"),
        ),
        query_status="no_current_records",
        evidence_status="known",
        evidence_truncated=False,
    )
    assert result["existence"] == "formal_negative"
    assert result["coverage"] == "unknown"


def test_review_deadline_and_half_open_statement_validity_follow_fact_time():
    row = statement(
        "review",
        target_ref=TARGET,
        review_due_at=NOW + timedelta(days=1),
        valid_to=NOW + timedelta(days=3),
    )
    assert (
        assess(row, records=(fact(),), reference=TARGET, related_ref=None)["freshness"]
        == "within_review_period"
    )
    assert (
        assess(
            row,
            records=(fact(),),
            reference=TARGET,
            related_ref=None,
            as_of=NOW + timedelta(days=1),
        )["freshness"]
        == "review_overdue"
    )
    assert (
        assess(
            row,
            records=(fact(),),
            reference=TARGET,
            related_ref=None,
            as_of=NOW + timedelta(days=3),
        )["freshness"]
        == "unknown"
    )


def test_old_last_success_without_formal_deadline_does_not_invent_staleness():
    result = assess(
        statement(
            "source",
            source_status="available",
            target_ref=TARGET,
            last_success_at=NOW - timedelta(days=500),
        ),
        records=(fact(),),
        reference=TARGET,
        related_ref=None,
    )
    assert result["freshness"] == "unknown"
    assert result["source_availability"] == "available"


def test_exact_target_statement_does_not_apply_to_an_unresolved_empty_target():
    result = assess(
        statement("negative", target_ref=TARGET, negative_code="no_office", purpose="office"),
        reference=TARGET,
        related_ref=None,
        status="no_current_records",
    )
    assert result["statements"] == []


def test_quality_denial_discards_even_supplied_rows():
    result = api().evaluate_quality(
        fact_kind="organization_location",
        reference=TARGET,
        related_ref=None,
        name=None,
        factual_at=NOW,
        records=(fact(),),
        statements=(statement("source", target_ref=TARGET, source_status="unavailable"),),
        query_status="known",
        evidence_status="forbidden",
        evidence_truncated=False,
    )
    assert result["statements"] == []
    assert result["source_availability"] == "unknown"


def test_official_channel_negative_never_denies_another_channel():
    owner = "f2:location:" + "d" * 64
    quality = api().QualityFact(
        "f2:fact_quality:email",
        "fact_quality",
        {
            "quality_kind": "negative",
            "target_fact_kind": "official_contact",
            "owner_kind": "location",
            "location_ref": owner,
            "verified_at": NOW,
            "negative_code": "no_official_channel",
            "contact_kind": "email",
        },
        NOW,
        None,
    )
    phone = api().QualityFact(
        "f2:official_contact:phone",
        "official_contact",
        {
            "owner_kind": "location",
            "location_ref": owner,
            "contact_kind": "phone",
        },
        NOW,
        None,
    )
    result = api().evaluate_quality(
        fact_kind="official_contact",
        reference=None,
        related_ref=owner,
        name=None,
        factual_at=NOW,
        records=(phone,),
        statements=(quality,),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )
    assert result["existence"] == "known"
    assert result["conflict"] == "unknown"
    assert result["statements"][0]["applies_to_records"] == []
    assert result["statements"][0]["contact_kind"] == "email"


@pytest.mark.parametrize("owner_kind", ["person", "location", "school"])
def test_exact_owner_roots_accept_their_own_quality(owner_kind):
    target = f"f2:{owner_kind}:" + "e" * 64
    fields = {
        "quality_kind": "source",
        "target_fact_kind": owner_kind,
        "target_ref": target,
        "owner_kind": owner_kind,
        "verified_at": NOW,
        "source_status": "available",
    }
    if owner_kind != "school":
        fields[owner_kind + "_ref"] = target
    result = api().evaluate_quality(
        fact_kind=owner_kind,
        reference=target,
        related_ref=None,
        name=None,
        factual_at=NOW,
        records=(api().QualityFact(target, owner_kind, {}, NOW, None),),
        statements=(api().QualityFact("f2:fact_quality:one", "fact_quality", fields, NOW, None),),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )
    assert result["source_availability"] == "available"
    assert result["statements"][0]["applies_to_query"] is True
