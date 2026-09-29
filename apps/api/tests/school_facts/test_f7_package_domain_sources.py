"""Registered sources reuse owned import purposes, not client canonical commands."""

import json
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from tests.school_facts.test_f7_full_scope_import import AT, change, manifest


def declaration(raw, version, *, kind="student_bed_residence", options=None):
    return manifest(
        sources=[
            {
                **manifest().sources[0].model_dump(mode="json"),
                "source_sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "mapping_version": version,
                "mapping_config_sha256": sha256(version.encode()).hexdigest(),
                "fact_kinds": [kind],
                "domain_options": options,
            }
        ]
    ).sources[0]


def test_residence_source_replays_existing_import_with_current_actor_and_proof():
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter

    raw = json.dumps(
        {
            "input_version": "f7_residence_import_v1",
            "request": {
                "fact_kind": "student_bed_residence",
                "operation": "upsert",
                "target_ref": "f6:student_bed_residence:synthetic",
                "fields": {},
                "reason": "已批准合成维护",
                "effective_at": AT.isoformat(),
                "import_rows": [
                    {
                        "fact_kind": "student_bed_residence",
                        "operation": "end",
                        "target_ref": "f6:student_bed_residence:synthetic",
                        "fields": {},
                        "reason": "已批准结束",
                        "effective_at": AT.isoformat(),
                    }
                ],
            },
        }
    ).encode()
    calls = []
    canonical = change(kind="student_bed_residence", key="residence:synthetic", version=4)

    def resolve(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            change_set=SimpleNamespace(base_version=6, changes=(canonical,)),
            summary=SimpleNamespace(model_dump=lambda **_: {"label": "本批住宿维护"}),
            summary_revalidate=lambda actor: calls.append(actor),
        )

    adapter = PackageDomainSourceAdapter(residence_resolver=resolve)
    source = declaration(raw, "f7_residence_import_v1")
    result = adapter.parse(actor="actor-one", declared=source, raw=raw, base_version=6)
    assert calls[0]["actor"] == "actor-one"
    assert calls[0]["payload"].import_mode == "incremental"
    assert result.changes == (canonical,)
    result.domain_review.revalidate()
    assert calls[-1] == "actor-one"
    assert result.row_lineage[0].source_sheet_code == "residence_request"


def test_source_cannot_switch_import_purpose_or_infer_a_separate_replacement():
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter

    raw = json.dumps(
        {
            "input_version": "f7_residence_import_v1",
            "request": {
                "fact_kind": "student_bed_residence",
                "operation": "end",
                "target_ref": "f6:student_bed_residence:synthetic",
                "fields": {},
                "reason": "已批准合成维护",
                "effective_at": AT.isoformat(),
                "import_mode": "replace",
                "import_rows": [],
                "import_replacement_refs": ["f6:student_bed_residence:synthetic"],
            },
        }
    ).encode()
    adapter = PackageDomainSourceAdapter(residence_resolver=lambda **_: pytest.fail("must reject"))
    with pytest.raises(ValueError, match="SOURCE_REPLACEMENT_MUST_USE_PACKAGE_SCOPE"):
        adapter.parse(
            actor="actor",
            declared=declaration(raw, "f7_residence_import_v1"),
            raw=raw,
            base_version=6,
        )


def test_teaching_workbook_reuses_parser_and_owned_import_not_canonical_input():
    from io import BytesIO

    from openpyxl import Workbook

    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter

    book = Workbook()
    sheet = book.active
    sheet.title = "teaching"
    sheet.append(["source_key", "display_name"])
    sheet.append(["course:a", "跨学期综合课程"])
    output = BytesIO()
    book.save(output)
    raw = output.getvalue()
    captured = []
    c = change(kind="teaching_course", key="course:a", version=3)

    def resolve(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            change_set=SimpleNamespace(base_version=6, changes=(c,)),
            summary=SimpleNamespace(model_dump=lambda **_: {}),
            summary_revalidate=lambda _: None,
        )

    options = {
        "effective_at": AT.isoformat(),
        "target_refs": ["f4:teaching_course:known"],
        "reason": "已选择的合成课程批量维护",
    }
    parsed = PackageDomainSourceAdapter(education_resolver=resolve).parse(
        actor="actor",
        declared=declaration(raw, "f7_teaching_xlsx_v1", kind="teaching_course", options=options),
        raw=raw,
        base_version=6,
    )
    assert captured[0]["payload"].import_rows[0]["display_name"] == "跨学期综合课程"
    assert captured[0]["payload"].import_target_refs == tuple(options["target_refs"])
    assert parsed.row_lineage[0].source_row_number == 2


