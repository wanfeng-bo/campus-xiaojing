from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.school_facts.test_f4_education_query import External, Repository, module
from tests.school_facts.test_f4_education_reader import actor


def gateway_module():
    name = "app.composition.school_fact_education_gateway"
    assert find_spec(name) is not None, "Education gateway missing"
    return import_module(name)


def fixture():
    repo = Repository()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    gateway = gateway_module().EducationToolGateway(
        resolve_actor=lambda ref: identity,
        query=module().EducationQueryService(repo, external_reader=External()),
        product=SimpleNamespace(),
        describe_adapter=SimpleNamespace(),
    )
    context = ToolCallContext("actor", "conversation", "turn", "run", 1, uuid4())
    call = ModelToolCall(
        call_id="q1",
        name="school_facts.education.query",
        arguments={"name": "2023班", "fields": ["teachers"]},
    )
    return repo, gateway, context, call


@pytest.mark.parametrize("family", ["directory", "family", "education", "teaching"])
@pytest.mark.parametrize("code", [None, "prepare_resolution_denied", "prepare_maintenance_denied"])
def test_prepare_denial_stage_is_fixed_across_shared_consumers(family, code):
    from app.composition.school_fact_directory_gateway import DirectoryFactToolGateway
    from app.composition.school_fact_directory_product import PrepareAccessDenied
    from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway

    _, gateway, context, _ = fixture()
    if family == "teaching":
        gateway = gateway_module().EducationToolGateway(
            resolve_actor=gateway.resolve_actor,
            query=gateway.education_query,
            product=SimpleNamespace(),
            describe_adapter=SimpleNamespace(),
            teaching_enabled=True,
        )
    elif family != "education":
        cls = DirectoryFactToolGateway if family == "directory" else FamilyStaffToolGateway
        gateway = cls(
            resolve_actor=gateway.resolve_actor,
            query=SimpleNamespace(),
            product=SimpleNamespace(),
            retention_seconds=60,
        )

    def denied(**kwargs):
        raise PrepareAccessDenied(code) if code else PermissionError("PRIVATE_SCOPE")

    gateway.product.prepare = denied
    descriptor = next(d for d in gateway.descriptors if d.name.endswith(".prepare"))
    kind = {
        "directory": "person",
        "family": "student_school_relationship",
        "education": "education_class",
        "teaching": "teaching_assignment",
    }[family]
    result = gateway.execute(
        ModelToolCall(
            call_id="prepare",
            name=descriptor.name,
            arguments={
                "fact_kind": kind,
                "target_ref": "synthetic-existing-reference",
                "fields": {},
                "reason": "synthetic offline check",
            },
        ),
        context,
    )
    assert result.model_content["status"] == "denied"
    assert result.model_content.get("code") == code
    if code:
        assert (
            result.model_content["reason"]
            == {
                "prepare_resolution_denied": "本次所选目标或关联资料未能获准核对，未生成修改方案。",
                "prepare_maintenance_denied": "本次改动未通过当前维护授权核验，未生成可确认方案。",
            }[code]
        )
    assert "PRIVATE" not in str(result.model_content)
    assert result.history_execution_note is None
    assert (
        result.fact_card is None and result.module_result is None and result.history_check is None
    )


def test_describe_creation_choices_never_assert_existing_update_authority():
    from app.composition.school_fact_family_staff_gateway import FamilyStaffToolGateway

    for owner in (FamilyStaffToolGateway, gateway_module().EducationToolGateway):
        descriptor = next(d for d in owner.descriptors if d.name.endswith(".describe"))
        assert "creation_choices" in descriptor.description
        assert "新增槽位" in descriptor.description
        assert "不代表已有对象的更新许可" in descriptor.description
        assert "空列表也不证明没有维护权" in descriptor.description


def test_query_gateway_keeps_evidence_private_and_checks_history_version():
    repo, gateway, context, call = fixture()
    turn = gateway.for_turn("actor")
    result = turn.execute(call, context)
    assert "陈老师" in str(result.model_content)
    assert "source_key" not in str(result.model_content)
    assert "private_proof" not in str(result.fact_card)
    assert result.history_check.valid()
    repo.projection.current_version = lambda school: 11
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor")


