from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_product import DirectoryFactProductService
from app.composition.school_fact_family_staff_gateway import (
    MODULE,
    FamilyStaffQueryPort,
    FamilyStaffToolGateway,
)
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.test_f2_directory_product import actor


class Query:
    def query(self, **kwargs):
        assert kwargs["limit"] <= 20
        assert "query_text" in kwargs and "as_of" in kwargs
        assert "related_field" in kwargs and "related_ref" in kwargs
        return {
            "kind": "family_facts",
            "coverage": "authorized_slice",
            "records": [
                {
                    "label": "学生甲",
                    "content": {"display_name": "学生甲"},
                    "factual_at": "2026-09-13T00:00:00Z",
                    "reference": "f3:student:private",
                    "source": {
                        "label": "学校正式学籍",
                        "reference": "source-private",
                        "trace_reference": "trace-private",
                        "published_at": "2026-09-13T00:00:00Z",
                    },
                }
            ],
        }

    def describe(self, **kwargs):
        return {
            "fact_kind": kwargs["fact_kind"],
            "fields": [{"value": "display_name", "label": "姓名"}],
            "creation_choices": [{"label": "新增学生", "creation_ref": "f3create:student:private"}],
        }


class Product:
    @staticmethod
    def _own():
        return {
            "kind": "own_fact_receipt",
            "reference": "sf2receipt_" + "a" * 64,
            "status": "succeeded",
            "message": "完成",
            "changed_count": 1,
            "target_version": 2,
            "request_id": None,
            "confirmed_at": "2026-09-13T00:00:00Z",
            "read_only": True,
            "targets": [
                {
                    "fact_kind": "student",
                    "label": "学生基础资料",
                    "reference": "f3:student:private",
                    "operation": "upsert",
                    "target_version": 2,
                }
            ],
        }

    def own_receipts(self, **kwargs):
        return SimpleNamespace(
            model_dump=lambda **_: {
                "kind": "own_fact_receipts",
                "items": [self._own()],
                "has_more": False,
                "notice": "只读",
            }
        )

    def own_receipt(self, **kwargs):
        return SimpleNamespace(model_dump=lambda **_: self._own())

    def proposal(self, **kwargs):
        return self.prepare(**kwargs)

    def prepare(self, **kwargs):
        return SimpleNamespace(
            proposal_ref="sf3plan_" + "a" * 32,
            model_dump=lambda **_: {
                "proposal_ref": "sf3plan_" + "a" * 32,
                "summary": {"label": "学生甲", "reference": "f3:student:private"},
                "expires_at": "2026-09-13T01:00:00Z",
            },
        )


def gateway(identity=None):
    identity = identity or SimpleNamespace(actor=actor(), session_binding="session")
    return FamilyStaffToolGateway(
        resolve_actor=lambda _: cast(ProductActor, identity),
        query=cast(FamilyStaffQueryPort, Query()),
        product=cast(DirectoryFactProductService, Product()),
    )


def context():
    return ToolCallContext("identity", "conversation", "turn", "run", 1, uuid4())


@pytest.mark.parametrize(
    ("operation", "read_only"),
    [
        ("read", True),
        ("student_profile", True),
        ("describe", True),
        ("prepare", False),
        ("receipt", False),
        ("own_receipt", False),
        ("own_receipts", False),
    ],
)
def test_denied_staff_reads_keep_only_fixed_past_execution_note(operation, read_only):
    owner = gateway()

    def denied(_):
        raise PermissionError("private actor and resource details")

    owner.resolve_actor = denied
    result = owner.execute(
        ModelToolCall(
            call_id="denied",
            name=MODULE + "." + operation,
            arguments={"private_parameter": "sensitive"},
        ),
        context(),
    )
    assert result.history_execution_note == ("read_returned_no_values" if read_only else None)
    assert result.history_check is None and result.fact_card is None
    assert "private" not in str(result.model_content)
    assert "sensitive" not in str(result.model_content)


@pytest.mark.parametrize(
    "stage,fact_kind", [(3, "student"), (4, "education_term"), (5, "teaching_assignment")]
)
def test_maintenance_families_share_authorized_original_result_continuation(stage, fact_kind):
    from app.composition.school_fact_education_gateway import EducationToolGateway
    from app.composition.school_fact_product_contacts import ContactReceipt

    class ReceiptProduct(Product):
        invoked = False
        allowed = True
        reads = 0

        def require_invoked(self, **kwargs):
            if not self.invoked or not self.allowed:
                raise PermissionError("original operation unavailable")

        def receipt(self, **kwargs):
            self.require_invoked(**kwargs)
            self.reads += 1
            return ContactReceipt(
                reference=kwargs["proposal_ref"],
                status="succeeded",
                message="完成",
                changed_count=1,
                target_version=2,
            )

    product = ReceiptProduct()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    if stage == 3:
        owner = FamilyStaffToolGateway(
            resolve_actor=lambda _: identity,
            query=Query(),
            product=product,
        )
    else:
        owner = EducationToolGateway(
            resolve_actor=lambda _: identity,
            query=SimpleNamespace(),
            product=product,
            describe_adapter=SimpleNamespace(),
            teaching_enabled=stage == 5,
        )
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="prepare",
            name=owner.module_ref + ".prepare",
            arguments={
                "fact_kind": fact_kind,
                "target_ref": "f3:student:private",
                "fields": {"status": "active"},
                "reason": "approved correction",
            },
        ),
        context(),
    )
    assert result.continuation is not None
    with pytest.raises(PermissionError):
        result.continuation.load()
    assert product.reads == 0
    product.invoked = True
    evidence = result.continuation.load()
    assert evidence.fact_card["status"] == "succeeded"
    assert evidence.model_content["tool_context"]["action_owner"] == owner.module_ref
    hint = evidence.model_content["tool_context"]["read_original_targets"]
    assert hint["tool"] == owner.module_ref + ".own_receipt"
    assert hint["arguments"]["receipt_ref"].startswith("sf2receipt_")
    assert "sf3plan_" not in str(evidence.model_content)
    assert evidence.history_check.valid()
    assert product.reads == 2
    product.allowed = False
    assert not evidence.history_check.valid()
    with pytest.raises(PermissionError):
        result.continuation.load()


def test_read_material_keeps_labels_source_time_coverage_and_hides_private_refs():
    result = gateway().execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={"fact_kind": "student", "limit": 20, "fields": []},
        ),
        context(),
    )
    assert result.model_content["coverage"] == "authorized_slice"
    row = result.model_content["records"][0]
    assert row["label"] == "学生甲" and row["factual_at"]
    assert row["supporting_evidence"]["source"]["label"] == "学校正式学籍"
    assert row["supporting_evidence"]["source"]["published_at"]
    assert "private" not in str({k: v for k, v in row.items() if k != "tool_context"})
    assert row["tool_context"]["reference"].startswith("f3:student:")
    assert result.fact_card["records"][0]["source"]["trace_reference"] == "trace-private"


