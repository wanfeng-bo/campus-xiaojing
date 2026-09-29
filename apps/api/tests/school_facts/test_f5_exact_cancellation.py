"""Exact F5 future cancellation must retain unrelated slices and independent ends."""

from datetime import UTC, datetime

import pytest

from app.domains.students.education_change_planner import (
    EducationFactSnapshot,
    EducationTimelineSegment,
)


def at(day):
    return datetime(2027, 9, day, tzinfo=UTC)


def piece(start, end, name):
    return EducationTimelineSegment(
        effective_at=at(start), valid_to=at(end) if end else None, content={"display_name": name}
    )


def request():
    return dict(
        schema_version=3,
        fact_kind="teaching_course",
        source_key="subject",
        expected_version=3,
        operation="cancel",
        effective_at=at(10),
        content=None,
        correction_reason="取消选中的未来安排",
        origin={"source_kind": "manual"},
    )


def snapshot():
    return EducationFactSnapshot(
        fact_kind="teaching_course",
        source_key="subject",
        version=3,
        effective_at=at(1),
        content={"display_name": "A"},
        latest_operation="upsert",
        segments=(piece(1, 10, "A"), piece(10, 20, "B"), piece(20, None, "C")),
        prior_segments=(piece(1, 10, "A"), piece(10, None, "B")),
    )


def proof(end=None):
    return dict(
        source_key="subject",
        fact_kind="teaching_course",
        version=3,
        selected_start=at(10),
        introduction_version=2,
        publication_reference="publication:approved-boundary",
        introduced_content={"display_name": "B"},
        prior_segments=[
            dict(
                effective_at=at(1), valid_to=at(end) if end else None, content={"display_name": "A"}
            )
        ],
    )


@pytest.mark.parametrize("independent_end", [None, 15])
def test_cancel_middle_retains_later_arrangement_and_original_independent_end(independent_end):
    from app.domains.students.teaching_change_planner import cancel_teaching_future

    result = cancel_teaching_future(request(), snapshot(), proof=proof(independent_end), now=at(2))
    assert result["operation"] == "correct"
    rows = result["replacement_segments"]
    assert [(s["effective_at"], s["valid_to"], s["content"]["display_name"]) for s in rows] == [
        (at(1), at(independent_end or 20), "A"),
        (at(20), None, "C"),
    ]
    assert "publication:approved-boundary" in result["correction_reason"]


def test_cancel_requires_exact_future_and_current_root_version():
    from app.domains.students.teaching_change_planner import cancel_teaching_future

    for now, evidence in [
        (at(10), proof()),
        (at(2), {**proof(), "version": 2}),
        (at(2), {**proof(), "selected_start": at(20)}),
    ]:
        with pytest.raises(ValueError):
            cancel_teaching_future(request(), snapshot(), proof=evidence, now=now)


def test_formal_maintenance_selects_middle_boundary_using_trusted_current_clock(monkeypatch):
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment

    original = snapshot()
    row = dict(
        fact_kind=original.fact_kind,
        source_key=original.source_key,
        version=3,
        status="available",
        latest_operation="upsert",
        segments=[
            dict(effective_at=s.effective_at, valid_to=s.valid_to, content=s.content)
            for s in original.segments
        ],
        prior_segments=[],
    )
    coordinator, actor = environment([row])
    coordinator.repository.clock = lambda: at(2)
    read = coordinator.repository.read_records

    def scoped_read(*args, **kwargs):
        if kwargs.get("cancellation_start"):
            assert kwargs["cancellation_start"] == at(10)
            return SimpleNamespace(
                records=[proof()],
                school_fact_version=3,
                proof=SimpleNamespace(revalidate=lambda **kw: None),
            )
        return read(*args, **kwargs)

    coordinator.repository.read_records = scoped_read
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            dict(
                fact_kind="teaching_course",
                target_ref="f4:teaching_course:subject",
                operation="cancel",
                fields={},
                reason="取消未来安排",
                cancel_from=at(10).isoformat(),
                effective_at=at(10).isoformat(),
                purpose="cancel_future",
            )
        )
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=at(10)
    )
    from app.composition.school_fact_directory_product import DirectoryFactProductService
    from app.composition.school_facts import InMemorySchoolFactPlanStore
    from tests import test_f2_directory_product as directory_test

    monkeypatch.setattr(directory_test, "NOW", at(2))
    boundary = directory_test.Boundary()
    service = DirectoryFactProductService(
        boundary=boundary,
        resolve_change=coordinator.resolve_change,
        require_maintenance=lambda *a, **kw: None,
        require_receipt=lambda *a, **kw: None,
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: at(2)),
        clock=lambda: at(2),
    )
    proposal = service.prepare(
        actor=actor, session_binding="session-one", payload=payload, request_id=uuid4()
    )
    assert proposal.summary.effective_at == at(10)
    assert [name for name, _ in boundary.calls] == ["preflight", "prepare"]
    rows = result.change_set.changes[0].replacement_segments
    assert [(s.effective_at, s.valid_to, s.content.display_name) for s in rows] == [
        (at(1), at(20), "A"),
        (at(20), None, "C"),
    ]


