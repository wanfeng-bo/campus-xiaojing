"""F3 quality creation stays inside the existing confirmed school-fact product."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.test_secure_reader_projection import _actor

AT = datetime(2026, 9, 24, tzinfo=UTC)


def test_existing_non_family_quality_uses_exact_kind_only_before_routing():
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter

    actor = _actor()
    seen = []
    adapter = FamilyQualityProductAdapter(
        directory=SimpleNamespace(repository=SimpleNamespace(
            source_for=lambda **_kwargs: "quality:exact"
        )),
        family=None,
        guard=None,
        existing=SimpleNamespace(target_kind=lambda *_args: pytest.fail("F3 detail was read")),
        existing_target_kind=lambda _actor, key: (seen.append(key), "organization")[1],
    )
    payload = SimpleNamespace(
        fact_kind="fact_quality", creation_ref=None, target_ref="f2:quality:exact"
    )
    assert adapter.recognizes(actor, payload) is False
    assert seen == ["quality:exact"]


@pytest.mark.parametrize("kind,field", [
    ("student_school_relationship", "student_number"),
    ("login_mapping", "status"),
    ("student_contact", "priority"),
    ("guardian_relationship", "status"),
    ("person_region", "district_name"),
])
def test_f3_exact_complete_prepares_typed_proof_for_each_formal_target(kind, field):
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
    from app.platform.school_facts.f7_coverage_basis import mother_set_digest

    actor = _actor()
    owner_content = (
        {"person_source_key": "person:a"}
        if kind in {"login_mapping", "person_region"}
        else {"student_source_key": "student:a"}
    )
    target = SimpleNamespace(
        source_key=f"{kind}:a", expected_version=3, school_fact_version=11,
        content=owner_content,
    )
    directory = SimpleNamespace(repository=SimpleNamespace(
        source_for=lambda **kwargs: (
            "quality:a" if kwargs.get("creation") else "person:a"
        ),
    ))
    def read_target(**kwargs):
        if kwargs["fact_kind"] == "student":
            return SimpleNamespace(
                source_key="student:a", expected_version=2, school_fact_version=11,
                content={"person_source_key": "person:a"},
            )
        return target
    family = SimpleNamespace(repository=SimpleNamespace(
        source_for_reference=lambda who, ref: (kind, target.source_key),
        reference_for=lambda **kwargs: "f3:student:signed",
        prepare_target=read_target,
    ))
    guard = SimpleNamespace(require_change_set=lambda who, changes: SimpleNamespace(
        revalidate=lambda: None
    ))
    payload = FactChangeInput(
        fact_kind="fact_quality", creation_ref="f2create:fact_quality:a",
        operation="upsert", reason="学校核查",
        fields={
            "quality_kind": "coverage", "coverage_status": "complete",
            "target_fact_kind": kind, "target_ref": f"f3:{kind}:a",
            "person_ref": "f2:person:a", "field_name": field,
            "basis_reference": "正式母集核查", "responsibility": "学校维护人",
            "verified_at": AT.isoformat(),
        },
    )
    result = FamilyQualityProductAdapter(
        directory=directory, family=family, guard=guard
    ).resolve_change(actor=actor, payload=payload, request_id=uuid4(), effective_at=AT)
    basis = result.change_set.changes[0].content.coverage_basis
    assert basis.target_fact_kind == kind
    assert basis.member_sha256 == mother_set_digest(((target.source_key, 3),))[1]


def test_first_family_quality_creation_resolves_exact_f3_reference_and_confirmed_change_set():
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter

    actor = _actor()
    checked = []
    directory = SimpleNamespace(
        repository=SimpleNamespace(
            source_for=lambda **kwargs: (
                "q:student-a" if kwargs.get("creation") else "person:student-a"
            ),
        )
    )
    family = SimpleNamespace(
        repository=SimpleNamespace(
            source_for_reference=lambda who, ref: ("student", "student:a"),
            prepare_target=lambda **kwargs: SimpleNamespace(
                source_key="student:a", expected_version=3, school_fact_version=11,
                content={"person_source_key": "person:student-a", "status": "active"},
            ),
        )
    )
    guard = SimpleNamespace(
        require_change_set=lambda who, changes: (
            checked.append(changes), SimpleNamespace(revalidate=lambda: checked.append("rechecked"))
        )[1]
    )
    payload = FactChangeInput(
        fact_kind="fact_quality",
        creation_ref="f2create:fact_quality:example",
        operation="upsert",
        fields={
            "quality_kind": "source",
            "target_fact_kind": "student",
            "target_ref": "f3:student:example",
            "person_ref": "f2:person:example",
            "field_name": "status",
            "basis_reference": "正式发布依据",
            "responsibility": "学籍责任者",
            "verified_at": AT.isoformat(),
            "source_status": "available",
        },
        reason="登记正式来源",
    )
    adapter = FamilyQualityProductAdapter(directory=directory, family=family, guard=guard)
    resolved = adapter.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    change = resolved.change_set.changes[0]
    assert change.schema_version == 4
    assert change.content.target_source_key == "student:a"
    assert change.content.person_source_key == "person:student-a"
    assert resolved.summary.after["来源状态"] == "可用（学校登记）"
    assert resolved.summary.after["核查依据"] == "正式发布依据"
    assert resolved.summary.after["责任主体"] == "学籍责任者"
    assert resolved.summary.after["目标记录"] == "student:a"
    assert resolved.summary.after["所属人员"] == "person:student-a"
    assert resolved.summary.after["核查时间"] == AT.isoformat()
    assert checked[0] == resolved.change_set
    resolved.summary_revalidate(actor)
    assert checked[-1] == "rechecked"


def test_f3_quality_creation_rejects_signed_but_unpublished_target_without_owner_body():
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
    from app.composition.school_fact_family_staff_reader import FamilyFactNotRecorded

    actor = _actor()
    calls = []
    directory = SimpleNamespace(repository=SimpleNamespace(
        source_for=lambda **kwargs: (
            calls.append(kwargs), "quality:new" if kwargs.get("creation") else "person:a"
        )[1],
    ))
    family = SimpleNamespace(repository=SimpleNamespace(
        source_for_reference=lambda *_args: ("student", "student:absent"),
        prepare_target=lambda **_kwargs: (_ for _ in ()).throw(FamilyFactNotRecorded()),
    ))
    guard = SimpleNamespace(require_change_set=lambda *_args: pytest.fail("Writer proof ran"))
    payload = FactChangeInput(
        fact_kind="fact_quality", creation_ref="f2create:fact_quality:signed",
        fields={
            "quality_kind": "source", "source_status": "available",
            "target_fact_kind": "student", "target_ref": "f3:student:signed",
            "person_ref": "f2:person:signed", "field_name": "status",
            "basis_reference": "核查", "responsibility": "维护者", "verified_at": AT.isoformat(),
        },
        reason="登记正式来源",
    )
    with pytest.raises(FamilyFactNotRecorded):
        FamilyQualityProductAdapter(directory=directory, family=family, guard=guard).resolve_change(
            actor=actor, payload=payload, request_id=uuid4(), effective_at=AT,
        )
    assert len(calls) == 1 and calls[0]["creation"] is True


@pytest.mark.parametrize("kind,field,relation", [
    ("student", "status", "person_source_key"),
    ("student_school_relationship", "student_number", "student_source_key"),
    ("login_mapping", "status", "person_source_key"),
    ("student_contact", "priority", "student_source_key"),
    ("guardian_relationship", "status", "student_source_key"),
    ("person_region", "district_name", "person_source_key"),
])
def test_f3_quality_owner_is_current_formal_target_not_an_unchecked_person_reference(
    kind, field, relation
):
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter

    actor = _actor()
    read_kinds = []
    owner_value = "person:a" if relation == "person_source_key" else "student:a"

    def read_target(**kwargs):
        read_kinds.append(kwargs["fact_kind"])
        if kwargs["fact_kind"] == "student" and kind != "student":
            return SimpleNamespace(
                source_key="student:a", expected_version=3, school_fact_version=11,
                content={"person_source_key": "person:a"},
            )
        return SimpleNamespace(
            source_key=f"{kind}:a", expected_version=3, school_fact_version=11,
            content={relation: owner_value},
        )

    directory = SimpleNamespace(repository=SimpleNamespace(
        source_for=lambda **kwargs: (
            "quality:new" if kwargs.get("creation") else "person:other"
        ),
    ))
    family = SimpleNamespace(repository=SimpleNamespace(
        source_for_reference=lambda *_args: (kind, f"{kind}:a"),
        reference_for=lambda **_kwargs: "f3:student:signed",
        prepare_target=read_target,
    ))
    adapter = FamilyQualityProductAdapter(
        directory=directory, family=family,
        guard=SimpleNamespace(require_change_set=lambda *_args: SimpleNamespace(
            revalidate=lambda: None
        )),
    )
    fields = {
        "quality_kind": "source", "source_status": "available",
        "target_fact_kind": kind, "target_ref": f"f3:{kind}:signed",
        "field_name": field, "basis_reference": "正式核查",
        "responsibility": "维护者", "verified_at": AT.isoformat(),
    }
    payload = FactChangeInput(
        fact_kind="fact_quality", creation_ref="f2create:fact_quality:signed",
        fields=fields, reason="登记正式来源",
    )
    change = adapter.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT,
    ).change_set.changes[0]
    assert change.content.person_source_key == "person:a"
    assert read_kinds.count("student") >= (1 if relation == "student_source_key" else 0)
    with pytest.raises(PermissionError, match="owner"):
        adapter.resolve_change(
            actor=actor,
            payload=payload.model_copy(update={"fields": {
                **fields, "person_ref": "f2:person:other",
            }}),
            request_id=uuid4(), effective_at=AT,
        )


def test_existing_family_quality_end_reuses_original_target_and_rechecks_its_proof():
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter

    actor = _actor()
    checked = []
    original = {
        "version": 3,
        "content": {
            "quality_kind": "source",
            "target_fact_kind": "student",
            "target_source_key": "student:a",
            "owner_kind": "person",
            "person_source_key": "person:a",
            "field_name": "status",
            "basis_reference": "正式依据",
            "responsibility": "登记责任人",
            "verified_at": AT,
        },
        "proof": SimpleNamespace(revalidate=lambda: checked.append("original")),
    }
    existing = SimpleNamespace(
        target_kind=lambda who, key: "student",
        __call__=lambda who, key: original,
        projection=SimpleNamespace(current_version=lambda school: 13),
    )

    class Existing:
        projection = existing.projection

        def target_kind(self, who, key):
            return "student"

        def __call__(self, who, key):
            return original

    directory = SimpleNamespace(
        repository=SimpleNamespace(source_for=lambda **kwargs: "quality:a")
    )
    guard = SimpleNamespace(
        require_change_set=lambda who, changes: SimpleNamespace(
            revalidate=lambda: checked.append("new")
        )
    )
    adapter = FamilyQualityProductAdapter(
        directory=directory, family=SimpleNamespace(), guard=guard, existing=Existing()
    )
    payload = FactChangeInput(
        fact_kind="fact_quality", target_ref="f2:fact_quality:a", operation="end",
        fields={}, reason="正式结束旧来源说明",
    )
    assert adapter.recognizes(actor, payload)
    resolved = adapter.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    change = resolved.change_set.changes[0]
    assert change.expected_version == 3
    assert change.content is None
    assert change.operation == "end"
    resolved.summary_revalidate(actor)
    assert checked == ["original", "new", "original", "new"]


def test_f3_read_asks_quality_only_for_exact_target_and_keeps_details_separate():
    from app.composition.school_fact_family_product_adapter import FamilyStaffProductQuery

    actor = _actor()
    quality_calls = []

    class Repository:
        def query(self, **kwargs):
            return {
                "queried_at": AT.isoformat(), "records": [{
                    "reference": "f3:student:signed", "label": "林同学",
                    "content": {"status": "active"}, "version": 2,
                    "valid_from": AT.isoformat(), "valid_to": None,
                    "source_reference": "publication:signed", "source_name": "学校正式登记",
                    "source_trace_reference": None, "source_published_at": AT.isoformat(),
                }],
                "status": "known", "truncated": False, "coverage": "unknown",
            }

        def source_for_reference(self, who, ref):
            assert who == actor and ref == "f3:student:signed"
            return "student", "student:a"

    class Quality:
        def query_quality_for_target(self, **kwargs):
            quality_calls.append(kwargs)
            return SimpleNamespace(records=(
                {"quality_kind": "source", "source_status": "available"},
                {"quality_kind": "dispute", "dispute_status": "open"},
            ))

    query = FamilyStaffProductQuery(Repository(), None, None, quality_reader=Quality())
    plain = query.query(actor=actor, fact_kind="student", reference="f3:student:signed")
    assert "quality" not in plain
    assert quality_calls == []
    detailed = query.query(
        actor=actor, fact_kind="student", reference="f3:student:signed",
        include_quality=True, requested_fields=("status",),
    )
    assert quality_calls[0]["target_key"] == "student:a"
    assert quality_calls[0]["field_names"] == ("status",)
    assert detailed["quality"]["status"] == "claims"
    assert len(detailed["quality"]["details"]) == 2
    assert any("争议" in note for note in detailed["quality"]["summary"])


@pytest.mark.parametrize("kind,field", [
    ("student_school_relationship", "student_number"),
    ("login_mapping", "status"),
    ("student_contact", "priority"),
    ("guardian_relationship", "status"),
    ("person_region", "district_name"),
])
def test_f3_exact_quality_only_promotes_current_authorized_target(kind, field):
    from app.composition.school_fact_family_product_adapter import FamilyStaffProductQuery
    from app.platform.school_facts.f7_coverage_basis import mother_set_digest

    actor = _actor()
    source = f"{kind}:a"
    basis = {
        "schema_version": 1, "school_id": str(actor.school_id),
        "owner_kind": "person", "owner_source_key": "person:a",
        "target_fact_kind": kind, "target_source_key": source,
        "field_name": field, "purpose": None, "contact_kind": None,
        "valid_from": AT.isoformat(), "valid_to": None,
        "school_fact_version": 12, "member_count": 1,
        "member_sha256": mother_set_digest(((source, 3),))[1],
    }

    class Repository:
        def query(self, **kwargs):
            return {
                "queried_at": AT.isoformat(), "records": [{
                "reference": f"f3:{kind}:signed", "content": {field: "value"},
                "version": 3, "label": "正式资料", "valid_from": AT.isoformat(),
                "valid_to": None, "source_reference": "publication:signed",
                "source_name": "学校正式登记", "source_trace_reference": None,
                "source_published_at": AT.isoformat(),
                }],
                "status": "known", "truncated": False, "coverage": "unknown",
            }

        def source_for_reference(self, who, ref):
            return kind, source

    class Quality:
        school_fact_version = 13

        def query_quality_for_target(self, **kwargs):
            return SimpleNamespace(records=(
                {"quality_kind": "coverage", "coverage_status": "complete",
                 "owner_source_key": "person:a", "field_name": field,
                 "coverage_basis": basis},
            ), school_fact_version=self.school_fact_version)

    quality = Quality()
    repository = Repository()
    query = FamilyStaffProductQuery(repository, None, None, quality_reader=quality)
    view = query.query(actor=actor, fact_kind=kind, reference=f"f3:{kind}:signed",
                       include_quality=True, requested_fields=(field,))
    assert "学校已核对该项正式登记的覆盖范围" in view["quality"]["summary"]
    original_query = repository.query

    def changed_target(**kwargs):
        result = original_query(**kwargs)
        result["records"][0]["version"] = 4
        return result

    repository.query = changed_target
    stale = query.query(actor=actor, fact_kind=kind, reference=f"f3:{kind}:signed",
                        include_quality=True, requested_fields=(field,))
    assert "学校已核对该项正式登记的覆盖范围" not in stale["quality"]["summary"]


def test_f3_model_can_request_quality_without_sending_maintenance_details_by_default():
    from app.composition.school_fact_family_staff_gateway import (
        FamilyModelReadInput,
        _selected_read_material,
    )

    request = FamilyModelReadInput(
        fact_kind="student", reference="f3:student:signed",
        fields=("status",), include_quality=True,
    )
    assert request.include_quality is True
    view = {
        "records": [{"fields": {"status": "active"}}],
        "quality": {
            "status": "claims", "summary": ["学校登记了这项资料的争议"],
            "details": [{"quality_kind": "dispute", "dispute_status": "open"}],
        },
    }
    assert "details" not in _selected_read_material(view, ("status",))["quality"]
    assert "details" in view["quality"]


def test_quality_attached_to_f3_result_forces_full_current_reread_before_model_release():
    from app.composition.school_fact_family_staff_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f3_family_staff_gateway import EvidenceQuery, context, gateway

    class Query(EvidenceQuery):
        summary = "学校登记的来源状态为可用（非独立核实）"

        def query(self, **kwargs):
            view = super().query(**kwargs)
            if kwargs.get("include_quality"):
                view["quality"] = {
                    "status": "claims", "summary": [self.summary],
                    "details": [{"quality_kind": "source", "source_status": "available"}],
                }
            return view

    owner = gateway()
    query = Query()
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(
            call_id="quality", name=MODULE + ".read",
            arguments={"fact_kind": "student", "fields": ["status"], "include_quality": True},
        ),
        context(),
    )
    assert result.history_check is not None
    assert "details" not in result.model_content["supporting_evidence"]["quality"]
    assert "details" in result.fact_card["quality"]
    assert result.history_check.valid()
    assert query.reads == 2
    query.summary = "学校登记了这项资料的争议"
    assert not result.history_check.valid()


def test_staff_student_profile_includes_only_requested_current_quality_risks():
    from app.composition.school_fact_family_product_adapter import FamilyStaffProductQuery

    actor = _actor()
    calls = []

    class Repository:
        def query(self, **kwargs):
            kind = kwargs["fact_kind"]
            fields = {
                "person": {"display_name": "林同学"},
                "student": {"person_ref": "f3:person:a"},
                "student_school_relationship": {
                    "student_ref": "f3:student:a", "enrollment_status": "enrolled",
                    "residence_mode": "boarding",
                },
            }[kind]
            return {
                "queried_at": AT.isoformat(), "status": "known", "truncated": False,
                "coverage": "unknown", "records": [{
                    "reference": f"f3:{kind}:a", "label": "林同学", "content": fields,
                    "version": 2, "valid_from": AT.isoformat(), "valid_to": None,
                    "source_reference": "publication:a", "source_name": "学校正式登记",
                    "source_trace_reference": None, "source_published_at": AT.isoformat(),
                }],
            }

        def source_for_reference(self, who, ref):
            assert who == actor
            kind = ref.split(":")[1]
            return kind, {"student_school_relationship": "school:a"}.get(kind, "person:a")

    class Quality:
        def query_quality_for_target(self, **kwargs):
            calls.append(kwargs)
            claims = (
                {"quality_kind": "dispute", "dispute_status": "open",
                 "field_name": "enrollment_status", "source_key": "secret-quality-id"},
                {"quality_kind": "source", "source_status": "unavailable",
                 "field_name": "residence_mode"},
            )
            return SimpleNamespace(records=tuple(
                claim for claim in claims
                if claim["field_name"] in kwargs["field_names"]
            ))

    query = FamilyStaffProductQuery(Repository(), None, None, quality_reader=Quality())
    view = query.student_profile(
        actor=actor, name="林同学", student_number=None,
        fields=("enrollment_status", "residence_mode"), as_of=None,
    )
    assert view["profile"]["enrollment_status"]["status"] == "known"
    assert view["quality"]["enrollment_status"]["status"] == "claims"
    assert view["quality"]["enrollment_status"]["risks"] == ["学校登记了这项资料的争议"]
    assert view["quality"]["residence_mode"]["risks"] == ["这项资料的来源目前无法核实"]
    assert calls[0]["target_kind"] == "student_school_relationship"
    assert calls[0]["target_key"] == "school:a"
    assert calls[0]["field_names"] == ("enrollment_status", "residence_mode")
    assert len(calls) == 1
    assert "secret-quality-id" not in str(view["quality"])


def test_staff_student_profile_quality_change_invalidates_result_use():
    from app.composition.school_fact_family_staff_gateway import MODULE
    from app.platform.assistant.tool_loop import ModelToolCall
    from tests.test_f3_family_staff_gateway import EvidenceQuery, context, gateway

    class Query(EvidenceQuery):
        risk = "学校登记了这项资料的争议"

        def student_profile(self, **kwargs):
            return {
                "kind": "f3_student_profile", "status": "known", "profile": {
                    "enrollment_status": {"status": "known", "value": "在校"},
                }, "quality": {"enrollment_status": {
                    "status": "claims", "risks": [self.risk],
                    "details": [{"evidence_reference": "private-review-record"}],
                }},
                "sources": {}, "evidence": [{"reference": "f3:student_school_relationship:a"}],
            }

    owner = gateway()
    query = Query()
    owner.query = query
    result = owner.for_turn("identity").execute(
        ModelToolCall(call_id="profile", name=MODULE + ".student_profile",
                      arguments={"name": "林同学", "fields": ["enrollment_status"]}),
        context(),
    )
    assert result.history_check is not None
    quality = result.model_content["supporting_evidence"]["quality"]
    assert quality["enrollment_status"]["risks"] == [query.risk]
    assert "private-review-record" not in str(result.model_content)
    assert "private-review-record" in str(result.fact_card)
    assert result.history_check.valid()
    query.risk = "来源已经无法核验"
    assert not result.history_check.valid()


def test_school_family_reference_routes_quality_without_treating_signature_as_authority():
    from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
    from app.platform.school_facts.school_scope_reference import encode_school_scope_reference

    actor = _actor()
    ref = encode_school_scope_reference(
        secret=b"x" * 32,
        family="f3",
        school_id=actor.school_id,
        assignment_id=actor.assignment_id,
        fact_kind="student",
        source_key="student:future",
        creation=False,
    )
    adapter = FamilyQualityProductAdapter(directory=None, family=None, guard=None)
    payload = SimpleNamespace(
        fact_kind="fact_quality", creation_ref="pending", fields={"target_ref": ref}
    )
    assert adapter.recognizes(actor, payload)
    payload.fields["target_ref"] = ref.replace(":o:", ":c:")
    assert not adapter.recognizes(actor, payload)
