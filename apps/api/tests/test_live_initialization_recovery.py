from datetime import timedelta
from uuid import UUID

import pytest

from tests.test_live_initialization import manifest_payload


def recovery():
    from app.composition.school_fact_initialization_recovery_contracts import (
        SchoolVersionRecoveryManifest,
    )
    from app.composition.school_fact_live_initialization_contracts import LiveInitializationManifest

    original = LiveInitializationManifest.model_validate(manifest_payload())
    original = original.model_copy(update={"publication_policy": "deferred"})
    now = original.approved_at + timedelta(hours=1)
    payload = dict(
        purpose="school-facts-missing-school-version-recovery",
        school_id=original.school_id,
        anchor_id=original.anchor_id,
        expected_system_identifier=original.expected_system_identifier,
        database_name=original.database_name,
        corp_id=original.corp_id,
        external_subject=original.external_subject,
        initialization_id=UUID(int=22),
        recovery_request_id=UUID(int=23),
        original_manifest_sha256=original.manifest_sha256,
        school_source_key="school:test",
        school_code="TEST",
        school_display_name="开源示例中学",
        timezone_name="Asia/Shanghai",
        approval_reference="recovery:approved",
        issuer_reference="reviewer:independent",
        issuer_qualification_reference="school:delegation",
        operator_reference="operator:deployment",
        source_authority_reference="school:opening",
        approved_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    return original, SchoolVersionRecoveryManifest.model_validate(payload)


def test_recovery_only_derives_one_original_time_school_change():
    original, manifest = recovery()
    changes = manifest.change_set(original)
    assert changes.base_version == 2
    assert len(changes.changes) == 1
    change = changes.changes[0]
    assert (change.fact_kind, change.operation, change.expected_version) == ("school", "upsert", 1)
    assert change.effective_at == original.approved_at
    assert change.content.display_name == manifest.school_display_name
    assert changes.source_sha256 == manifest.manifest_sha256


@pytest.mark.parametrize(
    "field,value",
    [
        ("school_id", UUID(int=99)),
        ("anchor_id", UUID(int=99)),
        ("corp_id", "different-corp"),
        ("external_subject", "different-subject"),
        ("original_manifest_sha256", "0" * 64),
    ],
)
def test_recovery_rejects_changed_original_binding(field, value):
    original, manifest = recovery()
    with pytest.raises(ValueError):
        manifest.model_copy(update={field: value}).change_set(original)


def test_recovery_never_reopens_original_window_and_new_window_is_finite():
    original, manifest = recovery()
    with pytest.raises(ValueError):
        original.require_current(manifest.approved_at)
    manifest.require_current(manifest.approved_at)
    with pytest.raises(ValueError):
        manifest.require_current(manifest.expires_at)
    with pytest.raises(ValueError):
        type(manifest).model_validate(
            dict(manifest.model_dump(), expires_at=manifest.approved_at + timedelta(minutes=16))
        )


def test_recovery_refuses_arbitrary_changes_and_required_publication():
    original, manifest = recovery()
    with pytest.raises(ValueError):
        manifest.change_set(original.model_copy(update={"publication_policy": "required"}))
    with pytest.raises(ValueError):
        type(manifest).model_validate(dict(manifest.model_dump(), first_facts={}))


def test_initialization_preflight_rejects_missing_school_slice_before_preparation():
    from app.composition.school_fact_initialization import SchoolFactInitializer
    from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRejected

    original, _ = recovery()

    class MissingSchool:
        def scalar(self, *args, **kwargs):
            return False

    initializer = SchoolFactInitializer(session_factory=None)
    with pytest.raises(SchoolFactInitializationRejected):
        initializer._require_school_version(MissingSchool(), original.initialization_request())


def test_initialization_existing_effective_school_still_allows_four_facts():
    from app.composition.school_fact_initialization import SchoolFactInitializer

    original, _ = recovery()

    class ExistingSchool:
        def scalar(self, *args, **kwargs):
            return True

    initializer = SchoolFactInitializer(session_factory=None)
    initializer._require_school_version(ExistingSchool(), original.initialization_request())