def test_cancel_derived_cutoff_uses_original_cause_version_not_latest_root_version():
    from dataclasses import replace

    from app.domains.students.education_change_planner import EndProvenance
    from app.domains.students.teaching_change_planner import cancel_teaching_dependents

    old = EducationFactSnapshot(
        fact_kind="teaching_assignment",
        source_key="old",
        version=2,
        effective_at=at(1),
        content={"display_name": "old"},
        segments=(
            replace(
                piece(1, 10, "old"),
                provenance=EndProvenance(
                    cause_source_key="subject",
                    cause_version=2,
                    ended_target_version=2,
                    prior_effective_at=at(1),
                    prior_content={"display_name": "old", "valid_to": at(15)},
                ),
            ),
        ),
    )
    restored = cancel_teaching_dependents(
        request(), (old,), proof=proof(), explicit_keys=frozenset({"subject"})
    )
    assert len(restored) == 1
    assert restored[0]["replacement_segments"][0]["valid_to"] == at(15)
    with pytest.raises(ValueError):
        cancel_teaching_dependents(
            request(),
            (replace(old, version=3),),
            proof=proof(),
            explicit_keys=frozenset({"subject"}),
        )


def test_cancellation_reader_requires_both_duties_and_expires_at_selected_boundary():
    from types import SimpleNamespace

    from app.composition.school_fact_education_reader import EducationReadStale, EducationRepository
    from tests.school_facts.test_f4_education_reader import Authority, Projection, actor

    class TeachingAuthority(Authority):
        def require_scope(self, who, *, fact_kind, action, checked_at):
            self.checks.append((fact_kind, action))
            if action == "maintain" and not self.allowed:
                raise PermissionError("maintenance revoked")
            return SimpleNamespace(
                actor=who,
                action=action,
                scope=SimpleNamespace(
                    field_names=("display_name",), object_keys=("subject",), creation_keys=()
                ),
            )

    projection = Projection()
    authority = TeachingAuthority()
    clock = [at(2)]
    projection.records = [{**proof(), "next_valid_boundary": at(10).isoformat()}]
    repo = EducationRepository(
        projection=projection,
        authority=authority,
        reference_key=b"fixed-owned-proof-key",
        clock=lambda: clock[0],
    )

    def read():
        return repo.read_records(
            actor(),
            "teaching_course",
            source_keys=("subject",),
            fields=("display_name",),
            as_of=at(2),
            cancellation_start=at(10),
            expected_version=3,
        )

    result = read()
    assert authority.checks == [("teaching_course", "read"), ("teaching_course", "maintain")]
    clock[0] = at(10)
    with pytest.raises(EducationReadStale):
        result.proof.revalidate(actor=actor())
    clock[0] = at(2)
    authority.allowed = False
    before = len(projection.calls)
    with pytest.raises(PermissionError):
        read()
    assert len(projection.calls) == before


def test_each_batch_cancellation_has_its_own_exact_boundary():
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment

    rows = []
    for key in ("one", "two"):
        rows.append(
            dict(
                fact_kind="teaching_course",
                source_key=key,
                version=3,
                status="available",
                segments=[
                    dict(effective_at=s.effective_at, valid_to=s.valid_to, content=s.content)
                    for s in snapshot().segments
                ],
                prior_segments=[],
                latest_operation="upsert",
            )
        )
    coordinator, actor = environment(rows)
    coordinator.repository.clock = lambda: at(2)
    original = coordinator.repository.read_records
    seen = []

    def read(*args, **kw):
        if kw.get("cancellation_start"):
            key = kw["source_keys"][0]
            seen.append((key, kw["cancellation_start"]))
            p = {**proof(), "source_key": key, "selected_start": kw["cancellation_start"]}
            if key == "two":
                p.update(
                    introduction_version=3,
                    introduced_content={"display_name": "C"},
                    prior_segments=[
                        dict(effective_at=at(10), valid_to=None, content={"display_name": "B"})
                    ],
                )
            return SimpleNamespace(
                records=[p],
                school_fact_version=3,
                proof=SimpleNamespace(revalidate=lambda **kw: None),
            )
        return original(*args, **kw)

    coordinator.repository.read_records = read

    def entry(key, day):
        return dict(
            fact_kind="teaching_course",
            target_ref="f4:teaching_course:" + key,
            operation="cancel",
            fields={},
            reason="逐段取消",
            cancel_from=at(day).isoformat(),
        )

    payload = EducationBatchInput.model_validate_json(
        json.dumps({**entry("one", 10), "additional_changes": [entry("two", 20)]})
    )
    result = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=at(2)
    )
    assert seen == [("one", at(10)), ("two", at(20))]
    assert len(result.change_set.changes) == 2
    missing = payload.model_copy(
        update={
            "additional_changes": (
                payload.additional_changes[0].model_copy(update={"cancel_from": None}),
            )
        }
    )
    with pytest.raises(ValueError):
        coordinator.resolve_change(
            actor=actor, payload=missing, request_id=uuid4(), effective_at=at(2)
        )


