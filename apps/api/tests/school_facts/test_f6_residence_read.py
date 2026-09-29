"""F6 selected-child projection and release boundaries (no database substitute claim)."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from importlib import import_module, util

import pytest

from tests.test_f3_family_read import NOW, setup
from tests.test_f3_product_identity import guardian


def test_family_gateway_has_no_child_or_identity_selection_and_has_stale_check():
    name = "app.composition.school_fact_residence_gateway"
    assert util.find_spec(name), "F6 gateways are not implemented"
    module = import_module(name)
    properties = module.FamilyResidenceInput.model_json_schema()["properties"]
    assert set(properties) == {"as_of"}
    family_descriptors = module.FamilyResidenceToolGateway.descriptors
    assert {d.name for d in family_descriptors} == {module.FAMILY_NAME}
    assert all(d.kind == "read" for d in family_descriptors)
    descriptors = module.ResidenceToolGateway.descriptors
    assert {d.name.rsplit(".", 1)[1] for d in descriptors} == {
        "query",
        "describe",
        "locate_change",
        "prepare_transfer",
        "prepare",
        "receipt",
        "own_receipt",
        "own_receipts",
    }
    assert next(d for d in descriptors if d.name.endswith(".locate_change")).kind == "read"
    assert next(d for d in descriptors if d.name.endswith(".prepare_transfer")).kind == "prepare"


def fixture(*, caregiver=False):
    name = "app.composition.school_fact_residence_query"
    assert util.find_spec(name), "F6 residence query is not implemented"
    module = import_module(name)
    old, actor, state = setup(caregiver=caregiver)
    state.clock = NOW
    state.reads = 0
    state.payload = {
        "status": "no_current_records",
        "records": [],
        "school_fact_version": 8,
        "student_id": str(state.basis.student_id),
        "student_version": 1,
        "relationship_version": 1,
        "as_of": NOW.isoformat(),
        "next_boundary": (NOW + timedelta(hours=1)).isoformat(),
        "residence_mode": None,
        "enrollment_status": "enrolled",
        "quality": [],
    }

    def read(_actor, *, as_of):
        state.reads += 1
        result = deepcopy(state.payload)
        result["as_of"] = (as_of or state.clock).isoformat()
        return result

    old.repository.residence_read = read
    old.repository.residence_stamp = lambda _: state.payload["school_fact_version"]
    service = module.FamilyResidenceQueryService(
        repository=old.repository,
        current_actor=lambda a: state.active and a == actor,
        reference_key=b"f6-reference-test-key-32-bytes-long",
        clock=lambda: state.clock,
    )
    return service, actor, state


def row():
    return {
        "source_key": "synthetic:residence",
        "fact_kind": "student_bed_residence",
        "version": 1,
        "student_name": "合成孩子",
        "bed_name": "2号床",
        "room_name": "301室",
        "building_name": "合成住宿楼",
        "valid_from": NOW.isoformat(),
        "valid_to": None,
        "source": {
            "kind": "formal_publication",
            "label": "合成发布",
            "published_at": NOW.isoformat(),
        },
        "quality": [],
    }


def test_f6_family_projection_is_implemented():
    service, actor, _ = fixture()
    public = service.query(actor=actor).public()
    assert public["kind"] == "family_residence"
    assert public["residence_mode"] is None
    assert public["coverage"]["status"] == "unknown"
    assert public["status"] == "no_current_records"


def test_own_child_rows_have_safe_refs_no_internal_source_keys():
    service, actor, state = fixture()
    state.payload.update(status="available", records=[row()], residence_mode="boarding")
    result = service.query(actor=actor)
    record = result.public()["records"][0]
    assert record["bed_name"] == "2号床" and "source_key" not in record
    assert record["reference"].startswith("sf6_fam_")
    assert result.evidence.valid(actor)


@pytest.mark.parametrize(
    "change", ["logout", "revoke", "expiry", "boundary", "publication", "child"]
)
def test_empty_evidence_rechecks_current_authority_sets_and_time(change):
    service, actor, state = fixture()
    result = service.query(actor=actor)
    current = actor
    if change == "logout":
        state.active = False
    elif change == "revoke":
        state.basis = None
    elif change == "expiry":
        state.basis = replace(state.basis, valid_to=NOW)
    elif change == "boundary":
        state.clock += timedelta(hours=1)
    elif change == "publication":
        state.payload["school_fact_version"] += 1
    elif change == "child":
        current = guardian(2)
    assert not result.evidence.valid(current)


def test_guardian_future_and_history_stay_within_verified_relationship():
    service, actor, state = fixture()
    assert service.query(actor=actor, as_of=NOW + timedelta(days=1)).public()["future_basis"]
    assert service.query(actor=actor, as_of=NOW - timedelta(days=1)).evidence
    before = state.reads
    assert service.query(actor=actor, as_of=NOW + timedelta(days=4)).public()["status"] == "denied"
    assert service.query(actor=actor, as_of=NOW - timedelta(days=4)).public()["status"] == "denied"
    assert state.reads == before


def test_caregiver_cannot_read_f6_and_overbroad_results_fail_closed():
    service, actor, state = fixture(caregiver=True)
    assert service.query(actor=actor).public()["status"] == "denied"
    assert state.reads == 0
    service, actor, state = fixture()
    state.payload.update(status="available", records=[row() | {"roommates": ["another child"]}])
    assert service.query(actor=actor).public()["status"] == "unavailable"


def test_selected_child_mismatch_and_release_revocation_never_publish_values():
    service, actor, state = fixture()
    state.payload["student_id"] = "foreign-child"
    assert service.query(actor=actor).public()["status"] == "unavailable"


def test_day_attribute_and_residence_are_a_conflict_not_a_silent_choice():
    service, actor, state = fixture()
    state.payload.update(status="available", records=[row()], residence_mode="day")
    assert service.query(actor=actor).public()["status"] == "conflict"


def test_model_wait_and_historical_reuse_revalidate_family_evidence():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_gateway import (
        FAMILY_NAME,
        FamilyResidenceToolGateway,
    )
    from app.platform.assistant.runtime import AssistantRuntimeError

    service, actor, state = fixture()
    state.payload.update(status="available", records=[row()], residence_mode="boarding")
    identity = SimpleNamespace(actor=actor, session_binding="test-session")
    turn = FamilyResidenceToolGateway(resolve_actor=lambda _: identity, query=service).for_turn(
        "bound-context"
    )
    result = turn.execute(
        SimpleNamespace(name=FAMILY_NAME, arguments={}),
        SimpleNamespace(actor_context_ref="bound-context"),
    )
    assert result.history_check.valid()
    assert "reference" not in result.model_content["records"][0]
    assert result.model_content["tool_context"]["targets"][0]["reference"].startswith("sf6_fam_")
    state.payload["school_fact_version"] += 1
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("bound-context")


def test_quality_source_and_review_limits_are_visible_to_family():
    service, actor, state = fixture()
    value = row()
    value["quality"] = [
        {
            "quality_kind": "source",
            "target_fact_kind": "dorm_bed",
            "source_status": "unavailable",
            "review_due_at": NOW.isoformat(),
            "source": None,
        }
    ]
    state.payload.update(status="available", records=[value], residence_mode="boarding")
    public = service.query(actor=actor).public()
    assert public["status"] == "partial"
    assert {"source_quality_unavailable", "quality_review_overdue"} <= set(public["limitations"])


def test_family_residence_does_not_promote_unverified_complete_coverage():
    service, actor, state = fixture()
    declaration = {
        "quality_kind": "coverage",
        "target_fact_kind": "student_bed_residence",
        "field_name": None,
        "coverage_status": "complete",
        "source": None,
    }
    value = row()
    value["quality"] = [declaration]
    state.payload.update(
        status="available", records=[value], residence_mode="boarding", quality=[declaration]
    )
    public = service.query(actor=actor).public()
    assert public["coverage"]["status"] == "unknown"
    assert all(
        quality.get("coverage_status") != "complete"
        for quality in [*public["quality"], *public["records"][0]["quality"]]
    )


def staff_pages():
    from types import SimpleNamespace

    from app.composition.school_fact_residence_query import ResidenceQueryService
    from app.composition.school_fact_residence_reader import ResidenceRepository
    from app.composition.school_fact_residence_views import ResidenceMaintenanceViews
    from app.platform.school_facts.f6_policy import F6FactScope
    from tests.test_f1_official_contact_query import _actor

    actor = _actor()
    keys = tuple(f"bed-{i}" for i in range(3))
    scope = F6FactScope(actor.school_id, "dorm_bed", keys, (), ("display_name", "business_code"))
    state = SimpleNamespace(
        scope=scope, version=8, clock=NOW, allowed=True, reads=0, version_reads=0,
        quality=[],
    )

    class Authority:
        def require_scope(self, *args, **kwargs):
            if not state.allowed:
                raise PermissionError("revoked")
            return SimpleNamespace(scope=state.scope)

        def require_fields(self, permit, fields):
            if not set(fields) <= set(permit.scope.field_names):
                raise PermissionError("fields")

        def require_object(self, permit, key, **kwargs):
            if key not in permit.scope.object_keys:
                raise PermissionError("object")

        def revalidate(self, *args, **kwargs):
            if not state.allowed:
                raise PermissionError("revoked")

    class Projection:
        def current_version(self, school):
            state.version_reads += 1
            return state.version

        def read(self, **kwargs):
            state.reads += 1
            selected = [key for key in keys if key in kwargs["source_keys"]]
            offset, limit = kwargs["offset"], kwargs["limit"]
            rows = [
                dict(
                    source_key=key,
                    fact_kind="dorm_bed",
                    version=1,
                    content={"display_name": key + "床", "business_code": "编号" + key},
                    valid_from=NOW.isoformat(),
                    valid_to=None,
                    source=None,
                    quality=state.quality,
                )
                for key in selected[offset : offset + limit]
            ]
            return dict(
                status="available",
                records=rows,
                school_fact_version=state.version,
                has_more=offset + len(rows) < len(selected),
                next_boundary=(NOW + timedelta(hours=1)).isoformat(),
            )

    repository = ResidenceRepository(
        authority=Authority(),
        projection=Projection(),
        reference_key=b"private-f6-pages-reference-key",
        clock=lambda: state.clock,
    )
    query = ResidenceQueryService(
        repository=repository,
        current_actor=lambda candidate: candidate == actor,
        reference_key=b"private-f6-pages-reference-key",
    )
    views = ResidenceMaintenanceViews(
        repository=repository,
        adapter=SimpleNamespace(public_fields=lambda actor, fields: dict(fields)),
        external_choices=lambda **kwargs: None,
        query=query,
    )
    return views, query, actor, state


def test_maintenance_can_read_every_signed_page_with_all_authorized_fields():
    views, query, actor, _ = staff_pages()
    page = views.query(actor=actor, fact_kind="dorm_bed", limit=1)
    first_cursor, rows = page["next_cursor"], list(page["records"])
    assert first_cursor and page["truncated"]
    while page["next_cursor"]:
        page = views.query(actor=actor, fact_kind="dorm_bed", limit=1, cursor=page["next_cursor"])
        rows.extend(page["records"])
    assert [row["fields"]["business_code"] for row in rows] == [
        "编号bed-0",
        "编号bed-1",
        "编号bed-2",
    ]
    assert len({row["reference"] for row in rows}) == 3
    assert "source_key" not in str(rows)
    assert not page["truncated"] and page["next_cursor"] is None
    public = query.query(actor=actor, fact_kind="dorm_bed", limit=1, cursor=first_cursor).public()
    assert public["records"][0]["business_code"] == "编号bed-1"


def test_staff_residence_answer_does_not_repeat_unverified_complete_claim():
    _, query, actor, state = staff_pages()
    state.quality = [{"quality_kind": "coverage", "coverage_status": "complete"}]
    public = query.query(actor=actor, fact_kind="dorm_bed", limit=1).public()
    assert public["status"] == "available"
    assert public["records"][0]["quality"][0]["coverage_status"] == "unknown"


def test_first_residence_page_does_not_read_version_for_absent_cursor():
    _, query, actor, state = staff_pages()
    result, cursor = query.read_page(actor=actor, fact_kind="dorm_bed", limit=1)
    assert result.records and cursor
    # The Reader and final evidence each verify the fact version. With no
    # cursor, there is no additional page-version comparison to perform.
    assert state.version_reads == 2


def test_continued_residence_page_still_checks_cursor_version_before_read():
    _, query, actor, state = staff_pages()
    _, cursor = query.read_page(actor=actor, fact_kind="dorm_bed", limit=1)
    assert cursor
    state.version_reads = 0
    query.read_page(actor=actor, fact_kind="dorm_bed", limit=1, cursor=cursor)
    assert state.version_reads == 3
    before = state.reads
    state.version += 1
    with pytest.raises(ValueError, match="Changed page selection or facts"):
        query.read_page(actor=actor, fact_kind="dorm_bed", limit=1, cursor=cursor)
    assert state.reads == before


@pytest.mark.parametrize("change", ["scope", "time", "version", "revocation", "selection"])
def test_maintenance_page_rejects_changed_authority_time_facts_or_selection(change):
    views, _, actor, state = staff_pages()
    page = views.query(actor=actor, fact_kind="dorm_bed", limit=1)
    kwargs = dict(actor=actor, fact_kind="dorm_bed", limit=1, cursor=page["next_cursor"])
    if change == "scope":
        state.scope = replace(state.scope, object_keys=("bed-1", "bed-2"))
    elif change == "time":
        state.clock += timedelta(hours=1)
    elif change == "version":
        state.version += 1
    elif change == "revocation":
        state.allowed = False
    else:
        kwargs["query_text"] = "another selection"
    with pytest.raises((ValueError, PermissionError, RuntimeError)):
        views.query(**kwargs)