def test_query_material_leaves_display_summary_in_card_without_losing_facts():
    _, gateway, context, call = fixture()
    result = gateway.execute(call, context)

    assert result.fact_card["text"]
    assert "text" not in result.model_content
    assert result.model_content["name"] == result.fact_card["name"]
    assert result.model_content["tool_context"]["reference"] == result.fact_card["reference"]
    assert result.model_content["teachers"]["items"][0]["name"] == "陈老师"
    assert result.model_content["teachers"]["status"] == result.fact_card["teachers"]["status"]
    assert result.history_check.valid()


@pytest.mark.parametrize(
    "view",
    [
        {"status": "not_recorded", "text": "该学生的正式归班不符合所选班级。"},
        {"status": "denied", "text": "该班成员资料不在当前职责范围内。"},
        {"status": "unavailable", "text": "当前来源不可用。"},
        {"fields": {"text": "获准的原文字段", "status": "known", "name": "原文"}},
    ],
)
def test_material_preserves_nonredundant_reason_and_authorized_text(view):
    from copy import deepcopy

    original = deepcopy(view)
    assert gateway_module().education_model_material(view) == original
    assert view == original


def test_gateway_descriptors_have_same_owner_and_bounded_query_schema():
    _, gateway, _, _ = fixture()
    assert all(d.owner == "school_facts.education" for d in gateway.descriptors)
    names = {d.name for d in gateway.descriptors}
    assert "school_facts.education.prepare" in names
    assert "school_facts.education.student_profile" not in names
    with pytest.raises(ValueError):
        gateway_module().EducationQueryInput(name="2023班", fields=["raw_sql"])
    with pytest.raises(ValueError):
        gateway_module().EducationQueryInput(
            name="2023班", fields=["grade"], as_of="2026-09-01T00:00:00"
        )


def test_cross_actor_turn_reuse_rejected():
    _, gateway, context, call = fixture()
    turn = gateway.for_turn("actor")
    turn.execute(call, context)
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("different")


def test_bad_selection_is_not_reported_as_permission_denial():
    _, gateway, context, call = fixture()
    bad = call.model_copy(update={"arguments": {"name": "2023班", "fields": ["raw_sql"]}})
    assert gateway.execute(bad, context).model_content["status"] == "unsupported_selection"


@pytest.mark.parametrize("operation", ["query", "records", "describe"])
def test_schema_rejection_retains_only_identity_bound_nonfactual_history(operation):
    repo, gateway, context, call = fixture()
    bad = call.model_copy(
        update={
            "name": "school_facts.education." + operation,
            "arguments": {"unexpected_private_input": "not a fact"},
        }
    )
    result = gateway.execute(bad, context)
    assert result.model_content["status"] == "unsupported_selection"
    assert not repo.calls and result.fact_card is None
    assert "not a fact" not in str(result.model_content)
    assert result.history_check is not None
    assert result.history_check.valid() and result.history_check.refresh()
    identity = gateway.resolve_actor("actor")
    gateway.resolve_actor = lambda _: SimpleNamespace(actor=identity.actor, session_binding="new")
    assert not result.history_check.valid()


@pytest.mark.parametrize("error", [ValueError, PermissionError, RuntimeError])
def test_postvalidation_failure_never_becomes_replayable_schema_rejection(error):
    _, gateway, context, call = fixture()

    def fail(*args, **kwargs):
        raise error("private source detail")

    gateway.education_query.query = fail
    result = gateway.execute(call, context)
    assert result.history_check is None and result.fact_card is None
    assert "private source detail" not in str(result.model_content)


