"""Known domain preflight feedback survives shared transports without private values."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition import school_fact_directory_product as product_api
from app.composition.school_fact_product_contacts import ContactProductUnavailable
from app.interfaces.http.errors import ApiError
from app.interfaces.http.school_fact_product_contacts import product_unavailable
from app.platform.assistant.tool_loop import ModelToolCall
from app.platform.school_facts.contracts import PreflightIssueV1, PreflightReportV1
from tests.test_f2_directory_product import actor, setup
from tests.test_f3_family_staff_gateway import context, gateway

CAPACITY = "residence.capacity_requires_same_batch_resolution"
REASON = "可用床位超过房间容量，请在同一修改方案中处理相关床位。未生成可确认方案。"


def failed_report():
    return PreflightReportV1(
        plan_sha256="a" * 64,
        is_clean=False,
        issues=tuple(
            PreflightIssueV1(
                sheet_code="private_target",
                row_number=n,
                column_code="private_field",
                error_code=code,
            )
            for n, code in enumerate((CAPACITY, CAPACITY), 1)
        ),
    )


def service_with_report(formatter=None):
    original, boundary, _, payload = setup()
    boundary.preflight = lambda **_: failed_report()
    service = product_api.DirectoryFactProductService(
        boundary=boundary,
        resolve_change=original._resolve,
        require_maintenance=original._require,
        require_receipt=original._require_receipt,
        plan_store=original._plans,
        clock=original._clock,
        preflight_rejection_formatter=formatter,
    )
    return service, boundary, payload


def test_preflight_formatter_gets_only_unique_codes_and_creates_no_confirmation():
    observed = []
    service, boundary, payload = service_with_report(lambda codes: observed.append(codes) or REASON)
    with pytest.raises(ContactProductUnavailable) as rejected:
        service.prepare(
            actor=actor(), session_binding="session", payload=payload, request_id=uuid4()
        )
    assert observed == [(CAPACITY,)]
    assert type(rejected.value).__name__ == "FactPreflightRejected"
    assert rejected.value.reason == REASON
    assert boundary.calls == [] and service._confirmations == {}
    assert "private" not in str(rejected.value)


@pytest.mark.parametrize("formatter", [None, lambda codes: None])
def test_default_and_unrecognized_preflight_keep_legacy_failure(formatter):
    service, _, payload = service_with_report(formatter)
    with pytest.raises(ContactProductUnavailable) as rejected:
        service.prepare(
            actor=actor(), session_binding="session", payload=payload, request_id=uuid4()
        )
    assert type(rejected.value) is ContactProductUnavailable
    with pytest.raises(ApiError) as http:
        product_unavailable(rejected.value)
    assert http.value.code == "school_facts.proposalproduct_unavailable"
    assert "private" not in http.value.message and "residence." not in http.value.message


@pytest.mark.parametrize(
    "code,fragment",
    [
        (CAPACITY, "房间容量"),
        ("residence.room_availability_requires_same_batch_resolution", "房间"),
        ("residence.bed_availability_requires_same_batch_resolution", "床位"),
        ("residence.eligibility_requires_same_batch_resolution", "寄宿资格"),
        ("residence.student_overlap_requires_same_batch_resolution", "学生"),
        ("residence.bed_overlap_requires_same_batch_resolution", "重复"),
        ("residence.bed_identity_changed", "所属房间"),
        ("residence.timeline_requires_explicit_resolution", "有效期"),
    ],
)
def test_residence_formatter_uses_only_fixed_known_messages(code, fragment):
    from app.composition.school_fact_residence_preflight import format_residence_preflight_rejection

    reason = format_residence_preflight_rejection((code, code, "private.object.identifier"))
    assert fragment in reason and "未生成可确认方案" in reason
    assert "private" not in reason and "residence." not in reason
    assert len(reason) < 600
    assert format_residence_preflight_rejection(("private.object.identifier",)) is None


def test_shared_http_retains_safe_domain_reason_but_never_arbitrary_exception_text():
    with pytest.raises(ApiError) as rejected:
        product_unavailable(product_api.FactPreflightRejected(REASON))
    assert rejected.value.status_code == 409
    assert rejected.value.code == "school_facts.preflight_rejected"
    assert rejected.value.message == REASON
    for error in (ValueError("private diagnostic"), PermissionError("private diagnostic")):
        with pytest.raises(ApiError) as hidden:
            product_unavailable(error)
        assert "private" not in hidden.value.message


@pytest.mark.parametrize("domain", ["family", "residence"])
def test_prepare_gateway_preserves_safe_preflight_reason_without_action(domain):
    owner = gateway()
    kind, prefix = "student_school_relationship", "f3"
    if domain == "residence":
        from app.composition.school_fact_residence_gateway import ResidenceToolGateway

        owner = ResidenceToolGateway(
            resolve_actor=owner.resolve_actor,
            query=owner.query,
            product=owner.product,
            describe_adapter=SimpleNamespace(),
        )
        kind, prefix = "dorm_room", "f6"

    def reject(**_):
        raise product_api.FactPreflightRejected(REASON)

    owner.product.prepare = reject
    result = owner.execute(
        ModelToolCall(
            call_id="preflight",
            name=owner.module_ref + ".prepare",
            arguments=dict(
                fact_kind=kind,
                target_ref=f"{prefix}:{kind}:" + "a" * 64,
                fields={},
                reason="核对正式资料",
            ),
        ),
        context(),
    )
    assert result.model_content == {
        "status": "rejected",
        "code": "preflight_rejected",
        "reason": REASON,
    }
    assert result.continuation is None and result.fact_card is None
