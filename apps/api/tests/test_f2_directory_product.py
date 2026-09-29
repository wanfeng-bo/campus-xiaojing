from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.composition.school_fact_product_contacts import ContactProductUnavailable
from app.composition.school_facts import InMemorySchoolFactPlanStore
from app.platform import contracts as actors
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.school_facts.change_boundary import SchoolFactChangeBoundary
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    CanonicalFactContentV2,
    ConfirmationReceiptV1,
    FactOriginV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.ports import SchoolFactApplyResult, SchoolFactResultLookup

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.mark.parametrize("stage", ["resolve", "maintenance", "maintenance_again", "preflight"])
def test_prepare_denial_reports_only_proven_fixed_stage(stage):
    service, boundary, _, payload = setup()
    calls = []

    def denied(*args, **kwargs):
        raise PermissionError("PRIVATE_TARGET_AND_SCOPE")

    if stage == "resolve":
        service._resolve = denied
    elif stage.startswith("maintenance"):

        def require(*args):
            calls.append(True)
            if stage == "maintenance" or len(calls) == 2:
                denied()

        service._require = require
    else:
        boundary.preflight = denied
    with pytest.raises(PermissionError) as error:
        service.prepare(
            actor=actor(), session_binding="session", payload=payload, request_id=uuid4()
        )
    expected = {
        "resolve": "prepare_resolution_denied",
        "maintenance": "prepare_maintenance_denied",
        "maintenance_again": "prepare_maintenance_denied",
    }.get(stage)
    assert getattr(error.value, "code", None) == expected
    if expected:
        assert "PRIVATE" not in str(error.value)
    assert [name for name, _ in boundary.calls] == (
        ["preflight"] if stage == "maintenance_again" else []
    )
    assert service._confirmations == {}


def actor():
    return actors._issue_staff_actor_context(
        account_user_id=7,
        school_id=SchoolId(UUID(int=11)),
        person_id=PersonId(UUID(int=13)),
        assignment_id=AssignmentId(UUID(int=14)),
        assignment_version=1,
        authorization_version=1,
    )


class Boundary:
    def __init__(self):
        self.calls = []
        self.result = None
        self.uncertain = False

    def preflight(self, **kw):
        self.calls.append(("preflight", kw))
        return PreflightReportV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]), is_clean=True, issues=()
        )

    def prepare_confirmation(self, **kw):
        self.calls.append(("prepare", kw))
        return ConfirmationReceiptV1(
            plan_sha256=canonical_plan_sha256(kw["change_set"]),
            credential="C" * 43,
            expires_at=NOW + timedelta(minutes=5),
        )

    def confirm(self, **kw):
        self.calls.append(("confirm", kw))
        self.result = SchoolFactApplyResult(UUID(int=91), 8, 1, False)
        if self.uncertain:
            raise TimeoutError("private transport")
        return self.result

    def check_result_by_locator(self, **kw):
        self.calls.append(("receipt", kw))
        return SchoolFactResultLookup(found=True, result=self.result)