def test_describe_creation_reference_is_tool_context_only():
    result = gateway().execute(
        ModelToolCall(call_id="1", name=MODULE + ".describe", arguments={"fact_kind": "student"}),
        context(),
    )
    choice = result.model_content["creation_choices"][0]
    assert choice["label"] == "新增学生"
    assert choice["tool_context"]["creation_ref"].startswith("f3create:")


def test_guardian_shaped_identity_is_denied_before_repository():
    result = gateway(SimpleNamespace(actor=object(), session_binding="guardian")).execute(
        ModelToolCall(
            call_id="1", name=MODULE + ".read", arguments={"fact_kind": "student", "fields": []}
        ),
        context(),
    )
    assert result.model_content["status"] == "denied"


def test_prepare_rejects_f2_kind_and_never_enters_directory_product():
    result = gateway().execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".prepare",
            arguments={
                "fact_kind": "official_contact",
                "target_ref": "f2:official_contact:x",
                "fields": {"contact_value": "x"},
                "reason": "wrong family",
            },
        ),
        context(),
    )
    assert result.model_content["status"] == "rejected"


def test_catalog_has_required_tools_and_module_owner():
    assert {d.name for d in gateway().descriptors} == {
        MODULE + ".student_profile",
        MODULE + ".read",
        MODULE + ".describe",
        MODULE + ".prepare",
        MODULE + ".receipt",
        MODULE + ".own_receipts",
        MODULE + ".own_receipt",
    }
    assert all(d.owner == MODULE for d in gateway().descriptors)


def test_turn_stable_digest_ignores_advancing_observation_time_only():
    query = Query()
    state = {"tick": 0, "label": "学生甲", "valid_from": "2026-09-01T00:00:00Z", "allowed": True}

    def changing(**kwargs):
        if not state["allowed"]:
            raise PermissionError("revoked")
        state["tick"] += 1
        view = Query.query(query, **kwargs)
        view["queried_at"] = f"2026-09-13T00:00:0{state['tick']}Z"
        view["factual_at"] = view["queried_at"]
        view["records"][0]["factual_at"] = view["queried_at"]
        view["records"][0]["label"] = state["label"]
        view["records"][0]["valid_from"] = state["valid_from"]
        view["records"][0]["valid_to"] = None
        return view

    query.query = changing
    owner = FamilyStaffToolGateway(
        resolve_actor=lambda _: cast(
            ProductActor, SimpleNamespace(actor=actor(), session_binding="session")
        ),
        query=cast(FamilyStaffQueryPort, query),
        product=cast(DirectoryFactProductService, Product()),
    )
    turn = owner.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="1", name=MODULE + ".read", arguments={"fact_kind": "student", "fields": []}
        ),
        context(),
    )
    assert result.history_check.revalidation_covers_content
    assert result.history_check.valid()
    state["label"] = "学生乙"
    assert not result.history_check.valid()
    state["label"] = "学生甲"
    state["valid_from"] = "2026-09-02T00:00:00Z"
    assert not result.history_check.valid()
    state["valid_from"] = "2026-09-01T00:00:00Z"
    state["allowed"] = False
    assert not result.history_check.valid()


def test_own_receipt_tools_are_read_only_and_keep_references_in_tool_context():
    page = gateway().execute(
        ModelToolCall(call_id="1", name=MODULE + ".own_receipts", arguments={"limit": 5}),
        context(),
    )
    assert page.model_content["receipts"][0]["outcome"] == "正式修改已完成"
    assert page.model_content["receipts"][0]["tool_context"]["receipt_ref"].startswith(
        "sf2receipt_"
    )
    assert next(d for d in gateway().descriptors if d.name.endswith(".own_receipts")).kind == "read"


def test_prepare_family_owner_does_not_break_proposal_revalidation():
    turn = gateway().for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".prepare",
            arguments={
                "fact_kind": "student",
                "target_ref": "f3:student:private",
                "fields": {"status": "active"},
                "reason": "approved correction",
            },
        ),
        context(),
    )
    assert result.fact_card["family_owner"] == MODULE
    assert result.history_check.valid()


def test_model_material_uses_formal_field_labels_and_keeps_machine_values_for_tools():
    from app.composition.school_fact_family_staff_gateway import _safe_material

    view = {
        "fact_kind": "student_school_relationship",
        "records": [
            {
                "fact_kind": "student_school_relationship",
                "reference": "f3:student_school_relationship:private",
                "expected_version": 4,
                "fields": {
                    "enrollment_status": "enrolled",
                    "residence_mode": "boarding",
                    "primary_campus_ref": "f2:location:private",
                    "student_ref": "f3:student:private",
                },
                "source_reference": "publication-private",
                "source_trace_reference": "trace-private",
            }
        ],
    }
    material = _safe_material(view)
    row = material["records"][0]
    fields = row["fields"]
    assert fields["enrollment_status"] == "在读"
    assert fields["residence_mode"] == "住校"
    assert fields["field_labels"]["primary_campus_ref"] == "主要校区"
    assert fields["tool_context"]["primary_campus_ref"] == "f2:location:private"
    assert fields["tool_context"]["field_values"]["residence_mode"] == "boarding"
    assert "primary_campus_ref" not in {
        k: v for k, v in fields.items() if k not in {"tool_context", "field_labels"}
    }
    assert "expected_version" not in row and row["tool_context"]["expected_version"] == 4
    assert "publication-private" not in str(material) and "trace-private" not in str(material)
    assert view["records"][0]["fields"]["residence_mode"] == "boarding"


class EvidenceQuery(Query):
    def __init__(self, *, supported=True):
        self.reads = 0
        self.current = True
        self.supported = supported
        self.captures = []
        self.actors = []
        self.evidence = self

    @contextmanager
    def capture(self, *, actor, current_time):
        self.captures.append((actor, current_time))
        yield SimpleNamespace(seal=lambda: self if self.supported else None)

    def check(self, current_actor):
        self.actors.append(current_actor)
        return self.current and current_actor == self.captures[-1][0]

    def query(self, **kwargs):
        self.reads += 1
        return super().query(**kwargs)


@pytest.mark.parametrize("as_of", [None, "2026-09-01T00:00:00Z"])
def test_turn_reuses_read_proof_without_reloading_and_revocation_still_closes(as_of):
    query = EvidenceQuery()
    owner = gateway()
    owner.query = query
    turn = owner.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={"fact_kind": "student", "as_of": as_of, "fields": []},
        ),
        context(),
    )
    assert query.reads == 1
    assert query.captures[0][1] is (as_of is None)
    for _ in range(3):
        turn.revalidate("identity")
        assert result.history_check.valid()
    assert query.reads == 1
    assert query.actors
    query.current = False
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("identity")
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("another-identity")