@pytest.mark.parametrize("operation", ["query", "records", "describe", "prepare", "receipt"])
def test_denied_education_read_note_never_claims_write_or_receipt_outcome(operation):
    _, gateway, context, call = fixture()

    def denied(_):
        raise PermissionError("private actor and target")

    gateway.resolve_actor = denied
    result = gateway.execute(
        call.model_copy(
            update={
                "name": "school_facts.education." + operation,
                "arguments": {"private_parameter": "sensitive"},
            }
        ),
        context,
    )
    assert result.history_execution_note == (
        "read_returned_no_values" if operation in {"query", "records", "describe"} else None
    )
    assert result.history_check is None and result.fact_card is None
    assert "private" not in str(result.model_content)
    assert "sensitive" not in str(result.model_content)


@pytest.mark.parametrize("change", ["content", "permission", "identity"])
def test_describe_history_revalidates_complete_view_and_identity(change):
    _, gateway, context, call = fixture()
    view = {"fact_kind": "education_class", "fields": ["display_name"]}
    gateway.describe_adapter.describe = lambda **kwargs: dict(view)
    call = call.model_copy(
        update={
            "name": "school_facts.education.describe",
            "arguments": {"fact_kind": "education_class"},
        }
    )
    result = gateway.for_turn("actor").execute(call, context)
    assert result.history_check is not None and result.history_check.valid()
    if change == "content":
        view["fields"] = []
    elif change == "permission":

        def denied(**kwargs):
            raise PermissionError("expired")

        gateway.describe_adapter.describe = denied
    else:
        identity = gateway.resolve_actor("actor")
        gateway.resolve_actor = lambda _: SimpleNamespace(
            actor=identity.actor, session_binding="new"
        )
    assert not result.history_check.valid() and not result.history_check.refresh()


def related_family_reader(*, outcome="available", error=None):
    """Use the real F3 authorization/projection reader in the education join."""
    from contextlib import contextmanager

    from app.composition.school_fact_education_external import EducationExternalReader
    from app.composition.school_fact_family_authority import FamilyFactTarget
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.composition.school_fact_family_staff_reader import FamilyStaffRepository
    from tests.school_facts.test_f4_education_query import AT
    from tests.test_f3_family_staff_reader import Permit, payload

    calls = []

    class Evidence:
        version = 10

        @contextmanager
        def capture(self, **kwargs):
            yield SimpleNamespace(seal=lambda: self)

        def check(self, selected):
            return selected == actor()

    evidence = Evidence()

    class Projection:
        def read(self, **kwargs):
            kwargs["before_read"]()
            kind = kwargs["fact_kind"]
            fields = kwargs["field_names"]()
            calls.append((kind, kwargs["source_key"], fields))
            if kind == "person" and error is not None:
                raise error("private exception detail must not escape")
            kwargs["after_read"]()
            return {
                **payload(
                    content={
                        f: {"person_source_key": "person:a", "display_name": "学生甲"}[f]
                        for f in fields
                    }
                ),
                "status": outcome if kind == "person" else "available",
                "fact_kind": kind,
                "source_key": kwargs["source_key"],
                "school_fact_version": 10,
                "factual_at": AT.isoformat(),
            }

    projection = Projection()
    projection.evidence = evidence
    configuration = FamilyConfiguration(
        approval_reference="already-approved",
        scope_bindings=(),
        source_catalog={"student": ("student:a",), "person": ("person:a",)},
        creation_catalog={},
    )

    def authorize(selected, action, kind, source, creation, checked_at):
        fields = ("display_name",) if kind == "person" else ("person_source_key",)
        target = FamilyFactTarget(kind, source, fields, selected.authorization_version)
        return Permit(selected, target, fields)

    repository = FamilyStaffRepository(
        projection=projection,
        configuration=configuration,
        reference_key=b"test-only-private-reference" * 2,
        authorize_scope=authorize,
        revalidate=lambda *args: None,
        clock=lambda: AT,
    )
    directory = SimpleNamespace(
        reference_for=lambda **kwargs: "opaque:assignment",
        query=lambda **kwargs: SimpleNamespace(
            status="known",
            has_more=False,
            records=(SimpleNamespace(content={"person_ref": "opaque:person"}),),
        ),
        source_for=lambda **kwargs: "person:a",
    )
    return EducationExternalReader(
        directory=directory,
        evidence=evidence,
        family=SimpleNamespace(repository=repository, configuration=configuration),
    ), calls


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("no_current_records", "not_recorded"), ("conflict", "conflict")],
)
def test_related_name_failure_does_not_claim_the_class_or_roster_is_absent(outcome, expected):
    from tests.school_facts.test_f4_education_query import record

    repo, gateway, context, call = fixture()
    repo.rows["student_class_membership"] = [
        record(
            "student_class_membership",
            "membership:a",
            {
                "class_source_key": "class:a",
                "student_source_key": "student:a",
                "membership_status": "assigned",
                "school_relationship_source_key": "school:a",
            },
        )
    ]
    external, calls = related_family_reader(outcome=outcome)
    gateway.education_query.external_reader = external
    call = call.model_copy(update={"arguments": {"name": "2023班", "fields": ["members"]}})
    result = gateway.execute(call, context)
    assert result.model_content["status"] == "incomplete"
    assert result.model_content["related_fact"] == {
        "status": expected,
        "fact_kind": "person",
        "fields": ["display_name"],
    }
    assert "members" not in result.model_content and "items" not in result.model_content
    assert result.fact_card is None and result.history_check is None
    assert calls == [
        ("student", "student:a", ("person_source_key",)),
        ("person", "person:a", ("display_name",)),
    ]


