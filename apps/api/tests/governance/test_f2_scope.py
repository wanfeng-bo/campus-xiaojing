from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.platform.governance.contracts import ScopeResolutionRequest
from app.platform.school_facts.f2_policy import F2FactScope, F2ScopeResolver


def test_exact_scope_rejects_unbounded_and_missing_existing():
    school = uuid4()
    with pytest.raises(ValueError):
        F2FactScope(school, "person", ("*",), ())
    with pytest.raises(ValueError):
        F2FactScope(school, "guardian", ("a",), ())
    with pytest.raises(ValueError):
        F2FactScope(school, "school", (), ("replacement-root",))
    scope = F2FactScope(school, "person", ("existing",), ("finite-new",))
    assert F2FactScope.from_reference(scope.reference) == scope
    request = ScopeResolutionRequest(
        school,
        uuid4(),
        "school_facts.f2",
        "exact_fact_set",
        1,
        scope.document,
        scope.document_sha256,
        datetime.now(UTC),
    )
    seen = []
    resolver = F2ScopeResolver(
        lookup=lambda school, kind, key, now: seen.append((kind, key)) or key != "existing"
    )
    assert resolver.resolve(request) is None
    assert ("person", "finite-new") not in seen
    resolved = F2ScopeResolver(lookup=lambda *args: True).resolve(request)
    assert resolved is not None and resolved.reference == scope.reference


def test_scope_resolver_batches_exact_existing_keys_without_creation_keys():
    school = uuid4()
    scope = F2FactScope(school, "person", ("existing-a", "existing-b"), ("new-only",))
    request = ScopeResolutionRequest(
        school,
        uuid4(),
        "school_facts.f2",
        "exact_fact_set",
        1,
        scope.document,
        scope.document_sha256,
        datetime.now(UTC),
    )

    calls = []

    def batch(value, now):
        calls.append((value, now))
        return True

    resolver = F2ScopeResolver(
        lookup=lambda *args: pytest.fail("per-key fallback invoked"), lookup_scope=batch
    )
    resolved = resolver.resolve(request)
    assert resolved is not None and resolved.reference == scope.reference
    assert calls == [(scope, request.as_of)]
    assert (
        F2ScopeResolver(lookup=lambda *args: True, lookup_scope=lambda *args: False).resolve(
            request
        )
        is None
    )


def test_scope_set_batches_valid_exact_requests_and_preserves_each_rejection():
    from dataclasses import replace

    school = uuid4()
    scopes = (F2FactScope(school, "person", ("one",)), F2FactScope(school, "location", ("two",)))
    at = datetime.now(UTC)
    requests = tuple(
        ScopeResolutionRequest(
            school,
            uuid4(),
            "school_facts.f2",
            "exact_fact_set",
            1,
            scope.document,
            scope.document_sha256,
            at,
        )
        for scope in scopes
    )
    calls = []
    resolver = F2ScopeResolver(
        lookup=lambda *args: pytest.fail("single lookup used"),
        lookup_scope_many=lambda values, times: calls.append((values, times)) or (True, False),
    )
    invalid = replace(requests[0], reference_sha256="bad")
    result = resolver.resolve_many((requests[0], invalid, requests[1]))
    assert result[0] is not None and result[0].reference == scopes[0].reference
    assert result[1:] == (None, None)
    assert calls == [(scopes, (at, at))]


@pytest.mark.parametrize(
    "family,kind",
    [
        (2, "person"),
        (3, "person"),
        (4, "administrative_class"),
        (5, "teaching_assignment"),
        (6, "dorm_room"),
    ],
)
def test_explicit_school_coverage_preserves_exact_contract_and_field_limits(family, kind):
    import hashlib
    import importlib
    import json

    module = importlib.import_module(f"app.platform.school_facts.f{family}_policy")
    cls = getattr(module, f"F{family}FactScope")
    if kind not in getattr(module, f"F{family}_FACT_KINDS"):
        kind = sorted(getattr(module, f"F{family}_FACT_KINDS"))[0]
    school = uuid4()
    fields = (
        {}
        if family == 2
        else {"field_names": (sorted(getattr(module, f"F{family}_FACT_FIELDS")[kind])[0],)}
    )
    exact = cls(school, kind, ("old-key",), **fields)
    old_doc = {
        "school_id": str(school),
        "fact_kind": kind,
        "object_keys": ["old-key"],
        "creation_keys": [],
    }
    if fields:
        old_doc["field_names"] = list(fields["field_names"])
    expected = json.dumps(old_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert exact.reference == expected
    assert exact.document_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    with pytest.raises(ValueError):
        cls(school, kind, **fields)
    scope = cls(school, kind, coverage="school", **fields)
    assert scope.is_school_wide
    assert scope.allows_key("future:record")
    assert scope.allows_key("future:record", is_creation=True)
    assert not scope.allows_key("*")
    assert not scope.allows_key("")
    assert cls.from_document(scope.document) == scope
    assert scope.document["coverage"] == "school"
    assert scope.document["object_keys"] == []
    assert not exact.allows_key("future:record")
    for changes in (
        {"coverage": "exact"},
        {"coverage": "all"},
        {"coverage": "school", "object_keys": ["mixed"]},
    ):
        with pytest.raises(ValueError):
            cls.from_document(scope.document | changes)
    if fields:
        with pytest.raises(ValueError):
            cls(school, kind, coverage="school", field_names=("invented_field",))
    if family == 6:
        with pytest.raises(ValueError):
            cls(school, kind, coverage="school", reserved_read_keys=("reserved",), **fields)


def test_school_coverage_resolver_still_checks_school_and_school_creation_is_forbidden():
    from dataclasses import replace

    school = uuid4()
    scope = F2FactScope(school, "school", coverage="school")
    assert not scope.allows_key("new-school", is_creation=True)
    request = ScopeResolutionRequest(
        school,
        uuid4(),
        "school_facts.f2",
        "exact_fact_set",
        1,
        scope.document,
        scope.document_sha256,
        datetime.now(UTC),
    )
    resolver = F2ScopeResolver(lookup=lambda sid, *args: sid == school)
    assert resolver.resolve(request) is not None
    assert resolver.resolve(replace(request, school_id=uuid4())) is None
    assert F2ScopeResolver(lookup=lambda *args: False).resolve(request) is None