def setup():
    from app.composition.school_fact_directory_product import (
        DirectoryFactProductService,
        FactChangeInput,
        FactChangeSummary,
        ResolvedFactChange,
    )

    b = Boundary()
    allowed = [True]

    def check(actor, change_set=None):
        if not allowed[0]:
            raise PermissionError("revoked")

    def resolve(*, actor, payload, request_id, effective_at):
        check(actor)
        changes = CanonicalFactChangeSetV1(
            source_sha256="a" * 64,
            base_version=7,
            changes=(
                CanonicalFactChangeV1(
                    operation="upsert",
                    fact_kind="person",
                    source_key="f2.person.one",
                    expected_version=2,
                    effective_at=effective_at,
                    origin=FactOriginV1(sheet_code="f2_maintenance", row_number=1),
                    content=CanonicalFactContentV1(
                        display_name=payload.fields["display_name"],
                        person_kind="staff",
                        lifecycle_status="active",
                    ),
                ),
            ),
        )
        summary = FactChangeSummary(
            fact_kind="person",
            label="合成人员甲",
            reference="person:" + str(UUID(int=92)),
            operation="upsert",
            before={"display_name": "合成人员甲"},
            after=payload.fields,
            effective_at=effective_at,
            reason=payload.reason,
            source_label="正式合成资料",
        )
        return ResolvedFactChange(change_set=changes, summary=summary)

    service = DirectoryFactProductService(
        boundary=cast(SchoolFactChangeBoundary, b),
        resolve_change=resolve,
        require_maintenance=check,
        require_receipt=check,
        plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    payload = FactChangeInput(
        fact_kind="person",
        target_ref="person:" + str(UUID(int=92)),
        fields={"display_name": "合成人员乙"},
        reason="校核名称",
    )
    return service, b, allowed, payload


def prepare(s, p):
    return s.prepare(actor=actor(), session_binding="session-one", payload=p, request_id=uuid4())


def test_prepare_only_preflights_with_server_version_and_no_secret():
    s, b, _, p = setup()
    proposal = prepare(s, p)
    assert [n for n, _ in b.calls] == ["preflight", "prepare"]
    assert b.calls[0][1]["change_set"].base_version == 7
    assert proposal.summary.before is not None
    assert proposal.summary.after is not None
    assert proposal.summary.before["display_name"] == "合成人员甲"
    assert proposal.summary.after["display_name"] == "合成人员乙"
    assert "credential" not in proposal.model_dump()


def test_wrong_session_and_revocation_never_reach_write():
    s, b, allow, p = setup()
    q = prepare(s, p)
    with pytest.raises(ContactProductUnavailable):
        s.confirm(
            actor=actor(), session_binding="other", proposal_ref=q.proposal_ref, request_id=uuid4()
        )
    allow[0] = False
    with pytest.raises(PermissionError):
        s.confirm(
            actor=actor(),
            session_binding="session-one",
            proposal_ref=q.proposal_ref,
            request_id=uuid4(),
        )
    assert not any(n == "confirm" for n, _ in b.calls)


def test_unknown_result_reads_original_receipt_without_reexecution():
    s, b, _, p = setup()
    q = prepare(s, p)
    b.uncertain = True
    r = s.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=q.proposal_ref,
        request_id=uuid4(),
    )
    assert r.status == "unknown" and "private transport" not in r.model_dump_json()
    r = s.confirm(
        actor=actor(),
        session_binding="session-one",
        proposal_ref=q.proposal_ref,
        request_id=uuid4(),
    )
    assert r.status == "succeeded" and sum(n == "confirm" for n, _ in b.calls) == 1


def test_input_rejects_identity_version_and_missing_or_dual_target():
    from app.composition.school_fact_directory_product import FactChangeInput

    base = {"fact_kind": "person", "fields": {"display_name": "甲"}, "reason": "校核"}
    for more in (
        {},
        {"target_ref": "person:12345678", "creation_ref": "create:12345678"},
        {"target_ref": "person:12345678", "school_id": "x"},
        {"target_ref": "person:12345678", "expected_version": 1},
    ):
        with pytest.raises(ValidationError):
            FactChangeInput.model_validate({**base, **more})


def test_full_policy_body_preserved_and_f3_maintenance_kind_is_accepted():
    from app.composition.school_fact_directory_product import FactChangeInput

    body = "完整制度正文。" * 1000
    p = FactChangeInput(
        fact_kind="policy_version",
        creation_ref="create:12345678",
        fields={"content_text": body},
        reason="正式发布",
    )
    assert p.fields["content_text"] == body
    f3 = FactChangeInput.model_validate(
        dict(
            fact_kind="student",
            creation_ref="create:12345678",
            fields={},
            reason="F3 maintenance",
        )
    )
    assert f3.fact_kind == "student"


def real_directory_adapter(kind, content, *, creation=False):
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
    from app.platform.authorization.f2_reader_contracts import F2FactRow
    from app.platform.authorization.secure_reader_contracts import SecureReadPage

    row = F2FactRow(
        UUID(int=91),
        kind,
        "fact.one",
        content,
        0 if creation else 1,
        9,
        NOW,
        NOW + timedelta(days=25),
        "formal_publication",
        UUID(int=92),
        NOW,
    )

    class Reader:
        def read(self, request):
            return SecureReadPage((row,), 1, NOW, None)

    repository = F2FactQueryRepository(
        reader=Reader(),
        reference_key=b"synthetic-test-reference-key",
        creation_catalog={kind: {"fact.one": "Authorized creation"}} if creation else None,
    )
    reference = repository.reference_for(
        actor=actor(),
        fact_kind=kind,
        source_key="fact.one",
        creation=creation,
    )
    return DirectoryProductAdapter(repository), reference


def resolve_real_content(kind, content, fields, *, creation=False):
    from app.composition.school_fact_directory_product import FactChangeInput

    adapter, ref = real_directory_adapter(kind, content, creation=creation)
    return adapter.resolve_change(
        actor=actor(),
        payload=FactChangeInput(
            fact_kind=kind,
            target_ref=None if creation else ref,
            creation_ref=ref if creation else None,
            fields=fields,
            reason="approved correction",
        ),
        request_id=uuid4(),
        effective_at=NOW + timedelta(days=1),
    )


