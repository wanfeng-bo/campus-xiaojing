"""Owned DB receipt/difference/Guardian readback integration; not a Product duty test."""
# ruff: noqa: E501 - fixed parameterized SQL statements

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_family_correction_writer import (
    ExactCorrectionReceiptVerifier,
    FamilyCorrectionReferenceCodec,
    SqlAppliedFieldDifferenceReader,
    SqlFamilyCorrectionRepository,
    SqlGuardianCorrectionAuthority,
)
from app.domains.students.family_corrections import FamilyCorrectionDenied
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.school_facts.test_f3_correction_postgres import bound, guardian
from tests.school_facts.test_f3_family_postgres import KEY
from tests.school_facts.test_f5_writer_reader_postgres import writer  # noqa: F401


@pytest.mark.parametrize(
    "kind,field", [("person", "display_name"), ("teaching_assignment", "teaching_role")]
)
def test_real_receipt_exact_difference_report_readback_and_guardian_loss(writer, kind, field):  # noqa: F811
    engines, staff, identity, context, service = writer
    parent_source = "person:f3-guardian"
    if kind == "teaching_assignment":
        with engines["migration"].connect() as c:
            parents = c.execute(
                text(
                    "SELECT p.source_key,g.school_id,g.guardian_person_id,g.id,g.version FROM guardian_relationships g JOIN school_people p ON p.id=g.guardian_person_id WHERE g.status='confirmed' AND g.relationship_kind='verified_guardian' AND g.revoked_at IS NULL AND g.authority_version=1 AND g.valid_from<=now() AND (g.valid_to IS NULL OR now()<g.valid_to)"
                )
            ).all()
        with engines["writer"].connect() as c:
            for candidate in parents:
                targets = c.execute(
                    text(
                        "SELECT * FROM app.school_fact_family_teaching_targets(:s,:a,:r,:v) WHERE fact_kind='teaching_assignment'"
                    ),
                    {
                        "s": candidate.school_id,
                        "a": candidate.guardian_person_id,
                        "r": candidate.id,
                        "v": candidate.version,
                    },
                ).all()
                if targets:
                    parent_source = candidate.source_key
                    break
            else:
                raise AssertionError("Owned fixture needs a current Guardian teaching target")
    parent, parent_session = guardian(engines, source=parent_source)
    with engines["migration"].connect() as c:
        leads = set(
            c.scalars(
                text(
                    "SELECT f.source_key FROM school_education_facts f JOIN school_education_versions v ON v.fact_id=f.id WHERE f.kind='teaching_assignment' AND v.superseded_at IS NULL AND v.valid_from<=now() AND (v.valid_to IS NULL OR now()<v.valid_to) AND v.payload->>'teaching_role'='lead'"
                )
            )
        )
        codes = tuple(
            c.scalars(
                text(
                    "SELECT DISTINCT semantic_role_code FROM staff_assignment_versions WHERE status='confirmed' AND revoked_at IS NULL AND semantic_role_code IS NOT NULL"
                )
            )
        )
    policy = FamilyLeadershipPolicy(
        school_id=staff.school_id,
        publication_reference="owned-formal-role-fixture",
        version=1,
        school_role_codes=codes,
        campus_role_codes=(),
    )
    codec = FamilyCorrectionReferenceCodec(KEY)

    def authority(s):
        return SqlGuardianCorrectionAuthority(
            session=s, codec=codec, leadership_policy=lambda: policy, f5_enabled=True
        )

    with bound(engines, parent, parent_session) as s:
        auth = authority(s)
        options = auth.options(parent)
        option = next(
            o
            for o in options
            if codec.decode(actor=parent, reference=o.reference)[0] == kind
            and o.field_name == field
            and (
                kind != "teaching_assignment"
                or codec.decode(actor=parent, reference=o.reference)[1] in leads
            )
        )
        permit = auth.require_submit(
            actor=parent,
            target_reference=option.reference,
            field_name=field,
            checked_at=datetime.now(UTC),
        )
        report = SqlFamilyCorrectionRepository(s).submit(
            permit=permit, statement="请核对正式合成资料", idempotency_key=uuid4().hex
        )
        auth.revalidate(permit, checked_at=datetime.now(UTC))
    source = permit.source_key
    with engines["migration"].connect() as c:
        base = c.scalar(
            text(
                "SELECT COALESCE(MAX(target_fact_version),1) FROM master_data_import_batches WHERE status='applied'"
            )
        )
        if kind == "teaching_assignment":
            content = dict(
                c.scalar(
                    text(
                        "SELECT v.payload FROM school_education_versions v JOIN school_education_facts f ON f.id=v.fact_id WHERE f.source_key=:k AND v.superseded_at IS NULL AND v.valid_from<=now() AND (v.valid_to IS NULL OR now()<v.valid_to)"
                    ),
                    {"k": source},
                )
            )
            content[field] = "co_teacher"
        else:
            content = dict(
                c.execute(
                    text(
                        "SELECT v.person_kind,v.lifecycle_status FROM school_person_profile_versions v JOIN school_people p ON p.id=v.person_id WHERE p.source_key=:k AND v.valid_from<=now() AND (v.valid_to IS NULL OR now()<v.valid_to)"
                    ),
                    {"k": source},
                )
                .mappings()
                .one()
            )
            content["display_name"] = "合成领导核对" + uuid4().hex[:8]
    plan = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "base_version": base,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": [
                    {
                        "schema_version": 3,
                        "fact_kind": kind,
                        "source_key": source,
                        "operation": "upsert",
                        "expected_version": permit.target_version,
                        "effective_at": datetime.now(UTC) - timedelta(milliseconds=1),
                        "origin": {"sheet_code": "education", "row_number": 1},
                        "content": content,
                    }
                ],
            },
            default=str,
        )
    )
    locator = sha256(uuid4().bytes).hexdigest()
    provenance = SchoolFactChangeProvenance(
        channel="admin",
        raw_source_sha256=plan.source_sha256,
        template_version="school_facts_v3",
        mapping_version="admin_v1",
        mapping_config_sha256="c" * 64,
        source_size_bytes=None,
        worksheet_names=(),
        result_locator_sha256=locator,
    )
    preview = service.prepare_confirmation(
        change_set=plan,
        context=context,
        session_binding=identity.session_nonce_hash,
        provenance=provenance,
    )
    result = service.confirm(
        credential=preview.credential,
        change_set=plan,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert (
        service.confirm(
            credential=preview.credential,
            change_set=plan,
            context=context,
            session_binding=identity.session_nonce_hash,
        ).batch_id
        == result.batch_id
    )

    class Own:
        def check(self, *, locator_sha256):
            return service.check_own_result_by_locator(
                context=context,
                allowed_targets=frozenset({(kind, source)}),
                locator_sha256=locator_sha256,
            )

    assert (
        service.check_own_result_by_locator(
            context=context,
            allowed_targets=frozenset({(kind, "not-this-target")}),
            locator_sha256=locator,
        )
        is None
    )

    with bound(engines, staff, identity) as s:
        verifier = ExactCorrectionReceiptVerifier(
            own_results=Own(),
            differences=SqlAppliedFieldDifferenceReader(session=s, school_id=staff.school_id),
        )
        if kind == "teaching_assignment":
            assert not SqlAppliedFieldDifferenceReader(
                session=s, school_id=staff.school_id
            ).changed(
                fact_kind=kind,
                source_key=source,
                target_version=permit.target_version + 1,
                field_name="assignment_source_key",
            )
        assert (
            verifier.require_applied_target(
                actor=staff,
                locator_sha256=locator,
                fact_kind=kind,
                source_key=source,
                field_name=field,
                minimum_target_version=permit.target_version,
                submitted_at=report.submitted_at,
            )
            == "maintain"
        )
        with pytest.raises(FamilyCorrectionDenied):
            verifier.require_applied_target(
                actor=staff,
                locator_sha256=locator,
                fact_kind=kind,
                source_key=source,
                field_name=field,
                minimum_target_version=permit.target_version + 1,
                submitted_at=report.submitted_at,
            )
        assert (
            SqlFamilyCorrectionRepository(s)
            .resolve(
                report_id=report.report_id, locator_sha256=locator, resolved_at=datetime.now(UTC)
            )
            .status
            == "resolved"
        )
    with bound(engines, parent, parent_session) as s:
        authority(s).require_current(parent, checked_at=datetime.now(UTC))
        assert (
            SqlFamilyCorrectionRepository(s).own(actor=parent, report_id=report.report_id).status
            == "resolved"
        )
    # Revoke only this owned prerequisite and restore its exact prior status in finally.
    # No production relationship or authority grant is touched.
    with engines["migration"].begin() as c:
        old_status = c.scalar(
            text("SELECT status FROM guardian_relationships WHERE id=:id"),
            {"id": parent.guardian_relationship_id},
        )
        c.execute(
            text("UPDATE guardian_relationships SET status='revoked' WHERE id=:id"),
            {"id": parent.guardian_relationship_id},
        )
    try:
        with pytest.raises((FamilyCorrectionDenied, DBAPIError)):
            with bound(engines, parent, parent_session) as s:
                authority(s).require_current(parent, checked_at=datetime.now(UTC))
    finally:
        with engines["migration"].begin() as c:
            c.execute(
                text("UPDATE guardian_relationships SET status=:status WHERE id=:id"),
                {"id": parent.guardian_relationship_id, "status": old_status},
            )
    with bound(engines, parent, parent_session) as s:
        authority(s).require_current(parent, checked_at=datetime.now(UTC))
        assert (
            SqlFamilyCorrectionRepository(s).own(actor=parent, report_id=report.report_id).status
            == "resolved"
        )
