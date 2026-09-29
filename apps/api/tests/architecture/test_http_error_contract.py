from __future__ import annotations

import pytest

from app.main import app


def test_all_openapi_422_responses_match_safe_runtime_error_contract() -> None:
    document = app.openapi()
    validation_responses = [
        operation["responses"]["422"]["content"]["application/json"]["schema"]
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"} and "422" in operation["responses"]
    ]

    assert validation_responses
    assert validation_responses == [{"$ref": "#/components/schemas/HttpErrorResponse"}] * len(
        validation_responses
    )
    assert "HTTPValidationError" not in document["components"]["schemas"]


@pytest.mark.parametrize("status", [401, 403, 409, 422, 503])
def test_api_error_retains_safe_envelope_through_python_context_managers(status):
    from contextlib import contextmanager
    from uuid import UUID

    from app.interfaces.http.errors import ApiError, render_api_error

    @contextmanager
    def request_lifetime():
        yield

    error = ApiError(status_code=status, code="safe.code", message="safe message")
    with pytest.raises(ApiError) as caught:
        with request_lifetime():
            raise error
    assert caught.value is error
    # BaseException traceback mutation is required by contextlib and re-raising.
    assert error.with_traceback(error.__traceback__) is error
    response = render_api_error(UUID(int=1), error)
    assert response.status_code == status
    assert b"safe.code" in response.body
