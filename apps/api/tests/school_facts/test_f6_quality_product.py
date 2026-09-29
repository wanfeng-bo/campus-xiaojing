"""Quality updates retain separate authority and never become accommodation changes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_residence_product import ResidenceBatchInput
from app.composition.school_fact_residence_quality_product import ResidenceQualityProductAdapter
from app.composition.school_fact_residence_query import ResidencePageCursor
from app.platform.school_facts.f2_policy import F2FactScope

NOW = datetime(2026, 9, 22, tzinfo=UTC)


class Bridge:
    def __init__(self):
        self.active = True
        self.calls = []
        self.projection = SimpleNamespace(current_version=lambda _: 11)

    def revalidate(self):
        if not self.active:
            raise PermissionError("revoked")

    def require_change_set(self, actor, change_set):
        self.calls.append(change_set)
        self.revalidate()
        return self


def fixture():
    bridge = Bridge()
    actor = SimpleNamespace(school_id=uuid4())
    repository = SimpleNamespace(
        source_for=lambda **kw: "f6:quality:no-residence",
        reference_for=lambda **kw: "f2:fact_quality:" + "a" * 64,
    )
    external = SimpleNamespace(
        source_for_reference=lambda **kw: "f3:person:student-a",
        reference_for=lambda **kw: "f3:person:" + "b" * 64,
    )
    residence = SimpleNamespace(
        source_for_reference=lambda **kw: "f6:residence:a",
        reference_for=lambda **kw: "f6:student_bed_residence:" + "c" * 64,
        clock=lambda: NOW,
        read_page_context=lambda *a, **kw: SimpleNamespace(
            scope_hash="f6-scope", expires=NOW + timedelta(hours=1), proof=bridge
        ),
    )
    adapter = ResidenceQualityProductAdapter(
        bridge=bridge,
        directory=SimpleNamespace(repository=repository),
        residence=residence,
        external=external,
        cursor_codec=ResidencePageCursor(b"unit-quality-private-cursor-key"),
    )
    return adapter, bridge, actor


def payload(**fields):
    return ResidenceBatchInput(
        fact_kind="fact_quality",
        creation_ref="f2create:fact_quality:" + "a" * 64,
        operation="upsert",
        reason="学校核实后发布",
        fields={
            "quality_kind": "negative",
            "target_fact_kind": "student_bed_residence",
            "owner_kind": "person",
            "person_ref": "f3:person:" + "b" * 64,
            "negative_code": "no_residence",
            "basis_reference": "合成学校正式核实",
            "responsibility": "合成住宿维护者",
            "verified_at": NOW.isoformat(),
            **fields,
        },
    )


def test_formal_negative_uses_v4_quality_only_and_rechecks_release():
    adapter, bridge, actor = fixture()
    result = adapter.resolve_change(
        actor=actor, payload=payload(), request_id=uuid4(), effective_at=NOW
    )
    assert result.change_set.schema_version == 4
    assert [item.fact_kind for item in result.change_set.changes] == ["fact_quality"]
    assert result.change_set.changes[0].content.person_source_key == "f3:person:student-a"
    assert "person_source_key" not in result.summary.after
    assert bridge.calls
    bridge.active = False
    with pytest.raises(PermissionError):
        result.summary_revalidate(actor)


def test_complete_quality_uses_exact_current_authorized_target_not_caller_basis():
    adapter, bridge, actor = fixture()
    target_proof = SimpleNamespace(active=True)

    def revalidate_target(*, actor=None):
        if not target_proof.active:
            raise PermissionError("target read revoked")

    target_proof.revalidate = revalidate_target
    read_scope = SimpleNamespace(field_names=("student_source_key",))
    target_read = SimpleNamespace(
        records=(
            {
                "source_key": "f6:residence:a",
                "fact_kind": "student_bed_residence",
                "version": 3,
                "valid_from": (NOW - timedelta(days=1)).isoformat(),
                "valid_to": None,
            },
        ),
        school_fact_version=11,
        has_more=False,
        proof=target_proof,
    )
    calls = []
    adapter.residence.repository = SimpleNamespace(
        clock=lambda: NOW,
        authority=SimpleNamespace(
            require_scope=lambda *args, **kwargs: SimpleNamespace(scope=read_scope)
        ),
        read=lambda *args, **kwargs: calls.append(kwargs) or target_read,
    )
    item = payload(
        quality_kind="coverage",
        coverage_status="complete",
        negative_code=None,
        field_name="student_source_key",
        target_ref="f6:student_bed_residence:" + "c" * 64,
    )
    result = adapter.resolve_change(
        actor=actor, payload=item, request_id=uuid4(), effective_at=NOW
    )
    basis = result.change_set.changes[0].content.coverage_basis
    assert basis.target_source_key == "f6:residence:a"
    assert basis.field_name == "student_source_key"
    assert basis.member_count == 1
    assert basis.school_fact_version == 11
    assert calls[0]["source_keys"] == ("f6:residence:a",)
    assert "coverage_basis" not in result.summary.after
    target_proof.active = False
    with pytest.raises(PermissionError, match="revoked"):
        result.summary_revalidate(actor)


def test_complete_quality_cannot_accept_supplied_basis_or_missing_target():
    adapter, _, actor = fixture()
    with pytest.raises(ValueError, match="不能由维护请求填写"):
        adapter.resolve_change(
            actor=actor,
            payload=payload(coverage_basis={"member_count": 1}),
            request_id=uuid4(),
            effective_at=NOW,
        )
    with pytest.raises((PermissionError, ValueError)):
        adapter.resolve_change(
            actor=actor,
            payload=payload(
                quality_kind="coverage", coverage_status="complete", negative_code=None,
                target_ref="f6:student_bed_residence:" + "c" * 64,
            ),
            request_id=uuid4(),
            effective_at=NOW,
        )


def test_quality_does_not_accept_education_owner_or_mixed_facility_batch():
    adapter, _, actor = fixture()
    with pytest.raises(ValueError):
        adapter.resolve_change(
            actor=actor,
            payload=payload(target_fact_kind="teaching_assignment"),
            request_id=uuid4(),
            effective_at=NOW,
        )
    with pytest.raises(ValueError):
        ResidenceBatchInput.model_validate(
            {
                **payload().model_dump(),
                "additional_changes": [
                    {
                        "fact_kind": "dorm_bed",
                        "creation_ref": "f6create:dorm_bed:" + "c" * 64,
                        "operation": "upsert",
                        "reason": "登记",
                        "fields": {},
                    }
                ],
            }
        )


def test_update_cannot_smuggle_an_unresolved_source_key():
    adapter, _, actor = fixture()
    with pytest.raises(ValueError):
        adapter.resolve_change(
            actor=actor,
            payload=payload(person_source_key="other-person"),
            request_id=uuid4(),
            effective_at=NOW,
        )


def test_quality_related_filter_resolves_selected_kind_once_before_other_kinds():
    adapter, bridge, actor = fixture()
    scope = F2FactScope(actor.school_id, "fact_quality", ("q-bed", "q-residence"))
    permit = SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))
    bridge.f2_guard = SimpleNamespace(
        require_current=lambda *a, **kw: permit, revalidate=lambda *a: None
    )
    bridge.coordinates = lambda actor, key: (
        {
            "content": {
                "target_fact_kind": "dorm_bed" if key == "q-bed" else "student_bed_residence",
                "target_source_key": "bed" if key == "q-bed" else "residence",
            }
        },
        permit,
    )
    seen = []

    def resolve(**kwargs):
        seen.append(kwargs["fact_kind"])
        assert kwargs["fact_kind"] == "student_bed_residence"
        return "residence"

    adapter.residence.source_for_reference = resolve
    bridge.read_content = lambda actor, key: {
        "content": {
            "quality_kind": "source",
            "target_fact_kind": "student_bed_residence",
            "source_status": "available",
        },
        "proof": bridge,
        "version": 1,
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_to": None,
    }
    result = adapter.query(
        actor=actor, related_ref="f6:student_bed_residence:" + "a" * 64, as_of=NOW
    )
    assert len(result["records"]) == 1
    assert seen == ["student_bed_residence"]


@pytest.mark.parametrize(
    "change",
    [
        "tamper",
        "actor",
        "reference",
        "related",
        "as_of",
        "limit",
        "f2_scope",
        "f6_scope",
        "version",
        "time",
        "revoke",
    ],
)
def test_quality_pages_bind_both_scopes_selection_and_release_time(change):
    adapter, bridge, actor = fixture()
    state = SimpleNamespace(
        scope=F2FactScope(actor.school_id, "fact_quality", ("q1", "q2", "q3")),
        version=11,
        f6="f6-scope",
        now=NOW,
    )
    bridge.projection.current_version = lambda _: state.version
    bridge.f2_guard = SimpleNamespace(
        require_current=lambda *a, **kw: SimpleNamespace(
            scope_plan=SimpleNamespace(reference=state.scope.reference)
        ),
        revalidate=lambda *a: None,
    )
    adapter.residence.clock = lambda: state.now
    adapter.residence.read_page_context = lambda *a, **kw: SimpleNamespace(
        scope_hash=state.f6, expires=NOW + timedelta(seconds=1), proof=bridge
    )
    bridge.coordinates = lambda actor, key: (
        {"content": {"target_fact_kind": "dorm_bed", "target_source_key": "bed"}},
        None,
    )
    bridge.read_content = lambda actor, key: {
        "content": {
            "quality_kind": "source",
            "target_fact_kind": "dorm_bed",
            "source_status": "available",
        },
        "proof": bridge,
        "version": 1,
        "valid_from": NOW - timedelta(days=1),
        "valid_to": None,
    }
    first = adapter.query(actor=actor, limit=1)
    assert first["truncated"] and first["next_cursor"]
    assert adapter.query(actor=actor, limit=1, cursor=first["next_cursor"])["returned_count"] == 1
    args = {"actor": actor, "limit": 1, "cursor": first["next_cursor"]}
    if change == "tamper":
        args["cursor"] = args["cursor"][:-1] + ("0" if args["cursor"][-1] != "0" else "1")
    elif change == "actor":
        args["actor"] = SimpleNamespace(school_id=actor.school_id, assignment_id=uuid4())
    elif change == "reference":
        args["reference"] = "f2:fact_quality:" + "a" * 64
    elif change == "related":
        args["related_ref"] = "f6:dorm_bed:" + "b" * 64
    elif change == "as_of":
        args["as_of"] = NOW
    elif change == "limit":
        args["limit"] = 2
    elif change == "f2_scope":
        state.scope = F2FactScope(actor.school_id, "fact_quality", ("q1", "q2"))
    elif change == "f6_scope":
        state.f6 = "new-scope"
    elif change == "version":
        state.version += 1
    elif change == "time":
        state.now += timedelta(seconds=1)
    elif change == "revoke":
        bridge.active = False
    with pytest.raises((ValueError, PermissionError)):
        adapter.query(**args)
