from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.composition.school_fact_education_views import EducationMaintenanceViews
from app.composition.school_fact_family_product_adapter import FamilyStaffProductQuery


class Evidence:
    @contextmanager
    def capture(self, **kwargs):
        yield SimpleNamespace(seal=lambda: SimpleNamespace(check=lambda actor: True))


@pytest.mark.parametrize(
    "kind", ["assignment", "person", "organization", "location", "education_class"]
)
def test_education_choices_use_declared_reference_owner(kind):
    view = EducationMaintenanceViews(
        SimpleNamespace(repository=None),
        directory=SimpleNamespace(evidence=Evidence()),
        family=None,
    )
    view.describe = Mock(
        return_value={
            "fields": [{"name": "selected_ref", "type": "reference", "reference_kind": kind}]
        }
    )
    view.query = Mock(return_value={"records": []})
    assert view.reference_choices(
        actor="actor", fact_kind="teaching_assignment", field_name="selected_ref"
    ) == {"records": []}
    view.query.assert_called_once_with(actor="actor", fact_kind=kind)


def test_undeclared_field_never_queries_any_candidate():
    view = EducationMaintenanceViews(
        SimpleNamespace(repository=None),
        directory=SimpleNamespace(evidence=Evidence()),
        family=None,
    )
    view.describe = Mock(return_value={"fields": []})
    view.query = Mock()
    with pytest.raises(ValueError):
        view.reference_choices(actor="actor", fact_kind="teaching_assignment", field_name="secret")
    view.query.assert_not_called()


@pytest.mark.parametrize("kind", ["assignment", "person", "organization", "location"])
def test_family_choices_use_native_or_existing_external_port(kind):
    external = SimpleNamespace(choices=Mock(return_value={"records": []}))
    view = FamilyStaffProductQuery(None, None, external)
    view.describe = Mock(
        return_value={
            "fields": [{"name": "selected_ref", "type": "reference", "reference_kind": kind}]
        }
    )
    view.query = Mock(return_value={"records": []})
    assert view.reference_choices(
        actor="actor", fact_kind="assignment", field_name="selected_ref"
    ) == {"records": []}
    selected = external.choices if kind in {"organization", "location"} else view.query
    selected.assert_called_once_with(actor="actor", fact_kind=kind)


