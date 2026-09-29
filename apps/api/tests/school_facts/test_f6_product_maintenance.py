"""Preview preserves history, future changes and a single atomic confirmation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

AT = datetime(2026, 9, 22, tzinfo=UTC)


def test_facility_future_cancellation_removes_only_selected_arrangement():
    from app.composition.school_fact_residence_product import facility_timeline

    rows = [
        dict(
            effective_at="2026-09-01T00:00:00Z",
            valid_to="2026-10-01T00:00:00Z",
            content={"display_name": "原床位"},
        ),
        dict(
            effective_at="2026-10-01T00:00:00Z",
            valid_to=None,
            content={"display_name": "已安排新名称"},
        ),
    ]
    result = facility_timeline(
        rows, operation="cancel", at=datetime(2026, 10, 1, tzinfo=UTC), fields={}, now=AT
    )
    assert len(result) == 1
    assert result[0]["valid_to"] == "2026-10-01T00:00:00Z"
    # Cancellation does not silently restore or extend the earlier arrangement.
    with pytest.raises(ValueError):
        facility_timeline(
            rows, operation="cancel", at=datetime(2026, 9, 1, tzinfo=UTC), fields={}, now=AT
        )


def test_facility_partial_update_splits_only_applicable_interval():
    from app.composition.school_fact_residence_product import facility_timeline

    rows = [
        dict(
            effective_at="2026-09-01T00:00:00Z",
            valid_to="2026-10-01T00:00:00Z",
            content={"display_name": "原名称", "availability": "available"},
        ),
        dict(
            effective_at="2026-10-01T00:00:00Z",
            valid_to=None,
            content={"display_name": "未来名称", "availability": "unavailable"},
        ),
    ]
    out = facility_timeline(
        rows, operation="upsert", at=AT, fields={"display_name": "更正名称"}, now=AT
    )
    assert len(out) == 3 and out[-1] == rows[-1]
    assert out[1]["content"] == {"display_name": "更正名称", "availability": "available"}
    assert out[0]["content"]["display_name"] == "原名称"


def test_facility_correction_requires_explicit_new_interval_when_no_original():
    from app.composition.school_fact_residence_product import facility_timeline

    with pytest.raises(ValueError):
        facility_timeline([], operation="upsert", at=AT, fields={"display_name": "猜测"}, now=AT)


def test_bounded_correction_keeps_the_unchanged_tail():
    from app.composition.school_fact_residence_product import facility_timeline

    rows = [
        dict(
            effective_at="2026-09-01T00:00:00Z",
            valid_to=None,
            content={"authorized_bed_capacity": 2},
        )
    ]
    result = facility_timeline(
        rows,
        operation="correct",
        at=AT,
        fields={"authorized_bed_capacity": 3, "valid_to": "2026-10-01T00:00:00Z"},
        now=AT,
    )
    assert len(result) == 3
    assert result[-1]["content"] == {"authorized_bed_capacity": 2}
    assert result[-1]["valid_to"] is None


def test_residence_batch_retains_opaque_refs_and_refuses_duplicate_targets():
    from app.composition.school_fact_residence_product import ResidenceBatchInput

    item = dict(
        fact_kind="dorm_bed",
        target_ref="f6:dorm_bed:opaque",
        reason="正式更新",
        fields={"display_name": "3号床"},
    )
    with pytest.raises(ValueError):
        ResidenceBatchInput(**item, additional_changes=[item])


def test_new_residence_proposal_preserves_version_and_requires_authorized_references():
    from app.composition.school_fact_residence_product import (
        ResidenceBatchInput,
        ResidenceProductAdapter,
    )

    calls = []
    proof = SimpleNamespace(revalidate=lambda **kw: calls.append("recheck"))
    repository = SimpleNamespace(
        source_for_reference=lambda **kw: "owned-new",
        read_for_maintenance=lambda **kw: SimpleNamespace(
            records=({"source_key": "owned-new", "version": 0, "segments": []},),
            school_fact_version=12,
            proof=proof,
        ),
        reference_for=lambda **kw: "f6:" + kw["source_key"],
    )
    external = SimpleNamespace(
        source_for_reference=lambda **kw: "student-source",
        reference_for=lambda **kw: "f3:student:opaque",
    )
    adapter = ResidenceProductAdapter(repository=repository, external=external, clock=lambda: AT)
    payload = ResidenceBatchInput(
        fact_kind="student_bed_residence",
        creation_ref="f6create:student_bed_residence:opaque",
        fields={"student_ref": "f3:student:opaque", "bed_ref": "f6:dorm_bed:opaque"},
        reason="正式安排",
    )
    result = adapter.resolve_change(
        actor=object(), payload=payload, request_id=uuid4(), effective_at=AT
    )
    assert result.change_set.schema_version == 4 and result.change_set.base_version == 12
    assert result.change_set.changes[0].expected_version == 0
    assert "source_key" not in result.summary.after
    result.summary_revalidate(object())
    assert calls


def replacement_environment():
    from copy import deepcopy
    from datetime import timedelta

    from app.composition.school_fact_residence_product import ResidenceProductAdapter

    state = SimpleNamespace(version=12, allowed=True, now=AT, read=[])

    def check(**kwargs):
        if not state.allowed:
            raise PermissionError("scope revoked")
        if state.version != 12 or state.now >= AT + timedelta(hours=1):
            raise RuntimeError("changed")

    proof = SimpleNamespace(revalidate=check)
    content = {"student_source_key": "student", "bed_source_key": "bed"}
    rows = {
        "current": {
            "version": 1,
            "segments": [
                {"effective_at": "2026-09-01T00:00:00Z", "valid_to": None, "content": content}
            ],
        },
        "future": {
            "version": 1,
            "segments": [
                {"effective_at": "2026-10-01T00:00:00Z", "valid_to": None, "content": content}
            ],
        },
        "past": {
            "version": 2,
            "segments": [
                {
                    "effective_at": "2026-08-01T00:00:00Z",
                    "valid_to": "2026-09-01T00:00:00Z",
                    "content": content,
                }
            ],
        },
        "new": {"version": 0, "segments": []},
    }

    def read(**kwargs):
        check()
        state.read.append(kwargs)
        key = kwargs["source_key"]
        return SimpleNamespace(
            records=({"source_key": key, **deepcopy(rows[key])},),
            school_fact_version=state.version,
            proof=proof,
        )

    repository = SimpleNamespace(
        source_for_reference=lambda **kw: kw["reference"].split(":")[-1],
        reference_for=lambda **kw: "f6:" + kw["fact_kind"] + ":" + kw["source_key"],
        read_for_maintenance=read,
        read_page_context=lambda *a, **kw: SimpleNamespace(proof=proof),
        current_version=lambda _: state.version,
    )
    external = SimpleNamespace(
        source_for_reference=lambda **kw: kw["reference"].split(":")[-1],
        reference_for=lambda **kw: "f3:student:" + kw["source_key"],
    )
    return ResidenceProductAdapter(
        repository=repository, external=external, clock=lambda: state.now
    ), state


def replacement_payload(rows=(), **overrides):
    import json

    from app.composition.school_fact_residence_product import ResidenceBatchInput

    return ResidenceBatchInput.model_validate_json(
        json.dumps(
            dict(
                fact_kind="student_bed_residence",
                target_ref="f6:student_bed_residence:current",
                operation="end",
                fields={},
                reason="明确核对住宿集合",
                effective_at=AT,
                import_mode="replace",
                import_replacement_refs=[
                    "f6:student_bed_residence:" + k for k in ("current", "future", "past")
                ],
                import_rows=rows,
                **overrides,
            ),
            default=str,
        )
    )


def test_explicit_empty_replace_ends_current_revokes_future_preserves_history():
    adapter, state = replacement_environment()
    result = adapter.resolve_change(
        actor=object(), payload=replacement_payload(), request_id=uuid4(), effective_at=AT
    )
    assert [(c.source_key, c.operation) for c in result.change_set.changes] == [
        ("current", "end"),
        ("future", "revoke"),
    ]
    assert result.summary.after_segments[0]["valid_to"] == AT.isoformat()
    assert result.summary.before_segments[0]["effective_at"] == "2026-09-01T00:00:00Z"
    assert "空表" in result.summary.source_label and "明确选中" in result.summary.source_label
    assert any(r.get("required_fields") for r in state.read)


def test_incremental_missing_rows_do_not_end_unmentioned_objects():
    import json

    adapter, state = replacement_environment()
    from app.composition.school_fact_residence_product import ResidenceBatchInput

    row = dict(
        fact_kind="student_bed_residence",
        creation_ref="f6create:student_bed_residence:new",
        fields={"student_ref": "f3:student:student", "bed_ref": "f6:dorm_bed:bed"},
        reason="新增",
    )
    payload = ResidenceBatchInput.model_validate_json(json.dumps(dict(**row, import_rows=[row])))
    result = adapter.resolve_change(
        actor=object(), payload=payload, request_id=uuid4(), effective_at=AT
    )
    assert [(c.source_key, c.operation) for c in result.change_set.changes] == [("new", "upsert")]
    assert all(r["source_key"] == "new" for r in state.read)


def test_replace_can_keep_exact_existing_and_create_without_rewriting_identity():
    adapter, _ = replacement_environment()
    rows = [
        dict(
            fact_kind="student_bed_residence",
            target_ref="f6:student_bed_residence:future",
            fields={},
            reason="保留",
        ),
        dict(
            fact_kind="student_bed_residence",
            creation_ref="f6create:student_bed_residence:new",
            fields={"student_ref": "f3:student:student", "bed_ref": "f6:dorm_bed:bed"},
            reason="新增",
        ),
    ]
    result = adapter.resolve_change(
        actor=object(), payload=replacement_payload(rows), request_id=uuid4(), effective_at=AT
    )
    assert {(c.source_key, c.operation) for c in result.change_set.changes} == {
        ("current", "end"),
        ("new", "upsert"),
    }
    rows[0]["fields"] = {"bed_ref": "f6:dorm_bed:other"}
    with pytest.raises(ValueError):
        adapter.resolve_change(
            actor=object(), payload=replacement_payload(rows), request_id=uuid4(), effective_at=AT
        )


@pytest.mark.parametrize("changed", ["version", "scope", "time"])
def test_replace_confirmation_rechecks_full_scope_and_new_fact_boundary(changed):
    adapter, state = replacement_environment()
    result = adapter.resolve_change(
        actor=object(), payload=replacement_payload(), request_id=uuid4(), effective_at=AT
    )
    if changed == "version":
        state.version += 1
    elif changed == "scope":
        state.allowed = False
    else:
        from datetime import timedelta

        state.now += timedelta(hours=1)
    with pytest.raises((PermissionError, RuntimeError)):
        result.summary_revalidate(object())


@pytest.mark.parametrize(
    "changes",
    [
        {"import_replacement_refs": None},
        {"effective_at": None},
        {"import_replacement_refs": []},
        {"import_mode": "incremental"},
        {"fact_kind": "dorm_bed"},
    ],
)
def test_replace_requires_explicit_scope_time_and_residence_contract(changes):
    from app.composition.school_fact_residence_product import ResidenceBatchInput

    data = replacement_payload().model_dump()
    data.update(changes)
    with pytest.raises(ValueError):
        ResidenceBatchInput.model_validate(data)


def test_replace_any_unreadable_or_unmaintainable_selected_object_rejects_whole_batch():
    adapter, _ = replacement_environment()
    original = adapter.repository.read_for_maintenance

    def narrowed(**kwargs):
        if kwargs["source_key"] == "future":
            raise PermissionError("one selected scope item unavailable")
        return original(**kwargs)

    adapter.repository.read_for_maintenance = narrowed
    with pytest.raises(PermissionError):
        adapter.resolve_change(
            actor=object(), payload=replacement_payload(), request_id=uuid4(), effective_at=AT
        )


def test_replace_tool_contract_reaches_same_finite_import_owner():
    from app.composition.school_fact_residence_gateway import ResidencePrepareInput, _prepare_schema

    payload = replacement_payload()
    parsed = ResidencePrepareInput.model_validate_json(payload.model_dump_json())
    assert parsed.import_mode == "replace" and parsed.import_rows == ()
    assert parsed.import_replacement_refs == payload.import_replacement_refs
    assert "import_rows" in _prepare_schema()["properties"]
    adapter, _ = replacement_environment()
    result = adapter.resolve_change(
        actor=object(), payload=parsed, request_id=uuid4(), effective_at=AT
    )
    assert len(result.change_set.changes) == 2