def test_turn_without_supported_read_proof_preserves_full_read_fallback():
    query = EvidenceQuery(supported=False)
    owner = gateway()
    owner.query = query
    turn = owner.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="1", name=MODULE + ".read", arguments={"fact_kind": "student", "fields": []}
        ),
        context(),
    )
    assert result.history_check.valid()
    assert query.reads == 2
    assert not query.actors


def test_turn_proof_revalidates_full_current_identity_and_cannot_survive_a_switch():
    from app.platform import contracts as actor_contracts

    query = EvidenceQuery()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    owner = gateway(identity)
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="1", name=MODULE + ".read", arguments={"fact_kind": "student", "fields": []}
        ),
        context(),
    )
    assert result.history_check.valid()
    identity.actor = actor_contracts._issue_staff_actor_context(
        **{**asdict(identity.actor), "authorization_version": 2}
    )
    assert not result.history_check.valid()


@pytest.mark.parametrize("as_of", ["2026-09-01T00:00:00", "tomorrow", 1])
def test_staff_model_history_rejects_unsupported_time_before_read(as_of):
    query = EvidenceQuery()
    owner = gateway()
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={"fact_kind": "student", "as_of": as_of, "fields": []},
        ),
        context(),
    )
    assert result.model_content["status"] == "rejected"
    assert query.reads == 0
    assert result.history_check is None


def test_incomplete_auxiliary_evidence_keeps_full_read_fallback():
    query = EvidenceQuery()
    original = query.query

    def partial(**kwargs):
        view = original(**kwargs)
        view["coverage"] = {"status": "source_unavailable"}
        return view

    query.query = partial
    owner = gateway()
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="1", name=MODULE + ".read", arguments={"fact_kind": "student", "fields": []}
        ),
        context(),
    )
    assert result.history_check.valid()
    assert query.reads == 2
    assert not query.actors


@pytest.mark.parametrize(
    ("kind", "fields", "selected", "omitted"),
    [
        (
            "person",
            {
                "display_name": "同名学生",
                "business_code": "person-business-42",
                "person_kind": "student",
            },
            ["display_name"],
            ["business_code", "person_kind"],
        ),
        (
            "person_region",
            {
                "district_name": "合成区",
                "district_code": "test-district-code",
                "region_purpose": "student_profile",
            },
            ["district_name"],
            ["district_code", "region_purpose"],
        ),
        (
            "student_school_relationship",
            {
                "student_number": "school-number-42",
                "primary_campus_ref": "f2:location:campus",
                "residence_mode": "boarding",
                "enrollment_status": "enrolled",
            },
            ["student_number", "primary_campus_ref"],
            ["residence_mode", "enrollment_status"],
        ),
    ],
)
def test_staff_model_receives_only_task_fields_but_formal_card_is_complete(
    kind, fields, selected, omitted
):
    import json

    owner = gateway()
    view = {
        "fact_kind": kind,
        "status": "known",
        "records": [
            {
                "fact_kind": kind,
                "label": "当前可读对象",
                "reference": "f3:" + kind + ":current",
                "fields": fields,
            }
        ],
        "coverage": {"status": "unknown"},
    }
    calls = []
    owner.query = SimpleNamespace(query=lambda **kw: (calls.append(kw), view)[1])
    result = owner.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={
                "fact_kind": kind,
                "fields": selected,
            },
        ),
        context(),
    )
    assert result.fact_card == {**view, "family_owner": MODULE}
    assert all("fields" not in call for call in calls)
    material = json.dumps(result.model_content, ensure_ascii=False)
    for field in omitted:
        assert field not in material and fields[field] not in material
    for field in selected:
        assert field in material
    assert "当前可读对象" in material


def test_empty_model_fields_preserve_readable_homonyms_and_exact_references():
    owner = gateway()
    records = [
        {
            "fact_kind": "person",
            "label": "人员 · 同名学生",
            "reference": "f3:person:" + suffix,
            "fields": {"display_name": "同名学生", "business_code": suffix},
        }
        for suffix in ("private-a", "private-b")
    ]
    owner.query = SimpleNamespace(
        query=lambda **_: {
            "fact_kind": "person",
            "status": "known",
            "records": records,
        }
    )
    result = owner.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "person",
                "query_text": "同名学生",
                "fields": [],
            },
        ),
        context(),
    )
    selected = result.model_content["records"]
    assert len(selected) == 2
    assert all(row["label"] == "人员 · 同名学生" and row["fields"] == {} for row in selected)
    assert {row["tool_context"]["reference"] for row in selected} == {
        "f3:person:private-a",
        "f3:person:private-b",
    }
    assert "business_code" not in str(selected)


def test_model_can_explicitly_request_technical_values_without_mislabeling_absent_fields():
    owner = gateway()
    owner.query = SimpleNamespace(
        query=lambda **_: {
            "fact_kind": "person_region",
            "status": "known",
            "records": [
                {
                    "label": "当前地区",
                    "fields": {"district_code": "test-code", "region_purpose": "student_profile"},
                }
            ],
        }
    )
    result = owner.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "person_region",
                "fields": ["district_code", "region_purpose", "district_name"],
            },
        ),
        context(),
    )
    row = result.model_content["records"][0]
    assert row["fields"]["district_code"] == "test-code"
    assert row["fields"]["region_purpose"] == "学生档案"
    assert row["unreturned_fields"] == ["district_name"]
    assert "missing" not in row


@pytest.mark.parametrize(
    "fields", [None, ["unknown"], ["district_code"], ["status", "status"], [1]]
)
def test_model_fields_must_be_explicit_unique_and_belong_to_the_fact_kind(fields):
    query = EvidenceQuery()
    owner = gateway()
    owner.query = query
    arguments = {"fact_kind": "student"}
    if fields is not None:
        arguments["fields"] = fields
    output = owner.execute(
        ModelToolCall(call_id="1", name=MODULE + ".read", arguments=arguments), context()
    )
    assert output.model_content["status"] == "rejected"
    assert query.reads == 0


def test_direct_http_read_contract_does_not_require_model_field_selection():
    from app.composition.school_fact_family_staff_gateway import FamilyReadInput

    payload = FamilyReadInput.model_validate({"fact_kind": "student"})
    assert payload.fact_kind == "student" and "fields" not in payload.model_dump()
    schema = next(d.parameters for d in gateway().descriptors if d.name.endswith(".read"))
    assert "fields" in schema["required"]


