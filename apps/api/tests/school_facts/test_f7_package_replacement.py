"""Explicit selected-set replacement, never file omission outside the selection."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.platform.master_data_imports.school_facts_package import FullScopeInputRejected
from app.platform.master_data_imports.school_facts_replacement import (
    ReplacementFactSnapshot,
    prepare_selected_replacement,
)
from tests.school_facts.test_f7_full_scope_import import AT, change, manifest, part


def selected(keys=("person:a", "person:b")):
    return manifest(
        mode="replace",
        scopes=[
            {
                "fact_kind": "person",
                "source_keys": keys,
                "effective_at": AT.isoformat(),
            }
        ],
    )


def snapshot(key, *, version=1, state="active", base=6):
    return ReplacementFactSnapshot(
        fact_kind="person",
        source_key=key,
        effective_at=AT,
        expected_version=version,
        school_fact_version=base,
        state=state,
        fields={"display_name": key},
        revalidate=lambda: None,
    )


def prepare(*, snapshots=None, incoming=None, keys=("person:a", "person:b")):
    captured = []
    snapshots = snapshots or {key: snapshot(key) for key in keys}

    def read(kind, key, at):
        captured.append((kind, key, at))
        return snapshots[key]

    result = prepare_selected_replacement(
        manifest=selected(keys),
        parts=(part(changes=(change(),) if incoming is None else incoming),),
        read_snapshot=read,
        prepare_end=lambda old: (change(key=old.source_key),),
    )
    return result, captured


def test_only_selected_omitted_active_target_becomes_explicit_end():
    result, called = prepare()
    assert [c.source_key for c in result.generated_changes] == ["person:b"]
    assert [t.source_key for t in result.targets] == ["person:a", "person:b"]
    assert called == [("person", k, AT) for k in ("person:a", "person:b")]
    assert result.preview[1]["before"] == {"display_name": "person:b"}
    assert result.preview[1]["selection_action"] == "end"


@pytest.mark.parametrize("state,version", [("missing", 0), ("ended", 2), ("future", 1)])
def test_omitted_missing_ended_and_future_records_are_not_ended(state, version):
    result, _ = prepare(
        snapshots={
            "person:a": snapshot("person:a"),
            "person:b": snapshot("person:b", state=state, version=version),
        }
    )
    assert result.generated_changes == ()
    assert result.targets[1].state == state


def test_input_outside_selection_is_rejected_before_reading():
    with pytest.raises(FullScopeInputRejected, match="REPLACEMENT_INPUT_OUTSIDE_SELECTION"):
        prepare(incoming=(change(key="person:outside"),))


@pytest.mark.parametrize(
    "mutation",
    [
        dict(school_fact_version=7),
        dict(expected_version=2),
        dict(effective_at=AT + timedelta(days=1)),
        dict(source_key="person:outside"),
    ],
)
def test_stale_wrong_coordinate_or_version_snapshot_rejects_whole_batch(mutation):
    with pytest.raises(FullScopeInputRejected):
        prepare(
            snapshots={
                "person:a": replace(snapshot("person:a"), **mutation),
                "person:b": snapshot("person:b"),
            }
        )


def test_revocation_during_collection_rejects_and_returns_no_preview():
    def denied():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        prepare(
            snapshots={
                "person:a": snapshot("person:a"),
                "person:b": replace(snapshot("person:b"), revalidate=denied),
            }
        )


def test_changed_linked_impact_cannot_override_explicit_input():
    with pytest.raises(FullScopeInputRejected, match="REPLACEMENT_IMPACT_OVERLAP"):
        prepare_selected_replacement(
            manifest=selected(),
            parts=(part(),),
            read_snapshot=lambda kind, key, at: snapshot(key),
            prepare_end=lambda old: (change(key=old.source_key), change()),
        )


def test_snapshot_survives_writer_transport_and_is_bound_to_plan_digest():
    from hashlib import sha256

    from app.platform.master_data_imports.school_facts_package import FullScopePackageBuilder
    from app.platform.school_facts.executor_contracts import SchoolFactExecutorProvenance
    from app.platform.school_facts.package_integrity import require_full_scope_provenance_integrity
    from app.platform.school_facts.ports import SchoolFactChangeProvenance

    result, _ = prepare()
    built = FullScopePackageBuilder().build(
        manifest=selected(),
        parts=(part(),),
        replacement_targets=result.targets,
        generated_changes=result.generated_changes,
    )
    provenance = SchoolFactChangeProvenance(
        channel="package",
        raw_source_sha256=built.raw_source_sha256,
        template_version=built.input_version,
        mapping_version="f7_v1",
        mapping_config_sha256=sha256(b"f7").hexdigest(),
        source_size_bytes=sum(s.size_bytes for s in built.source_manifest),
        worksheet_names=(),
        source_manifest=built.source_manifest,
        row_lineage=built.row_lineage,
        replacement_targets=built.replacement_targets,
    )
    transported = SchoolFactExecutorProvenance.model_validate_json(
        SchoolFactExecutorProvenance.from_domain(provenance).model_dump_json()
    ).to_domain()
    require_full_scope_provenance_integrity(transported, built.change_set)
    with pytest.raises(ValueError, match="摘要"):
        require_full_scope_provenance_integrity(
            replace(transported, replacement_targets=transported.replacement_targets[:1]),
            built.change_set,
        )
    assert built.row_lineage[-1].source_slot.startswith("f7_replacement_")
    assert built.change_set.changes[-1].source_key == "person:b"


@pytest.mark.parametrize("actual_version", [None, 2])
def test_writer_rejects_removed_or_changed_selection_before_any_apply(actual_version):
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from tests.school_facts.test_sf3_http import _context

    checked = []
    writer = SqlAlchemySchoolFactPersistence(
        session_factory=None,
        envelope_factory=None,
        package_final_authorization_guard=SimpleNamespace(
            require_replacement=lambda context, targets: checked.append("authorized")
        ),
    )
    session = SimpleNamespace(
        execute=lambda *args: SimpleNamespace(
            all=lambda: [] if actual_version is None else [("person:a", actual_version)]
        )
    )
    batch = SimpleNamespace(
        source_kind="package",
        mapping_payload={
            "replacement_targets": [snapshot("person:a").target().model_dump(mode="json")]
        },
    )
    with pytest.raises(SchoolFactChangeRejected, match="新增、移除或改变"):
        writer._revalidate_package_replacement(
            session, batch, _context().actor.school_id, _context()
        )
    assert checked == ["authorized"]


def test_writer_checks_selected_read_authority_before_querying_metadata():
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from tests.school_facts.test_sf3_http import _context

    def denied(*_):
        raise PermissionError("selected read revoked")

    writer = SqlAlchemySchoolFactPersistence(
        session_factory=None,
        envelope_factory=None,
        package_final_authorization_guard=SimpleNamespace(require_replacement=denied),
    )
    session = SimpleNamespace(execute=lambda *_: pytest.fail("no lookup before authorization"))
    batch = SimpleNamespace(
        source_kind="package",
        mapping_payload={
            "replacement_targets": [snapshot("person:a").target().model_dump(mode="json")]
        },
    )
    with pytest.raises(PermissionError, match="revoked"):
        writer._revalidate_package_replacement(
            session, batch, _context().actor.school_id, _context()
        )


def test_32_user_sources_leave_separate_capacity_for_derived_replacement_audit():
    from hashlib import sha256

    from app.platform.master_data_imports.school_facts_package import FullScopePackageBuilder
    from app.platform.school_facts.executor_contracts import SchoolFactExecutorProvenance
    from app.platform.school_facts.package_integrity import require_full_scope_provenance_integrity
    from app.platform.school_facts.ports import SchoolFactChangeProvenance

    sources = [
        {**manifest().sources[0].model_dump(mode="json"), "logical_source_code": f"input_{i}"}
        for i in range(32)
    ]
    # The user still supplies only 32 files. Ending a selected omitted record
    # creates a server audit artifact, not a 33rd uploaded file.
    request = manifest(sources=sources, mode="replace", scopes=[{
        "fact_kind": "person", "source_keys": ["person:a", "person:b"],
        "effective_at": AT,
    }])
    result, _ = prepare()
    built = FullScopePackageBuilder().build(
        manifest=request,
        parts=tuple(part(f"input_{i}", changes=(change(),) if i == 0 else ())
                    for i in range(32)),
        replacement_targets=result.targets, generated_changes=result.generated_changes,
    )
    assert len(built.source_manifest) == 33
    provenance = SchoolFactChangeProvenance(
        channel="package", raw_source_sha256=built.raw_source_sha256,
        template_version=built.input_version, mapping_version="f7_v1",
        mapping_config_sha256=sha256(b"f7").hexdigest(),
        source_size_bytes=sum(s.size_bytes for s in built.source_manifest), worksheet_names=(),
        source_manifest=built.source_manifest, row_lineage=built.row_lineage,
        replacement_targets=built.replacement_targets,
    )
    restored = SchoolFactExecutorProvenance.model_validate_json(
        SchoolFactExecutorProvenance.from_domain(provenance).model_dump_json()
    ).to_domain()
    require_full_scope_provenance_integrity(restored, built.change_set)


def test_derived_audit_cannot_bypass_user_source_or_internal_artifact_limits():
    from app.platform.school_facts.ports import SchoolFactChangeProvenance, SchoolFactSourceArtifact

    base = dict(
        channel="package", raw_source_sha256="a" * 64, template_version="school_facts_v5",
        mapping_version="f7_v1", mapping_config_sha256="b" * 64, worksheet_names=(),
        row_lineage=part().row_lineage, replacement_targets=(snapshot("person:a").target(),),
    )
    for codes in ([f"input_{i}" for i in range(33)],
                  ["source_a", "f7_replacement_6"],
                  ["source_a", "f7_replacement_1"]):
        # Last case is an unreferenced fake internal artifact.
        artifacts = tuple(SchoolFactSourceArtifact(code, "a" * 64, 1) for code in codes)
        with pytest.raises(ValueError):
            SchoolFactChangeProvenance(**base, source_manifest=artifacts,
                                      source_size_bytes=len(artifacts))


def test_full_10_mib_user_bytes_do_not_consume_internal_audit_capacity():
    from hashlib import sha256

    from app.platform.master_data_imports.school_facts_package import FullScopePackageBuilder

    raw = b"a" * (10 * 1024 * 1024)
    request = manifest(sources=[{
        **manifest().sources[0].model_dump(mode="json"),
        "source_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw),
    }], mode="replace", scopes=[{
        "fact_kind": "person", "source_keys": ["person:a", "person:b"],
        "effective_at": AT,
    }])
    result, _ = prepare()
    built = FullScopePackageBuilder().build(
        manifest=request, parts=(part(raw=raw),), replacement_targets=result.targets,
        generated_changes=result.generated_changes,
    )
    assert built.source_manifest[0].size_bytes == len(raw)
    assert 0 < built.source_manifest[1].size_bytes <= 1024 * 1024


def test_derived_audit_is_bounded_by_50000_changes_in_five_chunks():
    from app.platform.master_data_imports.school_facts_package import FullScopePackageBuilder

    keys = tuple(f"person:{i}" for i in range(50_000))
    request = selected(keys)
    built = FullScopePackageBuilder().build(
        manifest=request, parts=(part(changes=()),),
        replacement_targets=tuple(snapshot(key).target() for key in keys),
        generated_changes=tuple(change(key=key) for key in keys),
    )
    assert len(built.change_set.changes) == 50_000
    assert len(built.source_manifest) == 6
    assert all(s.size_bytes <= 1024 * 1024 for s in built.source_manifest[1:])
    with pytest.raises(FullScopeInputRejected, match="CHANGE_COUNT_EXCEEDED"):
        FullScopePackageBuilder().build(
            manifest=request, parts=(part(changes=()),),
            replacement_targets=built.replacement_targets,
            generated_changes=(*built.change_set.changes, change(key="person:extra")),
        )