@pytest.mark.parametrize(
    ("error_name", "expected"),
    [
        ("FamilyFactAuthorityDenied", "denied"),
        ("SecureReadDenied", "denied"),
        ("SecureReadAuthorizationDrift", "stale"),
        ("RuntimeError", "source_unavailable"),
        ("ValueError", "source_unavailable"),
    ],
)
def test_actual_related_reader_exception_is_classified_without_private_detail(error_name, expected):
    from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
    from app.platform.authorization.secure_reader_contracts import (
        SecureReadAuthorizationDrift,
        SecureReadDenied,
    )

    errors = {
        c.__name__: c
        for c in (
            FamilyFactAuthorityDenied,
            SecureReadDenied,
            SecureReadAuthorizationDrift,
            RuntimeError,
            ValueError,
        )
    }
    _, gateway, context, call = fixture()
    gateway.education_query.external_reader, calls = related_family_reader(error=errors[error_name])
    result = gateway.execute(call, context)
    assert result.model_content["status"] == expected
    assert calls[-1] == ("person", "person:a", ("display_name",))
    assert result.fact_card is None and result.history_check is None
    assert "private exception detail" not in str(result.model_content)


def test_related_person_scope_denial_occurs_before_reading_private_name():
    from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied

    _, gateway, context, call = fixture()
    external, calls = related_family_reader()
    authorized = external.family.repository._authorize_scope

    def deny_name(selected, action, kind, source, creation, checked_at):
        if kind == "person":
            raise FamilyFactAuthorityDenied("outside exact approved scope")
        return authorized(selected, action, kind, source, creation, checked_at)

    external.family.repository._authorize_scope = deny_name
    gateway.education_query.external_reader = external
    assert gateway.execute(call, context).model_content["status"] == "denied"
    assert calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [("no_current_records", "not_recorded"), ("conflict", "conflict")],
)
def test_related_directory_status_keeps_the_same_join_scope_as_family(status, expected):
    _, gateway, context, call = fixture()
    external, _ = related_family_reader()
    external.family = None
    original = external.directory.query

    def query(**kwargs):
        if kwargs["fact_kind"] == "person":
            return SimpleNamespace(status=status, has_more=False, records=())
        return original(**kwargs)

    external.directory.query = query
    gateway.education_query.external_reader = external
    result = gateway.execute(call, context)
    assert result.model_content["status"] == "incomplete"
    assert result.model_content["related_fact"] == {
        "status": expected,
        "fact_kind": "person",
        "fields": ["display_name"],
    }
    assert result.history_check is None