def test_student_profile_model_excludes_selection_evidence_and_rechecks_full_fallback():
    owner = gateway()
    state = {"version": 1}
    calls = []

    def profile(**kw):
        calls.append(kw)
        return {
            "kind": "f3_student_profile",
            "status": "known",
            "profile": {"region": {"status": "known", "value": "当前区县"}},
            "sources": {"region": {"label": "正式学生资料"}},
            "evidence": [
                {"version": state["version"], "fields": {"student_number": "selection-only"}}
            ],
            "tool_context": {"student_ref": "f3:student:private"},
        }

    owner.query = SimpleNamespace(student_profile=profile)
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="profile",
            name=MODULE + ".student_profile",
            arguments={
                "name": "同名学生",
                "student_number": "selection-only",
                "fields": ["region"],
            },
        ),
        context(),
    )
    assert result.model_content["profile"]["region"]["value"] == "当前区县"
    assert "selection-only" not in str(result.model_content)
    assert "evidence" in result.fact_card
    assert result.model_content["tool_context"]["student_ref"] == "f3:student:private"
    assert result.history_check.valid()
    state["version"] = 2
    assert not result.history_check.valid()
    assert all(call["fields"] == ("region",) for call in calls)


@pytest.mark.parametrize("change", ["campus", "permission", "time_boundary"])
def test_student_profile_full_fallback_runs_once_per_release_and_after_model_wait(change):
    owner = gateway()
    state = {"campus": "原校区", "allowed": True, "effective": "current"}
    calls = []

    def profile(**kwargs):
        calls.append(kwargs)
        if not state["allowed"]:
            raise PermissionError("current campus reading revoked")
        return {
            "kind": "f3_student_profile",
            "status": "known",
            "profile": {"campus": {"status": "known", "value": state["campus"]}},
            "evidence": [{"effective": state["effective"]}],
        }

    owner.query = SimpleNamespace(student_profile=profile)
    turn = owner.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="profile",
            name=MODULE + ".student_profile",
            arguments={"name": "学生", "fields": ["campus"]},
        ),
        context(),
    )
    assert result.history_check is not None
    for _ in range(2):
        before = len(calls)
        with turn.validation_scope():
            turn.revalidate("identity")
            assert result.history_check.valid()
        assert len(calls) == before + 1

    with pytest.raises(AssistantRuntimeError):
        with turn.validation_scope():
            turn.revalidate("identity")
            assert result.history_check.valid()
            if change == "campus":
                state["campus"] = "新校区"
            elif change == "permission":
                state["allowed"] = False
            else:
                state["effective"] = "expired"
    assert not result.history_check.valid()
    state.update(campus="原校区", allowed=True, effective="current")
    assert result.history_check.valid()


def test_student_profile_different_selections_keep_independent_history_checks():
    owner = gateway()
    calls = []

    def profile(**kwargs):
        calls.append(kwargs["name"])
        return {"kind": "f3_student_profile", "status": "object_unresolved", "evidence": []}

    owner.query = SimpleNamespace(student_profile=profile)
    turn = owner.for_turn("identity")
    results = [
        turn.execute(
            ModelToolCall(
                call_id=name,
                name=MODULE + ".student_profile",
                arguments={"name": name, "fields": ["region"]},
            ),
            context(),
        )
        for name in ("学生甲", "学生乙")
    ]
    first, second = (result.history_check for result in results)
    assert first is not None and second is not None
    assert first.key != second.key
    calls.clear()
    with turn.validation_scope():
        turn.revalidate("identity")
        assert first.valid() and second.valid()
    assert calls == ["学生甲", "学生乙"]


@pytest.mark.parametrize("timing", ["queued", "inside_reader"])
def test_student_profile_fallback_rechecks_identity_before_and_after_read(timing):
    owner = gateway()
    identity = [SimpleNamespace(actor=actor(), session_binding="session")]
    owner.resolve_actor = lambda _: identity[0]
    change_inside = [False]

    def logout():
        identity[0] = SimpleNamespace(actor=actor(), session_binding="changed-session")

    def profile(**_):
        if change_inside[0]:
            logout()
        return {"kind": "f3_student_profile", "status": "known", "profile": {"campus": "校区"}}

    owner.query = SimpleNamespace(student_profile=profile)
    turn = owner.for_turn("identity")
    result = turn.execute(
        ModelToolCall(
            call_id="profile",
            name=MODULE + ".student_profile",
            arguments={"name": "学生", "fields": ["campus"]},
        ),
        context(),
    )
    assert result.history_check is not None
    with pytest.raises(AssistantRuntimeError):
        with turn.validation_scope():
            assert result.history_check.valid()
            if timing == "queued":
                logout()
            else:
                change_inside[0] = True


@pytest.mark.parametrize("fields", [[], ["region", "region"], ["guardian"], None])
def test_student_profile_rejects_invalid_field_contract_before_any_read(fields):
    owner = gateway()
    owner.query = SimpleNamespace(student_profile=lambda **_: pytest.fail("must not read"))
    arguments = {"name": "同名学生"}
    if fields is not None:
        arguments["fields"] = fields
    result = owner.execute(
        ModelToolCall(call_id="profile", name=MODULE + ".student_profile", arguments=arguments),
        context(),
    )
    assert result.model_content["status"] == "rejected"


@pytest.mark.parametrize("incomplete", [False, True])
def test_student_profile_aggregates_complete_f3_evidence_only(incomplete):
    query = EvidenceQuery()

    def profile(**_):
        query.reads += 1
        return {
            "kind": "f3_student_profile",
            "status": "incomplete" if incomplete else "known",
            "profile": {"region": {"status": "forbidden" if incomplete else "known"}},
            "evidence": [{"fact_kind": "person"}, {"fact_kind": "student"}],
        }

    query.student_profile = profile
    owner = gateway()
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="profile",
            name=MODULE + ".student_profile",
            arguments={"name": "学生", "fields": ["region"]},
        ),
        context(),
    )
    assert len(query.captures) == 1 and query.reads == 1
    assert result.history_check.valid()
    assert query.reads == (2 if incomplete else 1)
    if not incomplete:
        query.current = False
        assert not result.history_check.valid()


def test_student_profile_rechecks_location_owner_name_instead_of_sealing_only_f3_proof():
    from app.composition.school_fact_family_product_adapter import FamilyStaffProductQuery
    from app.composition.school_fact_family_services import DirectoryFamilyReferences
    from tests.test_f3_family_maintenance_adapter import NOW, ProfileRepository

    state = {"campus": "原正式校区"}
    locations_read = []

    def location(**kw):
        locations_read.append(kw)
        return SimpleNamespace(
            status="known",
            has_more=False,
            records=(
                SimpleNamespace(
                    content={"display_name": state["campus"]},
                    fact_ref="f2:location:campus",
                    version=1,
                    valid_from=NOW,
                    valid_to=None,
                    publication_ref="publication:campus",
                    published_at=NOW,
                ),
            ),
        )

    external = DirectoryFamilyReferences(
        SimpleNamespace(
            query=location,
            reference_for=lambda **_: "f2:location:campus",
        )
    )
    repository = ProfileRepository()
    backend = EvidenceQuery()
    backend.unsupported = lambda: setattr(backend, "supported", False)
    repository.evidence = backend
    owner = gateway()
    owner.query = FamilyStaffProductQuery(repository, None, external)
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="profile",
            name=MODULE + ".student_profile",
            arguments={
                "name": "同名学生",
                "student_number": "N-b",
                "fields": ["campus"],
            },
        ),
        context(),
    )
    assert not backend.supported
    assert result.model_content["profile"]["campus"]["value"] == "原正式校区"
    assert result.history_check.valid()
    assert len(locations_read) == 2
    assert all(
        call["fact_kind"] == "location" and call["reference"] == "f2:location:campus"
        for call in locations_read
    )
    state["campus"] = "已改名校区"
    assert not result.history_check.valid()


