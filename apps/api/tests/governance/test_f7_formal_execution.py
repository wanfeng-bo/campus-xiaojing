"""Synthetic-only orchestration; no original DB, model, secret or service access."""

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from scripts.f4_engineering.runtime_configuration import canonical_manifest_json
from scripts.f7_engineering import runtime_configuration as scope

NOW = datetime(2026, 9, 24, 16, tzinfo=UTC)


def module():
    return importlib.import_module("scripts.f7_engineering.formal_execution")


def versions(group):
    if group == "teacher_dependencies":
        return {key: 1 for key in scope.TEACHER_DEPENDENCY_READ_SOURCES}
    if group == "dependencies":
        return {"f4:stage:junior": 1}
    if group in {"sample_create", "sample_stage_create_reviewed"}:
        return {}
    if group == "sample_published":
        return {key: 1 for key in (
            *scope.BUSINESS_SAMPLE_KEYS, "f4:organization:a", "f4:stage:junior",
        )}
    keys = (scope.REQUIRED_EXISTING_SOURCES if group == "first" else
            (*scope.NEW_QUALITY_SOURCES, *scope.RETAINED_F4_QUALITY_SOURCES,
             *scope.TEACHER_REQUIRED_EXISTING_SOURCES))
    return {key: 1 for key in keys}


def prepare(group="first"):
    return module().prepare_scope_phase(
        group=group, source_versions=versions(group), existing_duties=(),
        grant_epoch=NOW, execution_at=NOW + timedelta(minutes=1),
        authorization_reference="user-approved-F7-synthetic-only",
    )


def completed(manifest):
    m = module()
    digest = hashlib.sha256(canonical_manifest_json(manifest).encode()).hexdigest()
    receipt = {
        "manifest_sha256": digest, "status": "succeeded",
        "commands": {command.idempotency_key: {
            "state": "succeeded", "digest": m.adapter.command_digest(command),
            "receipt": {"target_id": str(UUID(int=100 + i)), "target_version": 1},
        } for i, command in enumerate(manifest.materialize())},
    }
    approval = {
        "state": "closed", "manifest_sha256": digest,
        "issuer_reference": m.runtime_governance.ISSUER_REFERENCE,
        "actor_reference": m.ACTOR_REFERENCE,
        "authorization_reference": manifest.approval_reference,
        "valid_from": manifest.valid_from.isoformat(),
        "valid_to": manifest.valid_to.isoformat(),
        "fixed_deadline": manifest.grant_valid_to.isoformat(),
        "closed_at": (manifest.valid_from + timedelta(minutes=1)).isoformat(),
    }
    return receipt, approval


def test_first_phase_has_nine_scopes_plus_terminal_and_second_has_two_plus_thirteen():
    first, second = prepare(), prepare("second")
    assert len([c for c in first.commands if c.command == "scope"]) == 9
    assert "f7_own_terminal_reader" in first.approved_duty_codes
    assert len([c for c in second.commands if c.command == "scope"]) == 15
    assert second.grant_epoch == first.grant_epoch == NOW
    assert second.grant_valid_to == first.grant_valid_to == NOW + timedelta(days=7)
    assert second.approved_assignment_ids == frozenset({
        scope.ORIGINAL_MAINTAINER_ASSIGNMENT, scope.ORIGINAL_TEACHER_ASSIGNMENT,
    })


@pytest.mark.parametrize("group,count", [
    ("first", 10), ("second", 15), ("dependencies", 1),
    ("sample_create", 2), ("sample_published", 4), ("sample_stage_create_reviewed", 1),
    ("teacher_dependencies", 2),
])
def test_grants_come_only_from_exact_closed_scope_receipts(group, count):
    m = module()
    manifest = prepare(group)
    evidence, approval = completed(manifest)
    grants = m.prepare_grant_phase(
        group=group, scope_manifest=manifest, evidence=evidence, approval=approval,
        execution_at=NOW + timedelta(minutes=3), terminal_scope=m.REVIEWED_TERMINAL_SCOPE,
    )
    assert len(grants.commands) == count
    assert all(c.command == "grant" for c in grants.commands)
    assert all(c.planned_valid_to == NOW + timedelta(days=7) for c in grants.commands)
    if group == "first":
        terminal = next(c for c in grants.commands if c.duty_code == "f7_own_terminal_reader")
        assert str(terminal.scope_id) == m.REVIEWED_TERMINAL_SCOPE["scope_id"]
        assert terminal.scope_owner == "school_facts.school"
    elif group == "second":
        assert sum(c.assignment_id == scope.ORIGINAL_TEACHER_ASSIGNMENT
                   for c in grants.commands) == 13
    elif group == "dependencies":
        assert grants.commands[0].duty_code == "f4_education_stage_reader"
        assert grants.commands[0].assignment_id == scope.ORIGINAL_MAINTAINER_ASSIGNMENT
    elif group == "teacher_dependencies":
        assert {c.duty_code for c in grants.commands} == {
            "f3_person_reader", "f3_assignment_reader",
        }
        assert all(c.assignment_id == scope.ORIGINAL_TEACHER_ASSIGNMENT for c in grants.commands)
        assert all(c.valid_from == NOW + timedelta(minutes=3) for c in grants.commands)