def test_changed_external_evidence_is_stale_not_a_new_permission_decision():
    _, gateway, context, call = fixture()
    external, _ = related_family_reader()
    external.evidence.check = lambda selected: False
    gateway.education_query.external_reader = external
    assert gateway.execute(call, context).model_content["status"] == "stale"


def test_time_or_fact_change_has_own_status():
    _, gateway, context, call = fixture()

    def changed(*args, **kwargs):
        raise module().EducationReadStale("changed")

    gateway.education_query.query = changed
    assert gateway.execute(call, context).model_content["status"] == "stale"


@pytest.mark.parametrize("tool", ("query", "records"))
def test_final_proof_change_is_stale_and_never_returns_unverified_facts(tool):
    repo, gateway, context, call = fixture()
    query = gateway.education_query.query
    completed_reads = []

    def changed_after_read(*args, **kwargs):
        result = query(*args, **kwargs)
        completed_reads.append(result)
        repo.projection.current_version = lambda school: 11
        return result

    if tool == "records":
        gateway.describe_adapter.query_with_proof = lambda **kwargs: changed_after_read(
            kwargs["actor"], name="2023班", fields=("grade",)
        )
        call = call.model_copy(
            update={
                "name": "school_facts.education.records",
                "arguments": {"fact_kind": "education_class", "limit": 20},
            }
        )
    else:
        gateway.education_query.query = changed_after_read

    result = gateway.execute(call, context)

    assert len(completed_reads) == 1
    assert result.model_content["status"] == "stale"
    assert result.fact_card is None and result.history_check is None
    assert "陈老师" not in str(result.model_content)


def test_education_model_material_separates_machine_references_and_role_values():
    material = gateway_module().education_model_material(
        {
            "fact_kind": "class_teacher_assignment",
            "reference": "opaque-teacher",
            "fields": {"teacher_role": "primary", "class_ref": "opaque-class"},
            "source": {"label": "学校登记", "source_key": "private", "source_ref": "trace"},
        }
    )
    assert material["fields"]["teacher_role"] == "班主任"
    assert material["fields"]["tool_context"]["class_ref"] == "opaque-class"
    assert material["tool_context"]["reference"] == "opaque-teacher"
    assert "private" not in str(material)
    assert "trace" not in str(material)


def test_generic_records_retains_current_proof_and_uses_f4_view():
    repo, gateway, context, call = fixture()
    captured = []

    def read(**kwargs):
        captured.append(kwargs)
        return gateway.education_query.query(kwargs["actor"], name="2023班", fields=("grade",))

    gateway.describe_adapter.query_with_proof = read
    record_call = call.model_copy(
        update={
            "name": "school_facts.education.records",
            "arguments": {"fact_kind": "education_calendar_rule", "limit": 20},
        }
    )
    result = gateway.for_turn("actor").execute(record_call, context)
    assert captured[0]["fact_kind"] == "education_calendar_rule"
    assert result.history_check.valid()
    repo.projection.current_version = lambda school: 11
    assert not result.history_check.valid()
    descriptor = next(d for d in gateway.descriptors if d.name.endswith(".prepare"))
    assert "additional_changes" in descriptor.parameters["properties"]


@pytest.mark.parametrize("tool", ["query", "records"])
def test_education_evidence_can_settle_on_the_real_runtime_without_losing_proof(tool):
    from app.platform.assistant.runtime import AssistantRuntime, RunSettlement

    _, gateway, context, call = fixture()
    if tool == "records":
        gateway.describe_adapter.query_with_proof = lambda **kwargs: gateway.education_query.query(
            kwargs["actor"], name="2023班", fields=("grade",)
        )
        call = call.model_copy(
            update={
                "name": "school_facts.education.records",
                "arguments": {"fact_kind": "education_class", "limit": 20},
            }
        )
    result = gateway.for_turn("actor").execute(call, context)
    runtime = AssistantRuntime()
    conversation = runtime.create_conversation("actor", uuid4())
    request = uuid4()
    accepted = runtime.accept_turn(conversation.conversation_ref, "actor", request, "测试资料")
    snapshot = runtime.settle_run(
        accepted.conversation_ref,
        "actor",
        accepted.run_ref,
        accepted.generation,
        request,
        RunSettlement(
            display_kind="card",
            display_payload={"facts": [result.fact_card]},
            model_history_checks=(result.history_check,),
        ),
    )
    assert snapshot.display_ledger[-1].visible_state == "completed"
    evidence = snapshot.display_ledger[-1].display_payload["facts"][0]["evidence"]
    assert isinstance(evidence, list) and evidence
    assert result.history_check.valid()