def test_cancel_restores_only_proven_remaining_intervals_and_keeps_later_gap():
    from dataclasses import replace

    from app.domains.students.teaching_change_planner import cancel_teaching_future

    row = replace(
        snapshot(),
        segments=(piece(1, 10, "A"), piece(10, 12, "B"), piece(15, 20, "B"), piece(20, None, "C")),
    )
    evidence = {
        **proof(),
        "cancellation_segments": [
            {"effective_at": at(10), "valid_to": at(12)},
            {"effective_at": at(15), "valid_to": at(20)},
        ],
    }
    result = cancel_teaching_future(request(), row, proof=evidence, now=at(2))
    assert [
        (s["effective_at"], s["valid_to"], s["content"]["display_name"])
        for s in result["replacement_segments"]
    ] == [(at(1), at(12), "A"), (at(15), at(20), "A"), (at(20), None, "C")]
    assert (result["cause_source_key"], result["cause_version"]) == ("subject", 2)


def test_cancel_dependent_stops_restoration_at_independent_successor():
    from dataclasses import replace

    from app.domains.students.education_change_planner import EndProvenance
    from app.domains.students.teaching_change_planner import cancel_teaching_dependents

    old = replace(
        snapshot(),
        fact_kind="teaching_assignment",
        source_key="old",
        segments=(
            replace(
                piece(1, 10, "A"),
                provenance=EndProvenance(
                    cause_source_key="subject",
                    cause_version=2,
                    ended_target_version=3,
                    prior_effective_at=at(1),
                    prior_content={"display_name": "A", "valid_to": None},
                ),
            ),
            piece(20, None, "C"),
        ),
    )
    result = cancel_teaching_dependents(
        request(), (old,), proof=proof(), explicit_keys=frozenset({"subject"})
    )
    assert [
        (s["effective_at"], s["valid_to"], s["content"]["display_name"])
        for s in result[0]["replacement_segments"]
    ] == [(at(1), at(20), "A"), (at(20), None, "C")]


def test_normal_maintenance_end_keeps_future_successor_in_its_preview():
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment

    original = snapshot()
    coordinator, actor = environment(
        [
            dict(
                fact_kind=original.fact_kind,
                source_key=original.source_key,
                version=3,
                status="available",
                latest_operation="upsert",
                prior_segments=[],
                segments=[
                    dict(effective_at=s.effective_at, valid_to=s.valid_to, content=s.content)
                    for s in original.segments
                ],
            )
        ]
    )
    result = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput(
            fact_kind="teaching_course",
            target_ref="f4:teaching_course:subject",
            operation="end",
            fields={},
            reason="只结束当前安排并保留后续安排",
        ),
        request_id=uuid4(),
        effective_at=at(15),
    )
    rows = result.change_set.changes[0].replacement_segments
    assert [(s.effective_at, s.valid_to, s.content.display_name) for s in rows] == [
        (at(1), at(10), "A"),
        (at(10), at(15), "B"),
        (at(20), None, "C"),
    ]


def test_cancel_does_not_merge_independently_introduced_equal_prefix_values():
    from dataclasses import replace

    from app.domains.students.teaching_change_planner import cancel_teaching_future

    row = replace(
        snapshot(), segments=(piece(1, 10, "X"), piece(10, 20, "X"), piece(20, None, "Y"))
    )
    evidence = {
        **proof(),
        "selected_start": at(20),
        "introduction_version": 3,
        "introduced_content": {"display_name": "Y"},
        "prior_segments": [dict(effective_at=at(10), valid_to=None, content={"display_name": "X"})],
    }
    result = cancel_teaching_future(
        {**request(), "effective_at": at(20)}, row, proof=evidence, now=at(2)
    )
    assert [(s["effective_at"], s["valid_to"]) for s in result["replacement_segments"]] == [
        (at(1), at(10)),
        (at(10), None),
    ]