def test_teacher_dependency_late_execution_keeps_original_deadline_and_exact_source_set():
    m = module()
    manifest = m.prepare_scope_phase(
        group="teacher_dependencies", source_versions=versions("teacher_dependencies"),
        existing_duties=(), grant_epoch=NOW, execution_at=NOW + timedelta(hours=4),
        authorization_reference="user-approved-F7-synthetic-only",
    )
    evidence, approval = completed(manifest)
    grants = m.prepare_grant_phase(
        group="teacher_dependencies", scope_manifest=manifest, evidence=evidence, approval=approval,
        execution_at=NOW + timedelta(hours=4, minutes=3),
        terminal_scope=m.REVIEWED_TERMINAL_SCOPE,
    )
    assert len(grants.commands) == 2
    assert grants.grant_epoch == NOW
    assert grants.grant_valid_to == NOW + timedelta(days=7)
    assert all(c.valid_from == NOW + timedelta(hours=4, minutes=3)
               and c.planned_valid_to == NOW + timedelta(days=7)
               for c in grants.commands)
    for invalid in ({}, versions("teacher_dependencies") | {"f3:person:other": 1}):
        with pytest.raises(ValueError):
            m._rows("teacher_dependencies", invalid)


@pytest.mark.parametrize("mutation", ["unclosed", "digest", "missing_receipt", "rolling"])
def test_bad_prerequisites_do_not_create_new_grants(mutation):
    m = module()
    manifest = prepare()
    evidence, approval = completed(manifest)
    if mutation == "unclosed":
        approval["state"] = "open"
    elif mutation == "digest":
        next(iter(evidence["commands"].values()))["digest"] = "0" * 64
    elif mutation == "missing_receipt":
        evidence["commands"].pop(next(iter(evidence["commands"])))
    else:
        approval["fixed_deadline"] = (NOW + timedelta(days=8)).isoformat()
    with pytest.raises(ValueError):
        m.prepare_grant_phase(
            group="first", scope_manifest=manifest, evidence=evidence,
            approval=approval, execution_at=NOW + timedelta(minutes=3),
            terminal_scope=m.REVIEWED_TERMINAL_SCOPE,
        )


def test_dispatch_defaults_to_review_and_never_calls_governance(monkeypatch):
    m = module()
    monkeypatch.setattr(m.runtime_governance, "run_phase",
                        lambda **_: pytest.fail("unexpected formal dispatch"))
    result = m.dispatch_phase(manifest=prepare())
    assert result["status"] == "prepared_not_executed"
    assert result["model_calls"] == 0


def references():
    return {key: "synthetic-ref-" + str(i) for i, key in enumerate((
        *scope.NEW_QUALITY_SOURCES, "f3:student:a", "f4:teacher:a-2023:current",
        "f4:calendar:junior", "f4:location:a",
    ))}