@pytest.mark.parametrize(
    "raw",
    [
        {"grade": {"status": "known", "name": "初二", "cycle": 2026}},
        {
            "profile": {
                "grade": [{"status": "known", "display_name": "初二", "academic_year": 2026}]
            }
        },
    ],
)
def test_grade_model_material_has_explicit_academic_year_not_cohort(raw):
    from copy import deepcopy

    original = deepcopy(raw)
    value = gateway_module().education_model_material(raw)
    grade = value["grade"] if "grade" in value else value["profile"]["grade"][0]
    assert grade["academic_year"] == {"start_year": 2026, "label": "2026学年"}
    assert "cycle" not in grade
    assert "cohorts" not in str(value)
    assert raw == original


def test_query_parameter_schema_defines_exact_locator_and_two_time_axes():
    schema = gateway_module().EducationQueryInput.model_json_schema()["properties"]
    assert "正式班名" in schema["name"]["description"]
    assert "年级" in schema["grade_name"]["description"]
    assert "班序" in schema["class_number"]["description"]
    assert "事实" in schema["as_of"]["description"]
    assert "审计" in schema["known_at"]["description"]
    assert "届" not in schema["grade_name"]["description"]


def test_unknown_coverage_material_is_collection_metadata_not_record_untrustworthiness():
    view = {
        "teachers": {"status": "known", "items": [{"name": "正式教师"}], "coverage": "unknown"},
        "cohorts": {"items": [{"kind": "expected_graduation", "academic_year": 2029}]},
    }
    material = gateway_module().education_model_material(view)
    assert material["teachers"]["reality_coverage"] == {"status": "unknown"}
    assert "登记完整程度" in material["interpretation"]["coverage"]
    assert material["teachers"]["fact_reading"]["outcome"] == "已取得本次所查正式登记"
    assert material["teachers"]["items"][0]["name"] == "正式教师"
    assert material["cohorts"]["items"][0]["academic_year"] == 2029


def test_records_schema_keeps_dependency_year_separate_from_fact_time():
    schema = gateway_module().EducationRecordsInput.model_json_schema()["properties"]
    assert "适用学年" in schema["academic_year"]["description"]
    assert "事实" in schema["as_of"]["description"]
    assert "正式名称" in schema["query_text"]["description"]


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("Asia/Shanghai", "2026-10-01 00:00:00"),
        ("America/New_York", "2026-09-30 12:00:00"),
        (None, None),
        ("UTC", None),
        ("not-a-school-timezone", None),
    ],
)
def test_material_labels_fact_and_source_times_without_losing_iso_or_guessing_zone(zone, expected):
    raw = {
        "fact_timezone_name": zone,
        "as_of": "2026-09-30T16:00:00+00:00",
        "sources": [{"effective_at": "2026-09-30T16:00:00+00:00"}],
    }
    value = gateway_module().education_model_material(raw)
    assert value["as_of"] == raw["as_of"]
    assert value["sources"][0]["effective_at"] == raw["sources"][0]["effective_at"]
    if expected is None:
        assert "time_labels" not in value
        assert "time_labels" not in value["sources"][0]
    else:
        assert expected in value["time_labels"]["as_of"]
        assert zone in value["time_labels"]["as_of"]
        assert expected in value["sources"][0]["time_labels"]["effective_at"]


def test_teachers_query_field_is_headteacher_not_subject_teaching():
    schema = gateway_module().EducationQueryInput.model_json_schema()["properties"]
    assert (
        "班主任" in schema["fields"]["description"]
        and "学科任课" in schema["fields"]["description"]
    )
