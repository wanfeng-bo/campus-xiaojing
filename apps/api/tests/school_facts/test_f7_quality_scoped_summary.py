"""A per-record quality claim must not become a claim about a whole list."""

from datetime import UTC, datetime, timedelta

from app.platform.school_facts.f2_quality import QualityFact, evaluate_quality

AT = datetime(2026, 9, 24, tzinfo=UTC)
OWNER = "f2:organization:" + "a" * 64


def record(suffix: str) -> QualityFact:
    return QualityFact(
        "f2:organization_location:" + suffix * 64,
        "organization_location",
        {"organization_ref": OWNER, "purpose": "office"},
        AT - timedelta(days=2),
        None,
    )


def claim(kind: str, target: str, **fields: object) -> QualityFact:
    return QualityFact(
        "f2:fact_quality:" + kind,
        "fact_quality",
        {
            "quality_kind": kind,
            "target_fact_kind": "organization_location",
            "owner_kind": "organization",
            "organization_ref": OWNER,
            "target_ref": target,
            "verified_at": AT - timedelta(hours=1),
            **fields,
        },
        AT - timedelta(hours=1),
        None,
    )


def assess(rows: tuple[QualityFact, ...], statements: tuple[QualityFact, ...]) -> dict:
    return evaluate_quality(
        fact_kind="organization_location",
        reference=None,
        related_ref=OWNER,
        name=None,
        factual_at=AT,
        records=rows,
        statements=statements,
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )


def test_one_record_source_and_review_claim_do_not_describe_a_two_record_list():
    first, second = record("b"), record("c")
    result = assess(
        (first, second),
        (
            claim("source", first.reference, source_status="unavailable"),
            claim("review", first.reference, review_due_at=AT),
        ),
    )

    assert result["source_availability"] == "unknown"
    assert result["freshness"] == "unknown"
    assert len(result["statements"]) == 2
    assert all(item["applies_to_query"] is False for item in result["statements"])
    assert all(item["applies_to_records"] == [first.reference] for item in result["statements"])


def test_exact_record_still_receives_its_own_source_and_review_claim():
    first = record("b")
    result = evaluate_quality(
        fact_kind="organization_location",
        reference=first.reference,
        related_ref=None,
        name=None,
        factual_at=AT,
        records=(first,),
        statements=(
            claim("source", first.reference, source_status="unavailable"),
            claim("review", first.reference, review_due_at=AT),
        ),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )

    assert result["source_availability"] == "unavailable"
    assert result["freshness"] == "review_overdue"


def test_single_returned_row_with_more_pages_cannot_describe_the_whole_page_set():
    first = record("b")
    result = evaluate_quality(
        fact_kind="organization_location",
        reference=None,
        related_ref=OWNER,
        name=None,
        factual_at=AT,
        records=(first,),
        statements=(claim("source", first.reference, source_status="unavailable"),),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
        records_truncated=True,
    )
    assert result["source_availability"] == "unknown"


def test_complete_coverage_requires_a_verified_population_in_f7():
    first = record("b")
    complete = claim("coverage", first.reference, coverage_status="complete")
    common = dict(
        fact_kind="organization_location",
        reference=first.reference,
        related_ref=None,
        name=None,
        factual_at=AT,
        records=(first,),
        statements=(complete,),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
    )
    assert (
        evaluate_quality(**common, verified_complete_claim_refs=frozenset())["coverage"]
        == "unknown"
    )
    assert evaluate_quality(
        **common, verified_complete_claim_refs=frozenset({complete.reference})
    )["coverage"] == "complete"


def test_verified_complete_owner_collection_is_not_borrowed_by_a_truncated_page():
    first = record("b")
    complete = claim("coverage", "", coverage_status="complete")
    complete = QualityFact(
        complete.reference,
        complete.fact_kind,
        {**complete.fields, "target_ref": None},
        complete.valid_from,
        complete.valid_to,
    )
    common = dict(
        fact_kind="organization_location",
        reference=None,
        related_ref=OWNER,
        name=None,
        factual_at=AT,
        records=(first,),
        statements=(complete,),
        query_status="known",
        evidence_status="known",
        evidence_truncated=False,
        verified_complete_claim_refs=frozenset({complete.reference}),
    )
    assert evaluate_quality(**common)["coverage"] == "complete"
    assert evaluate_quality(**common, records_truncated=True)["coverage"] == "unknown"