class ProductClient:
    base_url = "http://127.0.0.1:5174"

    def __init__(self, *, unknown=False, mutate_summary=None, receipt_status=200,
                 mutate_receipt=None):
        self.calls = []
        self.unknown = unknown
        self.context = "synthetic-staff-context"
        self.mutate_summary = mutate_summary
        self.receipt_status = receipt_status
        self.mutate_receipt = mutate_receipt

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/session"):
            body = {"status": "signed_in", "selected_context_kind": "staff_assignment",
                    "selected_context_ref": self.context}
        elif url.endswith("/prepare"):
            payload = kwargs["json"]
            after = deepcopy(payload["fields"])
            if payload["fields"]["target_fact_kind"] == "student":
                fields = payload["fields"]
                after = {
                    "所属资料": "student", "目标记录": "f3:student:a",
                    "所属人员": "synthetic:person:student-a", "适用字段": "status",
                    "声明类型": "source", "核查依据": fields["basis_reference"],
                    "责任主体": fields["responsibility"], "核查时间": fields["verified_at"],
                    "有效至": "未预设", "来源状态": "可用（学校登记）",
                }
            body = {"proposal_ref": "synthetic-proposal", "summary": {
                "fact_kind": "fact_quality", "reference": payload["creation_ref"],
                "before": None, "after": after, "operation": payload["operation"],
                "effective_at": payload["effective_at"].replace("+00:00", "Z"),
                "reason": payload["reason"], "impacts": [], "before_segments": [],
                "after_segments": [], "regroup": None,
                "label": "合成质量资料", "source_label": "学校正式登记资料",
            }}
            if self.mutate_summary:
                self.mutate_summary(body["summary"])
        elif "/history-receipts/" in url:
            body = {"reference": "sf2receipt_" + hashlib.sha256(b"synthetic-proposal").hexdigest(),
                    "status": "unknown" if self.unknown else "succeeded", "changed_count": 1,
                    "target_version": 21, "targets": [], "read_only": True,
                    "target_page": {"offset": 0, "next_offset": 1, "target_count": 1,
                                    "has_more": False}}
            if self.mutate_receipt:
                self.mutate_receipt(body)
            return type("Response", (), {"status_code": self.receipt_status,
                                         "json": lambda _: body})()
        else:
            body = {"reference": "synthetic-proposal", "status": (
                "unknown" if self.unknown else "succeeded"
            ), "changed_count": 1, "target_version": 21, "request_id": "synthetic-request"}
        return type("Response", (), {"status_code": 200, "json": lambda _: body})()


def publisher(tmp_path, monkeypatch, client):
    m = module()
    monkeypatch.setattr(m.private_paths, "require_external_private_storage", lambda p, **_: p)
    return m.QualityPublication(
        client=client, expected_context_ref=client.context,
        journal_path=tmp_path / "synthetic-journal.json",
        payloads=m.quality_payloads(references=references(), verified_at=NOW, academic_year=2026),
        verified_student_person_source_key="synthetic:person:student-a",
    )


def test_quality_payloads_use_formal_references_and_no_invented_person():
    rows = module().quality_payloads(references=references(), verified_at=NOW, academic_year=2026)
    assert set(rows) == set(scope.NEW_QUALITY_SOURCES)
    assert rows[scope.NEW_QUALITY_SOURCES[0]]["route"] == "facts"
    assert rows[scope.NEW_QUALITY_SOURCES[1]]["route"] == "education-facts"
    assert "person_ref" not in rows[scope.NEW_QUALITY_SOURCES[0]]["payload"]["fields"]
    assert rows[scope.NEW_QUALITY_SOURCES[2]]["payload"]["fields"]["coverage_status"] == "complete"
    assert rows[scope.NEW_QUALITY_SOURCES[2]]["payload"]["fields"]["academic_year"] == 2026


def test_publication_requires_explicit_execute_and_confirmation(tmp_path, monkeypatch):
    client = ProductClient()
    value = publisher(tmp_path, monkeypatch, client)
    assert value.publish(scope.NEW_QUALITY_SOURCES[0])["status"] == "prepared_not_executed"
    assert client.calls == []
    with pytest.raises(ValueError):
        value.publish(scope.NEW_QUALITY_SOURCES[0], execute=True, confirmation="wrong")
    assert client.calls == []


def test_unknown_confirm_is_receipt_only_on_resume_and_never_replayed(tmp_path, monkeypatch):
    m = module()
    client = ProductClient(unknown=True)
    value = publisher(tmp_path, monkeypatch, client)
    key = scope.NEW_QUALITY_SOURCES[0]
    with pytest.raises(m.FormalExecutionStopped):
        value.publish(key, execute=True, confirmation=value.confirmation_marker)
    assert sum(url.endswith("/confirm") for _, url, _ in client.calls) == 1
    client.unknown = False
    result = value.publish(key, execute=True, confirmation=value.confirmation_marker)
    assert result["status"] == "succeeded"
    assert sum(url.endswith("/confirm") for _, url, _ in client.calls) == 1
    assert sum(url.endswith("/prepare") for _, url, _ in client.calls) == 1
    saved = json.loads((tmp_path / "synthetic-journal.json").read_text())
    assert "payload" not in saved and "summary" not in saved
    assert "synthetic-staff-context" not in json.dumps(saved)