def test_task_fields_do_not_weaken_full_read_fallback_invalidation():
    owner = gateway()
    state = {"district_code": "before-code"}
    owner.query = SimpleNamespace(
        query=lambda **_: {
            "fact_kind": "person_region",
            "status": "known",
            "records": [
                {
                    "fact_kind": "person_region",
                    "label": "学生地区",
                    "fields": {"district_name": "合成区", "district_code": state["district_code"]},
                }
            ],
        }
    )
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "person_region",
                "fields": ["district_name"],
            },
        ),
        context(),
    )
    assert "before-code" not in str(result.model_content)
    assert result.history_check.valid()
    state["district_code"] = "after-code"
    assert not result.history_check.valid()


def test_model_task_fields_do_not_trim_formal_maintenance_preview():
    owner = gateway()
    preview = {
        "proposal_ref": "sf3plan_" + "a" * 32,
        "summary": {
            "label": "同名学生（学号test-42）的区县资料",
            "fields": {
                "district_name": "合成区",
                "district_code": "new-code",
                "region_purpose": "student_profile",
            },
            "differences": [{"field": "district_code", "before": "old-code", "after": "new-code"}],
        },
    }
    owner.product = SimpleNamespace(
        prepare=lambda **_: SimpleNamespace(
            proposal_ref=preview["proposal_ref"],
            model_dump=lambda **_: preview,
        )
    )
    result = owner.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".prepare",
            arguments={
                "fact_kind": "person_region",
                "target_ref": "f3:person_region:current",
                "fields": {"district_code": "new-code", "district_name": "合成区"},
                "reason": "用户确认的地区纠正",
            },
        ),
        context(),
    )
    assert result.fact_card["summary"] == preview["summary"]
    assert result.model_content["summary"]["fields"]["district_code"] == "new-code"
    assert result.module_result.action_offer is not None


def test_selected_nullable_relation_keeps_authorized_null_without_exposing_raw_source_keys():
    owner = gateway()
    owner.query = SimpleNamespace(
        query=lambda **_: {
            "fact_kind": "guardian_relationship",
            "status": "known",
            "records": [
                {
                    "label": "当前家校关系",
                    "fields": {
                        "review_of_relationship_source_key": None,
                        "guardian_person_source_key": "private-canonical-source",
                    },
                }
            ],
        }
    )
    result = owner.execute(
        ModelToolCall(
            call_id="1",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "guardian_relationship",
                "fields": ["review_of_relationship_ref", "guardian_person_ref"],
            },
        ),
        context(),
    )
    row = result.model_content["records"][0]
    assert row["fields"]["tool_context"]["review_of_relationship_ref"] is None
    assert row["unreturned_fields"] == ["guardian_person_ref"]
    assert "source_key" not in str(result.model_content)
    assert "private-canonical-source" not in str(result.model_content)


class ReceiptProduct(Product):
    def __init__(self):
        self.prepares = 0
        self.receipt_reads = []
        self.status = "succeeded"
        self.read_denied = False

    def prepare(self, **kwargs):
        self.prepares += 1
        return self.proposal(proposal_ref="sf3plan_" + f"{self.prepares:032x}")

    def proposal(self, **kwargs):
        ref = kwargs["proposal_ref"]
        return SimpleNamespace(
            proposal_ref=ref,
            model_dump=lambda **_: {
                "proposal_ref": ref,
                "summary": {"label": "原对象"},
                "expires_at": "2026-09-13T01:00:00Z",
            },
        )

    def receipt(self, **kwargs):
        if self.read_denied:
            raise PermissionError("current read authorization revoked")
        ref = kwargs["proposal_ref"]
        self.receipt_reads.append(ref)
        return SimpleNamespace(
            status=self.status,
            model_dump=lambda **_: {
                "reference": ref,
                "status": self.status,
                "message": "原操作回执",
                "changed_count": 1 if self.status == "succeeded" else None,
                "target_version": 22 if self.status == "succeeded" else None,
                "request_id": None,
            },
        )


def prepare_action(owner, ctx=None):
    return owner.execute(
        ModelToolCall(
            call_id="prepare",
            name=MODULE + ".prepare",
            arguments={
                "fact_kind": "person_region",
                "target_ref": "f3:person_region:private",
                "fields": {"district_name": "合成区", "district_code": "330110"},
                "reason": "核实更正",
            },
        ),
        ctx or context(),
    )


def receipt_call(**arguments):
    return ModelToolCall(call_id="receipt", name=MODULE + ".receipt", arguments=arguments)


@pytest.mark.parametrize("status", ["succeeded", "rejected", "unknown"])
def test_receipt_is_read_only_and_defaults_to_same_bound_recent_action(status):
    owner = gateway()
    product = ReceiptProduct()
    product.status = status
    owner.product = product
    prepared = prepare_action(owner)
    number = prepared.model_content["tool_context"]["action_number"]
    assert prepared.fact_card["proposal_ref"] not in str(prepared.model_content)
    result = owner.execute(receipt_call(), context())
    assert result.fact_card["reference"] == prepared.fact_card["proposal_ref"]
    assert result.model_content["tool_context"] == {
        "action_number": number,
        "action_owner": MODULE,
        **({"school_fact_version": 22} if status == "succeeded" else {}),
    }
    assert "school_fact_version" not in result.model_content
    assert result.module_result is None and product.prepares == 1
    if status == "unknown":
        assert "不重新提交" in result.model_content["recovery"]


@pytest.mark.parametrize("change", ["context", "conversation", "session", "actor"])
def test_recent_action_and_explicit_number_do_not_cross_identity_boundaries(change):
    owner = gateway()
    product = ReceiptProduct()
    owner.product = product
    original = context()
    prepared = prepare_action(owner, original)
    number = prepared.model_content["tool_context"]["action_number"]
    current = original
    if change == "context":
        current = replace(original, actor_context_ref="another-identity")
    elif change == "conversation":
        current = replace(original, conversation_ref="another-conversation")
    elif change == "session":
        owner.resolve_actor = lambda _: SimpleNamespace(actor=actor(), session_binding="another")
    else:
        from app.platform import contracts as actors

        changed_actor = actors._issue_staff_actor_context(
            **{**asdict(actor()), "assignment_id": uuid4()}
        )
        owner.resolve_actor = lambda _: SimpleNamespace(
            actor=changed_actor,
            session_binding="session",
        )
    for call in (receipt_call(), receipt_call(action_number=number)):
        result = owner.for_turn(current.actor_context_ref).execute(call, current)
        assert result.model_content["status"] == "clarification_required"
        assert result.history_check is None and result.fact_card is None
    assert product.receipt_reads == []


