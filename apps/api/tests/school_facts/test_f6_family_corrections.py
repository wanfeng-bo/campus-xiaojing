from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.composition.school_fact_family_correction_product import PublicCorrectionOption
from app.composition.school_fact_family_correction_writer import (
    ExactCorrectionReceiptVerifier,
    SqlAppliedFieldDifferenceReader,
    StaffCorrectionAuthorityAdapter,
)
from app.domains.students.family_corrections import FamilyCorrectionDenied


@pytest.mark.parametrize("field", ["bed_source_key", "room_source_key", "effective_at"])
def test_f6_public_report_fields_have_finite_contract(field):
    assert (
        PublicCorrectionOption(
            field_name=field, label="孩子住宿安排", reference="signed:reference"
        ).field_name
        == field
    )


def test_f6_difference_reader_and_current_authority_stay_with_residence_owner():
    session = MagicMock()
    session.scalar.return_value = True
    assert SqlAppliedFieldDifferenceReader(session=session, school_id=uuid4()).changed(
        fact_kind="student_bed_residence",
        source_key="own:residence",
        target_version=2,
        field_name="bed_source_key",
    )
    assert "school_fact_f6_field_changed" in str(session.scalar.call_args.args[0])
    authority = MagicMock()
    adapter = StaffCorrectionAuthorityAdapter(MagicMock(), residence_authority=authority)
    args = dict(
        actor=MagicMock(),
        fact_kind="student_bed_residence",
        source_key="own:residence",
        field_name="bed_source_key",
        action="maintain",
        checked_at=datetime.now(UTC),
    )
    adapter.require_action(**args)
    authority.require_action.assert_called_once_with(**args)
    with pytest.raises(FamilyCorrectionDenied):
        StaffCorrectionAuthorityAdapter(MagicMock()).require_action(**args)


@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize("operation", ["upsert", "end", "revoke"])
def test_f6_report_resolution_requires_exact_formal_changed_field(changed, operation):
    own, differences = MagicMock(), MagicMock()
    own.check.return_value = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="student_bed_residence",
                source_key="own:residence",
                operation=operation,
                target_version=2,
            ),
        )
    )
    differences.changed.return_value = changed
    verifier = ExactCorrectionReceiptVerifier(own_results=own, differences=differences)
    args = dict(
        actor=MagicMock(),
        locator_sha256="a" * 64,
        fact_kind="student_bed_residence",
        source_key="own:residence",
        field_name="bed_source_key",
        minimum_target_version=1,
        submitted_at=datetime.now(UTC),
    )
    if changed:
        assert verifier.require_applied_target(**args) == "maintain"
    else:
        with pytest.raises(FamilyCorrectionDenied):
            verifier.require_applied_target(**args)


def test_f6_signed_own_child_report_revalidates_current_target_and_is_disabled_by_default():
    from app.composition.school_fact_family_correction_writer import (
        FamilyCorrectionReferenceCodec,
        SqlGuardianCorrectionAuthority,
    )
    from tests.test_f3_family_read import setup

    _, actor, state = setup()
    target = dict(
        field_name="bed_source_key",
        label="孩子住宿安排",
        fact_kind="student_bed_residence",
        fact_source_key="own:residence",
        target_version=1,
        student_id=state.basis.student_id,
    )

    class Session:
        rows = [target]
        calls = []

        def execute(self, sql, params):
            self.calls.append(str(sql))
            if "residence_targets" in str(sql):
                assert params["relationship"] == actor.guardian_relationship_id
                assert params["relationship_version"] == actor.relationship_version
                self.result = list(self.rows)
            else:
                self.result = []
            return self

        def mappings(self):
            return self

        def all(self):
            return self.result

    session = Session()
    codec = FamilyCorrectionReferenceCodec(b"x" * 32)
    assert SqlGuardianCorrectionAuthority(session=session, codec=codec).options(actor) == ()
    assert not any("residence_targets" in query for query in session.calls)
    authority = SqlGuardianCorrectionAuthority(session=session, codec=codec, f6_enabled=True)
    (option,) = authority.options(actor)
    assert option.fact_kind == target["fact_kind"]
    assert (
        PublicCorrectionOption.model_validate(option.model_dump()).fact_kind == target["fact_kind"]
    )
    args = dict(
        actor=actor,
        target_reference=option.reference,
        field_name="bed_source_key",
        checked_at=datetime.now(UTC),
    )
    assert authority.require_submit(**args).student_id == state.basis.student_id
    session.rows = [target | {"target_version": 2}]
    with pytest.raises(FamilyCorrectionDenied):
        authority.require_submit(**args)
    session.rows = []
    with pytest.raises(FamilyCorrectionDenied):
        authority.require_submit(**args)
    session.rows = [target | {"field_name": "occupant_count"}]
    with pytest.raises(FamilyCorrectionDenied):
        authority.options(actor)


def test_real_writer_options_transport_preserves_owner_kind_to_product():
    from contextlib import nullcontext

    import httpx
    from fastapi.testclient import TestClient

    from app.composition.school_fact_family_correction_executor import (
        FamilyCorrectionExecutorClient,
        create_family_correction_executor_app,
    )
    from app.composition.school_fact_family_correction_product import FamilyCorrectionProduct
    from app.composition.school_fact_family_correction_writer import (
        GuardianCorrectionOption,
        SqlFamilyCorrectionExecutorKernel,
    )
    from app.composition.school_fact_product_identity import ProductIdentity
    from app.platform.identity.executor_context_codec import CommandContextTransportCodec
    from tests.test_f3_product_identity import guardian

    actor = guardian()
    rows = (
        GuardianCorrectionOption(
            fact_kind="student_bed_residence",
            field_name="valid_to",
            label="孩子住宿安排",
            reference="opaque-residence",
        ),
        GuardianCorrectionOption(
            fact_kind="dormitory_building_profile",
            field_name="location_source_key",
            label="孩子宿舍楼或住宿区域",
            reference="opaque-building",
        ),
    )
    authority = SimpleNamespace(options=MagicMock(return_value=rows), require_current=MagicMock())
    kernel = object.__new__(SqlFamilyCorrectionExecutorKernel)
    kernel._transaction = lambda *args: nullcontext(object())
    kernel._guardian_authority = lambda session: authority
    codec = CommandContextTransportCodec(b"k" * 32)
    caller_key = "executor-test-key-only-" + "x" * 32
    app = create_family_correction_executor_app(
        kernel=kernel, context_codec=codec, caller_auth_key=caller_key
    )
    with TestClient(app) as server:

        def transport(request):
            response = server.post(
                request.url.path, content=request.content, headers=dict(request.headers)
            )
            assert response.status_code == 200
            return httpx.Response(response.status_code, content=response.content)

        client = FamilyCorrectionExecutorClient(
            settings=SimpleNamespace(
                base_url="http://writer",
                request_timeout_seconds=2,
                caller_auth_key=SimpleNamespace(get_secret_value=lambda: caller_key),
            ),
            context_codec=codec,
            transport=httpx.MockTransport(transport),
        )
        product = FamilyCorrectionProduct(
            executor=client,
            current_actor=lambda selected: selected == actor,
            staff_source=lambda *args: pytest.fail("No staff lookup"),
        )
        try:
            actual = product.options(ProductIdentity(actor, "session", "private-ref"), uuid4())
        finally:
            client.close()
    assert [row.fact_kind for row in actual] == [row.fact_kind for row in rows]
    assert [row.field_name for row in actual] == ["valid_to", "location_source_key"]
    assert authority.options.call_count == 2
    authority.require_current.assert_called_once()