def test_relogin_recovers_original_terminal_without_target_read_or_confirmation_replay(
    tmp_path, monkeypatch,
):
    client = ProductClient(unknown=True)
    first = publisher(tmp_path, monkeypatch, client)
    key = scope.NEW_QUALITY_SOURCES[1]
    with pytest.raises(module().FormalExecutionStopped):
        first.publish(key, execute=True, confirmation=first.confirmation_marker)
    assert first.journal["items"][key]["state"] == "confirming"
    # The new login is independently authenticated; only owner terminal scope is
    # currently readable. Original target details may legitimately be absent.
    new_login = ProductClient()
    new_login.context = "same-owner-new-login-context"
    recovered = publisher(tmp_path, monkeypatch, new_login)
    result = recovered.publish(key, execute=True, confirmation=recovered.confirmation_marker)
    assert result["reference"] == "synthetic-proposal"
    history_ref = "sf2receipt_" + hashlib.sha256(b"synthetic-proposal").hexdigest()
    assert result["history_reference"] == history_ref
    assert result["status"] == "succeeded" and result["changed_count"] == 1
    recovery_requests = [
        (method, url) for method, url, _ in new_login.calls if not url.endswith("/session")
    ]
    assert recovery_requests == [
        ("GET", "/api/v1/school-fact-product/facts/history-receipts/" + history_ref),
    ]
    assert sum(url.endswith("/confirm") for _, url, _ in client.calls) == 1


def test_other_owner_cannot_recover_or_reconfirm_original_publication(tmp_path, monkeypatch):
    client = ProductClient(unknown=True)
    first = publisher(tmp_path, monkeypatch, client)
    key = scope.NEW_QUALITY_SOURCES[0]
    with pytest.raises(module().FormalExecutionStopped):
        first.publish(key, execute=True, confirmation=first.confirmation_marker)
    other = ProductClient(receipt_status=403)
    other.context = "different-owner"
    resumed = publisher(tmp_path, monkeypatch, other)
    with pytest.raises(module().FormalExecutionStopped, match="response_rejected"):
        resumed.publish(key, execute=True, confirmation=resumed.confirmation_marker)
    assert resumed.journal["items"][key]["state"] == "confirming"
    assert not any(method == "POST" for method, _, _ in other.calls)


@pytest.mark.parametrize("field,value", [
    ("reference", "sf2receipt_" + "a" * 64), ("changed_count", 2),
    ("target_version", 0), ("target_version", True),
])
def test_unrelated_or_incomplete_terminal_never_marks_publication_succeeded(
    tmp_path, monkeypatch, field, value,
):
    client = ProductClient(mutate_receipt=lambda body: body.update({field: value}))
    service = publisher(tmp_path, monkeypatch, client)
    key = scope.NEW_QUALITY_SOURCES[0]
    with pytest.raises(module().FormalExecutionStopped, match="receipt_differs"):
        service.publish(key, execute=True, confirmation=service.confirmation_marker)
    assert service.journal["items"][key]["state"] == "confirming"


def test_identity_change_prevents_prepare(tmp_path, monkeypatch):
    m = module()
    client = ProductClient()
    value = publisher(tmp_path, monkeypatch, client)
    client.context = "another-identity"
    with pytest.raises(m.FormalExecutionStopped):
        value.publish(scope.NEW_QUALITY_SOURCES[0], execute=True,
                      confirmation=value.confirmation_marker)
    assert not any(url.endswith("/prepare") for _, url, _ in client.calls)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_exact_quality_preview_with_equivalent_utc_spelling_can_be_confirmed(
    tmp_path, monkeypatch, index
):
    client = ProductClient()
    value = publisher(tmp_path, monkeypatch, client)
    result = value.publish(scope.NEW_QUALITY_SOURCES[index], execute=True,
                           confirmation=value.confirmation_marker)
    assert result["status"] == "succeeded"
    assert sum(url.endswith("/confirm") for _, url, _ in client.calls) == 1


@pytest.mark.parametrize("index", [1, 2])
def test_single_education_batch_primary_impact_is_verified_and_confirmed(
    tmp_path, monkeypatch, index,
):
    from copy import deepcopy
    def batch_summary(summary):
        summary["impacts"] = [deepcopy({k: v for k, v in summary.items() if k != "impacts"})]
    client = ProductClient(mutate_summary=batch_summary)
    value = publisher(tmp_path, monkeypatch, client)
    result = value.publish(scope.NEW_QUALITY_SOURCES[index], execute=True,
                           confirmation=value.confirmation_marker)
    assert result["status"] == "succeeded"
    assert sum(url.endswith("/confirm") for _, url, _ in client.calls) == 1


