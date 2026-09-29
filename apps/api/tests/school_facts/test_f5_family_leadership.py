from copy import deepcopy
from importlib import import_module

import pytest

from tests.test_f3_family_read import NOW, setup


def fixture(caregiver=False):
    old, actor, state = setup(caregiver=caregiver)
    state.payload = dict(
        status="available",
        student_id=str(state.basis.student_id),
        student_version=1,
        relationship_version=1,
        configuration=[{"version": 1}],
        leaders=[
            dict(
                display_name="正式校长",
                position_title="校长",
                assignment_kind="acting",
                role_kind="school_leader",
                organization="学校办公室",
                campuses=["另一校区"],
                official_contacts=[],
                source={"publication_reference": "publication:test"},
            )
        ],
        quality=[],
        targets=[],
    )

    def read(actor, *, role_kind, include_official_contacts):
        state.calls += 1
        return deepcopy(state.payload)

    old.repository.leadership_read = read
    module = import_module("app.composition.school_fact_family_leadership_query")
    service = module.FamilyLeadershipQueryService(
        repository=old.repository,
        current_actor=lambda a: state.active and a == actor,
        clock=lambda: NOW,
    )
    return service, actor, state


def test_guardian_without_staff_reads_other_campus_leader_without_channel():
    service, actor, state = fixture()
    result = service.query(actor=actor)
    assert result.public()["leaders"][0]["display_name"] == "正式校长"
    assert "coverage" not in result.public()
    assert "student_id" not in result.public()
    assert result.evidence.valid(actor)


@pytest.mark.parametrize("change", ["revoke", "configuration", "new_leader", "quality"])
def test_evidence_rechecks_relation_configuration_empty_set_and_quality(change):
    service, actor, state = fixture()
    state.payload["leaders"] = []
    state.payload["status"] = "no_current_records"
    result = service.query(actor=actor)
    if change == "revoke":
        state.active = False
    elif change == "configuration":
        state.payload["configuration"] = [{"version": 2}]
    else:
        state.payload["leaders" if change == "new_leader" else "quality"] = [{"new": True}]
    assert not result.evidence.valid(actor)


def test_caregiver_denied_before_directory_read():
    service, actor, state = fixture(caregiver=True)
    assert service.query(actor=actor).public() == {
        "kind": "family_school_leadership",
        "status": "denied",
    }
    assert state.calls == 0


@pytest.mark.parametrize("field", ["appointment_source", "approval_reference", "private_phone"])
def test_overbroad_leader_projection_fails_closed(field):
    service, actor, state = fixture()
    state.payload["leaders"][0][field] = "secret"
    assert service.query(actor=actor).public() == {
        "kind": "family_school_leadership",
        "status": "unavailable",
    }


def test_no_implicit_contact_read_and_current_only_input():
    module = import_module("app.composition.school_fact_family_leadership_gateway")
    from pydantic import ValidationError

    assert not module.FamilyLeadershipInput().include_official_contacts
    for field in ("school_id", "student_id", "as_of", "semantic_role_code"):
        with pytest.raises(ValidationError):
            module.FamilyLeadershipInput.model_validate({field: "untrusted"})


def test_policy_is_school_bound_finite_and_does_not_infer_role_keywords():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy

    policy = FamilyLeadershipPolicy(
        school_id=uuid4(),
        publication_reference="formal:roles",
        version=1,
        school_role_codes=("formal_001",),
        campus_role_codes=("formal_002",),
    )
    assert policy.codes("school_leader") == (("formal_001",), ())
    assert policy.model_copy(update={"version": 2}).fingerprint != policy.fingerprint
    with pytest.raises(ValidationError):
        FamilyLeadershipPolicy.model_validate(
            policy.model_dump() | {"campus_role_codes": ("formal_001",)}
        )


def test_gateway_release_and_history_revalidate_and_omit_source_locators():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_family_leadership_gateway import (
        NAME,
        FamilyLeadershipToolGateway,
    )
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, actor, state = fixture()
    turn = FamilyLeadershipToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor), query=service
    ).for_turn("trusted")
    result = turn.execute(
        ModelToolCall(call_id="l", name=NAME, arguments={}),
        ToolCallContext("trusted", "c", "t", "r", 1, uuid4()),
    )
    assert "publication:test" not in str(result.model_content)
    assert result.model_content["leaders"][0]["source"]["label"] == "学校正式发布的任职资料"
    assert result.history_check.valid()
    state.active = False
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("trusted")


def test_quality_projection_rejects_internal_evidence_and_unknown_columns():
    service, actor, state = fixture()
    state.payload["quality"] = [{"evidence_reference": "private approval"}]
    assert service.query(actor=actor).public() == {
        "kind": "family_school_leadership",
        "status": "unavailable",
    }


