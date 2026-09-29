from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    OwnReceipt,
    OwnReceiptPage,
    OwnReceiptTarget,
)
from app.composition.school_fact_family_staff_gateway import FamilyStaffQueryPort
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_fact_family_staff import create_family_staff_product_router
from tests.test_f2_directory_product import actor

BASE = "/api/v1/school-fact-product/family-facts"


class Query:
    def query(self, **kwargs):
        return {
            "kind": "family_facts",
            "coverage": "unknown",
            "records": [],
            "limit": kwargs["limit"],
        }

    def describe(self, **kwargs):
        return {
            "fact_kind": kwargs["fact_kind"],
            "fields": ["display_name"],
            "creation_choices": [],
            "coverage": "unknown",
        }


class Product:
    calls = []

    def own_receipts(self, **kwargs):
        return OwnReceiptPage(items=(), has_more=False, notice="只读")

    def own_receipt(self, **kwargs):
        return OwnReceipt.model_validate(
            {
                "kind": "own_fact_receipt",
                "reference": kwargs["receipt_ref"],
                "status": "succeeded",
                "message": "完成",
                "changed_count": 1,
                "target_version": 2,
                "request_id": None,
                "confirmed_at": datetime(2026, 9, 13, tzinfo=UTC),
                "read_only": True,
                "targets": (
                    OwnReceiptTarget(
                        fact_kind="student",
                        label="学生基础资料",
                        reference="f3:student:private",
                        operation="upsert",
                        target_version=2,
                    ),
                ),
            }
        )

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("unexpected")


def client(identity, *, product=None):
    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)

    async def dependency():
        yield cast(ProductActor, identity)

    app.include_router(
        create_family_staff_product_router(
            identity_dependency=dependency,
            query=cast(FamilyStaffQueryPort, Query()),
            product=cast(DirectoryFactProductService, Product() if product is None else product),
        )
    )
    return TestClient(app)


def test_catalog_describe_and_query_are_f3_only_no_store():
    c = client(SimpleNamespace(actor=actor(), session_binding="staff-session"))
    catalog = c.get(BASE + "/catalog")
    assert catalog.status_code == 200 and catalog.headers["cache-control"] == "no-store", (
        catalog.text
    )
    assert {x["value"] for x in catalog.json()["families"]} == {
        "person",
        "login_mapping",
        "assignment",
        "student",
        "student_school_relationship",
        "student_contact",
        "guardian_relationship",
        "person_region",
    }
    assert c.get(BASE + "/describe/student").json()["coverage"] == "unknown"
    response = c.post(BASE + "/query", json={"fact_kind": "student", "limit": 20})
    assert response.status_code == 200 and response.json()["records"] == []


def test_guardian_cannot_enter_staff_query_or_catalog():
    c = client(SimpleNamespace(actor=object(), session_binding="guardian-session"))
    assert c.get(BASE + "/catalog").status_code == 403
    assert c.post(BASE + "/query", json={"fact_kind": "student"}).status_code == 403


def test_f2_kind_is_rejected_before_product_prepare():
    Product.calls = []
    c = client(SimpleNamespace(actor=actor(), session_binding="staff-session"))
    response = c.post(
        BASE + "/prepare",
        json={
            "fact_kind": "official_contact",
            "target_ref": "f2:official_contact:abcdefgh",
            "fields": {},
            "reason": "wrong family",
        },
    )
    assert response.status_code == 422
    assert Product.calls == []


def test_own_receipt_http_is_read_only_and_uses_current_identity():
    c = client(SimpleNamespace(actor=actor(), session_binding="new-login"))
    page = c.get(BASE + "/history-receipts", params={"limit": 5})
    assert page.status_code == 200 and page.json()["items"] == []
    ref = "sf2receipt_" + "a" * 64
    detail = c.get(BASE + "/history-receipts/" + ref)
    assert detail.status_code == 200 and detail.json()["read_only"] is True
