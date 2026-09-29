"""Approved optional-data onboarding contracts; synthetic inputs only."""

from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.composition import school_fact_live_initialization_contracts as contracts
from tests.test_live_initialization import manifest_payload


def handoff_payload():
    raw = manifest_payload()
    keys = (
        "school_id",
        "anchor_id",
        "expected_system_identifier",
        "database_name",
        "corp_id",
        "external_subject",
        "approval_reference",
        "issuer_reference",
        "issuer_qualification_reference",
        "operator_reference",
        "source_authority_reference",
        "approved_at",
        "expires_at",
    )
    return dict(purpose="school-facts-live-session-handoff", **{k: raw[k] for k in keys})


def test_identity_handoff_does_not_require_a_person_name_or_business_facts():
    approval_type = getattr(contracts, "LiveSessionHandoffApproval", None)
    assert approval_type is not None, "identity-first approval contract is missing"
    approval = approval_type.model_validate(handoff_payload())
    assert "first_facts" not in approval.model_dump()
    approval.require_current(approval.approved_at)
    with pytest.raises(ValueError):
        approval.require_current(approval.expires_at)
    assert approval.corp_id == "test-corp"
    assert approval.external_subject == "test-first-operator"


@pytest.mark.parametrize(
    "field,value",
    [
        ("school_id", UUID(int=0)),
        ("anchor_id", UUID(int=0)),
        ("database_name", "campus_ai_identity_test"),
        ("corp_id", ""),
        ("external_subject", ""),
        ("duties", ["school_admin"]),
    ],
)
def test_identity_handoff_rejects_invalid_target_or_business_authority(field, value):
    approval_type = getattr(contracts, "LiveSessionHandoffApproval", None)
    assert approval_type is not None
    raw = handoff_payload()
    raw[field] = value
    with pytest.raises(ValidationError):
        approval_type.model_validate(raw)


def test_deferred_publication_is_bound_to_a_different_reviewed_initialization():
    original = contracts.LiveInitializationManifest.model_validate(manifest_payload())
    assert (
        original.manifest_sha256
        == "0208529ed0ee9360921ecb99728ca65d2b4b487458225cf7a60de7b1f8b17654"
    )
    assert (
        original.initialization_request().manifest_sha256()
        == "6d157e6a14d465e792113e5a79f7a3a4e7057e4f9880b020fc75cf59d74b4e23"
    )
    raw = manifest_payload()
    raw["publication_policy"] = "deferred"
    deferred = contracts.LiveInitializationManifest.model_validate(raw)
    assert deferred.manifest_sha256 != original.manifest_sha256
    assert (
        deferred.initialization_request().manifest_sha256()
        != original.initialization_request().manifest_sha256()
    )
    assert deferred.grant_valid_to - deferred.grant_valid_from == timedelta(days=90)
    raw["publication_policy"] = "ignore_existing_publication"
    with pytest.raises(ValidationError):
        contracts.LiveInitializationManifest.model_validate(raw)


def test_verified_display_name_is_a_candidate_not_an_additional_identity_credential():
    base = dict(
        token=SecretStr("synthetic-only-token"),
        corp_id="test-corp",
        external_subject="test-first-operator",
    )
    current = contracts.LiveInitializationSession(**base, display_name_candidate="钉钉测试教师")
    assert current.display_name_candidate == "钉钉测试教师"
    assert current.token == contracts.LiveInitializationSession(**base).token
    assert "synthetic-only-token" not in current.model_dump_json()
