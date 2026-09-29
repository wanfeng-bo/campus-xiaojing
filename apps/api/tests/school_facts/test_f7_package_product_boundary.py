"""The formal package channel never confers legacy broad fact-management power."""

import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.school_facts.test_f7_package_http import _fact_source, _request
from tests.school_facts.test_sf3_http import _http_client

PREFIX = "/api/v1/school-fact-product/imports"


def fixture_preview(actor, change_set, read=None):
    from app.platform.master_data_imports.school_facts_replacement import (
        PreparedReplacementEnd,
        prepare_package_preview,
    )

    return prepare_package_preview(
        change_set=change_set,
        read_snapshot=lambda c: read(actor, c.fact_kind, c.source_key, c.effective_at),
        review_change=lambda c: PreparedReplacementEnd((c,), {}, lambda: None),
    )


def upload(client):
    raw = _fact_source()
    response = client.post(
        PREFIX + "/sources",
        json={
            "filename_summary": "synthetic.json",
            "content_base64": base64.b64encode(raw).decode("ascii"),
        },
    )
    assert response.status_code == 201, response.text
    return _request(response.json()["source_ref"], raw)


def set_identity(identities):
    identities["current"] = replace(
        identities["current"], authorized_actions=frozenset({"school_facts.package.submit"})
    )


def test_package_mount_requires_exact_authorizers():
    with pytest.raises(ValueError):
        with _http_client(enable_package=True, package_only=True):
            pass


def test_clean_package_cannot_be_confirmed_without_complete_authorized_preview():
    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
    ) as (client, identities, writer):
        set_identity(identities)
        response = client.post(PREFIX + "/plans/package", json=upload(client))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_clean"] and body["preview_status"] == "unavailable"
        confirmation = client.post(
            PREFIX + f"/plans/{body['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "cannot-skip-preview"},
        )
        assert confirmation.status_code == 422, confirmation.text
        assert not writer.prepared


def test_replace_returns_authorized_before_and_writer_receives_bound_selection(monkeypatch):
    from types import SimpleNamespace

    from app.platform.master_data_imports.school_facts_replacement import ReplacementFactSnapshot
    from app.platform.school_facts.contracts import CanonicalFactChangeV1
    from app.platform.school_facts.ports import FactVersionProjection
    from tests.school_facts.test_sf3_http import NOW, _Reader

    monkeypatch.setattr(
        _Reader,
        "find_fact_version",
        lambda self, school_id, fact_kind, source_key: (
            FactVersionProjection(2) if source_key == "school:old" else None
        ),
    )
    selected_checks = []

    def read(actor, kind, key, at):
        assert kind == "school" and at == NOW
        return ReplacementFactSnapshot(
            kind,
            key,
            at,
            2 if key == "school:old" else 0,
            1,
            "active" if key == "school:old" else "missing",
            {"display_name": "已发布合成名称"} if key == "school:old" else {},
            lambda: None,
        )

    def end(actor, old):
        return (
            CanonicalFactChangeV1(
                schema_version=4,
                fact_kind="school",
                operation="end",
                source_key=old.source_key,
                expected_version=old.expected_version,
                effective_at=old.effective_at,
                content=None,
                origin={"sheet_code": "selection", "row_number": 2},
            ),
        )

    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
        package_replacement=SimpleNamespace(
            read=read, end=end, preview=lambda actor, cs: fixture_preview(actor, cs, read)
        ),
        authorize_package_selection=lambda identity, targets: selected_checks.append(targets),
    ) as (client, identities, writer):
        set_identity(identities)
        payload = upload(client)
        payload["manifest"].update(
            mode="replace",
            replacement_scopes=[
                {
                    "fact_kind": "school",
                    "source_keys": ["school:demo", "school:old"],
                    "effective_at": NOW.isoformat(),
                }
            ],
        )
        response = client.post(PREFIX + "/plans/package", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_clean"]
        assert body["replacement_preview"][1]["before"] == {"display_name": "已发布合成名称"}
        assert body["changes"][1]["operation"] == "end"
        prepared = client.post(
            PREFIX + f"/plans/{body['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "replacement-same-snapshot"},
        )
        assert prepared.status_code == 200, prepared.text
        assert len(selected_checks) == 2
        assert len(writer.prepared[-1].provenance.replacement_targets) == 2


def test_replace_read_revoked_during_preflight_never_releases_old_fields(monkeypatch):
    from types import SimpleNamespace

    from app.platform.master_data_imports.school_facts_replacement import ReplacementFactSnapshot
    from tests.school_facts.test_sf3_http import NOW, _Reader

    state = {"allowed": True}

    def preflight_version(self, school_id):
        state["allowed"] = False
        return 1

    def proof():
        if not state["allowed"]:
            raise PermissionError("current selected read revoked")

    monkeypatch.setattr(_Reader, "current_school_fact_version", preflight_version)
    reader = SimpleNamespace(
        read=lambda actor, kind, key, at: ReplacementFactSnapshot(
            kind, key, at, 0, 1, "missing", {}, proof
        ),
        end=lambda *_: pytest.fail("no end"),
    )
    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=lambda *_: None,
        authorize_package_result=lambda _: lambda: None,
        package_replacement=reader,
        authorize_package_selection=lambda *_: None,
    ) as (client, identities, writer):
        set_identity(identities)
        payload = upload(client)
        payload["manifest"].update(
            mode="replace",
            replacement_scopes=[
                {
                    "fact_kind": "school",
                    "source_keys": ["school:demo"],
                    "effective_at": NOW.isoformat(),
                }
            ],
        )
        response = client.post(PREFIX + "/plans/package", json=payload)
        assert response.status_code == 403, response.text
        assert "replacement_preview" not in response.json()
        assert not writer.prepared