def test_expired_or_invalid_explicit_action_never_falls_back_to_new_recent_action():
    owner = gateway()
    product = ReceiptProduct()
    owner.product = product
    first = prepare_action(owner)
    old_number = first.model_content["tool_context"]["action_number"]
    owner.purge_expired(now=datetime.now(UTC) + timedelta(days=1, seconds=1))
    second = prepare_action(owner)
    new_number = second.model_content["tool_context"]["action_number"]
    assert new_number > old_number
    for number in (old_number, new_number + 1):
        result = owner.execute(receipt_call(action_number=number), context())
        assert result.model_content["status"] == "clarification_required"
    assert product.receipt_reads == []
    result = owner.execute(receipt_call(action_number=new_number), context())
    assert result.fact_card["reference"] == second.fact_card["proposal_ref"]


def test_receipt_history_remains_bound_to_original_and_rechecks_current_read_authorization():
    owner = gateway()
    product = ReceiptProduct()
    owner.product = product
    first = prepare_action(owner)
    result = owner.for_turn("identity").execute(receipt_call(), context())
    prepare_action(owner)
    assert result.history_check.valid()
    assert set(product.receipt_reads) == {first.fact_card["proposal_ref"]}
    product.read_denied = True
    assert not result.history_check.valid()
    denied = owner.execute(receipt_call(), context()).model_content
    assert denied["status"] == "denied"
    assert "不能据此判断原操作结果" in denied["reason"]
    assert "不重新执行" in denied["reason"]
    assert product.prepares == 2


def test_recent_receipt_crossing_expiry_does_not_recreate_action_locator():
    owner = gateway()
    product = ReceiptProduct()
    owner.product = product
    prepared = prepare_action(owner)
    original_read = product.receipt

    def expires(**kwargs):
        receipt = original_read(**kwargs)
        owner.purge_expired(now=datetime.now(UTC) + timedelta(days=1, seconds=1))
        return receipt

    product.receipt = expires
    result = owner.execute(receipt_call(), context())
    assert result.model_content["status"] == "rejected"
    assert result.fact_card is None
    number = prepared.model_content["tool_context"]["action_number"]
    assert (
        owner.execute(receipt_call(action_number=number), context()).model_content["status"]
        == "clarification_required"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"action_number": True},
        {"action_number": 0},
        {"proposal_ref": "invented"},
        {"action_number": 1, "proposal_ref": "sf3plan_" + "a" * 32},
    ],
)
def test_receipt_rejects_malformed_or_ambiguous_selector(arguments):
    assert (
        gateway().execute(receipt_call(**arguments), context()).model_content["status"]
        == "rejected"
    )


@pytest.mark.parametrize("action", ["receipt", "own_receipt", "own_receipts"])
def test_unavailable_receipt_does_not_relabel_successful_original_operation(action):
    from app.composition.school_fact_product_contacts import ContactProductUnavailable

    owner = gateway()
    product = ReceiptProduct()
    owner.product = product
    prepare_action(owner)
    original = owner.execute(receipt_call(), context())
    assert original.fact_card["status"] == "succeeded"

    def unavailable(**kwargs):
        raise ContactProductUnavailable("original lookup currently unavailable")

    setattr(product, action, unavailable)
    arguments = {"receipt_ref": "sf2receipt_" + "a" * 64} if action == "own_receipt" else {}
    result = owner.execute(
        ModelToolCall(call_id="unavailable", name=MODULE + "." + action, arguments=arguments),
        context(),
    )
    assert result.model_content["status"] == "rejected"
    assert "不能据此判断原操作结果" in result.model_content["reason"]
    assert "不重新执行" in result.model_content["reason"]
    assert "没有执行写入" not in result.model_content["reason"]
    assert result.fact_card is None and result.module_result is None
    assert original.fact_card["status"] == "succeeded" and product.prepares == 1


def test_action_number_contract_is_explicitly_scoped_to_family_tools():
    owner = gateway()
    descriptor = next(item for item in owner.descriptors if item.name == MODULE + ".receipt")
    assert (
        "不能代入其他领域编号"
        in descriptor.parameters["properties"]["action_number"]["description"]
    )
    prepared = prepare_action(owner)
    assert prepared.model_content["tool_context"]["action_owner"] == MODULE


@pytest.mark.parametrize("status,count", [("known", 1), ("ambiguous", 2)])
def test_read_card_has_explicit_family_owner_without_changing_model_or_query_evidence(
    status, count
):
    owner = gateway()
    view = {
        "fact_kind": "person",
        "status": status,
        "records": [
            {
                "label": "同名学生",
                "reference": f"f3:person:opaque-{index}",
                "fields": {"display_name": "同名学生"},
            }
            for index in range(count)
        ],
    }
    owner.query = SimpleNamespace(query=lambda **_: view)
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="read",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "person",
                "query_text": "同名学生",
                "fields": ["display_name"],
            },
        ),
        context(),
    )
    assert result.fact_card == {**view, "family_owner": MODULE}
    assert "family_owner" not in view and "family_owner" not in str(result.model_content)
    assert result.history_check.valid()
    view["records"][0]["fields"]["display_name"] = "实际资料已变化"
    assert not result.history_check.valid()


def test_prepare_schema_declares_only_existing_f3_owner_kinds():
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_staff_gateway import FamilyModelPrepareInput
    from app.platform.school_facts.f3_policy import F3_FACT_KINDS

    schema = FamilyModelPrepareInput.model_json_schema()
    field = schema["properties"]["fact_kind"]
    assert set(schema["$defs"][field["$ref"].split("/")[-1]]["enum"]) == F3_FACT_KINDS
    descriptor = next(d for d in FamilyStaffToolGateway.descriptors if d.name.endswith(".prepare"))
    assert descriptor.parameters == schema
    assert FamilyStaffToolGateway.prepare_input_model is FactChangeInput
    old = FactChangeInput.model_json_schema()
    assert {k: v for k, v in schema["properties"].items() if k != "fact_kind"} == {
        k: v for k, v in old["properties"].items() if k != "fact_kind"
    }
    assert schema["required"] == old["required"]
    assert schema["additionalProperties"] is False
    for kind in F3_FACT_KINDS:
        args = dict(
            fact_kind=kind,
            target_ref="f3:" + kind + ":reference",
            fields={},
            reason="Explicit reason",
        )
        assert FamilyModelPrepareInput.model_validate(args).fact_kind == kind
    with pytest.raises(ValueError):
        FamilyModelPrepareInput.model_validate(
            dict(
                fact_kind="teaching_assignment",
                target_ref="f4:teaching_assignment:reference",
                fields={},
                reason="Explicit reason",
            )
        )
    with pytest.raises(ValueError):
        FamilyModelPrepareInput.model_validate(
            dict(fact_kind="student", target_ref="f3:student:reference", fields={}, reason=123)
        )