def test_report_targets_are_private_and_only_displayed_values_are_retained():
    service, actor, state = fixture()
    state.payload["targets"] = [
        {
            "fact_kind": "person",
            "fact_source_key": "leader:1",
            "field_name": "display_name",
            "target_version": 1,
            "label": "领导姓名",
        }
    ]
    result = service.query(actor=actor)
    assert result.correction_targets[0]["fact_kind"] == "person"
    assert "leader:1" not in str(result.public())


def test_repository_releases_session_and_uses_only_server_policy_codes():
    from app.composition.school_fact_family_leadership_query import FamilyLeadershipRepository
    from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy

    service, actor, state = fixture()
    policy = FamilyLeadershipPolicy(
        school_id=actor.school_id,
        publication_reference="approved:1",
        version=1,
        school_role_codes=("formal_001",),
        campus_role_codes=(),
    )
    active = []

    class Session:
        def __enter__(self):
            active.append(True)
            return self

        def __exit__(self, *args):
            active.pop()

        def execute(self, sql, params):
            assert active == [True]
            assert params["school"] == actor.school_id
            assert params["school_codes"] == ["formal_001"]
            assert not params["contacts"]
            return self

        def scalar_one(self):
            return state.payload

    repo = FamilyLeadershipRepository(Session, lambda: policy)
    result = repo.leadership_read(actor, role_kind="all", include_official_contacts=False)
    assert not active
    assert result["configuration"] == policy.fingerprint


def test_quality_keeps_specific_leader_and_its_own_publication_basis():
    service, actor, state = fixture()
    state.payload["quality"] = [
        {
            "quality_kind": "dispute",
            "applies_to": "李校长",
            "source": {
                "publication_reference": "publication:quality",
                "published_at": "2026-09-01T00:00:00Z",
            },
        }
    ]
    result = service.query(actor=actor).public()
    assert result["status"] == "available"
    assert result["quality"][0]["applies_to"] == "李校长"
    assert result["quality"][0]["source"]["publication_reference"] == "publication:quality"


def test_leadership_quality_requires_exact_applied_lineage():
    from pathlib import Path

    sql = (
        Path(__file__).parents[2] / "alembic/versions/20260914_33_f5_family_leadership.py"
    ).read_text(encoding="utf-8")
    assert "ql.target_id=q.id AND ql.target_version=q.version" in sql
    assert "ql.fact_kind='fact_quality'" in sql


def leadership_history_environment():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.composition.school_fact_family_leadership_gateway import (
        NAME,
        FamilyLeadershipToolGateway,
    )
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext

    service, actor, state = fixture()
    state.selected_actor, state.resolves = actor, 0

    def resolve(_):
        state.resolves += 1
        if state.selected_actor is None:
            raise AssistantRuntimeError("synthetic logout")
        return SimpleNamespace(actor=state.selected_actor)

    turn = FamilyLeadershipToolGateway(resolve_actor=resolve, query=service).for_turn("family")
    result = turn.execute(
        ModelToolCall(call_id="history", name=NAME, arguments={}),
        ToolCallContext("family", "c", "t", "r", 1, uuid4()),
    )
    assert result.history_check is not None
    state.calls = state.resolves = 0
    return turn, result.history_check, state


def test_leadership_history_registers_without_duplicate_identity_resolution():
    from app.composition.school_fact_read_evidence import validation_batch

    turn, check, state = leadership_history_environment()
    with validation_batch():
        turn.revalidate("family")
        assert check.valid() and check.refresh()
        assert (state.resolves, state.calls) == (1, 0)
    assert (state.resolves, state.calls) == (2, 1)
    with validation_batch():
        assert check.valid()
    assert (state.resolves, state.calls) == (3, 2)


@pytest.mark.parametrize("change", ["child", "logout", "revoked", "boundary", "configuration"])
def test_leadership_history_checks_fresh_identity_and_complete_evidence_at_release(change):
    from dataclasses import replace

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from tests.test_f3_product_identity import guardian

    _, check, state = leadership_history_environment()
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            assert check.valid()
            if change == "child":
                state.selected_actor = guardian(2)
            elif change == "logout":
                state.selected_actor = None
            elif change == "revoked":
                state.active = False
            elif change == "boundary":
                state.basis = replace(state.basis, valid_to=NOW)
            else:
                state.payload["configuration"] = [{"version": 2}]


def test_leadership_history_outside_release_is_immediate_and_not_cached():
    _, check, state = leadership_history_environment()
    assert check.valid()
    assert (state.resolves, state.calls) == (1, 1)
    state.active = False
    assert not check.refresh()
    assert state.resolves == 2