def test_package_mount_excludes_legacy_routes_and_denies_before_preflight():
    calls = []

    def denied(identity, changes):
        calls.append(changes)
        raise PermissionError("not granted")

    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=denied,
        authorize_package_result=lambda _identity: lambda: None,
    ) as (client, identities, writer):
        set_identity(identities)
        payload = upload(client)
        response = client.post(PREFIX + "/plans/package", json=payload)
        assert response.status_code == 403, response.text
        assert not writer.prepared
        for path in ("/plans/admin", "/plans/excel", "/plans/adapted"):
            assert client.post(PREFIX + path, json={}).status_code == 404
        assert client.post("/api/v1/school-facts/plans/package", json=payload).status_code == 404
    assert len(calls) == 1


def test_preflight_rechecks_before_release_and_confirmation_rechecks_current_scope():
    calls = []
    state = {"allowed": True}

    def guard(identity, changes):
        calls.append((identity.actor, changes))
        if not state["allowed"]:
            raise PermissionError("revoked")

    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=guard,
        authorize_package_result=lambda _identity: lambda: None,
    ) as (client, identities, writer):
        set_identity(identities)
        planned = client.post(PREFIX + "/plans/package", json=upload(client))
        assert planned.status_code == 200, planned.text
        assert len(calls) == 2
        state["allowed"] = False
        prepared = client.post(
            PREFIX + f"/plans/{planned.json()['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "f7-package-revoked"},
        )
        assert prepared.status_code == 403, prepared.text
        assert not writer.prepared


def test_lost_permission_during_preflight_does_not_release_old_values():
    count = [0]

    def guard(_identity, _changes):
        count[0] += 1
        if count[0] > 1:
            raise PermissionError("revoked after read")

    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=guard,
        authorize_package_result=lambda _identity: lambda: None,
    ) as (client, identities, _writer):
        set_identity(identities)
        response = client.post(PREFIX + "/plans/package", json=upload(client))
    assert response.status_code == 403
    assert "changes" not in response.json()


def test_confirmation_and_result_keep_current_authority_and_owned_session():
    state = {"maintain": True, "read": True}
    checked = []

    def guard(_identity, _changes):
        if not state["maintain"]:
            raise PermissionError("revoked")

    def result_guard(_identity):
        if not state["read"]:
            raise PermissionError("own terminal duty revoked")
        return lambda: checked.append("result-release")

    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=guard,
        authorize_package_result=result_guard,
        package_replacement=SimpleNamespace(preview=fixture_preview),
    ) as (client, identities, writer):
        set_identity(identities)
        planned = client.post(PREFIX + "/plans/package", json=upload(client)).json()
        path = PREFIX + f"/plans/{planned['plan_ref']}"
        prepared = client.post(path + "/confirmation", headers={"Idempotency-Key": "prepare"})
        assert prepared.status_code == 200
        state["maintain"] = False
        denied = client.post(
            path + "/confirm",
            json={"credential": prepared.json()["credential"]},
            headers={"Idempotency-Key": "confirm"},
        )
        assert denied.status_code == 403
        assert writer.confirm_calls == 0
        assert client.get(path + "/result").status_code == 200
        assert checked == ["result-release"]
        state["read"] = False
        assert client.get(path + "/result").status_code == 403
        state["read"] = True
        identities["current"] = replace(identities["current"], session_binding="other-session")
        assert client.get(path + "/result").status_code == 404