@pytest.mark.parametrize("mutation", ["different_target", "different_after", "extra_impact"])
def test_education_batch_primary_impact_must_match_complete_review(tmp_path, monkeypatch, mutation):
    from copy import deepcopy
    def batch_summary(summary):
        impact = deepcopy({k: v for k, v in summary.items() if k != "impacts"})
        summary["impacts"] = [impact]
        if mutation == "different_target":
            impact["reference"] = "another-controlled-object"
        elif mutation == "different_after":
            impact["after"]["field_name"] = "teacher_role"
        else:
            summary["impacts"].append(deepcopy(impact))
    client = ProductClient(mutate_summary=batch_summary)
    value = publisher(tmp_path, monkeypatch, client)
    with pytest.raises(module().FormalExecutionStopped, match="preview_differs"):
        value.publish(scope.NEW_QUALITY_SOURCES[1], execute=True,
                      confirmation=value.confirmation_marker)
    assert not any(url.endswith("/confirm") for _, url, _ in client.calls)


@pytest.mark.parametrize("index,field,altered", [
    (0, "目标记录", "f3:student:another"),
    (0, "所属人员", "synthetic:person:another"),
    (0, "来源状态", "不可用（学校登记）"),
    (0, "核查依据", "another basis"),
    (0, "责任主体", "another maintainer"),
    (0, "适用字段", "student_number"),
    (0, "有效至", "2026-09-24T19:00:00Z"),
    (1, "education_target_ref", "synthetic-another-owner"),
    (1, "target_ref", "synthetic-another-target"),
    (1, "source_status", "unavailable"),
    (1, "field_name", "teacher_role"),
    (1, "verified_at", "2026-09-24T19:00:00Z"),
    (2, "campus_ref", "synthetic-other-campus"),
    (2, "academic_year", 2027),
    (2, "coverage_status", "partial"),
    (2, "target_fact_kind", "education_class"),
    (2, "owner_kind", "person"),
    (2, "additional_field", "unreviewed"),
])
def test_changed_quality_preview_field_never_reaches_confirmation(
    tmp_path, monkeypatch, index, field, altered
):
    client = ProductClient(mutate_summary=lambda s: s["after"].update({field: altered}))
    value = publisher(tmp_path, monkeypatch, client)
    with pytest.raises(module().FormalExecutionStopped, match="preview_differs"):
        value.publish(scope.NEW_QUALITY_SOURCES[index], execute=True,
                      confirmation=value.confirmation_marker)
    assert not any(url.endswith("/confirm") for _, url, _ in client.calls)
    assert not value.journal["items"]


@pytest.mark.parametrize("field,altered", [
    ("operation", "revoke"), ("reason", "unreviewed operation"),
    ("effective_at", "2026-09-24T19:00:00Z"), ("effective_at", "2026-09-24T16:00:00"),
    ("after", None), ("after", {}), ("impacts", [{"unreviewed": True}]),
    ("before_segments", [{"unreviewed": True}]),
    ("after_segments", [{"unreviewed": True}]), ("regroup", {"unreviewed": True}),
])
def test_changed_quality_operation_or_unreviewed_impact_never_reaches_confirmation(
    tmp_path, monkeypatch, field, altered
):
    client = ProductClient(mutate_summary=lambda s: s.update({field: altered}))
    value = publisher(tmp_path, monkeypatch, client)
    with pytest.raises(module().FormalExecutionStopped, match="preview_differs"):
        value.publish(scope.NEW_QUALITY_SOURCES[1], execute=True,
                      confirmation=value.confirmation_marker)
    assert not any(url.endswith("/confirm") for _, url, _ in client.calls)
    assert not value.journal["items"]


def test_new_cross_family_samples_have_no_student_or_active_offering_dependencies():
    m = module()
    batch = m.business_sample_batch(stage="create", base_version=1, effective_at=NOW,
                                    current_versions={})
    assert {c.fact_kind for c in batch.changes} == {"organization", "education_stage"}
    assert all(c.expected_version == 0 for c in batch.changes)
    changed = m.business_sample_batch(
        stage="rename", base_version=2, effective_at=NOW + timedelta(minutes=1),
        current_versions={key: 1 for key in m.BUSINESS_SAMPLE_KEYS},
    )
    assert len(changed.changes) == 2
    assert all(c.content.display_name.endswith("（已更正）") for c in changed.changes)
    with pytest.raises(ValueError):
        m.business_sample_batch(stage="rename", base_version=2, effective_at=NOW,
                                current_versions={})