def preparation_feedback_fixture(stage):
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.composition.school_fact_education_gateway import EducationToolGateway

    class CheckedProduct(Product):
        calls = 0
        failure = None

        def prepare(self, **kwargs):
            self.calls += 1
            if self.failure is not None:
                raise self.failure
            if stage == 2:
                return SimpleNamespace(
                    proposal_ref="sf2plan_" + "a" * 32,
                    model_dump=lambda **_: {
                        "proposal_ref": "sf2plan_" + "a" * 32,
                        "summary": {"fact_kind": "person", "operation": "upsert"},
                    },
                )
            return super().prepare(**kwargs)

    product = CheckedProduct()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    if stage == 2:
        owner = DirectoryFactToolGateway(
            resolve_actor=lambda _: identity,
            query=SimpleNamespace(),
            product=product,
            retention_seconds=60,
        )
    elif stage == 3:
        owner = FamilyStaffToolGateway(
            resolve_actor=lambda _: identity,
            query=Query(),
            product=product,
        )
    else:
        owner = EducationToolGateway(
            resolve_actor=lambda _: identity,
            query=SimpleNamespace(),
            product=product,
            describe_adapter=SimpleNamespace(),
            teaching_enabled=stage == 5,
        )
    arguments = {
        "fact_kind": {2: "person", 3: "student", 4: "education_term", 5: "teaching_assignment"}[
            stage
        ],
        "target_ref": "synthetic-private-target",
        "fields": {},
        "reason": "approved change",
    }
    call = ModelToolCall(
        call_id="feedback",
        name=("school_facts.directory" if stage == 2 else owner.module_ref) + ".prepare",
        arguments=arguments,
    )
    return owner, product, call


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
def test_prepare_schema_feedback_is_recoverable_before_product_or_action(stage):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments)
    del bad["reason"]
    bad["effective_at"] = "2026-09-15T00:00:00"
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    assert result.model_content == {
        "status": "rejected",
        "reason": "参数、对象或候选不可用；没有执行写入。",
        "code": "invalid_arguments",
        "errors": [
            {"type": "timezone_aware", "loc": ["effective_at"]},
            {"type": "missing", "loc": ["reason"]},
        ],
    }
    assert product.calls == 0
    assert result.fact_card is None and result.continuation is None
    assert result.module_result is None and result.history_check is None
    corrected = owner.execute(call, context())
    assert product.calls == 1 and corrected.continuation is not None


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
def test_prepare_schema_feedback_never_echoes_extra_keys_values_or_unbounded_errors(stage):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments)
    bad.update({f"private-field-{i}": "private-value" for i in range(40)})
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    material = result.model_content
    assert material["code"] == "invalid_arguments"
    assert len(material["errors"]) == 8
    assert all(e == {"type": "extra_forbidden", "loc": ["*"]} for e in material["errors"])
    assert "private" not in str(material)
    assert all(set(e) == {"type", "loc"} for e in material["errors"])
    assert product.calls == 0 and result.continuation is None


@pytest.mark.parametrize("stage", [4, 5])
def test_prepare_schema_feedback_preserves_declared_nested_paths_only(stage):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments)
    nested = dict(call.arguments, reason=[], private_nested_key="private-value")
    bad["additional_changes"] = [nested]
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    assert result.model_content["code"] == "invalid_arguments"
    assert {(e["type"], tuple(e["loc"])) for e in result.model_content["errors"]} == {
        ("string_type", ("additional_changes", 0, "reason")),
        ("extra_forbidden", ("additional_changes", 0, "*")),
    }
    assert "private" not in str(result.model_content)
    assert product.calls == 0


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
def test_prepare_schema_model_validator_does_not_expose_exception_context(stage):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments, creation_ref="private-second-target")
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    assert result.model_content["code"] == "invalid_arguments"
    assert result.model_content["errors"] == [
        {
            "type": "value_error",
            "loc": [],
            "code": "fact_target_choice",
            "reason": (
                "target_ref和creation_ref必须且只能填写一个；修改已有对象用target_ref，创建用creation_ref。"
            ),
        }
    ]
    assert "private" not in str(result.model_content)
    assert product.calls == 0


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
@pytest.mark.parametrize(
    "violation,code",
    [
        ("missing_target", "fact_target_choice"),
        ("creation_operation", "fact_creation_operation"),
        ("reserved_fields", "fact_reserved_fields"),
    ],
)
def test_fixed_prepare_constraints_are_distinguishable_and_recoverable(stage, violation, code):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments)
    if violation == "missing_target":
        del bad["target_ref"]
    elif violation == "creation_operation":
        bad.update(creation_ref=bad.pop("target_ref"), operation="end")
    else:
        bad["fields"] = {"source_key": "private-sentinel"}
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    error = result.model_content["errors"][0]
    assert error["type"] == "value_error" and error["code"] == code
    assert error["loc"] == [] and error["reason"]
    assert "private-sentinel" not in str(result.model_content)
    assert product.calls == 0 and result.module_result is None
    corrected = owner.execute(call, context())
    assert product.calls == 1 and corrected.continuation is not None


@pytest.mark.parametrize("stage", [2, 3])
def test_non_education_correction_has_its_own_fixed_input_reason(stage):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments, operation="correct")
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    assert result.model_content["errors"][0]["code"] == "fact_education_operation"
    assert product.calls == 0


@pytest.mark.parametrize("stage", [4, 5])
@pytest.mark.parametrize(
    "violation,code,location",
    [
        ("nested_target", "fact_target_choice", ["additional_changes", 0]),
        ("duplicate_target", "education_duplicate_target", []),
        ("wrong_family", "education_batch_family", []),
        ("invalid_cancel_from", "education_cancel_from", []),
        ("relationship_correction", "education_relationship_correction", []),
    ],
)
def test_education_constraints_keep_safe_reason_and_nested_location(
    stage, violation, code, location
):
    owner, product, call = preparation_feedback_fixture(stage)
    bad = dict(call.arguments)
    if violation == "nested_target":
        nested = dict(call.arguments, creation_ref="private-second-target")
        bad["additional_changes"] = [nested]
    elif violation == "duplicate_target":
        bad["additional_changes"] = [dict(call.arguments)]
    elif violation == "wrong_family":
        bad["fact_kind"] = "person"
    elif violation == "invalid_cancel_from":
        bad["cancel_from"] = "2026-09-21T00:00:00"
    else:
        bad.update(fact_kind="student_school_relationship", operation="correct")
        bad["creation_ref"] = bad.pop("target_ref")
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    error = result.model_content["errors"][0]
    assert error["code"] == code and error["loc"] == location and error["reason"]
    assert "private-second-target" not in str(result.model_content)
    assert product.calls == 0 and result.module_result is None