def test_plan_can_retain_only_short_lived_package_request_metadata():
    from app.composition.school_facts import InMemorySchoolFactPlanStore
    from app.platform.master_data_imports.ports import PrivateSourceOwner
    from app.platform.school_facts.contracts import PreflightReportV1
    from app.platform.school_facts.ports import SchoolFactChangeProvenance
    from tests.school_facts.test_sf3_http import NOW, SCHOOL, _change_set

    store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    owner = PrivateSourceOwner(str(SCHOOL), 9, "d" * 64)
    metadata = '{"sources":[{"source_ref":"opaque"}]}'
    provenance = SchoolFactChangeProvenance(
        channel="admin",
        raw_source_sha256="a" * 64,
        template_version="v1",
        mapping_version="v1",
        mapping_config_sha256="b" * 64,
        source_size_bytes=None,
        worksheet_names=(),
    )
    result = store.put(
        owner=owner,
        change_set=_change_set(),
        provenance=provenance,
        preflight=PreflightReportV1(plan_sha256="e" * 64, is_clean=True, issues=()),
        package_request_json=metadata,
    )
    assert store.get(result.plan_ref, owner=owner).package_request_json == metadata
    store.retire(result.plan_ref, owner=owner)
    assert not hasattr(store.locate(result.plan_ref, owner=owner), "package_request_json")


def test_real_shared_teaching_import_retains_complete_owned_impact():
    from io import BytesIO

    from openpyxl import Workbook

    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from tests.school_facts.test_f4_education_maintenance import environment, record

    records = [
        record("teaching_group", "group", organization_source_key="org", group_mode="shared")
    ]
    for suffix in ("a", "b"):
        records += [
            record(
                "offering_member_source",
                "source-" + suffix,
                offering_source_key="offer-" + suffix,
                source_kind="independent_group",
                group_source_key="group",
            ),
            record(
                "teaching_offering",
                "offer-" + suffix,
                course_source_key="course",
                offering_status="active",
            ),
        ]
    coordinator, actor = environment(records, school_version=6)
    book = Workbook()
    book.active.title = "teaching"
    book.active.append(["source_key", "operation"])
    book.active.append(["group", "end"])
    output = BytesIO()
    book.save(output)
    raw = output.getvalue()
    source = declaration(
        raw,
        "f7_teaching_xlsx_v1",
        kind="teaching_group",
        options={
            "fact_kind": "teaching_group",
            "effective_at": AT.isoformat(),
            "target_refs": ["f4:teaching_group:group"],
            "reason": "正式结束共享教学群体",
        },
    ).model_copy(
        update={"fact_kinds": ("teaching_group", "offering_member_source", "teaching_offering")}
    )
    parsed = PackageDomainSourceAdapter(education_resolver=coordinator.resolve_change).parse(
        actor=actor,
        declared=source,
        raw=raw,
        base_version=6,
    )
    assert len(parsed.changes) == 5
    assert len(parsed.domain_review.preview["impacts"]) == 5
    assert all(row.source_row_number == 2 for row in parsed.row_lineage)
    from app.platform.master_data_imports.school_facts_package import FullScopePackageBuilder
    from app.platform.master_data_imports.school_facts_replacement import ReplacementFactSnapshot
    from tests.school_facts.test_f7_package_preview import owned_education_provider

    built = FullScopePackageBuilder().build(
        manifest=manifest(sources=[source.model_dump(mode="json")]), parts=(parsed,)
    )
    provider = owned_education_provider(coordinator)

    def read(actor, kind, key, at):
        old = next(r for r in records if r["fact_kind"] == kind and r["source_key"] == key)
        return ReplacementFactSnapshot(
            kind,
            key,
            at,
            1,
            6,
            "active",
            old["segments"][0]["content"],
            lambda: None,
            segments=tuple(old["segments"]),
        )

    provider.read = read
    preview = provider.preview(actor, built.change_set, source_reviews=(parsed.domain_review,))
    assert preview.status == "complete", preview.issues
    assert len(preview.changes) == 5
    assert all(row["before_segments"] for row in preview.changes)
    assert all(row["after_segments"] or row["operation"] == "end" for row in preview.changes)


