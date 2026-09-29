"""Own historical receipts retain current authorization at every release."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_gateway import (
    MODULE,
    DirectoryFactToolGateway,
    DirectoryQueryPort,
    ProductActor,
)
from app.composition.school_fact_directory_product import DirectoryFactProductService
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f2_directory_product import actor

REF = "sf2receipt_" + "a" * 64
TARGET = "f2:official_contact:YWJjZGVmZ2g"


@pytest.mark.parametrize("prefix,family,kind", [
    ("f2", "f2", "organization"), ("f3", "f3", "student"),
    ("f4", "f4", "education_class"), ("f4", "f5", "teaching_course"),
    ("f6", "f6", "dorm_room"), ("f4audit", "f4", "education_class"),
    ("f4audit", "f5", "teaching_course"), ("f6audit", "f6", "dorm_room"),
])
def test_own_receipt_projection_accepts_current_authorized_school_coordinates(prefix, family, kind):
    from app.composition.school_fact_directory_product import project_own_receipt_target
    from app.platform.school_facts.ports import SchoolFactOwnResultTarget
    from app.platform.school_facts.school_scope_reference import encode_school_scope_reference

    current = actor()
    reference = encode_school_scope_reference(
        secret=b"test-school-receipt-secret", family=family, school_id=current.school_id,
        assignment_id=current.assignment_id, fact_kind=kind, source_key="new-object",
        creation=prefix.endswith("audit"),
    )
    if prefix.endswith("audit"):
        reference = prefix + ":" + reference
    selected = project_own_receipt_target(
        current, SchoolFactOwnResultTarget(kind, "new-object", "upsert", 3),
        reference_for=lambda **kw: reference, labels={kind: "学校资料"}, prefix=prefix,
    )
    assert selected.reference == reference
    assert selected.target_version == 3


@pytest.mark.parametrize("family,kind,creation", [
    ("f6", "education_class", True),
    ("f4", "student_class_membership", True),
    ("f4", "education_class", False),
])
def test_own_creation_receipt_projection_rejects_wrong_coordinate(family, kind, creation):
    from app.composition.school_fact_directory_product import project_own_receipt_target
    from app.composition.school_fact_product_contacts import ContactProductUnavailable
    from app.platform.school_facts.ports import SchoolFactOwnResultTarget
    from app.platform.school_facts.school_scope_reference import encode_school_scope_reference

    current = actor()
    reference = "f4audit:" + encode_school_scope_reference(
        secret=b"test-school-receipt-secret", family=family, school_id=current.school_id,
        assignment_id=current.assignment_id, fact_kind=kind, source_key="new-object",
        creation=creation,
    )
    with pytest.raises(ContactProductUnavailable):
        project_own_receipt_target(
            current, SchoolFactOwnResultTarget("education_class", "new-object", "upsert", 3),
            reference_for=lambda **kw: reference,
            labels={"education_class": "班级"}, prefix="f4audit",
        )


def receipt(ref=REF):
    return {
        "kind": "own_fact_receipt",
        "reference": ref,
        "status": "succeeded",
        "message": "本人原正式修改已完成；本次只读核对，未重新执行。",
        "changed_count": 1,
        "target_version": 13,
        "request_id": None,
        "confirmed_at": "2026-09-12T12:00:00Z",
        "targets": [
            {
                "fact_kind": "official_contact",
                "label": "官方联系方式",
                "reference": TARGET,
                "operation": "upsert",
                "target_version": 13,
            }
        ],
    }


class Product:
    def __init__(self):
        self.allowed = True
        self.calls = []
        self.value = receipt()
        self.items = [self.value]
        self.has_more = False

    def _check(self, kw):
        self.calls.append(kw)
        if not self.allowed or kw["actor"] != actor():
            raise PermissionError("private reason")

    def own_receipts(self, **kw):
        self._check(kw)
        view = {
            "kind": "own_fact_receipts",
            "items": deepcopy(self.items[: kw["limit"]]),
            "has_more": self.has_more,
            "notice": "仅当前身份可读的本人最近正式回执，不代表全部历史。",
        }
        return SimpleNamespace(model_dump=lambda **_: view)

    def own_receipt(self, **kw):
        self._check(kw)
        if kw["receipt_ref"] != self.value["reference"]:
            raise ValueError("not found")
        return SimpleNamespace(model_dump=lambda **_: deepcopy(self.value))


def setup():
    product = Product()
    identity = [SimpleNamespace(actor=actor(), session_binding="fresh-login")]
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(ProductActor, identity[0]),
        query=cast(DirectoryQueryPort, SimpleNamespace()),
        product=cast(DirectoryFactProductService, product),
        retention_seconds=60,
    )
    return product, identity, gateway, gateway.for_turn("identity")


def call(action, arguments=None):
    return ModelToolCall(call_id="read", name=MODULE + "." + action, arguments=arguments or {})


def context():
    return ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4())


def test_directory_is_bounded_and_only_readonly_tool_references_are_material():
    product, _, gateway, turn = setup()
    product.has_more = True
    result = turn.execute(call("own_receipts", {"limit": 1}), context())
    assert result.model_content["has_more"] is True
    assert len(result.model_content["receipts"]) == 1
    material = result.model_content["receipts"][0]
    assert material["outcome"] == "正式修改已完成"
    assert material["tool_context"]["receipt_ref"] == REF
    assert "targets" not in material["tool_context"]
    assert result.fact_card["items"][0]["targets"][0]["reference"] == TARGET
    assert result.model_content["read_detail"] == {
        "tool": MODULE + ".own_receipt",
        "argument": "receipt_ref",
    }
    assert material["fact_types"] == ["官方联系方式"]
    assert "sf2receipt_" not in str({k: v for k, v in material.items() if k != "tool_context"})
    assert result.module_result is None
    assert all(
        d.kind == "read"
        for d in gateway.descriptors
        if d.name.endswith((".own_receipts", ".own_receipt"))
    )
    assert product.calls[0]["session_binding"] == "fresh-login"


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_own_receipt_has_on_demand_audit_explanation_without_changing_card(owner):
    product, _, gateway = receipt_owner_setup(owner)
    turn = gateway.for_turn("identity")
    before = deepcopy(product.value)
    result = turn.execute(
        ModelToolCall(
            call_id="read", name="school_facts." + owner + ".own_receipt",
            arguments={"receipt_ref": REF, "include_audit": True},
        ), context(),
    )
    material = result.model_content
    assert set(material) == {"kind", "outcome", "changed_count", "fact_types", "tool_context"}
    assert material["outcome"] == "正式修改已完成"
    assert material["changed_count"] == 1
    assert "school_fact_version" not in material
    assert material["tool_context"]["school_fact_version"] == 13
    assert material["tool_context"]["confirmed_at"] == before["confirmed_at"]
    for meaning in (
        "只读回执",
        "再次执行",
        "维护权",
        "当前存在",
        "字段可读",
        "另行查询",
    ):
        assert meaning in material["tool_context"]["meaning"]
    assert "不是当前记录版本" in material["tool_context"]["version_meaning"]
    assert result.fact_card == before
    assert product.value == before
    assert result.history_check.valid()


@pytest.mark.parametrize("limit", [0, 21, True])
def test_invalid_limit_does_not_reach_product(limit):
    product, _, _, turn = setup()
    result = turn.execute(call("own_receipts", {"limit": limit}), context())
    assert result.model_content["status"] == "rejected"
    assert not product.calls


@pytest.mark.parametrize(
    "action,args", [("own_receipts", {}), ("own_receipt", {"receipt_ref": REF})]
)
def test_revocation_invalidates_waiting_and_historical_receipt(action, args):
    product, _, _, turn = setup()
    result = turn.execute(call(action, args), context())
    assert result.history_check.valid()
    product.allowed = False
    assert not result.history_check.valid()
    assert not result.history_check.refresh()
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        turn.revalidate("identity")


def test_directory_membership_change_invalidates_empty_saved_result():
    product, _, _, turn = setup()
    product.items = []
    result = turn.execute(call("own_receipts"), context())
    assert result.history_check.valid()
    product.items = [product.value]
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("identity")


def test_explicit_receipt_selection_never_switches_to_a_newest_record():
    product, _, _, turn = setup()
    result = turn.execute(call("own_receipt", {"receipt_ref": REF}), context())
    product.items = [receipt("sf2receipt_" + "b" * 64), product.value]
    assert result.history_check.valid()
    turn.revalidate("identity")
    assert {kw["receipt_ref"] for kw in product.calls} == {REF}


@pytest.mark.parametrize(
    "ref", ["sf3plan_" + "a" * 32, "sf2receipt_" + "z" * 64, "", "sf2receipt_" + "b" * 64]
)
def test_bad_or_unavailable_ref_does_not_fallback_or_offer_execution(ref):
    product, _, _, turn = setup()
    result = turn.execute(call("own_receipt", {"receipt_ref": ref}), context())
    assert result.model_content["status"] == "rejected"
    assert result.module_result is None
    assert all("limit" not in kw for kw in product.calls)


def test_identity_is_resolved_again_before_using_result():
    product, identity, _, turn = setup()
    result = turn.execute(call("own_receipt", {"receipt_ref": REF}), context())
    identity[0] = SimpleNamespace(actor=object(), session_binding="other-login")
    assert not result.history_check.valid()
    with pytest.raises(PermissionError):
        turn.revalidate("identity")
    assert product.calls[-1]["session_binding"] == "other-login"


def test_real_product_dto_survives_gateway_material_and_history_revalidation():
    from tests.test_f2_own_receipt_product import own_service

    service, boundary, adapter = own_service()
    gateway = DirectoryFactToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="new-session")
        ),
        query=cast(DirectoryQueryPort, adapter),
        product=service,
        retention_seconds=60,
    )
    turn = gateway.for_turn("identity")
    page = turn.execute(call("own_receipts", {"limit": 1}), context())
    assert page.fact_card["items"][0]["kind"] == "own_fact_receipt"
    reference = page.model_content["receipts"][0]["tool_context"]["receipt_ref"]
    result = turn.execute(call("own_receipt", {"receipt_ref": reference}), context())
    assert result.fact_card["kind"] == "own_fact_receipt"
    assert result.model_content["tool_context"]["targets"][0]["reference"].startswith(
        "f2:official_contact:"
    )
    assert result.history_check.valid()
    turn.revalidate("identity")
    assert not any(name in {"prepare", "confirm"} for name, _ in boundary.calls)


@pytest.mark.parametrize("family", [False, True])
def test_release_batches_overlapping_receipt_pages_and_exact_detail(family):
    from app.composition.school_fact_family_staff_gateway import (
        MODULE as FAMILY_MODULE,
    )
    from app.composition.school_fact_family_staff_gateway import (
        FamilyStaffQueryPort,
        FamilyStaffToolGateway,
    )
    from tests.test_f2_own_receipt_product import own_service

    service, boundary, adapter = own_service()
    identity_reads = []

    def resolve_identity(reference):
        identity_reads.append(reference)
        return cast(ProductActor, SimpleNamespace(actor=actor(), session_binding="new-session"))

    gateway = DirectoryFactToolGateway(
        resolve_actor=resolve_identity,
        query=cast(DirectoryQueryPort, adapter),
        product=service,
        retention_seconds=60,
    )
    if family:
        gateway = FamilyStaffToolGateway(
            resolve_actor=gateway.resolve_actor,
            query=cast(FamilyStaffQueryPort, adapter),
            product=service,
        )

    def read(action, arguments):
        return ModelToolCall(
            call_id="read",
            name=(FAMILY_MODULE if family else MODULE) + "." + action,
            arguments=arguments,
        )

    turn = gateway.for_turn("identity")
    results = [
        turn.execute(read("own_receipts", {"limit": 1}), context()),
        turn.execute(read("own_receipts", {"limit": 20}), context()),
        turn.execute(read("own_receipt", {"receipt_ref": REF}), context()),
    ]
    boundary.calls.clear()
    identity_reads.clear()
    with turn.validation_scope():
        turn.revalidate("identity")
        for result in results:
            assert result.history_check is not None
            assert result.history_check.valid()
    assert [name for name, _ in boundary.calls] == ["own_list"]
    assert boundary.calls[0][1]["limit"] == 20
    assert len(identity_reads) == (9 if family else 6)

    # A later release must freshly authorize/read again, never reuse the batch.
    boundary.calls.clear()
    with turn.validation_scope():
        turn.revalidate("identity")
        for result in results:
            assert result.history_check is not None
            assert result.history_check.valid()
    assert [name for name, _ in boundary.calls] == ["own_list"]


@pytest.mark.parametrize("change", ["membership", "target", "has_more", "revocation"])
def test_receipt_batch_does_not_release_changes_after_checks_were_queued(change):
    product, _, _, turn = setup()
    result = turn.execute(call("own_receipts", {"limit": 20}), context())
    assert result.history_check is not None
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        with turn.validation_scope():
            assert result.history_check.valid()
            if change == "membership":
                product.items = []
            elif change == "target":
                product.items[0]["targets"][0]["target_version"] += 1
            elif change == "has_more":
                product.has_more = True
            else:
                product.allowed = False


def test_receipt_detail_outside_page_uses_exact_authorized_locator():
    product, _, _, turn = setup()
    page = turn.execute(call("own_receipts", {"limit": 1}), context())
    other = receipt("sf2receipt_" + "b" * 64)
    product.value = other
    detail_result = turn.execute(
        call("own_receipt", {"receipt_ref": other["reference"]}), context()
    )
    assert page.history_check is not None and detail_result.history_check is not None
    product.calls.clear()
    with turn.validation_scope():
        turn.revalidate("identity")
        assert page.history_check.valid()
        assert detail_result.history_check.valid()
    assert len(product.calls) == 2
    assert product.calls[0]["limit"] == 1
    assert product.calls[1]["receipt_ref"] == other["reference"]


@pytest.mark.parametrize("timing", ["queued", "inside_reader"])
def test_receipt_batch_rejects_logout_before_and_after_reader(timing):
    product, identity, _, turn = setup()
    result = turn.execute(call("own_receipts", {"limit": 1}), context())
    assert result.history_check is not None
    original_identity = identity[0]
    original_read = product.own_receipts

    def logout():
        identity[0] = SimpleNamespace(actor=actor(), session_binding="different-login")

    def changed_read(**kwargs):
        page = original_read(**kwargs)
        logout()
        return page

    if timing == "inside_reader":
        product.own_receipts = changed_read
    with pytest.raises(PermissionError):
        with turn.validation_scope():
            assert result.history_check.valid()
            if timing == "queued":
                logout()
    # Failed scopes clear their pending work and cannot poison the next release.
    identity[0] = original_identity
    product.own_receipts = original_read
    assert result.history_check.valid()


def test_receipt_batch_preserves_truncated_prefix_and_empty_page_membership():
    product, _, gateway, turn = setup()
    product.items.extend([receipt("sf2receipt_" + "b" * 64), receipt("sf2receipt_" + "c" * 64)])
    original_read = product.own_receipts

    def bounded_read(**kwargs):
        product.has_more = len(product.items) > kwargs["limit"]
        return original_read(**kwargs)

    product.own_receipts = bounded_read
    small = turn.execute(call("own_receipts", {"limit": 1}), context())
    large = turn.execute(call("own_receipts", {"limit": 20}), context())
    assert small.history_check is not None and large.history_check is not None
    with turn.validation_scope():
        assert small.history_check.valid()
        assert large.history_check.valid()
    product.items = []
    turn = gateway.for_turn("identity")
    empty = turn.execute(call("own_receipts", {"limit": 20}), context())
    assert empty.history_check is not None
    with pytest.raises(AssistantRuntimeError):
        with turn.validation_scope():
            assert empty.history_check.valid()
            product.items = [product.value]


@pytest.mark.parametrize("binding", [(1, "other-reference"), (2, "identity")])
def test_receipt_identity_checks_never_merge_distinct_resolvers_or_references(binding):
    from app.composition.school_fact_directory_product import validate_own_receipt_read
    from app.composition.school_fact_read_evidence import validation_batch

    product, _, _, _ = setup()
    request_id = uuid4()
    expected = product.own_receipts(
        actor=actor(), session_binding="fresh-login", request_id=request_id, limit=1
    ).model_dump()
    checked = []
    valid = [True]

    def first():
        checked.append("first")

    def second():
        checked.append("second")
        if not valid[0]:
            raise PermissionError("other authenticated binding expired")

    kwargs = dict(
        actor=actor(),
        session_binding="fresh-login",
        request_id=request_id,
        expected=expected,
        limit=1,
    )
    with validation_batch():
        validate_own_receipt_read(
            product, **kwargs, require_identity=first, identity_binding=(1, "identity")
        )
        validate_own_receipt_read(
            product, **kwargs, require_identity=second, identity_binding=binding
        )
    assert checked == ["first", "second", "first", "second"]
    with pytest.raises(PermissionError):
        with validation_batch():
            validate_own_receipt_read(
                product, **kwargs, require_identity=first, identity_binding=(1, "identity")
            )
            validate_own_receipt_read(
                product, **kwargs, require_identity=second, identity_binding=binding
            )
            valid[0] = False


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_receipt_summary_detail_contract_resolves_each_owner_and_rechecks_access(owner):
    from app.composition.school_fact_education_gateway import EducationToolGateway
    from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway

    product, _, directory, _ = setup()
    gateway = directory
    if owner != "directory":
        cls = FamilyStaffToolGateway if owner == "family_staff" else EducationToolGateway
        gateway = cls(
            resolve_actor=directory.resolve_actor,
            query=directory.query,
            product=directory.product,
            **({"describe_adapter": SimpleNamespace()} if owner == "education" else {}),
        )
    turn = gateway.for_turn("identity")
    module = "school_facts." + owner
    page = turn.execute(
        ModelToolCall(call_id="list", name=module + ".own_receipts", arguments={}), context()
    )
    entry = page.model_content["read_detail"]
    assert entry["tool"] == module + ".own_receipt"
    ref = page.model_content["receipts"][0]["tool_context"][entry["argument"]]
    detail_call = ModelToolCall(
        call_id="detail", name=entry["tool"], arguments={entry["argument"]: ref}
    )
    detail = turn.execute(detail_call, context())
    assert detail.model_content["tool_context"]["targets"][0]["reference"] == TARGET
    assert detail.fact_card["targets"] == product.value["targets"]
    product.allowed = False
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        turn.execute(detail_call, context())
    assert not detail.history_check.valid()


def receipt_owner_setup(owner):
    from app.composition.school_fact_education_gateway import EducationToolGateway
    from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway

    product, identity, gateway, _ = setup()
    if owner != "directory":
        cls = FamilyStaffToolGateway if owner == "family_staff" else EducationToolGateway
        gateway = cls(
            resolve_actor=gateway.resolve_actor,
            query=gateway.query,
            product=gateway.product,
            **({"describe_adapter": SimpleNamespace()} if owner == "education" else {}),
        )
    return product, identity, gateway


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_user_supplied_receipt_reference_can_be_read_without_listing_history(owner):
    product, _, gateway = receipt_owner_setup(owner)
    name = "school_facts." + owner + ".own_receipt"
    descriptor = next(item for item in gateway.descriptors if item.name == name)
    assert "用户提供" in descriptor.description
    assert "无需先列目录" in descriptor.description
    assert "用户同时询问当前事实时" in descriptor.description
    result = gateway.for_turn("identity").execute(
        ModelToolCall(call_id="exact", name=name, arguments={"receipt_ref": REF}),
        context(),
    )
    assert result.model_content["outcome"] == "正式修改已完成"
    assert result.fact_card == product.value
    assert len(product.calls) == 1
    assert product.calls[0]["receipt_ref"] == REF
    assert "limit" not in product.calls[0]
    assert result.history_check.valid()
    product.allowed = False
    assert not result.history_check.valid()


def teaching_receipt_targets(count):
    return [
        {
            "fact_kind": "teaching_assignment",
            "label": "持续任教",
            "reference": "f4:teaching_assignment:" + f"{index:064x}",
            "operation": "upsert",
            "target_version": index + 1,
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_large_receipt_model_pages_cover_original_targets_without_expanding_each_call(owner):
    product, _, gateway = receipt_owner_setup(owner)
    product.value["targets"] = teaching_receipt_targets(100)
    original = deepcopy(product.value)
    turn = gateway.for_turn("identity")
    module = "school_facts." + owner
    request = {"tool": module + ".own_receipt", "arguments": {"receipt_ref": REF}}
    references = []
    for offset in range(0, 100, 20):
        result = turn.execute(
            ModelToolCall(
                call_id=str(offset), name=request["tool"], arguments=request["arguments"]
            ),
            context(),
        )
        material = result.model_content
        assert len(json.dumps(material, ensure_ascii=False, separators=(",", ":"))) <= 24_000
        page = material["tool_context"]["target_page"]
        assert page == {
            "offset": offset,
            "end": offset + 20,
            "total_count": 100,
            "has_more": offset + 20 < 100,
        }
        targets = material["tool_context"]["targets"]
        assert len(targets) == 20
        references.extend(target["reference"] for target in targets)
        for target in targets:
            assert target["current_read"] == {
                "tool": "school_facts.teaching.query",
                "arguments": {"view": "teaching", "target_reference": target["reference"]},
            }
        assert result.fact_card == original
        assert result.history_check.valid()
        if page["has_more"]:
            request = material["tool_context"]["read_next"]
            assert request == {
                "tool": module + ".own_receipt",
                "arguments": {
                    "receipt_ref": REF,
                    "target_offset": offset + 20,
                    "target_limit": 20,
                },
            }
        else:
            assert "read_next" not in material["tool_context"]
    assert references == [target["reference"] for target in original["targets"]]
    assert product.value == original
    assert {entry["receipt_ref"] for entry in product.calls} == {REF}
    assert {entry["target_offset"] for entry in product.calls} == set(range(0, 100, 20))
    assert all(entry["target_limit"] == 20 for entry in product.calls)


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_receipt_requested_page_keeps_original_outcome_and_checks_unreturned_targets(owner):
    product, _, gateway = receipt_owner_setup(owner)
    product.value["targets"] = teaching_receipt_targets(5)
    turn = gateway.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="page",
            name="school_facts." + owner + ".own_receipt",
            arguments={
                "receipt_ref": REF,
                "target_offset": 2,
                "target_limit": 2,
                "include_audit": True,
            },
        ),
        context(),
    )
    material = result.model_content
    assert material["outcome"] == "正式修改已完成"
    assert "school_fact_version" not in material
    assert material["tool_context"]["school_fact_version"] == 13
    assert material["tool_context"]["target_page"] == {
        "offset": 2, "end": 4, "total_count": 5, "has_more": True,
    }
    assert material["tool_context"]["targets"][0]["record_version"] == 3
    assert material["tool_context"]["read_next"]["arguments"]["target_limit"] == 2
    assert result.history_check.valid()
    product.value["targets"][0]["target_version"] += 1
    assert not result.history_check.valid()


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"target_offset": -1},
        {"target_offset": True},
        {"target_limit": 0},
        {"target_limit": 21},
        {"target_limit": True},
    ],
)
def test_invalid_receipt_page_rejects_before_product_read(owner, invalid):
    product, _, gateway = receipt_owner_setup(owner)
    result = gateway.execute(
        ModelToolCall(
            call_id="invalid",
            name="school_facts." + owner + ".own_receipt",
            arguments={"receipt_ref": REF, **invalid},
        ),
        context(),
    )
    assert result.model_content["status"] == "rejected"
    assert not product.calls


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_receipt_outside_target_range_rejects_without_switching_receipt(owner):
    product, _, gateway = receipt_owner_setup(owner)
    result = gateway.execute(
        ModelToolCall(
            call_id="outside",
            name="school_facts." + owner + ".own_receipt",
            arguments={"receipt_ref": REF, "target_offset": 1},
        ),
        context(),
    )
    assert result.model_content["status"] == "rejected"
    assert len(product.calls) == 1
    assert product.calls[0]["receipt_ref"] == REF


def aggregate_receipt_setup(*, omitted=None, different=None):
    from app.composition import school_fact_product as composition
    from app.composition.school_fact_directory_gateway import CombinedFactTurnGateway

    products, owners = [], []
    boundary = SimpleNamespace(allowed_targets={"official_contact", "student", "teaching_offering"})
    def projection(_actor, target):
        return target

    identity = [SimpleNamespace(actor=actor(), session_binding="fresh-login")]

    def resolve_actor(_ref):
        if identity[0] is None:
            raise PermissionError("login ended")
        return identity[0]

    for name in ("directory", "family_staff", "education"):
        product, _, owner = receipt_owner_setup(name)
        owner.resolve_actor = resolve_actor
        product._boundary = boundary
        product._receipt_target_projection = projection
        product.value["targets"] = [
            {
                **product.value["targets"][0],
                "fact_kind": kind,
                "label": kind,
                "reference": reference,
            }
            for kind, reference in (
                ("official_contact", TARGET),
                ("student", "f3:student:" + "b" * 64),
                ("teaching_offering", "f4:teaching_offering:" + "c" * 64),
            )
        ]
        original_check = product._check

        def check(kw, *, require=original_check):
            require(kw)
            if boundary.allowed_targets != {"official_contact", "student", "teaching_offering"}:
                raise PermissionError("complete receipt target scope unavailable")

        product._check = check
        products.append(product)
        owners.append(owner if name != omitted else None)
    if different == "boundary":
        products[2]._boundary = object()
    elif different == "projection":
        products[2]._receipt_target_projection = lambda _actor, target: target
    elif different == "identity":
        owners[2].resolve_actor = lambda _ref: identity[0]
    elif different == "missing_projection":
        for product in products:
            product._receipt_target_projection = None
    aliases = composition.shared_own_receipt_tool_aliases(*owners)
    turns = [owner.for_turn("identity") for owner in owners if owner is not None]
    combined = CombinedFactTurnGateway(
        *turns, **({"unadvertised_tools": aliases} if aliases else {})
    )
    return products, owners, identity, combined


@pytest.mark.parametrize("action", ["own_receipt", "own_receipts"])
def test_aggregate_receipt_catalog_does_not_offer_three_calls_for_the_same_receipt(action):
    products, owners, _, combined = aggregate_receipt_setup()
    descriptors = [d for d in combined.descriptors if d.name.endswith("." + action)]
    results = [
        combined.execute(
            ModelToolCall(
                call_id=f"candidate-{index}",
                name=descriptor.name,
                arguments={"receipt_ref": REF} if action == "own_receipt" else {"limit": 1},
            ),
            context(),
        )
        for index, descriptor in enumerate(descriptors)
    ]
    assert len(results) == 1
    assert sum(len(product.calls) for product in products) == 1
    assert descriptors[0].name == MODULE + "." + action
    assert len([d for d in combined.descriptors if d.name.endswith(".receipt")]) == 3
    # Each original owner retains its independent maintenance and internal consumer contract.
    assert all(
        any(d.name.endswith("." + action) for d in owner.descriptors) for owner in owners
    )


def test_aggregate_receipt_retains_all_families_and_canonical_paging_consumers():
    products, _, _, combined = aggregate_receipt_setup()
    detail = combined.execute(
        call("own_receipt", {"receipt_ref": REF, "target_limit": 1}), context()
    )
    assert {item["fact_kind"] for item in detail.fact_card["targets"]} == {
        "official_contact", "student", "teaching_offering"
    }
    next_read = detail.model_content["tool_context"]["read_next"]
    assert next_read["tool"] == MODULE + ".own_receipt"
    assert next_read["tool"] in {descriptor.name for descriptor in combined.descriptors}
    assert next_read["arguments"]["receipt_ref"] == REF
    assert detail.history_check.valid()
    # Revocation of the unchanged complete receipt fails the release boundary.
    products[0].allowed = False
    assert not detail.history_check.valid()
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        combined.revalidate("identity")


@pytest.mark.parametrize("old_owner", ["family_staff", "education"])
def test_aggregate_receipt_legacy_internal_alias_keeps_its_original_authority(old_owner):
    products, _, identity, combined = aggregate_receipt_setup()
    name = "school_facts." + old_owner + ".own_receipt"
    assert name not in {d.name for d in combined.descriptors}
    result = combined.execute(
        ModelToolCall(call_id="legacy", name=name, arguments={"receipt_ref": REF}), context()
    )
    index = 1 if old_owner == "family_staff" else 2
    assert len(products[index].calls) == 1
    assert not products[0].calls
    assert result.history_check.valid()
    identity[0] = None
    assert not result.history_check.valid()


@pytest.mark.parametrize("omitted", ["directory", "family_staff", "education"])
def test_aggregate_receipt_partial_composition_keeps_existing_tools(omitted):
    _, owners, _, combined = aggregate_receipt_setup(omitted=omitted)
    assert {d.name for d in combined.descriptors} == {
        d.name for owner in owners if owner is not None for d in owner.descriptors
    }


@pytest.mark.parametrize("different", ["boundary", "projection", "identity", "missing_projection"])
def test_aggregate_receipt_does_not_merge_unproven_owner_equivalence(different):
    _, owners, _, combined = aggregate_receipt_setup(different=different)
    assert {d.name for d in combined.descriptors} == {
        d.name for owner in owners for d in owner.descriptors
    }


@pytest.mark.parametrize("revoked_kind", ["official_contact", "student", "teaching_offering"])
def test_aggregate_receipt_each_family_permission_still_gates_the_complete_result(revoked_kind):
    products, _, _, combined = aggregate_receipt_setup()
    detail = combined.execute(call("own_receipt", {"receipt_ref": REF}), context())
    assert detail.history_check.valid()
    products[0]._boundary.allowed_targets.remove(revoked_kind)
    assert not detail.history_check.valid()
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        combined.revalidate("identity")


def test_aggregate_receipt_retained_descriptor_and_result_have_identical_owner_evidence():
    from app.composition.school_fact_directory_gateway import OwnReceiptInput, OwnReceiptsInput
    from app.composition.school_fact_family_staff_gateway import (
        FamilyOwnReceiptInput,
        FamilyOwnReceiptsInput,
    )

    products, owners, _, combined = aggregate_receipt_setup()
    for original, alternate in (
        (OwnReceiptInput, FamilyOwnReceiptInput), (OwnReceiptsInput, FamilyOwnReceiptsInput)
    ):
        left, right = original.model_json_schema(), alternate.model_json_schema()
        left.pop("title")
        right.pop("title")
        if original is OwnReceiptInput:
            original_value = left["properties"].pop("include_original")
            assert original_value["type"] == "boolean" and original_value["default"] is False
            current = left["properties"].pop("include_current")
            assert current["type"] == "boolean" and current["default"] is False
            assert "include_current" not in left.get("required", [])
            assert "include_current" not in right["properties"]
        assert left == right
    results = [
        owner.for_turn("identity").execute(
            ModelToolCall(
                call_id=f"owner-{index}",
                name=next(d.name for d in owner.descriptors if d.name.endswith(".own_receipt")),
                arguments={"receipt_ref": REF},
            ),
            context(),
        )
        for index, owner in enumerate(owners)
    ]
    assert all(result.fact_card == results[0].fact_card for result in results)
    assert all(result.model_content == results[0].model_content for result in results)
    assert all(result.history_check.revalidation_covers_content for result in results)
    assert all(result.history_check.valid() for result in results)
    for product in products:
        product.allowed = False
    assert all(not result.history_check.valid() for result in results)
    assert all(
        next(d for d in combined.descriptors if d.name == MODULE + "." + action)
        == next(d for d in owners[0].descriptors if d.name == MODULE + "." + action)
        for action in ("own_receipt", "own_receipts")
    )


def test_aggregate_receipt_real_products_preserve_shared_writer_and_projection_contract():
    from copy import copy
    from dataclasses import replace

    from app.composition.school_fact_directory_gateway import CombinedFactTurnGateway
    from app.composition.school_fact_directory_product import OwnReceiptTarget
    from app.composition.school_fact_product import shared_own_receipt_tool_aliases
    from app.platform.school_facts.ports import SchoolFactOwnResultTarget
    from tests.test_f2_own_receipt_product import own_service

    _, owners, _, _ = aggregate_receipt_setup()
    source, boundary, _ = own_service()
    kinds = ("official_contact", "student", "teaching_offering")
    boundary.selected = replace(
        boundary.selected,
        targets=tuple(
            SchoolFactOwnResultTarget(kind, "private-" + kind, "upsert", 3) for kind in kinds
        ),
    )

    def projection(_actor, target):
        return OwnReceiptTarget(
            fact_kind=target.fact_kind,
            label=target.fact_kind,
            reference="selected:" + target.fact_kind,
            operation=target.operation,
            target_version=target.target_version,
        )

    def maintenance_only_guard(_actor):
        raise AssertionError("own-result reading must not use an owner's old maintenance guard")

    for kind, owner in zip(kinds, owners, strict=True):
        owner.product = copy(source)
        owner.product._receipt_target_projection = projection
        owner.product._receipt_fact_labels = {kind: kind}
        owner.product._require_receipt = maintenance_only_guard
    combined = CombinedFactTurnGateway(
        *(owner.for_turn("identity") for owner in owners),
        unadvertised_tools=shared_own_receipt_tool_aliases(*owners),
    )
    same_context = context()
    results = [
        combined.execute(
            ModelToolCall(
                call_id=f"original-{index}",
                name=next(d.name for d in owner.descriptors if d.name.endswith(".own_receipt")),
                arguments={"receipt_ref": REF},
            ),
            same_context,
        )
        for index, owner in enumerate(owners)
    ]
    assert all(result.fact_card == results[0].fact_card for result in results)
    assert all(result.model_content == results[0].model_content for result in results)
    assert {target["fact_kind"] for target in results[0].fact_card["targets"]} == set(kinds)
    assert all(result.history_check.valid() for result in results)
    assert all(name == "own_detail" for name, _ in boundary.calls)
    boundary.error = PermissionError("current complete scope revoked")
    assert all(not result.history_check.valid() for result in results)


def receipt_current_setup(count=1):
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    product, identity, gateway, turn = setup()
    product.value["targets"] = teaching_receipt_targets(count)
    state = {"version": 1, "live": True, "error": None, "status": "available", "proof": True}
    calls = []

    def current(target, context):
        calls.append((target, context))
        if state["error"] is not None:
            raise state["error"]
        version = state["version"]
        return ToolExecutionResult(
            model_content={
                "status": state["status"],
                "items": []
                if state["status"] == "no_current_records"
                else [{"teacher": "same-named teacher", "version": version}],
                "tool_context": {"target_reference": target.reference},
            },
            fact_card={"private_current_card": target.reference},
            history_check=HistoryCheck(
                lambda: state["live"] and state["version"] == version,
                revalidation_covers_content=True,
            )
            if state["proof"]
            else None,
        )

    gateway.receipt_current_query = current
    return product, identity, gateway, turn, state, calls


def current_call(**arguments):
    return call("own_receipt", {"receipt_ref": REF, "include_current": True, **arguments})


def test_receipt_current_default_false_preserves_original_result_and_no_extra_read():
    product, _, _, turn, _, calls = receipt_current_setup()
    result = turn.execute(call("own_receipt", {"receipt_ref": REF}), context())
    assert result.fact_card == product.value
    assert result.model_content["outcome"] == "正式修改已完成"
    assert "current_results" not in result.model_content and not calls
    assert len(product.calls) == 1


def test_receipt_current_reads_original_exact_target_and_keeps_card_immutable():
    product, _, _, turn, _, calls = receipt_current_setup()
    original = deepcopy(product.value)
    result = turn.execute(current_call(), context())
    assert result.model_content["receipt"]["outcome"] == "正式修改已完成"
    current = result.model_content["current_results"][0]
    assert current["tool_context"]["reference"] == original["targets"][0]["reference"]
    assert current["result"]["status"] == "available"
    assert len(calls) == 1 and calls[0][0].reference == original["targets"][0]["reference"]
    assert result.fact_card == original and product.value == original
    assert result.model_content["evidence_scope"]["interval"]["events"] == "not_queried"
    assert result.model_content["tool_context"]["relationship_basis"] == (
        "original_receipt_target_reference"
    )
    assert result.history_check.revalidation_covers_content
    assert result.history_check.valid()


@pytest.mark.parametrize("limit,expected", [(20, 3), (2, 2), (1, 1)])
def test_receipt_current_has_three_target_bound_and_truthful_next_page(limit, expected):
    product, _, _, turn, _, calls = receipt_current_setup(8)
    result = turn.execute(current_call(target_offset=1, target_limit=limit), context())
    receipt = result.model_content["receipt"]
    assert receipt["tool_context"]["target_page"] == {
        "offset": 1,
        "end": 1 + expected,
        "total_count": 8,
        "has_more": True,
    }
    next_page = receipt["tool_context"]["read_next"]
    assert next_page["arguments"] == {
        "receipt_ref": REF,
        "target_offset": 1 + expected,
        "target_limit": expected,
        "include_current": True,
    }
    assert [target.reference for target, _ in calls] == [
        target["reference"] for target in product.value["targets"][1 : 1 + expected]
    ]
    assert len(result.model_content["current_results"]) == expected


def test_receipt_current_zero_current_records_is_proven_not_another_same_named_target():
    _, _, _, turn, state, calls = receipt_current_setup()
    state["status"] = "no_current_records"
    result = turn.execute(current_call(), context())
    assert result.model_content["current_results"][0]["result"]["items"] == []
    assert len(calls) == 1 and result.history_check.valid()
    state["version"] += 1
    assert not result.history_check.valid()


@pytest.mark.parametrize("failure", ["receipt", "current", "logout", "version"])
def test_receipt_current_both_proofs_and_original_identity_guard_every_release(failure):
    product, identity, gateway, turn, state, _ = receipt_current_setup()
    result = turn.execute(current_call(), context())
    assert result.history_check is not None and result.history_check.valid()
    if failure == "receipt":
        product.value["targets"][0]["target_version"] += 1
    elif failure == "current":
        state["live"] = False
    elif failure == "version":
        state["version"] += 1
    else:
        original_resolver = gateway.resolve_actor

        def logged_out(ref):
            if identity[0] is None:
                raise PermissionError("login ended")
            return original_resolver(ref)

        gateway.resolve_actor = logged_out
        identity[0] = None
    assert not result.history_check.valid()
    assert not result.history_check.refresh()
    with pytest.raises((PermissionError, AssistantRuntimeError)):
        turn.revalidate("identity")


@pytest.mark.parametrize("error", [PermissionError("private"), RuntimeError("private")])
def test_receipt_current_failure_keeps_only_original_success_and_fixed_failure(error):
    product, _, _, turn, state, _ = receipt_current_setup()
    state["error"] = error
    result = turn.execute(current_call(), context())
    assert result.model_content["receipt"]["outcome"] == "正式修改已完成"
    current = result.model_content["current_results"][0]["result"]
    assert current["status"] in {"denied", "source_unavailable"}
    assert "items" not in current and "private" not in str(result.model_content)
    assert result.fact_card == product.value and result.history_check.valid()


def test_receipt_current_unproven_or_failed_payload_cannot_release_values():
    from app.platform.assistant.tool_loop import ToolExecutionResult

    for status in ("available", "denied", "source_unavailable", "unsupported_selection"):
        _, _, gateway, turn, _, _ = receipt_current_setup()
        gateway.receipt_current_query = lambda *_, status=status: ToolExecutionResult(
            model_content={"status": status, "items": ["must not escape"]}
        )
        result = turn.execute(current_call(), context())
        current = result.model_content["current_results"][0]["result"]
        assert current["status"] != "available"
        assert "must not escape" not in str(result.model_content)


def test_receipt_current_partial_assembly_explicitly_reports_unsupported_targets():
    product, _, gateway, turn, _, calls = receipt_current_setup(2)
    gateway.receipt_current_query = None
    result = turn.execute(current_call(), context())
    assert not calls and result.fact_card == product.value
    assert len(result.model_content["current_results"]) == 2
    assert all(
        row["result"]["status"] == "unsupported" for row in result.model_content["current_results"]
    )
    assert result.history_check.valid()


def test_receipt_current_identity_change_during_query_does_not_release_original_actor_values():
    _, identity, gateway, turn, _, _ = receipt_current_setup()
    original = gateway.receipt_current_query

    def change_identity(*args):
        result = original(*args)
        identity[0] = SimpleNamespace(actor=actor(), session_binding="different-session")
        return result

    gateway.receipt_current_query = change_identity
    result = turn.execute(current_call(), context())
    assert result.fact_card is None and result.model_content["status"] == "denied"


def test_receipt_current_distinct_current_snapshots_have_distinct_history_keys():
    _, _, gateway, _, state, _ = receipt_current_setup()
    first = gateway.for_turn("identity").execute(current_call(), context())
    state["version"] += 1
    second = gateway.for_turn("identity").execute(current_call(), context())
    assert first.fact_card == second.fact_card
    assert first.history_check.key != second.history_check.key
    assert not first.history_check.valid() and second.history_check.valid()


@pytest.mark.parametrize("invalid", [True, 1, "true"])
def test_receipt_current_invalid_reference_or_flag_never_reads_other_operation(invalid):
    product, _, _, turn, _, calls = receipt_current_setup()
    arguments = (
        {"receipt_ref": "not-the-original"} if invalid is True else {"include_current": invalid}
    )
    result = turn.execute(current_call(**arguments), context())
    assert result.model_content["status"] == "rejected"
    assert not product.calls and not calls


@pytest.mark.parametrize("school_wide", [False, True])
def test_receipt_current_installed_teaching_callback_uses_only_approved_exact_arguments(
    school_wide,
):
    from app.composition.school_fact_directory_product import OwnReceiptTarget
    from app.composition.school_fact_product import read_receipt_current_teaching
    from app.platform.assistant.tool_loop import ToolExecutionResult

    calls = []
    teaching = SimpleNamespace(
        execute=lambda call, ctx: (
            calls.append((call, ctx)) or ToolExecutionResult(model_content={"status": "available"})
        )
    )
    target = OwnReceiptTarget.model_validate(teaching_receipt_targets(1)[0])
    if school_wide:
        target = target.model_copy(update={
            "reference": "schoolv1:f5:teaching_assignment:o:YQ:" + "a" * 64
        })
    expected_context = context()
    read_receipt_current_teaching(teaching, target, expected_context)
    assert len(calls) == 1
    assert calls[0][0].name == "school_facts.teaching.query"
    assert calls[0][0].arguments == {"view": "teaching", "target_reference": target.reference}
    assert calls[0][1] is expected_context


def test_receipt_current_mixed_unsupported_and_supported_targets_are_not_silently_dropped():
    from app.composition.school_fact_product import read_receipt_current_teaching
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    product, _, gateway, turn, _, calls = receipt_current_setup(5)
    product.value["targets"][1].update(fact_kind="student", reference="f3:student:original")
    teaching = SimpleNamespace(
        execute=lambda call, ctx: (
            calls.append((call, ctx))
            or ToolExecutionResult(
                model_content={"status": "available", "items": []},
                history_check=HistoryCheck(lambda: True, revalidation_covers_content=True),
            )
        )
    )
    gateway.receipt_current_query = lambda target, ctx: read_receipt_current_teaching(
        teaching, target, ctx
    )
    result = turn.execute(current_call(), context())
    rows = result.model_content["current_results"]
    assert len(rows) == 3 and len(calls) == 2
    assert [row["result"]["status"] for row in rows] == ["available", "unsupported", "available"]
    assert [row["tool_context"]["reference"] for row in rows] == [
        row["reference"] for row in product.value["targets"][:3]
    ]


def signed_receipt_current_setup():
    from app.composition.school_fact_product import read_receipt_current_teaching
    from app.composition.school_fact_teaching_gateway import StaffTeachingGateway
    from tests.school_facts.test_f5_staff_teaching_query import signed_service

    product, identity, gateway, turn, _, _ = receipt_current_setup()
    query, repository, source, external, state = signed_service()
    reference = repository.reference_for(actor(), "teaching_assignment", "teaching:a")
    product.value["targets"][0]["reference"] = reference
    teaching = StaffTeachingGateway(resolve_actor=gateway.resolve_actor, query=query)
    gateway.receipt_current_query = lambda target, ctx: read_receipt_current_teaching(
        teaching, target, ctx
    )
    return product, identity, gateway, turn, repository, source, external, state


def test_receipt_current_real_signed_query_ignores_independent_same_named_offering():
    product, _, _, turn, _, source, external, state = signed_receipt_current_setup()
    for kind in ("teaching_assignment", "teaching_offering", "offering_member_source"):
        other = deepcopy(source.rows[kind][0])
        other["source_key"] = other["source_key"].replace(":a", ":b")
        for key in ("offering_source_key", "assignment_source_key"):
            if key in other["segments"][0]["content"]:
                other["segments"][0]["content"][key] = other["segments"][0]["content"][key].replace(
                    ":a", ":b"
                )
        source.rows[kind].append(other)
    result = turn.execute(current_call(), context())
    rows = result.model_content["current_results"][0]["result"]["items"]
    assert len(rows) == 1 and len(rows[0]["teachers"]) == 1
    assert (
        rows[0]["teachers"][0]["tool_context"]["reference"]
        == product.value["targets"][0]["reference"]
    )
    assert not any(kind == "teaching_assignment" and filters for kind, filters, *_ in source.calls)
    assert not any(kind == "teaching_course" and filters for kind, filters, *_ in source.calls)
    assert all("assignment:b" not in keys for _, keys, _ in external.calls)
    assert result.history_check.valid()
    state.allowed = False
    assert not result.history_check.valid()


@pytest.mark.parametrize("during_receipt_recheck", [False, True])
def test_receipt_current_real_proof_expires_at_time_boundary(during_receipt_recheck):
    from datetime import timedelta

    from tests.school_facts.test_f4_education_query import AT

    product, _, _, turn, repository, source, _, _ = signed_receipt_current_setup()
    clock = [AT]
    deadline = AT + timedelta(seconds=1)
    repository.clock = lambda: clock[0]
    source.rows["teaching_assignment"][0]["next_valid_boundary"] = deadline.isoformat()
    if during_receipt_recheck:
        original = product._check

        def cross_deadline(kw):
            original(kw)
            if len(product.calls) > 1:
                clock[0] = deadline

        product._check = cross_deadline
    result = turn.execute(current_call(), context())
    if during_receipt_recheck:
        assert result.fact_card is None and result.model_content["status"] == "denied"
    else:
        assert result.history_check.valid()
        clock[0] = deadline
        assert not result.history_check.valid()


@pytest.mark.parametrize("changed", [False, True])
def test_receipt_current_refresh_delegates_without_accepting_changed_child_content(changed):
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    _, _, gateway, turn, _, calls = receipt_current_setup()
    state = {"credential": 1, "content": 1, "refreshes": 0}

    def read_current(target, ctx):
        calls.append((target, ctx))
        credential, expected = state["credential"], state["content"]

        def valid():
            return credential == state["credential"] and expected == state["content"]

        def refresh():
            nonlocal credential
            state["refreshes"] += 1
            if expected != state["content"]:
                return False
            credential = state["credential"]
            return valid()

        return ToolExecutionResult(
            model_content={"status": "available", "items": [{"version": expected}]},
            history_check=HistoryCheck(
                refresh, revalidate=valid, revalidation_covers_content=True
            ),
        )

    gateway.receipt_current_query = read_current
    result = turn.execute(current_call(), context())
    expected_material = deepcopy(result.model_content)
    state["credential"] += 1
    if changed:
        state["content"] += 1
    assert not result.history_check.valid()
    assert state["refreshes"] == 0
    assert result.history_check.refresh() is not changed
    assert state["refreshes"] == 1
    assert result.history_check.valid() is not changed
    assert result.model_content == expected_material and len(calls) == 1


def test_receipt_current_refresh_rechecks_earlier_child_after_later_child_io():
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    _, _, gateway, turn, _, calls = receipt_current_setup(2)
    state = {"first_live": True, "refreshes": 0}

    def read_current(target, ctx):
        first = not calls
        calls.append((target, ctx))

        def valid():
            return not first or state["first_live"]

        def refresh():
            state["refreshes"] += 1
            if not first:
                state["first_live"] = False
            return True

        return ToolExecutionResult(
            model_content={"status": "available", "items": []},
            history_check=HistoryCheck(
                refresh, revalidate=valid, revalidation_covers_content=True
            ),
        )

    gateway.receipt_current_query = read_current
    result = turn.execute(current_call(), context())
    assert result.history_check.valid()
    assert not result.history_check.refresh()
    assert state["refreshes"] == 2


@pytest.mark.parametrize("owner", ["directory", "family_staff", "education"])
def test_default_own_receipt_omits_audit_values_but_keeps_card_and_reference(owner):
    product, _, gateway = receipt_owner_setup(owner)
    result = gateway.for_turn("identity").execute(
        ModelToolCall(call_id="read", name="school_facts." + owner + ".own_receipt",
                      arguments={"receipt_ref": REF}), context())
    tool = result.model_content["tool_context"]
    assert tool["receipt_ref"] == REF
    assert "confirmed_at" not in tool
    assert "school_fact_version" not in tool
    assert "version_meaning" not in tool
    assert all("record_version" not in row for row in tool["targets"])
    assert result.fact_card == product.value
    assert result.history_check.valid()


@pytest.mark.parametrize("include_current", [False, True])
def test_original_receipt_values_have_independent_evidence_and_current_selection(include_current):
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult
    product, identity, gateway, turn, state, calls = receipt_current_setup()
    original = {"allowed": True}
    original_calls = []
    def read_original(view, selection, ctx):
        original_calls.append((deepcopy(view), selection, ctx))
        return ToolExecutionResult(
            model_content={
                "original_results": [
                    {
                        "tool_context": {"reference": view["targets"][0]["reference"]},
                        "result": {
                            "status": "available",
                            "items": [{"teacher_name": "Original teacher"}],
                        },
                    }
                ]
            },
            history_check=HistoryCheck(
                lambda: original["allowed"], revalidation_covers_content=True
            ),
        )

    gateway.receipt_original_query = read_original
    result = turn.execute(call("own_receipt", {"receipt_ref": REF,
        "include_original": True, "include_current": include_current}), context())
    assert len(original_calls) == 1
    assert len(calls) == int(include_current)
    assert (
        result.model_content["original_results"][0]["result"]["items"][0]["teacher_name"]
        == "Original teacher"
    )
    assert result.fact_card == product.value
    assert result.history_check.valid()
    original["allowed"] = False
    assert not result.history_check.valid()
    assert not result.history_check.refresh()


def test_current_comparison_automatically_includes_installed_original_evidence():
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult
    _, _, gateway, turn, _, _ = receipt_current_setup()
    calls = []
    def original(view, selection, ctx):
        calls.append(selection)
        return ToolExecutionResult(model_content={"original_results": []},
            history_check=HistoryCheck(lambda: True, revalidation_covers_content=True))
    gateway.receipt_original_query = original
    result = turn.execute(
        call("own_receipt", {"receipt_ref": REF, "include_current": True}), context()
    )
    assert len(calls) == 1
    assert "original_results" in result.model_content
    assert result.history_check.valid()


def test_receipt_current_uses_selected_targets_in_an_authorized_large_page():
    product, _, _, turn, _, calls = receipt_current_setup()
    product.value["changed_count"] = 4097
    product.value["target_page"] = {
        "offset": 4096,
        "next_offset": 4097,
        "target_count": 4097,
        "has_more": False,
    }
    result = turn.execute(current_call(target_offset=4096), context())
    assert len(result.model_content["current_results"]) == 1
    assert len(calls) == 1 and calls[0][0].reference == product.value["targets"][0]["reference"]
    assert result.history_check.valid()


@pytest.mark.parametrize("include_original", [False, True])
@pytest.mark.parametrize("include_current", [False, True])
def test_receipt_read_diagnostics_only_record_selection_counts_and_result_size(
    caplog, include_original, include_current
):
    import logging

    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    product, _, gateway, turn, _, current_calls = receipt_current_setup()

    def read_original(view, selection, ctx):
        return ToolExecutionResult(
            model_content={"original_results": [{"result": {"private_body": "never logged"}}]},
            history_check=HistoryCheck(lambda: True, revalidation_covers_content=True),
        )

    gateway.receipt_original_query = read_original
    ctx = context()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = turn.execute(call("own_receipt", {
            "receipt_ref": REF,
            "include_original": include_original,
            "include_current": include_current,
        }), ctx)

    records = [
        record.getMessage().removeprefix("school_fact_receipt_read ")
        for record in caplog.records
        if record.getMessage().startswith("school_fact_receipt_read ")
    ]
    assert len(records) == 1
    assert json.loads(records[0]) == {
        "request_id": str(ctx.request_id),
        "include_original": include_original,
        "include_current": include_current,
        "target_offset": 0,
        "target_count": 1,
        "original_result_count": int(include_original or include_current),
        "result_bytes": len(json.dumps(
            result.model_content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")),
    }
    assert len(current_calls) == int(include_current)
    assert all(secret not in records[0] for secret in (
        REF, product.value["targets"][0]["reference"], "never logged", "private_body",
    ))


def original_metadata_coverage_setup(monkeypatch, *, status="available", declared=True):
    from datetime import UTC, datetime, timedelta
    from functools import partial

    from app.composition import school_fact_directory_gateway as module
    from app.composition import school_fact_receipt_teaching as names
    from app.composition.school_fact_product import read_receipt_original_teaching
    from app.composition.school_fact_read_evidence import register_release_time_window

    product, identity, gateway, turn, current, _ = receipt_current_setup()
    state = {"fields_allowed": True, "reads": [], "clock": datetime(2026, 9, 15, tzinfo=UTC)}
    observed_at = state["clock"]
    deadline = observed_at + timedelta(seconds=30)
    def clock():
        return state["clock"]
    original = SimpleNamespace(targets=(SimpleNamespace(
        fact_kind="teaching_assignment", source_key="exact-original-key",
        original_result=SimpleNamespace(
            status=status, field_names=("assignment_source_key",),
            segments=("original-segment",), before_segments=(), field_changes=True,
        ),
    ),))

    def original_read(**kwargs):
        state["reads"].append(kwargs)
        if not product.allowed:
            raise PermissionError("Original metadata permission revoked")
        if state.get("advance_on_read"):
            state["clock"] = deadline
        return (
            SimpleNamespace(model_dump=lambda **_: deepcopy(product.value)),
            deepcopy(original),
        )

    def fields_check(*_args, **_kwargs):
        if not state["fields_allowed"]:
            raise PermissionError("Original field permission revoked")
        register_release_time_window(clock, observed_at=observed_at, valid_before=deadline)

    product.own_receipt_original = original_read
    monkeypatch.setattr(names, "project_original_teaching", lambda *_a, **_k: SimpleNamespace(
        material={"items": [{"teacher_name": "Original teacher"}]},
        proof=SimpleNamespace(revalidate=fields_check),
    ))
    teaching = SimpleNamespace(
        identity=gateway.resolve_actor,
        query=SimpleNamespace(external_reader=object(), repository=SimpleNamespace(
            clock=lambda: state["clock"],
            authority=SimpleNamespace(
                require_scope=lambda *_a, **_k: object(),
                require_object=fields_check, require_fields=fields_check,
                revalidate_many=fields_check,
            ),
        )),
    )
    callback = partial(read_receipt_original_teaching, gateway, teaching)
    if declared:
        callback = module.ReceiptOriginalQueryWithMetadataProof(callback)
    else:
        callback.revalidates_receipt_metadata = True
    gateway.receipt_original_query = callback
    result = turn.execute(current_call(include_original=True), context())
    return product, identity, gateway, turn, state, original, current, result


@pytest.mark.parametrize("status", ["available", "denied", "unsupported"])
def test_declared_original_proof_rechecks_metadata_once_per_independent_release(
    monkeypatch, status
):
    from app.composition.school_fact_read_evidence import validation_batch

    product, _, _, _, state, _, _, result = original_metadata_coverage_setup(
        monkeypatch, status=status
    )
    assert result.fact_card == product.value
    assert result.model_content["original_results"][0]["result"]["status"] == status
    assert len(product.calls) == 1  # Initial receipt; the original proof covers later releases.
    start = len(state["reads"])
    for boundary in range(1, 4):
        with validation_batch():
            assert result.history_check.valid()
            assert result.history_check.valid()
        assert len(state["reads"]) == start + boundary
    assert result.history_check.refresh()
    assert len(state["reads"]) == start + 4
    assert len(product.calls) == 1


@pytest.mark.parametrize("failure", ["metadata", "original", "fields", "identity", "session",
                                      "current", "metadata_permission"])
def test_declared_original_proof_keeps_all_combined_release_guards(monkeypatch, failure):
    from dataclasses import asdict

    from app.platform import contracts as actors

    product, identity, _, _, state, original, current, result = original_metadata_coverage_setup(
        monkeypatch
    )
    if failure == "metadata":
        product.value["targets"][0]["target_version"] += 1
    elif failure == "original":
        original.targets[0].original_result.segments = ("different-original",)
    elif failure == "fields":
        state["fields_allowed"] = False
    elif failure == "identity":
        changed_actor = actors._issue_staff_actor_context(
            **{**asdict(actor()), "person_id": uuid4()}
        )
        identity[0] = SimpleNamespace(actor=changed_actor, session_binding="fresh-login")
    elif failure == "session":
        identity[0] = SimpleNamespace(actor=actor(), session_binding="different-login")
    elif failure == "current":
        current["version"] += 1
    else:
        product.allowed = False
    assert not result.history_check.valid()
    assert not result.history_check.refresh()


@pytest.mark.parametrize("status", ["denied", "unsupported"])
def test_declared_original_without_values_still_detects_changed_metadata(monkeypatch, status):
    product, _, _, _, _, _, _, result = original_metadata_coverage_setup(
        monkeypatch, status=status
    )
    product.value["changed_count"] += 1
    assert not result.history_check.valid()


def test_undeclared_original_callback_keeps_independent_metadata_check(monkeypatch):
    product, _, _, _, state, _, _, result = original_metadata_coverage_setup(
        monkeypatch, declared=False
    )
    baseline = len(product.calls)
    assert baseline > 1
    assert result.history_check.valid()
    assert len(product.calls) == baseline + 1
    # An ordinary callback can never opt in merely by a truthy attribute.
    product.value["changed_count"] += 1
    assert not result.history_check.valid()
    assert state["reads"]


def test_declared_original_full_reread_cannot_cross_registered_field_deadline(monkeypatch):
    _, _, _, _, state, _, _, result = original_metadata_coverage_setup(monkeypatch)
    state["advance_on_read"] = True
    assert not result.history_check.valid()
    assert not result.history_check.refresh()


@pytest.mark.parametrize("proof", ["missing", "partial", "invalid"])
def test_declared_original_provider_still_requires_complete_valid_proof(proof):
    from app.composition.school_fact_directory_gateway import ReceiptOriginalQueryWithMetadataProof
    from app.platform.assistant.runtime import HistoryCheck
    from app.platform.assistant.tool_loop import ToolExecutionResult

    _, _, gateway, turn, _, _ = receipt_current_setup()
    gateway.receipt_original_query = ReceiptOriginalQueryWithMetadataProof(
        lambda *_args: ToolExecutionResult(
            model_content={"original_results": []},
            history_check=None if proof == "missing" else HistoryCheck(
                lambda: proof != "invalid", revalidation_covers_content=proof != "partial"
            ),
        )
    )
    with pytest.raises(AssistantRuntimeError, match="lack complete evidence"):
        turn.execute(current_call(include_original=True), context())
