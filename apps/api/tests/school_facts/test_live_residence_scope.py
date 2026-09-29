"""School coverage still uses bounded projections and current scoped authority."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.composition.school_fact_residence_authority import ResidenceAuthority
from app.composition.school_fact_residence_reader import ResidenceRepository
from tests.test_f1_official_contact_query import _actor
from tests.test_f3_family_read import NOW


def school_scope():
    return SimpleNamespace(
        is_school_wide=True,
        object_keys=(),
        creation_keys=(),
        readable_keys=(),
        field_names=("display_name",),
        fact_kind="dorm_bed",
        document_sha256="school-scope-test",
        allows_key=lambda key, **kw: isinstance(key, str) and bool(key) and "*" not in key,
    )


def test_school_authority_accepts_existing_and_creation_but_never_read_creation():
    scope = school_scope()
    for creation in (False, True):
        ResidenceAuthority.require_object(
            SimpleNamespace(scope=scope, action="maintain"), "new-bed", is_creation=creation
        )
    with pytest.raises(PermissionError):
        ResidenceAuthority.require_object(
            SimpleNamespace(scope=scope, action="read"), "new-bed", is_creation=True
        )


def repository(*, key_count=205):
    actor, scope = _actor(), school_scope()
    state = SimpleNamespace(version=8, active=True, pages=0, read_sizes=[], switch=False)
    keys = tuple(sorted(f"bed-{i:04}" for i in range(key_count)))

    def revalidate(*args, **kwargs):
        if not state.active:
            raise PermissionError("revoked")

    def scope_keys(**kwargs):
        state.pages += 1
        chosen = tuple(k for k in keys if kwargs["after_key"] is None or k > kwargs["after_key"])
        page = chosen[: kwargs["limit"]]
        return dict(
            keys=list(page),
            has_more=len(chosen) > len(page),
            next_after=page[-1] if len(chosen) > len(page) else None,
            school_fact_version=state.version,
        )

    def read(**kwargs):
        selected = kwargs["source_keys"]
        state.read_sizes.append(len(selected))
        assert len(selected) <= 100
        assert kwargs["fields"] == ("display_name",)
        rows = [
            dict(source_key=k, fact_kind="dorm_bed", content={"display_name": k})
            for k in selected
            if kwargs["name"] is None or k == kwargs["name"]
        ]
        offset, limit = kwargs["offset"], kwargs["limit"]
        result = dict(
            records=rows[offset : offset + limit],
            has_more=offset + limit < len(rows),
            school_fact_version=state.version,
            status="available" if rows else "no_current_records",
            next_boundary=None,
        )
        if state.switch:
            state.version += 1
        return result

    authority = SimpleNamespace(
        require_scope=lambda *a, **kw: SimpleNamespace(scope=scope, action=kw["action"]),
        require_fields=ResidenceAuthority.require_fields,
        require_object=ResidenceAuthority.require_object,
        revalidate=revalidate,
    )
    repo = ResidenceRepository(
        authority=authority,
        projection=SimpleNamespace(
            scope_keys=scope_keys, read=read, current_version=lambda _: state.version
        ),
        reference_key=b"school-residence-reference-test-key",
        clock=lambda: NOW,
    )
    return repo, actor, state


def test_school_read_paginates_past_100_without_truncation_or_duplicates():
    repo, actor, state = repository()
    result = repo.read(actor, "dorm_bed", offset=95, limit=100)
    assert [r["source_key"] for r in result.records] == [f"bed-{i:04}" for i in range(95, 195)]
    assert result.has_more
    last = repo.read(actor, "dorm_bed", offset=195, limit=100)
    assert len(last.records) == 10 and not last.has_more
    assert state.pages >= 6 and max(state.read_sizes) <= 100


def test_school_read_filters_each_projection_before_return_and_checks_whole_version():
    repo, actor, state = repository()
    result = repo.read(actor, "dorm_bed", name="bed-0204", limit=1)
    assert [r["source_key"] for r in result.records] == ["bed-0204"]
    assert not result.has_more
    state.switch = True
    with pytest.raises((RuntimeError, ValueError)):
        repo.read(actor, "dorm_bed", limit=1)


def test_school_key_enumeration_does_not_precede_field_authorization():
    repo, actor, state = repository()
    with pytest.raises(PermissionError):
        repo.read(actor, "dorm_bed", fields=("student_name",))
    assert state.pages == 0 and state.read_sizes == []


def test_school_empty_is_valid_and_revoked_result_is_not_reusable():
    repo, actor, state = repository(key_count=0)
    result = repo.read(actor, "dorm_bed")
    assert not result.records and not result.has_more
    state.active = False
    with pytest.raises(PermissionError):
        result.proof.revalidate(actor=actor)


def test_school_signed_reference_survives_repository_recreation_and_never_becomes_audit_selector():
    repo, actor, _ = repository()
    ref = repo.reference_for(actor, "dorm_bed", "future-bed", creation=True)
    fresh, _, _ = repository()
    assert fresh.source_for_reference(actor, "dorm_bed", ref, creation=True) == "future-bed"
    with pytest.raises((ValueError, PermissionError)):
        fresh.source_for_reference(actor, "dorm_bed", ref)
    audit = repo.creation_receipt_reference_for(
        actor=actor, fact_kind="dorm_bed", source_key="future-bed"
    )
    with pytest.raises((ValueError, PermissionError)):
        fresh.source_for_reference(actor, "dorm_bed", audit, creation=True)


def test_school_quality_still_requires_an_independent_owner_read():
    from app.composition.school_fact_residence_quality import ResidenceQualityBridge

    repo, actor, _ = repository()
    called = []

    def owner(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(revalidate=lambda: None)

    bridge = ResidenceQualityBridge(
        f2_guard=None, residence_authority=repo.authority, projection=None, owner_authorizer=owner
    )
    repo.authority.clock = lambda: NOW
    scope = school_scope()
    scope.quality_owners = ()
    repo.authority.require_scope = lambda *a, **kw: SimpleNamespace(
        scope=scope, action=kw["action"]
    )
    bridge._target(
        actor,
        dict(
            target_fact_kind="dorm_bed",
            owner_kind="location",
            owner_source_key="location-one",
            field_name="display_name",
        ),
        "maintain",
    )
    assert called[0]["content"]["owner_source_key"] == "location-one"
    bridge.owner_authorizer = None
    with pytest.raises(PermissionError):
        bridge._target(
            actor,
            dict(
                target_fact_kind="dorm_bed",
                owner_kind="location",
                owner_source_key="location-one",
                field_name="display_name",
            ),
            "maintain",
        )


def test_school_creation_choice_is_signed_bounded_and_has_no_internal_key_label():
    repo, actor, _ = repository()
    (choice,) = repo.catalog_creation_references(actor, "dorm_bed")
    key = repo.source_for_reference(actor, "dorm_bed", choice["reference"], creation=True)
    assert key.startswith("live:dorm_bed:") and len(key) < 100
    assert key not in choice["label"]


def test_school_residence_batch_accepts_signed_existing_refs_but_not_creation_as_replacement():
    from app.composition.school_fact_residence_product import ResidenceBatchInput

    repo, actor, _ = repository()
    ref = repo.reference_for(actor, "student_bed_residence", "residence-one")
    payload = dict(
        fact_kind="student_bed_residence",
        operation="upsert",
        target_ref=ref,
        fields={},
        reason="明确完整替换",
        effective_at=NOW,
        import_mode="replace",
        import_rows=(),
        import_replacement_refs=(ref,),
    )
    assert ResidenceBatchInput.model_validate(payload).import_replacement_refs == (ref,)
    creation = repo.reference_for(actor, "student_bed_residence", "new-one", creation=True)
    with pytest.raises(ValueError):
        ResidenceBatchInput.model_validate({**payload, "import_replacement_refs": (creation,)})


def test_school_student_impact_uses_complete_student_keys_and_both_grants():
    repo, actor, _ = repository()
    scope = school_scope()
    scope.fact_kind = "student_bed_residence"
    scope.field_names = ("student_source_key", "bed_source_key", "effective_at", "valid_to")
    repo.authority.require_scope = lambda *a, **kw: SimpleNamespace(
        scope=scope, action=kw["action"]
    )
    repo.projection.student_impact_keys = lambda **kw: dict(keys=["one"], school_fact_version=8)
    repo.projection.read_student_impacts = lambda **kw: dict(
        school_fact_version=8,
        next_boundary=None,
        records=[
            dict(
                source_key="one",
                fact_kind="student_bed_residence",
                student_source_key="student-one",
            )
        ],
    )
    result = repo.read_student_impacts(actor, "student-one")
    assert len(result.records) == 1
    repo.projection.student_impact_keys = lambda **kw: dict(keys=["one"], school_fact_version=9)
    with pytest.raises(ValueError):
        repo.read_student_impacts(actor, "student-one")


def test_long_legal_source_reference_roundtrips_through_f6_inputs():
    from app.composition.school_fact_residence_gateway import ResidenceQueryInput
    from app.composition.school_fact_residence_views import ResidenceRecordsInput

    repo, actor, _ = repository()
    key = "床" * 255
    reference = repo.reference_for(actor, "dorm_bed", key)
    assert len(reference) > 255
    assert ResidenceQueryInput(fact_kind="dorm_bed", reference=reference).reference == reference
    parsed = ResidenceRecordsInput(fact_kind="dorm_bed", reference=reference, related_ref=reference)
    assert repo.source_for_reference(actor, "dorm_bed", parsed.reference) == key
    with pytest.raises(ValueError):
        repo.source_for_reference(
            actor, "dorm_bed", reference[:-1] + ("0" if reference[-1] != "0" else "1")
        )


def test_school_read_retains_earliest_boundary_across_all_key_pages():
    repo, actor, _ = repository()
    original = repo.projection.read

    def read(**kwargs):
        value = original(**kwargs)
        seconds = 1 if "bed-0000" in kwargs["source_keys"] else 3600
        value["next_boundary"] = (NOW + timedelta(seconds=seconds)).isoformat()
        return value

    repo.projection.read = read
    result = repo.read(actor, "dorm_bed", offset=200, limit=5)
    assert result.proof.next_boundary == NOW + timedelta(seconds=1)
    repo.clock = lambda: NOW + timedelta(seconds=1)
    with pytest.raises(RuntimeError):
        result.proof.revalidate(actor=actor)


def test_school_quality_page_finds_future_added_keys_without_changing_exact_scopes():
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.school_facts.test_f6_quality_product import fixture

    adapter, bridge, actor = fixture()
    scope = F2FactScope(actor.school_id, "fact_quality", (), coverage="school")
    permit = SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))
    bridge.f2_guard = SimpleNamespace(
        require_current=lambda *a, **kw: permit, revalidate=lambda *a: bridge.revalidate()
    )
    keys = [f"quality-{i:03}" for i in range(105)]

    def scope_keys(**kwargs):
        selected = [key for key in keys if kwargs["after_key"] is None or key > kwargs["after_key"]]
        more = len(selected) > kwargs["limit"]
        selected = selected[: kwargs["limit"]]
        return dict(
            keys=selected,
            has_more=more,
            next_after=selected[-1] if more else None,
            school_fact_version=11,
        )

    bridge.projection.scope_keys = scope_keys
    bridge.coordinates = lambda *a: ({"content": {"target_fact_kind": "dorm_bed"}}, permit)
    bridge.read_content = lambda *a: dict(
        content=dict(quality_kind="source", target_fact_kind="dorm_bed"),
        proof=bridge,
        version=1,
        valid_from=NOW - timedelta(days=1),
        valid_to=None,
    )
    first = adapter.query(actor=actor, limit=100)
    assert first["returned_count"] == 100 and first["truncated"]
    last = adapter.query(actor=actor, limit=100, cursor=first["next_cursor"])
    assert last["returned_count"] == 5 and not last["truncated"]
    bridge.active = False
    with pytest.raises(PermissionError):
        adapter.query(actor=actor, limit=100)


def test_school_verified_cursor_can_continue_past_old_exact_offset_ceiling():
    from app.composition.school_fact_residence_query import ResidenceQueryService

    repo, actor, _ = repository(key_count=10105)
    query = ResidenceQueryService(
        repository=repo,
        current_actor=lambda a: a == actor,
        reference_key=b"school-residence-page-test-key",
    )
    selection = dict(
        kind="dorm_bed", reference=None, name=None, related_ref=None, at=None, limit=100
    )
    cursor = query.cursor_codec.encode(
        actor=actor,
        selection=selection,
        offset=10100,
        version=8,
        scope="school-scope-test",
        expires=NOW + timedelta(hours=1),
    )
    with pytest.raises(ValueError):
        query.cursor_codec.decode(
            actor=actor, cursor=cursor, selection=selection, version=8, now=NOW
        )
    result, next_cursor = query.read_page(
        actor=actor, fact_kind="dorm_bed", cursor=cursor, limit=100
    )
    assert len(result.records) == 5 and next_cursor is None
