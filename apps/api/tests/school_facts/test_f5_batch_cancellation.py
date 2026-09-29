"""A cancellation batch composes every proven cutoff on a shared dependent."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_education_product import EducationBatchInput
from tests.school_facts.test_f4_education_maintenance import environment
from tests.school_facts.test_f5_exact_cancellation import at


def content(offering, assignment="teacher"):
    return {
        "offering_source_key": offering,
        "assignment_source_key": assignment,
        "teaching_role": "lead",
    }


def batch_environment():
    records = []
    for key, start, end, offering in (
        ("source-x", 10, 15, "offering-x"),
        ("source-y", 25, 28, "offering-y"),
    ):
        records.append(
            dict(
                fact_kind="teaching_assignment",
                source_key=key,
                version=1,
                status="available",
                latest_operation="upsert",
                prior_segments=[],
                segments=[
                    dict(
                        effective_at=at(start),
                        valid_to=at(end),
                        content=content(offering, "replacement"),
                    )
                ],
            )
        )
    segments = []
    for start, cutoff, original_end, cause, offering in (
        (1, 10, 15, "source-x", "offering-x"),
        (20, 25, 28, "source-y", "offering-y"),
    ):
        segments.append(
            dict(
                effective_at=at(start),
                valid_to=at(cutoff),
                content=content(offering),
                cause_source_key=cause,
                cause_version=1,
                causal_prior=dict(
                    effective_at=at(start),
                    valid_to=at(original_end),
                    content=content(offering),
                    ended_target_version=3,
                ),
            )
        )
    segments.append(
        dict(
            effective_at=at(29),
            valid_to=at(30),
            content=content("offering-z"),
            cause_source_key="independent",
            cause_version=4,
            causal_prior=None,
        )
    )
    records.append(
        dict(
            fact_kind="teaching_assignment",
            source_key="dependent",
            version=3,
            status="available",
            latest_operation="correct",
            prior_segments=[],
            segments=segments,
        )
    )
    coordinator, actor = environment(records)
    coordinator.repository.clock = lambda: at(2)
    original_read = coordinator.repository.read_records
    revalidated = []

    def read(*args, **kwargs):
        if kwargs.get("cancellation_start"):
            key = kwargs["source_keys"][0]
            row = next(r for r in records if r["source_key"] == key)
            proof = dict(
                source_key=key,
                fact_kind=row["fact_kind"],
                version=row["version"],
                selected_start=kwargs["cancellation_start"],
                introduction_version=1,
                publication_reference="publication:" + key,
                introduced_content=row["segments"][0]["content"],
                prior_segments=[],
            )
            return SimpleNamespace(
                records=[proof],
                school_fact_version=3,
                proof=SimpleNamespace(revalidate=lambda **kw: revalidated.append(key)),
            )
        return original_read(*args, **kwargs)

    coordinator.repository.read_records = read
    return coordinator, actor, records, revalidated


def cancellation(key, day):
    return dict(
        fact_kind="teaching_assignment",
        target_ref="f4:teaching_assignment:" + key,
        operation="cancel",
        fields={},
        reason="撤销已选未来替代",
        cancel_from=at(day).isoformat(),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_restores_both_causes_once_and_keeps_independent_segment(reverse):
    coordinator, actor, _records, revalidated = batch_environment()
    items = [cancellation("source-x", 10), cancellation("source-y", 25)]
    if reverse:
        items.reverse()
    payload = EducationBatchInput.model_validate_json(
        json.dumps({**items[0], "additional_changes": items[1:]})
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=at(2)
    )
    changes = {str(c.source_key): c for c in result.change_set.changes}
    assert set(changes) == {"source-x", "source-y", "dependent"}
    assert len(result.change_set.changes) == 3
    dependent = changes["dependent"]
    assert [(s.effective_at, s.valid_to) for s in dependent.replacement_segments] == [
        (at(1), at(15)),
        (at(20), at(28)),
        (at(29), at(30)),
    ]
    assert (dependent.cause_source_key, dependent.cause_version) == (None, None)
    assert [(s.cause_source_key, s.cause_version) for s in dependent.replacement_segments] == [
        (None, None),
        (None, None),
        ("independent", 4),
    ]
    assert dependent.expected_version == 3
    assert set(revalidated) == {"source-x", "source-y"}
    assert len(result.summary.impacts) == 3


def test_single_cause_cancellation_preserves_existing_causal_contract():
    coordinator, actor, _records, _revalidated = batch_environment()
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput.model_validate_json(json.dumps(cancellation("source-x", 10))),
        request_id=uuid4(),
        effective_at=at(2),
    )
    dependent = next(c for c in result.change_set.changes if c.source_key == "dependent")
    assert (dependent.cause_source_key, dependent.cause_version) == ("source-x", 2)
    assert [s.valid_to for s in dependent.replacement_segments] == [at(15), at(25), at(30)]


def test_explicit_dependent_disposition_is_not_overridden_by_automatic_restoration():
    coordinator, actor, _records, _revalidated = batch_environment()
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                **cancellation("source-x", 10),
                "additional_changes": [
                    cancellation("source-y", 25),
                    dict(
                        fact_kind="teaching_assignment",
                        target_ref="f4:teaching_assignment:dependent",
                        operation="end",
                        fields={},
                        reason="明确结束原任教当前段",
                    ),
                ],
            }
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=at(2)
    )
    dependent = next(c for c in result.change_set.changes if c.source_key == "dependent")
    assert len(result.change_set.changes) == 3
    assert [s.valid_to for s in dependent.replacement_segments] == [at(2), at(25), at(30)]


def test_combined_restoration_does_not_manufacture_future_cutoff_evidence():
    from app.composition.school_fact_education_maintenance import snapshot_at
    from app.domains.students.teaching_change_planner import cancel_teaching_dependents

    coordinator, actor, records, _revalidated = batch_environment()
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {**cancellation("source-x", 10), "additional_changes": [cancellation("source-y", 25)]}
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=at(2)
    )
    dependent = next(c for c in result.change_set.changes if c.source_key == "dependent")
    row = next(r for r in records if r["source_key"] == "dependent")
    restored = snapshot_at(
        {
            **row,
            "version": 4,
            "segments": [s.model_dump(mode="python") for s in dependent.replacement_segments],
        },
        kind="teaching_assignment",
        at=at(2),
    )
    assert all(s.provenance is None for s in restored.segments)
    assert (
        cancel_teaching_dependents(
            {"source_key": "source-x"},
            (restored,),
            proof={"introduction_version": 1},
            explicit_keys=frozenset(),
        )
        == ()
    )