def test_cancel_and_reschedule_remain_distinct_in_safe_constraint_feedback():
    owner, product, call = preparation_feedback_fixture(5)
    bad = dict(
        call.arguments,
        operation="cancel",
        cancel_from="2026-09-21T00:00:00Z",
        reschedule_from="2026-09-22T00:00:00Z",
    )
    result = owner.execute(call.model_copy(update={"arguments": bad}), context())
    assert result.model_content["errors"][0]["code"] == "education_cancel_reschedule"
    assert product.calls == 0


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
def test_prepare_schema_describes_target_choice_without_changing_required_fields(stage):
    owner, _, call = preparation_feedback_fixture(stage)
    schema = next(d.parameters for d in owner.descriptors if d.name == call.name)
    assert schema["required"] == ["fact_kind", "fields", "reason"]
    assert "必须且只能" in schema["properties"]["target_ref"]["description"]
    assert "target_ref" in schema["properties"]["creation_ref"]["description"]


@pytest.mark.parametrize("stage", [2, 3, 4, 5])
@pytest.mark.parametrize("failure_kind", ["value", "unavailable", "validation", "permission"])
def test_prepare_owner_failure_is_not_misreported_as_input_feedback(stage, failure_kind):
    from pydantic import ValidationError

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_product_contacts import ContactProductUnavailable

    owner, product, call = preparation_feedback_fixture(stage)
    if failure_kind == "validation":
        try:
            FactChangeInput.model_validate({"private-owner-key": "private-owner-value"})
        except ValidationError as error:
            product.failure = error
    else:
        product.failure = {
            "value": ValueError,
            "unavailable": ContactProductUnavailable,
            "permission": PermissionError,
        }[failure_kind]("private-owner-message")
    result = owner.execute(call, context())
    assert product.calls == 1
    assert result.model_content["status"] == (
        "denied" if failure_kind == "permission" else "rejected"
    )
    if failure_kind == "permission" or stage == 2:
        assert "code" not in result.model_content
    else:
        assert result.model_content["code"] == "owner_rejected"
    assert "errors" not in result.model_content and "private" not in str(result.model_content)
    assert result.continuation is None and result.module_result is None


@pytest.mark.parametrize("stage", [3, 4, 5])
def test_prepare_identity_denial_precedes_schema_feedback(stage):
    owner, product, call = preparation_feedback_fixture(stage)

    def denied(_):
        raise PermissionError("private-identity-detail")

    owner.resolve_actor = denied
    result = owner.execute(call.model_copy(update={"arguments": {}}), context())
    assert result.model_content["status"] == "denied"
    assert "errors" not in result.model_content and "code" not in result.model_content
    assert product.calls == 0 and "private" not in str(result.model_content)


def test_prepare_schema_feedback_sanitizes_unknown_types_and_dict_paths():
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_staff_gateway import _prepare_argument_errors

    error = ValidationError.from_exception_data(
        "synthetic-private-title",
        [
            {
                "type": PydanticCustomError("private-type", "private-message"),
                "loc": ("fields", "reason", "private-deep-key"),
                "input": {"private-input": "private-value"},
            }
        ],
    )
    assert _prepare_argument_errors(error, FactChangeInput) == [
        {"type": "value_error", "loc": ["fields", "*"]},
    ]


@pytest.mark.parametrize(
    "custom_type,message,extra_context",
    [
        ("private-error-type", "private-message", None),
        ("fact_target_choice", "private-message", None),
        ("fact_target_choice", None, {"private-context": "private-value"}),
    ],
)
def test_custom_input_errors_cannot_claim_static_reason_with_untrusted_message_or_context(
    custom_type, message, extra_context
):
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_staff_gateway import _prepare_argument_errors

    error = ValidationError.from_exception_data(
        "private-title",
        [
            {
                "type": PydanticCustomError(
                    custom_type,
                    message
                    or (
                        "target_ref和creation_ref必须且只能填写一个；修改已有对象用target_ref，创建用creation_ref。"
                    ),
                    extra_context,
                ),
                "loc": (),
                "input": {"private-input": "private-value"},
            }
        ],
    )
    assert _prepare_argument_errors(error, FactChangeInput) == [
        {"type": "value_error", "loc": []},
    ]


@pytest.mark.parametrize("name", [None, "原名"])
def test_profile_student_number_selection_survives_history_without_invented_name(name):
    owner = gateway()
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return {"kind": "f3_student_profile", "status": "object_unresolved", "evidence": []}

    owner.query = SimpleNamespace(student_profile=read)
    arguments = {"student_number": "S-001", "fields": ["campus"]}
    if name is not None:
        arguments["name"] = name
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="profile-number", name=MODULE + ".student_profile", arguments=arguments
        ),
        context(),
    )
    assert calls and calls[0]["name"] == name
    assert result.history_check.valid()
    assert all(row["name"] == name and row["student_number"] == "S-001" for row in calls)


def test_profile_requires_name_or_student_number_not_whitespace():
    from pydantic import ValidationError

    from app.composition.school_fact_family_staff_gateway import FamilyStudentProfileInput

    for selection in ({}, {"name": "  "}, {"student_number": "  "}):
        with pytest.raises(ValidationError):
            FamilyStudentProfileInput.model_validate({**selection, "fields": ["campus"]})
    assert "name" not in FamilyStudentProfileInput.model_json_schema().get("required", [])


def test_read_student_number_is_exact_ssr_selection_and_reread_keeps_it():
    from pydantic import ValidationError

    from app.composition.school_fact_family_staff_gateway import FamilyReadInput

    for extra in (
        {"fact_kind": "person"},
        {"query_text": "姓名"},
        {"reference": "opaque:ref"},
        {"related_field": "student_ref", "related_ref": "opaque:student"},
    ):
        with pytest.raises(ValidationError):
            FamilyReadInput.model_validate(
                {"fact_kind": "student_school_relationship", "student_number": "S-001", **extra}
            )
    owner = gateway()
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return {"kind": "family_facts", "status": "no_current_records", "records": []}

    owner.query = SimpleNamespace(query=read)
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="read-number",
            name=MODULE + ".read",
            arguments={
                "fact_kind": "student_school_relationship",
                "student_number": "S-001",
                "fields": ["residence_mode"],
            },
        ),
        context(),
    )
    assert calls and calls[0]["student_number"] == "S-001"
    assert result.history_check.valid()
    assert all(row["query_text"] is None and row["student_number"] == "S-001" for row in calls)
