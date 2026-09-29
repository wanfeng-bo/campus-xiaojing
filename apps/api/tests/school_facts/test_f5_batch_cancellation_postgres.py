"""Owned Writer: two cancellations restore a shared timeline in one atomic batch."""

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_education_product import EducationBatchInput
from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.school_facts.test_f4_education_maintenance import environment
from tests.school_facts.test_f5_exact_cancellation_postgres import SQL
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    apply,
    change,
    read_rows,
)
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    graph as graph,
)
from tests.school_facts.test_f5_writer_reader_postgres import writer as owned_writer  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="explicit owned only")


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_dependent_restoration_is_atomic_and_preserves_original_stops(graph, reverse):
    writer, key, now, start, _ssr = graph
    dependent = key("teacher-old")
    kind = "teaching_assignment"
    points = {n: now + timedelta(days=n) for n in (2, 4, 6, 7, 9, 10, 11)}

    def content(offering, staff="old"):
        return dict(
            offering_source_key=key("offer-" + offering),
            assignment_source_key=key(staff + "-staff"),
            teaching_role="lead",
        )

    # An explicitly reviewed full correction may add disconnected future terms;
    # ordinary upsert must not silently revive a root after its independent end.
    apply(
        writer,
        [
            {
                **change(kind, dependent, points[6], operation="correct", expected=1),
                "replacement_segments": [
                    dict(effective_at=start, valid_to=points[4], content=content("a")),
                    dict(effective_at=points[6], valid_to=points[9], content=content("b")),
                    dict(effective_at=points[10], valid_to=points[11], content=content("a")),
                ],
            }
        ],
    )

    # Two independent replacements truncate different published intervals on the
    # same root. The sole Writer preserves the first cutoff's cause on later writes.
    for index, name, begin, end, offering in (
        (0, "replacement-x", 2, 4, "a"),
        (1, "replacement-y", 7, 9, "b"),
    ):
        rows = [
            dict(effective_at=start, valid_to=points[2], content=content("a")),
            dict(
                effective_at=points[6],
                valid_to=points[9 if index == 0 else 7],
                content=content("b"),
            ),
            dict(effective_at=points[10], valid_to=points[11], content=content("a")),
        ]
        apply(
            writer,
            [
                change(
                    kind,
                    key(name),
                    points[begin],
                    {**content(offering, "new"), "valid_to": points[end]},
                ),
                {
                    **change(
                        kind, dependent, points[begin], operation="correct", expected=2 + index
                    ),
                    "cause_source_key": key(name),
                    "cause_version": 1,
                    "replacement_segments": rows,
                },
            ],
        )

    sources = [key("replacement-x"), key("replacement-y"), dependent]
    records = read_rows(writer, kind, sources, now)
    original = next(r for r in records if r["source_key"] == dependent)
    assert original["version"] == 4
    assert [s["cause_source_key"] for s in original["segments"][:2]] == sources[:2]
    assert all(s["causal_prior"] is not None for s in original["segments"][:2])
    with writer[0]["reader"].connect() as conn:
        base = conn.scalar(
            text("SELECT app.school_fact_current_version(:s)"), {"s": writer[1].school_id}
        )
    coordinator, actor = environment(records, school_version=base)
    coordinator.repository.clock = lambda: now
    original_read = coordinator.repository.read_records

    def read(*args, **kwargs):
        if kwargs.get("cancellation_start"):
            with writer[0]["reader"].connect() as conn:
                evidence = conn.scalar(
                    SQL,
                    dict(
                        school=writer[1].school_id,
                        kind=kind,
                        source=kwargs["source_keys"][0],
                        fields=list(kwargs["fields"]),
                        start=kwargs["cancellation_start"],
                        version=kwargs["expected_version"],
                    ),
                )
            return SimpleNamespace(
                records=evidence["records"],
                school_fact_version=evidence["school_fact_version"],
                proof=SimpleNamespace(revalidate=lambda **kw: None),
            )
        return original_read(*args, **kwargs)

    coordinator.repository.read_records = read
    items = [
        dict(
            fact_kind=kind,
            target_ref="f4:" + kind + ":" + key(name),
            operation="cancel",
            fields={},
            reason="合成同批取消两次未来替代",
            cancel_from=points[day].isoformat(),
        )
        for name, day in (("replacement-x", 2), ("replacement-y", 7))
    ]
    if reverse:
        items.reverse()
    resolved = coordinator.resolve_change(
        actor=actor,
        payload=EducationBatchInput.model_validate_json(
            json.dumps({**items[0], "additional_changes": items[1:]})
        ),
        request_id=uuid4(),
        effective_at=now,
    )
    changes = [
        {"content": None, **c.model_dump(mode="python", exclude_unset=True)}
        for c in resolved.change_set.changes
    ]
    assert len(changes) == 3
    # One stale member rejects the entire confirmed plan before any root changes.
    invalid = [
        {**c, "expected_version": c["expected_version"] - 1} if c["source_key"] == dependent else c
        for c in changes
    ]
    with pytest.raises(SchoolFactChangeRejected):
        apply(writer, invalid)
    assert read_rows(writer, kind, sources, now) == records

    committed = apply(writer, changes)
    assert committed.changed_count == 3
    actual = read_rows(writer, kind, sources, now)
    assert all(not r["segments"] for r in actual if r["source_key"] != dependent)
    current = next(r for r in actual if r["source_key"] == dependent)
    assert current["version"] == 5
    assert [
        (datetime.fromisoformat(s["effective_at"]), datetime.fromisoformat(s["valid_to"]))
        for s in current["segments"]
    ] == [
        (start, points[4]),
        (points[6], points[9]),
        (points[10], points[11]),
    ]
    assert all(s["cause_source_key"] is None for s in current["segments"])
    assert all(s["causal_prior"] is None for s in current["segments"])
    with writer[0]["migration"].connect() as conn:
        lineage = conn.execute(
            text(
                "SELECT fact_source_key,operation FROM school_fact_change_lineage WHERE batch_id=:b"
            ),
            {"b": committed.batch_id},
        ).all()
        assert set(lineage) == {(source, "correct") for source in sources}
    # Cleared single-cause coordinates do not license a guessed later undo.
    with (
        writer[0]["reader"].connect() as conn,
        pytest.raises(DBAPIError, match="original arrangement proof"),
    ):
        conn.scalar(
            SQL,
            dict(
                school=writer[1].school_id,
                kind=kind,
                source=dependent,
                fields=["offering_source_key", "assignment_source_key", "teaching_role"],
                start=points[6],
                version=5,
            ),
        )