def test_record_source_label_preserves_publication_metadata_and_fact_name():
    from app.composition.school_fact_directory_adapter import record_view
    from app.composition.school_fact_f2_query_repository import F2PublicFact

    record = F2PublicFact(
        fact_ref="person.current",
        fact_kind="person",
        content={"display_name": "合成教师", "person_kind": "staff"},
        version=3,
        valid_from=NOW,
        valid_to=None,
        source_kind="formal_publication",
        publication_ref="publication:approved-record",
        published_at=NOW - timedelta(days=1),
    )
    view = record_view(record)
    assert view["label"] == "合成教师"
    assert view["fields"] == record.content
    assert view["source"] == {
        "kind": record.source_kind,
        "label": "学校事实发布记录",
        "reference": record.publication_ref,
        "published_at": record.published_at.isoformat(),
    }


def test_maintenance_summary_uses_neutral_channel_label():
    result = resolve_real_content(
        "person",
        {"display_name": "合成教师", "person_kind": "staff", "lifecycle_status": "active"},
        {"display_name": "核对后的姓名"},
    )
    assert result.summary.before is not None
    assert result.summary.after is not None
    assert result.summary.before["display_name"] == "合成教师"
    assert result.summary.after["display_name"] == "核对后的姓名"
    assert result.summary.source_label == "学校事实维护"


@pytest.mark.parametrize("replacement", ["omitted", None, "2026-11-01T00:00:00+00:00"])
def test_real_adapter_preserves_replaces_and_clears_relation_deadline(replacement):
    original = "2026-10-01T00:00:00+00:00"
    fields = {"function_summary": "edited"}
    if replacement != "omitted":
        fields["valid_to"] = replacement
    result = resolve_real_content(
        "organization_profile",
        {
            "organization_source_key": "org.one",
            "organization_type": "department",
            "function_summary": "original",
            "basis_reference": "approved",
            "valid_to": original,
        },
        fields,
    )
    expected = original if replacement == "omitted" else replacement
    canonical = result.change_set.changes[0].content
    assert canonical is not None
    assert result.summary.before is not None
    assert result.summary.after is not None
    assert canonical.valid_to == (datetime.fromisoformat(expected) if expected else None)
    assert result.summary.before["valid_to"] == original
    assert result.summary.after["valid_to"] == expected


def test_real_adapter_edits_json_policy_dates_and_preserves_full_text():
    full_text = "  正式原文\n" * 1000
    result = resolve_real_content(
        "policy_version",
        {
            "policy_source_key": "policy.one",
            "display_name": "Title",
            "business_code": "P-1",
            "category_code": "general",
            "classification": "internal",
            "published_at": NOW.isoformat(),
            "effective_from": NOW.isoformat(),
            "content_text": full_text,
        },
        {"version_note": "reviewed"},
    )
    canonical = result.change_set.changes[0].content
    assert canonical is not None
    assert canonical.published_at == canonical.effective_from == NOW
    assert canonical.content_text == full_text


@pytest.mark.parametrize("creation", [False, True])
def test_real_adapter_accepts_nested_quality_json_dates(creation):
    content = {
        "quality_kind": "review",
        "target_fact_kind": "school_service",
        "owner_kind": "school",
        "basis_reference": "approved",
        "responsibility": "directory",
        "verified_at": NOW.isoformat(),
        "review_due_at": (NOW + timedelta(days=20)).isoformat(),
    }
    result = resolve_real_content(
        "fact_quality",
        {} if creation else content,
        content if creation else {"responsibility": "new directory"},
        creation=creation,
    )
    canonical = result.change_set.changes[0].content
    assert isinstance(canonical, CanonicalFactContentV2)
    assert canonical.verified_at == NOW
    assert canonical.review_due_at == NOW + timedelta(days=20)


@pytest.mark.parametrize(
    "fields",
    [
        {"published_at": "2026-09-06T00:00:00"},
        {"unexpected": "value"},
    ],
)
def test_real_adapter_retains_strict_content_rejection(fields):
    with pytest.raises(ValidationError):
        resolve_real_content(
            "policy_version",
            {
                "policy_source_key": "policy.one",
                "display_name": "Title",
                "business_code": "P-1",
                "category_code": "general",
                "classification": "internal",
                "published_at": NOW.isoformat(),
                "effective_from": NOW.isoformat(),
                "content_text": "original",
            },
            fields,
        )