def test_teaching_workbook_blank_rows_keep_actual_source_coordinates():
    from io import BytesIO

    from openpyxl import Workbook

    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter

    book = Workbook()
    book.active.title = "teaching"
    book.active.append(["source_key", "operation"])
    book.active.append([None, None])
    book.active.append(["course:a", "end"])
    output = BytesIO()
    book.save(output)
    raw = output.getvalue()
    unit = change(kind="teaching_course", key="course:a", version=3)
    options = {
        "effective_at": AT.isoformat(),
        "target_refs": ["f4:teaching_course:course:a"],
        "reason": "明确结束合成课程",
    }
    adapter = PackageDomainSourceAdapter(
        education_resolver=lambda **_: SimpleNamespace(
            change_set=SimpleNamespace(base_version=6, changes=(unit,)),
            summary=SimpleNamespace(model_dump=lambda **_: {}),
            summary_revalidate=lambda _: None,
        )
    )
    result = adapter.parse(
        actor="actor",
        raw=raw,
        base_version=6,
        declared=declaration(raw, "f7_teaching_xlsx_v1", kind="teaching_course", options=options),
    )
    assert result.row_lineage[0].source_row_number == 3


def test_selected_form_uses_current_owned_references_and_server_manifest():
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter

    calls = []
    c = change(kind="person", key="person:verified", version=4)

    def resolve(**kw):
        calls.append(kw)
        assert kw["payload"].target_ref == "f2:person:current-authorized-reference"
        return SimpleNamespace(
            change_set=SimpleNamespace(base_version=6, changes=(c,)),
            summary=SimpleNamespace(model_dump=lambda **_: {"label": "老师甲"}),
            summary_revalidate=lambda actor: None,
        )

    adapter = PackageDomainSourceAdapter(form_resolvers={"directory": (FactChangeInput, resolve)})
    source, part = adapter.prepare_form(
        actor="actor",
        family="directory",
        slot="input_1",
        request={
            "fact_kind": "person",
            "operation": "end",
            "fields": {},
            "target_ref": "f2:person:current-authorized-reference",
            "reason": "正式结束",
        },
        effective_at=AT,
    )
    assert source.fact_kinds == ("person",)
    assert source.mapping_version == "f7_selected_form_v1"
    assert part.domain_review.base_version == 6
    raw = json.loads(part.original_bytes)
    assert datetime.fromisoformat(raw["request"]["effective_at"]) == AT
    assert "source_key" not in raw["request"]
    rebuilt = adapter.parse(actor="actor", declared=source, raw=part.original_bytes, base_version=6)
    assert rebuilt.changes == part.changes
    assert len(calls) == 2