@pytest.mark.parametrize("family", ["education", "family"])
def test_real_http_choices_route_checks_owner_and_rejects_client_target(family):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.interfaces.http.errors import ApiError
    from app.interfaces.http.framework_request import api_error_handler
    from app.interfaces.http.school_fact_education import create_education_product_router
    from app.interfaces.http.school_fact_family_staff import create_family_staff_product_router
    from tests.test_f1_official_contact_query import _actor

    actor = _actor()
    query = SimpleNamespace(reference_choices=Mock(return_value={"records": []}))
    common = dict(
        identity_dependency=lambda: SimpleNamespace(actor=actor), query=query, product=None
    )
    app = FastAPI()
    from app.core.request_id import assign_request_id

    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(
        create_education_product_router(**common, education_query=None, teaching_enabled=True)
        if family == "education"
        else create_family_staff_product_router(**common)
    )
    client = TestClient(app)
    owner = "teaching_assignment" if family == "education" else "assignment"
    path = f"/api/v1/school-fact-product/{family}-facts/choices"
    assert (
        client.post(path, json={"fact_kind": owner, "field_name": "assignment_ref"}).status_code
        == 200
    )
    query.reference_choices.assert_called_once_with(
        actor=actor, fact_kind=owner, field_name="assignment_ref"
    )
    assert (
        client.post(path, json={"fact_kind": "school", "field_name": "assignment_ref"}).status_code
        == 403
    )
    assert (
        client.post(
            path,
            json={
                "fact_kind": owner,
                "field_name": "assignment_ref",
                "target_kind": "person_contact",
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize("valid", [True, False])
def test_assignment_choice_label_uses_only_exact_related_person_and_rechecks(valid):
    class GuardedEvidence:
        @contextmanager
        def capture(self, **kwargs):
            yield SimpleNamespace(seal=lambda: SimpleNamespace(check=lambda actor: valid))

    names_proof = SimpleNamespace(revalidate=Mock())
    external = SimpleNamespace(
        source_for_reference=Mock(
            side_effect=lambda actor, kind, reference: (
                "person:chen" if kind == "person" else "assignment:chen"
            )
        ),
        reference_for=Mock(return_value="f3:assignment:canonical"),
        read_records=Mock(
            return_value={
                "records": [{"source_key": "person:chen", "content": {"display_name": "陈老师"}}],
                "proof": names_proof,
            }
        ),
    )
    view = EducationMaintenanceViews(
        SimpleNamespace(repository=None, external=external),
        directory=SimpleNamespace(evidence=GuardedEvidence()),
        family=None,
    )
    view.describe = Mock(
        return_value={
            "fields": [
                {"name": "assignment_ref", "type": "reference", "reference_kind": "assignment"}
            ]
        }
    )
    view.query = Mock(
        return_value={
            "records": [
                {
                    "reference": "f2:assignment:alias",
                    "label": "任课教师",
                    "fields": {"person_ref": "person-reference"},
                }
            ]
        }
    )
    if not valid:
        with pytest.raises(PermissionError):
            view.reference_choices(
                actor="actor", fact_kind="teaching_assignment", field_name="assignment_ref"
            )
        return
    result = view.reference_choices(
        actor="actor", fact_kind="teaching_assignment", field_name="assignment_ref"
    )
    assert result["records"][0]["label"] == "陈老师 · 任课教师"
    external.read_records.assert_called_once_with(
        "actor", "person", source_keys=("person:chen",), fields=("display_name",), as_of=None
    )
    names_proof.revalidate.assert_called_once_with()


@pytest.mark.parametrize("kind", ["fact_quality", "policy_scope"])
def test_disabled_teaching_preserves_only_existing_dependency_queries(kind):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.request_id import assign_request_id
    from app.interfaces.http.errors import ApiError
    from app.interfaces.http.framework_request import api_error_handler
    from app.interfaces.http.school_fact_education import create_education_product_router
    from tests.test_f1_official_contact_query import _actor

    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    query = SimpleNamespace(query=Mock(return_value={"records": []}))
    app.include_router(
        create_education_product_router(
            identity_dependency=lambda: SimpleNamespace(actor=_actor()),
            query=query,
            product=None,
            education_query=None,
            teaching_enabled=False,
        )
    )
    client = TestClient(app)
    path = "/api/v1/school-fact-product/education-facts/query"
    assert client.post(path, json={"fact_kind": kind}).status_code == 200
    for excluded in ("assignment", "person", "teaching_assignment"):
        assert client.post(path, json={"fact_kind": excluded}).status_code == 403
    assert (
        client.post(
            path.replace("/query", "/choices"),
            json={"fact_kind": "teaching_assignment", "field_name": "assignment_ref"},
        ).status_code
        == 403
    )
    assert query.query.call_count == 1


@pytest.mark.parametrize("kind", ["assignment", "person", "organization", "location"])
def test_external_choices_use_same_canonical_reference_owner_as_form_summary(kind):
    external = SimpleNamespace(
        source_for_reference=Mock(return_value="formal-source"),
        reference_for=Mock(return_value=f"f3:{kind}:canonical"),
    )
    view = EducationMaintenanceViews(
        SimpleNamespace(repository=None, external=external),
        directory=SimpleNamespace(evidence=Evidence()),
        family=None,
    )
    view.describe = Mock(
        return_value={
            "fields": [{"name": "selected_ref", "type": "reference", "reference_kind": kind}]
        }
    )
    view.query = Mock(
        return_value={
            "records": [{"reference": f"f2:{kind}:directory", "label": "正式候选", "fields": {}}]
        }
    )
    result = view.reference_choices(
        actor="actor", fact_kind="teaching_assignment", field_name="selected_ref"
    )
    assert result["records"][0]["reference"] == f"f3:{kind}:canonical"
    external.source_for_reference.assert_called_once_with("actor", kind, f"f2:{kind}:directory")
    external.reference_for.assert_called_once_with(
        actor="actor", fact_kind=kind, source_key="formal-source"
    )


@pytest.mark.parametrize("family_owned", [True, False])
def test_choices_real_external_owner_dispatch_matches_summary(family_owned):
    from app.composition.school_fact_education_external import EducationExternalReader

    directory = SimpleNamespace(
        source_for=Mock(return_value="assignment:chen"),
        reference_for=Mock(return_value="f2:assignment:canonical"),
    )
    family = SimpleNamespace(
        configuration=SimpleNamespace(
            source_catalog={"assignment": ("assignment:chen",) if family_owned else ()}
        ),
        repository=SimpleNamespace(reference_for=Mock(return_value="f3:assignment:canonical")),
    )
    external = EducationExternalReader(directory=directory, evidence=Evidence(), family=family)
    view = EducationMaintenanceViews(
        SimpleNamespace(repository=None, external=external),
        directory=SimpleNamespace(evidence=Evidence()),
        family=None,
    )
    view.describe = Mock(
        return_value={
            "fields": [
                {"name": "assignment_ref", "type": "reference", "reference_kind": "assignment"}
            ]
        }
    )
    view.query = Mock(
        return_value={
            "records": [{"reference": "f2:assignment:directory", "label": "任职", "fields": {}}]
        }
    )
    result = view.reference_choices(
        actor="actor", fact_kind="teaching_assignment", field_name="assignment_ref"
    )
    assert result["records"][0]["reference"] == external.reference_for(
        actor="actor", fact_kind="assignment", source_key="assignment:chen"
    )
    assert result["records"][0]["reference"].startswith("f3:" if family_owned else "f2:")