def test_real_adapter_rejects_string_boolean_in_its_allowed_family():
    with pytest.raises(ValidationError) as error:
        resolve_real_content(
            "organization_location",
            {
                "organization_source_key": "org.one",
                "location_source_key": "location.one",
                "purpose": "office",
                "is_primary": True,
                "basis_reference": "approved",
            },
            {"is_primary": "true"},
        )
    assert error.value.errors()[0]["type"] == "bool_type"


@pytest.mark.parametrize(
    "kind",
    [
        "organization",
        "organization_campus_relationship",
        "person_contact",
        "location",
        "official_contact",
    ],
)
def test_model_description_and_query_card_share_executable_selector_support(kind):
    import json

    from app.composition.school_fact_directory_gateway import MODULE, DirectoryFactToolGateway
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.platform.authorization.f2_reader_compiler import query_selector_support
    from app.platform.authorization.secure_reader_contracts import SecureReadPage

    class EmptyQualityReader:
        def read(self, request):
            return SecureReadPage((), 0, NOW, None)

    adapter, reference = real_directory_adapter(kind, {})
    adapter.repository._quality_readers[kind] = EmptyQualityReader()
    support = cast(
        dict[str, object], adapter.describe(actor=actor(), fact_kind=kind)["query_support"]
    )
    for key, value in query_selector_support(kind).items():
        assert support[key] == value
    query_descriptor = next(
        item for item in DirectoryFactToolGateway.descriptors if item.name == MODULE + ".query"
    )
    schema = cast(dict[str, Any], query_descriptor.parameters)
    description = schema["properties"]["fact_kind"]["description"]
    schema_support = json.loads(description.split("：", 1)[1])
    # Shared rules are stated once; the per-family matrix retains its executable
    # name and relationship selectors. Reconstruct and check the entire contract.
    assert "所有事实族均支持本族reference和获准列表" in description
    assert "名称只精确匹配" in description and "关联只匹配直接关系" in description
    assert schema["properties"]["scope"]["enum"] == ["selected", "all_authorized"]
    assert schema["properties"]["scope"]["default"] == "selected"
    assert "reference" in schema["properties"]
    assert {
        "reference": True,
        "all_authorized": True,
        "name_match": "exact",
        "related_match": "direct",
        **schema_support[kind],
    } == query_selector_support(kind)
    query = adapter.query(
        actor=actor(), payload=FactQueryInput(fact_kind=kind, reference=reference)
    )
    assert query["query_support"] == support
    assert "直接关联" in cast(str, support["guidance"])


def test_unsupported_selector_card_is_explicit_and_has_usable_next_step():
    from app.composition.school_fact_directory_product import FactQueryInput

    adapter, _ = real_directory_adapter("organization_campus_relationship", {})
    card = adapter.query(
        actor=actor(),
        payload=FactQueryInput(fact_kind="organization_campus_relationship", query_text="组织名称"),
    )
    assert card["status"] == "unsupported_selection"
    assert card["matched_count"] is None and card["records"] == []
    support = cast(dict[str, object], card["query_support"])
    assert support["name_fields"] == []
    assert support["related_kinds"] == ["organization", "location"]
    assert "取得获权对象引用" in cast(str, support["guidance"])
    assert cast(dict[str, object], card["quality"])["evidence_status"] == "not_queried"


@pytest.mark.parametrize(
    "kind,field_name,value,expected",
    [
        ("person", "person_kind", "other", "其他人员"),
        ("organization_profile", "organization_type", "other", "其他组织"),
    ],
)
def test_describe_enum_label_uses_its_fact_and_field_context(kind, field_name, value, expected):
    adapter, _ = real_directory_adapter(kind, {})
    fields = cast(
        list[dict[str, object]], adapter.describe(actor=actor(), fact_kind=kind)["fields"]
    )
    field = next(field for field in fields if field["name"] == field_name)
    options = cast(list[dict[str, str]], field["options"])
    assert next(option["label"] for option in options if option["value"] == value) == expected


@pytest.mark.parametrize("kind,expected", [("organization", "组织路径"), ("location", "地点路径")])
def test_describe_path_label_matches_its_fact_family(kind, expected):
    adapter, _ = real_directory_adapter(kind, {})
    fields = cast(
        list[dict[str, object]], adapter.describe(actor=actor(), fact_kind=kind)["fields"]
    )
    assert next(field["label"] for field in fields if field["name"] == "display_path") == expected