@pytest.mark.parametrize("later_state", ["same", "changed", "source_expired", "revoked"])
def test_http_confirmation_recomputes_owned_import_and_rejects_changed_or_missing_source(
    later_state,
):
    import base64
    from dataclasses import replace
    from datetime import timedelta

    from app.composition.school_facts import InMemoryPrivateSchoolFactSourceStore
    from app.platform.master_data_imports.school_facts_package import (
        FullScopeSourcePart,
        parse_full_scope_fact_json_source,
    )
    from app.platform.master_data_imports.school_facts_replacement import (
        PackageMaintenancePreview,
        PreparedReplacementEnd,
    )
    from tests.school_facts.test_f7_package_http import _fact_source, _request
    from tests.school_facts.test_sf3_http import NOW, _http_client

    now, state, called = [NOW], ["same"], []
    store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: now[0])
    raw = _fact_source()

    # Transport fixture: domain builders are tested separately above. This exact
    # source delegates one already-supported change to isolate replay and expiry.
    def parse(**kwargs):
        called.append("source-recomputed")
        if state[0] == "revoked":
            raise PermissionError("revoked")
        from app.platform.master_data_imports.school_facts_package import (
            FULL_SCOPE_JSON_MAPPING_SHA256,
        )

        declared = kwargs["declared"].model_copy(
            update={
                "mapping_version": "school_facts_source_v5",
                "mapping_config_sha256": FULL_SCOPE_JSON_MAPPING_SHA256,
            }
        )
        parsed = parse_full_scope_fact_json_source(declared, kwargs["raw"])
        changes = parsed.changes
        if state[0] == "changed":
            changes = (changes[0].model_copy(update={"effective_at": NOW + timedelta(days=1)}),)
        return FullScopeSourcePart(
            parsed.logical_source_code,
            raw,
            changes,
            parsed.row_lineage,
            PreparedReplacementEnd(changes, {}, lambda: None, 1),
        )

    def preview(actor, change_set, *, source_reviews=()):
        assert source_reviews
        return PackageMaintenancePreview("complete", (), (), lambda: None)

    prefix = "/api/v1/school-fact-product/imports"
    with _http_client(
        enable_package=True,
        package_only=True,
        source_store=store,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
        package_source_adapter=SimpleNamespace(parse=parse),
        package_replacement=SimpleNamespace(preview=preview),
    ) as (client, ids, writer):
        ids["current"] = replace(
            ids["current"], authorized_actions=frozenset({"school_facts.package.submit"})
        )
        uploaded = client.post(
            prefix + "/sources",
            json={
                "filename_summary": "synthetic.json",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )
        request = _request(uploaded.json()["source_ref"], raw)
        request["manifest"]["sources"][0].update(
            mapping_version="f7_residence_import_v1",
            mapping_config_sha256=sha256(b"f7_residence_import_v1").hexdigest(),
        )
        response = client.post(prefix + "/plans/package", json=request)
        assert response.status_code == 200, response.text
        state[0] = later_state
        if later_state == "source_expired":
            now[0] += timedelta(hours=25)
        result = client.post(
            prefix + "/plans/" + response.json()["plan_ref"] + "/confirmation",
            headers={"Idempotency-Key": "same-owned-import"},
        )
        if later_state == "same":
            assert result.status_code == 200, result.text
            assert len(writer.prepared) == 1
            assert len(called) == 2
        else:
            assert result.status_code in {403, 404, 422}, result.text
            assert not writer.prepared


def test_selected_form_http_builds_manifest_without_client_internal_fields():
    from dataclasses import replace

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from app.platform.master_data_imports.school_facts_replacement import PackageMaintenancePreview
    from tests.school_facts.test_sf3_http import _http_client

    calls = []
    item = change(kind="person", key="person:verified", version=4)

    def resolve(**kw):
        calls.append(kw["payload"])
        return SimpleNamespace(
            change_set=SimpleNamespace(base_version=1, changes=(item,)),
            summary=SimpleNamespace(model_dump=lambda **_: {}),
            summary_revalidate=lambda _: None,
        )

    adapter = PackageDomainSourceAdapter(form_resolvers={"directory": (FactChangeInput, resolve)})
    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
        package_source_adapter=adapter,
        package_replacement=SimpleNamespace(
            preview=lambda *a, **k: PackageMaintenancePreview("complete", (), (), lambda: None)
        ),
    ) as (client, ids, writer):
        ids["current"] = replace(
            ids["current"], authorized_actions=frozenset({"school_facts.package.submit"})
        )
        response = client.post(
            "/api/v1/school-fact-product/imports/plans/selected",
            json={
                "items": [
                    {
                        "family": "directory",
                        "request": {
                            "fact_kind": "person",
                            "operation": "end",
                            "fields": {},
                            "target_ref": "f2:person:current-authorized-reference",
                            "reason": "confirmed correction",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["changes"][0]["source_key"] == "person:verified"
        assert len(calls) == 2
        assert not writer.prepared
        invalid = client.post(
            "/api/v1/school-fact-product/imports/plans/selected",
            json={"school_id": str(ids["current"].actor.school_id), "items": []},
        )
        assert invalid.status_code == 422


def test_selected_residence_replace_reuses_current_future_and_history_owner_rules():
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from app.composition.school_fact_residence_product import ResidenceBatchInput
    from tests.school_facts.test_f6_product_maintenance import (
        replacement_environment,
        replacement_payload,
    )

    owned, state = replacement_environment()
    adapter = PackageDomainSourceAdapter(
        form_resolvers={"residence": (ResidenceBatchInput, owned.resolve_change)}
    )
    source, part = adapter.prepare_form(
        actor=object(),
        family="residence",
        slot="input_1",
        request=replacement_payload().model_dump(mode="json"),
        effective_at=state.now,
    )
    assert [(c.source_key, c.operation) for c in part.changes] == [
        ("current", "end"),
        ("future", "revoke"),
    ]
    assert all(c.source_key != "past" for c in part.changes)
    replay = adapter.parse(
        actor=object(),
        declared=source,
        raw=part.original_bytes,
        base_version=part.domain_review.base_version,
    )
    assert replay.changes == part.changes
    state.allowed = False
    with pytest.raises(PermissionError):
        part.domain_review.revalidate()


def test_selected_teaching_table_auto_references_then_replays_same_owned_change():
    from app.composition.school_fact_education_product import EducationBatchInput
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from tests.school_facts.test_f4_education_maintenance import environment, record

    coordinator, actor = environment(
        [record("teaching_course", "course:one", display_name="Original")], school_version=6
    )
    reference = "f4:teaching_course:course:one"
    adapter = PackageDomainSourceAdapter(
        form_resolvers={"education": (EducationBatchInput, coordinator.resolve_change)}
    )
    source, part = adapter.prepare_form(
        actor=actor,
        family="education",
        slot="input_1",
        effective_at=AT,
        request={
            "fact_kind": "teaching_course",
            "target_ref": reference,
            "fields": {},
            "reason": "verified name",
            "import_rows": [{"reference": reference, "display_name": "Corrected"}],
            "import_target_refs": [reference],
        },
    )
    assert part.changes[0].content.display_name == "Corrected"
    assert part.changes[0].source_key == "course:one"
    assert (
        adapter.parse(actor=actor, declared=source, raw=part.original_bytes, base_version=6).changes
        == part.changes
    )


def test_selected_real_directory_adapter_revalidates_old_values_without_optional_proof():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_f2_query_repository import F2MaintenanceTarget, F2PublicFact
    from app.composition.school_fact_package_sources import PackageDomainSourceAdapter
    from app.platform.master_data_imports.school_facts_package import FullScopeInputRejected

    state = {"allowed": True, "version": 4}

    def target(**kw):
        if not state["allowed"]:
            raise PermissionError("revoked")
        assert kw["target_ref"] == "f2:person:verified"
        content = {"display_name": "Example", "person_kind": "other", "lifecycle_status": "active"}
        row = F2PublicFact(
            kw["target_ref"],
            "person",
            content,
            state["version"],
            AT,
            None,
            "manual",
            "published",
            AT,
        )
        return F2MaintenanceTarget("person:verified", state["version"], 6, content, row)

    owned = DirectoryProductAdapter(
        SimpleNamespace(prepare_target=target, resolve_field_references=lambda **k: k["fields"])
    )
    adapter = PackageDomainSourceAdapter(
        form_resolvers={"directory": (FactChangeInput, owned.resolve_change)}
    )
    source, part = adapter.prepare_form(
        actor=object(),
        family="directory",
        slot="input_1",
        effective_at=AT,
        request={
            "fact_kind": "person",
            "target_ref": "f2:person:verified",
            "fields": {"display_name": "Correct"},
            "reason": "verified",
        },
    )
    assert part.changes[0].content.display_name == "Correct"
    part.domain_review.revalidate()
    state["version"] = 5
    with pytest.raises(FullScopeInputRejected, match="SOURCE_DOMAIN_SNAPSHOT_CHANGED"):
        part.domain_review.revalidate()
    state["allowed"] = False
    with pytest.raises(PermissionError):
        part.domain_review.revalidate()