def test_cross_owner_reference_denial_happens_before_any_writer_preflight_read(monkeypatch):
    import json
    from types import SimpleNamespace

    from app.composition.school_fact_package_authorization import (
        PackageChangeAuthorization,
        PackageReferenceAuthorization,
    )
    from tests.school_facts.test_f7_package_authorization import partition
    from tests.school_facts.test_sf3_http import NOW, _HttpReader

    def unexpected(*_args, **_kwargs):
        pytest.fail("Unauthorized reference reached the Writer preflight reader")

    monkeypatch.setattr(_HttpReader, "current_school_fact_version", unexpected)
    family = SimpleNamespace(
        require_scope=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("reference denied")
        )
    )
    edu = SimpleNamespace(
        require_scope=lambda *_args, **_kwargs: object(),
        require_object=lambda *_args, **_kwargs: None,
        revalidate=lambda *_args, **_kwargs: None,
    )
    authority = PackageChangeAuthorization(
        partition(),
        {"education": lambda *_args: None},
        references=PackageReferenceAuthorization(
            partition(),
            directory_guard=None,
            family_authority=family,
            education_authority=edu,
            residence_authority=None,
        ),
    )
    raw = json.dumps(
        {
            "input_version": "school_facts_source_v5",
            "changes": [
                {
                    "operation": "upsert",
                    "fact_kind": "student_class_membership",
                    "source_key": "authorized-membership",
                    "expected_version": 0,
                    "effective_at": NOW.isoformat(),
                    "content": {
                        "student_source_key": "unreadable-student",
                        "class_source_key": "allowed-class",
                        "school_relationship_source_key": "unreadable-enrollment",
                        "membership_status": "assigned",
                    },
                }
            ],
        }
    ).encode()
    with _http_client(
        enable_package=True,
        package_only=True,
        authorize_package=lambda identity, batch: authority(identity.actor, batch),
        authorize_package_result=lambda _identity: lambda: None,
    ) as (client, identities, _writer):
        set_identity(identities)
        uploaded = client.post(
            PREFIX + "/sources",
            json={
                "filename_summary": "synthetic.json",
                "content_base64": base64.b64encode(raw).decode(),
            },
        )
        payload = _request(uploaded.json()["source_ref"], raw)
        payload["manifest"]["sources"][0]["fact_kinds"] = ["student_class_membership"]
        result = client.post(PREFIX + "/plans/package", json=payload)
        assert result.status_code == 403, result.text


def test_package_preview_preserves_structured_fields_and_temporal_segments():
    from types import SimpleNamespace

    from app.interfaces.http.school_facts import _plan_response
    from app.platform.school_facts.contracts import CanonicalFactContentV4
    from tests.school_facts.test_sf3_http import NOW, _change_set

    original = _change_set().changes[0]
    content = CanonicalFactContentV4(display_name="合成班", teacher_role_limits={"primary": 1})
    candidate = original.model_copy(
        update={
            "fact_kind": "education_class",
            "content": None,
            "operation": "correct",
            "correction_reason": "学校登记纠错",
            "replacement_segments": (
                SimpleNamespace(
                    effective_at=NOW,
                    valid_to=None,
                    cause_source_key=None,
                    cause_version=None,
                    content=content,
                ),
            ),
        }
    )
    result = _plan_response(
        SimpleNamespace(
            plan_ref="sf3plan_example",
            preflight=SimpleNamespace(plan_sha256="a" * 64, is_clean=True, issues=()),
            change_set=_change_set().model_copy(update={"changes": (candidate,)}),
        )
    )
    preview = result.changes[0]
    assert preview.operation == "correct"
    assert preview.correction_reason == "学校登记纠错"
    assert preview.effective_at == NOW
    segment = preview.replacement_segments[0]
    assert segment.effective_at == NOW
    assert {item.field_code: item.new_value for item in segment.fields}["teacher_role_limits"] == {
        "primary": 1
    }