def test_formal_directory_field_states_use_fixed_read_columns_and_preserve_false_values():
    from app.composition.school_fact_directory_product import directory_record_field_states

    record = {
        "fact_kind": "official_contact",
        "reference": "f2:official_contact:abcdefgh",
        "source": {"kind": "formal_publication"},
        "fields": {
            "owner_kind": "school",
            "contact_kind": "email",
            "contact_value": "office@example.invalid",
            "lifecycle_status": "active",
            "visibility_scope": "school",
            "audience_code": "staff",
        },
    }
    assert directory_record_field_states(record) == {"service_hours": "not_provided"}
    record["fields"]["service_hours"] = "08:00–17:00"
    assert directory_record_field_states(record) == {}
    record["fields"]["service_hours"] = None
    assert directory_record_field_states(record) == {"service_hours": "not_provided"}
    personal = {
        "fact_kind": "person_contact",
        "reference": "f2:person_contact:abcdefgh",
        "source": {"kind": "formal_publication"},
        "fields": {
            "contact_kind": "phone",
            "contact_value": "123",
            "is_primary": False,
            "visibility_scope": "self",
            "audience_code": "self",
            "consent_reference": "已登记",
        },
    }
    assert directory_record_field_states(personal) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"source": {"kind": "legacy"}},
        {"source": {"label": "外部资料"}},
        {"reference": "external:official_contact:abcdefgh"},
        {"fact_kind": "unknown_family"},
    ],
)
def test_field_states_do_not_infer_missing_values_from_old_or_external_records(change):
    from app.composition.school_fact_directory_product import directory_record_field_states

    record = {
        "fact_kind": "official_contact",
        "reference": "f2:official_contact:abcdefgh",
        "source": {"kind": "formal_publication"},
        "fields": {"contact_value": "office@example.invalid"},
    }
    record.update(change)
    assert directory_record_field_states(record) == {}


def test_field_states_do_not_infer_unprovided_relational_or_unavailable_fields():
    from app.composition.school_fact_directory_product import directory_record_field_states

    record = {
        "fact_kind": "organization",
        "reference": "f2:organization:abcdefgh",
        "source": {"kind": "formal_publication"},
        "fields": {
            "business_code": "office",
            "display_name": "教务处",
            "short_name": "教务",
            "display_path": "学校/教务处",
        },
    }
    # parent_ref is a relationship and kind is maintenance-only for this family.
    assert directory_record_field_states(record) == {}
    record["fields"].pop("short_name")
    assert directory_record_field_states(record) == {"short_name": "not_provided"}


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        (
            "organization",
            {"display_name": "正式组织名", "short_name": "简称", "display_path": "学校/路径末段"},
            {"display_name": "正式组织名"},
        ),
        (
            "location",
            {"display_name": "正式地点", "aliases": ["地点简称"], "display_path": "校区/路径末段"},
            {"display_name": "正式地点"},
        ),
        ("location_alias", {"alias": "正式登记别名"}, {"alias": "正式登记别名"}),
        ("assignment", {"position_title": "教学负责人"}, {"position_title": "教学负责人"}),
        (
            "organization_profile",
            {"function_summary": "负责教学安排"},
            {"function_summary": "负责教学安排"},
        ),
        (
            "school_service",
            {"display_name": "资料服务", "service_summary": "提供资料借阅"},
            {"display_name": "资料服务", "service_summary": "提供资料借阅"},
        ),
        ("policy_version", {"display_name": "正式制度标题"}, {"display_name": "正式制度标题"}),
        ("official_contact", {"contact_value": "office@example.invalid"}, {}),
        ("organization_campus_relationship", {"organization_ref": "f2:organization:abcdefgh"}, {}),
    ],
)
def test_query_names_use_only_supported_present_fields(kind, fields, expected):
    from app.composition.school_fact_directory_product import directory_record_query_names

    record = {
        "fact_kind": kind,
        "reference": f"f2:{kind}:abcdefgh",
        "source": {"kind": "formal_publication"},
        "label": "展示标签不是查询名",
        "fields": fields,
    }
    assert directory_record_query_names(record) == expected


def test_query_names_do_not_invent_names_for_missing_or_unrecognized_projection():
    from app.composition.school_fact_directory_product import directory_record_query_names

    record = {
        "fact_kind": "organization",
        "reference": "f2:organization:abcdefgh",
        "source": {"kind": "formal_publication"},
        "label": "只有展示名",
        "fields": {"display_path": "学校/路径末段"},
    }
    assert directory_record_query_names(record) == {}
    record["source"]["kind"] = "external"
    assert directory_record_query_names(record) is None
