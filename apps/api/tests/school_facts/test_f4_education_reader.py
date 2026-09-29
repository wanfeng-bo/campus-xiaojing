from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.platform import contracts as contexts
from app.platform.school_facts.f4_policy import F4FactScope

NOW = datetime(2027, 8, 31, 15, 59, tzinfo=UTC)


def module():
    name = "app.composition.school_fact_education_reader"
    assert find_spec(name) is not None, "Education Reader is missing"
    return import_module(name)


def actor(assignment=3):
    return contexts._issue_staff_actor_context(
        account_user_id=1,
        school_id=contexts.SchoolId(UUID(int=1)),
        person_id=contexts.PersonId(UUID(int=2)),
        assignment_id=contexts.AssignmentId(UUID(int=assignment)),
        assignment_version=1,
        authorization_version=2,
    )


class Authority:
    def __init__(self):
        self.allowed = True
        self.checks = []

    def require_scope(self, actor, *, fact_kind, action, checked_at):
        if not self.allowed:
            raise PermissionError("revoked")
        self.checks.append((fact_kind, action))
        return SimpleNamespace(
            actor=actor,
            action=action,
            scope=F4FactScope(
                actor.school_id,
                fact_kind,
                ("f4:class:a",),
                ("f4:class:new",),
                ("display_name", "class_source_key")
                if fact_kind == "class_placement"
                else ("display_name",),
            ),
        )

    def require_fields(self, permit, fields):
        if not set(fields) <= set(permit.scope.field_names):
            raise PermissionError("fields")

    def require_object(self, permit, source_key, *, is_creation):
        if source_key not in (
            permit.scope.creation_keys if is_creation else permit.scope.object_keys
        ):
            raise PermissionError("object")

    def revalidate(self, permit, *, actor, checked_at):
        if not self.allowed or permit.actor != actor:
            raise PermissionError("revoked")


class Projection:
    def __init__(self):
        self.version = 10
        self.calls = []
        self.records = []
        self.after_fetch = lambda: None

    def current_version(self, school_id):
        return self.version

    def read(self, *, before_read, after_read, source_keys, field_names, **kwargs):
        before_read()
        self.calls.append({**kwargs, "source_keys": source_keys(), "field_names": field_names()})
        raw = {"status": "available", "school_fact_version": self.version, "records": self.records}
        self.after_fetch()
        after_read(raw)
        return raw


def repository():
    m, projection, authority = module(), Projection(), Authority()
    repository = m.EducationRepository(
        projection=projection,
        authority=authority,
        reference_key=b"private-key-for-f4-references",
        clock=lambda: NOW,
    )
    return repository, projection, authority


def test_school_education_authority_checks_future_key_and_action():
    from app.composition.school_fact_education_authority import EducationAuthority

    scope = F4FactScope(
        actor().school_id,
        "education_class",
        field_names=("display_name",),
        coverage="school",
    )
    permit = SimpleNamespace(scope=scope, action="maintain")
    EducationAuthority.require_object(permit, "class:created-later", is_creation=True)
    permit.action = "read"
    with pytest.raises(PermissionError):
        EducationAuthority.require_object(permit, "class:created-later", is_creation=True)


def test_school_education_reference_roundtrip_without_object_catalog():
    repo, _, authority = repository()
    scope = F4FactScope(
        actor().school_id,
        "education_class",
        field_names=("display_name",),
        coverage="school",
    )
    authority.require_scope = lambda a, *, fact_kind, action, checked_at: SimpleNamespace(
        actor=a, action=action, scope=scope
    )
    authority.require_object = lambda permit, source_key, *, is_creation: (
        None if permit.scope.allows_key(source_key, is_creation=is_creation)
        else (_ for _ in ()).throw(PermissionError("scope"))
    )
    ref = repo.reference_for(actor(), "education_class", "class:later")
    assert ref.startswith("schoolv1:f4:education_class:o:")
    assert repo.source_for_reference(actor(), "education_class", ref) == "class:later"
    with pytest.raises(ValueError):
        repo.source_for_reference(actor(assignment=9), "education_class", ref)


def test_school_education_read_scans_all_metadata_pages_before_empty_result():
    repo, projection, authority = repository()
    scope = F4FactScope(
        actor().school_id,
        "education_class",
        field_names=("display_name",),
        coverage="school",
    )
    authority.require_scope = lambda a, *, fact_kind, action, checked_at: SimpleNamespace(
        actor=a, action=action, scope=scope
    )
    authority.require_object = lambda permit, key, *, is_creation: (
        None if permit.scope.allows_key(key, is_creation=is_creation)
        else (_ for _ in ()).throw(PermissionError("scope"))
    )
    keys = tuple(f"class:{index:03d}" for index in range(105))
    pages = []

    def scope_keys(*, school_id, fact_kind, after_key, limit):
        offset = keys.index(after_key) + 1 if after_key else 0
        selected = keys[offset : offset + limit]
        more = offset + len(selected) < len(keys)
        pages.append(after_key)
        return {
            "keys": list(selected),
            "has_more": more,
            "next_after": selected[-1] if more else None,
            "school_fact_version": projection.version,
        }

    projection.scope_keys = scope_keys
    result = repo.read_records(actor(), "education_class", fields=("display_name",))
    assert result.records == () and len(projection.calls) == 2
    assert pages == [None, keys[99]]
    assert tuple(key for call in projection.calls for key in call["source_keys"]) == keys


def test_school_creation_catalog_issues_current_signed_candidate_without_static_keys():
    repo, _, authority = repository()
    scope = F4FactScope(
        actor().school_id, "education_class", field_names=("display_name",), coverage="school"
    )
    authority.require_scope = lambda a, *, fact_kind, action, checked_at: SimpleNamespace(
        actor=a, action=action, scope=scope
    )
    authority.require_object = lambda permit, key, *, is_creation: (
        None if permit.scope.allows_key(key, is_creation=is_creation)
        else (_ for _ in ()).throw(PermissionError("scope"))
    )
    choices = repo.catalog_creation_references(actor(), "education_class", ())
    assert len(choices) == 1
    source, reference = choices[0]
    assert source.startswith("live:education_class:")
    assert reference.startswith("schoolv1:f4:education_class:c:")
    assert repo.source_for_reference(actor(), "education_class", reference, creation=True) == source
    assert repo.creation_receipt_reference_for(
        actor=actor(), fact_kind="education_class", source_key=source
    ).startswith("f4audit:schoolv1:f4:education_class:c:")


def test_dependency_projection_enumerates_policy_objects_under_f2_family():
    from app.composition.school_fact_education_quality import SqlEducationDependencyProjection

    statements = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def scalar(self, statement):
            statements.append(statement)
            return {"keys": [], "has_more": False, "next_after": None, "school_fact_version": 3}

    base = SimpleNamespace(current_sessions=lambda: lambda: Session())
    projection = SqlEducationDependencyProjection(base)
    assert projection.scope_keys(
        school_id=actor().school_id, kind="policy", after_key=None, limit=100
    )["keys"] == []
    function = statements[0].selected_columns[0]
    assert function.name == "school_fact_scope_keys"
    assert tuple(part.value for part in list(function.clauses)[1:3]) == ("f2", "policy")


def batch_repository():
    repo, projection, authority = repository()

    def require_scopes(a, *, requests, checked_at):
        return tuple(
            authority.require_scope(a, fact_kind=k, action=op, checked_at=checked_at)
            for k, op in requests
        )

    authority.require_scopes = require_scopes
    authority.revalidate_many = lambda permits, *, actor, checked_at: [
        authority.revalidate(p, actor=actor, checked_at=checked_at) for p in permits
    ]
    authority.revalidate_read_wave = authority.revalidate_many

    def read_many(*, before_read, after_read, selections, **kwargs):
        before_read()
        selected = selections()
        projection.calls.append({**kwargs, "selections": selected})
        rows = tuple(
            {
                "status": "available",
                "school_fact_version": projection.version,
                "records": projection.records,
            }
            for _ in selected
        )
        projection.after_fetch()
        after_read(rows)
        return rows

    projection.read_many = read_many
    return repo, projection, authority


def batch_selections():
    return tuple(
        module().EducationReadSelection(
            kind=k, fields=("display_name",), source_keys=("f4:class:a",)
        )
        for k in ("education_class", "education_grade")
    )


def test_bounded_read_wave_shares_acquisition_but_retains_each_empty_dependency():
    repo, projection, authority = batch_repository()
    results = repo.read_many(actor(), batch_selections())
    assert len(projection.calls) == 1 and len(results) == 2
    assert authority.checks == [("education_class", "read"), ("education_grade", "read")]
    assert all(r.records == () and r.proof.actor == actor() for r in results)
    projection.version += 1
    for result in results:
        with pytest.raises(module().EducationReadStale):
            result.proof.revalidate()


@pytest.mark.parametrize("fault", ["field", "object", "filter", "kind", "empty", "capacity"])
def test_read_wave_all_preconditions_hold_before_any_projection(fault):
    repo, projection, authority = batch_repository()
    from dataclasses import replace

    rows = batch_selections()
    if fault == "field":
        rows = (rows[0], replace(rows[1], fields=("private_unknown",)))
    if fault == "object":
        rows = (rows[0], replace(rows[1], source_keys=("other",)))
    if fault == "filter":
        rows = (rows[0], replace(rows[1], filter={"class_source_key": "other"}))
    if fault == "kind":
        rows = (rows[0], replace(rows[1], kind="person"))
    if fault == "empty":
        rows = ()
    if fault == "capacity":
        rows = rows * 3
    with pytest.raises((PermissionError, ValueError)):
        repo.read_many(actor(), rows)
    assert projection.calls == []


@pytest.mark.parametrize("fault", ["revoke", "content", "clock"])
def test_read_wave_changes_before_release_reject_the_whole_result(fault):
    repo, projection, authority = batch_repository()
    if fault == "revoke":
        projection.after_fetch = lambda: setattr(authority, "allowed", False)
    if fault == "content":
        projection.after_fetch = lambda: setattr(projection, "version", 11)
    if fault == "clock":
        projection.records = [
            {
                "source_key": "f4:class:a",
                "fact_kind": "education_class",
                "segments": [],
                "next_valid_boundary": (NOW + timedelta(seconds=1)).isoformat(),
            }
        ]
        projection.after_fetch = lambda: setattr(repo, "clock", lambda: NOW + timedelta(seconds=2))
    selections = batch_selections()[:1] if fault == "clock" else batch_selections()
    with pytest.raises((PermissionError, module().EducationReadStale)):
        repo.read_many(actor(), selections)
    assert len(projection.calls) == 1


def test_read_wave_preserves_field_projection_and_actor_bound_proofs():
    repo, projection, _ = batch_repository()
    projection.records = [
        {
            "source_key": "f4:class:a",
            "fact_kind": "education_class",
            "segments": [{"content": {"display_name": "正式名称"}}],
        }
    ]
    (result,) = repo.read_many(actor(), batch_selections()[:1])
    assert result.references and result.records[0]["segments"][0]["content"] == {
        "display_name": "正式名称"
    }
    with pytest.raises(PermissionError):
        result.proof.revalidate(actor=actor(4))
    projection.records[0]["segments"][0]["content"]["private_unknown"] = "not permitted"
    with pytest.raises(PermissionError):
        repo.read_many(actor(), batch_selections()[:1])


def test_final_identity_io_cannot_cross_education_boundary_before_release():
    from app.composition.school_fact_product_identity import (
        ProductIdentity,
        ProductIdentityEvidence,
    )
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    repo, projection, _ = repository()
    now = [NOW]
    repo.clock = lambda: now[0]
    deadline = NOW + timedelta(seconds=1)
    proof = module().EducationReadProof(repo, actor(), (), 10, deadline)
    identity = ProductIdentity(actor(), "synthetic-session", "synthetic-reference")
    reads = []

    def version(_school):
        reads.append("content")
        return 10

    def final_identity():
        reads.append("identity")
        now[0] = deadline
        return identity

    projection.current_version = version
    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["identity"] = ProductIdentityEvidence(identity, final_identity)
            proof.revalidate()
    assert reads == ["content", "content", "identity"]


def test_denied_fields_or_objects_never_reach_sql_and_filter_fields_need_authority():
    repo, projection, _ = repository()
    for arguments in (
        {"fields": ("business_code",)},
        {"source_keys": ("other:class",)},
        {"filter": {"class_source_key": "f4:class:a"}},
    ):
        with pytest.raises(PermissionError):
            repo.read_records(actor(), "education_class", **arguments)
    assert projection.calls == []


def test_empty_result_has_version_dependency_and_revocation_blocks_reuse():
    repo, projection, authority = repository()
    result = repo.read_records(actor(), "education_class", fields=("display_name",))
    assert result.records == ()
    projection.version = 11
    with pytest.raises(module().EducationReadStale):
        result.proof.revalidate(actor=actor())
    projection.version = 10
    authority.allowed = False
    with pytest.raises(PermissionError):
        result.proof.revalidate(actor=actor())


def test_post_read_change_rejects_material_before_release():
    repo, projection, _ = repository()
    projection.after_fetch = lambda: setattr(projection, "version", 11)
    with pytest.raises(module().EducationReadStale):
        repo.read_records(actor(), "education_class", fields=("display_name",))


def test_references_are_actor_bound_and_resolved_only_from_current_scope():
    repo, _, authority = repository()
    reference = repo.reference_for(actor(), "education_class", "f4:class:a")
    assert "f4:class:a" not in reference
    assert repo.source_for_reference(actor(), "education_class", reference) == "f4:class:a"
    with pytest.raises(PermissionError):
        repo.source_for_reference(actor(4), "education_class", reference)
    authority.allowed = False
    with pytest.raises(PermissionError):
        repo.source_for_reference(actor(), "education_class", reference)


def test_maintenance_never_returns_existing_content_and_creation_must_be_missing():
    repo, projection, _ = repository()
    with pytest.raises(PermissionError):
        repo.read_records(actor(), "education_class", action="maintain")
    projection.records = [{"source_key": "f4:class:new", "version": 1, "status": "available"}]
    with pytest.raises(PermissionError):
        repo.read_records(
            actor(),
            "education_class",
            source_keys=("f4:class:new",),
            action="maintain",
            creation=True,
        )
    assert projection.calls[-1]["metadata_only"] is True


def test_current_time_boundary_expires_but_fixed_history_stays_at_its_fact_time():
    repo, projection, _ = repository()
    boundary = NOW + timedelta(seconds=30)
    projection.records = [
        {
            "fact_kind": "education_class",
            "source_key": "f4:class:a",
            "version": 1,
            "status": "available",
            "segments": [],
            "next_valid_boundary": boundary.isoformat(),
        }
    ]
    current = repo.read_records(actor(), "education_class")
    history = repo.read_records(actor(), "education_class", as_of=NOW)
    repo.clock = lambda: boundary
    with pytest.raises(module().EducationReadStale):
        current.proof.revalidate(actor=actor())
    history.proof.revalidate(actor=actor())


def test_causal_prior_cannot_smuggle_unrequested_fields():
    repo, projection, _ = repository()
    projection.records = [
        {
            "source_key": "f4:class:a",
            "fact_kind": "education_class",
            "version": 2,
            "segments": [
                {
                    "content": {"display_name": "班级"},
                    "causal_prior": {"content": {"business_code": "private"}},
                }
            ],
        }
    ]
    with pytest.raises(PermissionError):
        repo.read_records(actor(), "education_class", fields=("display_name",))


@pytest.mark.parametrize("change", ["version", "time"])
def test_release_exit_rechecks_education_content_after_other_callbacks(change):
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError

    state = {"version": 1, "time": NOW}
    repo = SimpleNamespace(
        authority=SimpleNamespace(revalidate_many=lambda *a, **k: None),
        projection=SimpleNamespace(current_version=lambda _: state["version"]),
        clock=lambda: state["time"],
    )
    proof = module().EducationReadProof(repo, actor(), (), 1, NOW + timedelta(seconds=1))
    with pytest.raises(AssistantRuntimeError):
        with validation_batch():
            proof.revalidate(actor=actor())
            if change == "version":
                state["version"] = 2
            else:
                state["time"] = NOW + timedelta(seconds=1)


def test_read_references_reuse_only_validated_rows_without_issuing_another_grant():
    repo, projection, authority = repository()
    projection.records = [
        {
            "fact_kind": "education_class",
            "source_key": "f4:class:a",
            "version": 1,
            "status": "available",
            "segments": [],
        }
    ]
    expected = repo.reference_for(actor(), "education_class", "f4:class:a")
    authority.checks.clear()
    result = repo.read_records(actor(), "education_class", fields=("display_name",))
    assert result.references == (("f4:class:a", expected),)
    assert authority.checks == [("education_class", "read")]
    assert "references" not in result.public_document()
    authority.allowed = False
    with pytest.raises(PermissionError):
        result.proof.revalidate(actor=actor())
    with pytest.raises(PermissionError):
        result.proof.revalidate(actor=actor(4))


def test_empty_read_does_not_issue_references_for_unreturned_scope_objects():
    repo, _, _ = repository()
    result = repo.read_records(actor(), "education_class", fields=("display_name",))
    assert result.references == ()


def test_creation_catalog_uses_one_current_grant_and_keeps_exact_creation_scope():
    repo, projection, authority = repository()
    refs = repo.catalog_creation_references(
        actor(), "education_class", ("f4:class:new", "f4:class:a", "other:class")
    )
    assert tuple(key for key, ref in refs) == ("f4:class:new",)
    assert refs[0][1].startswith("f4create:education_class:")
    assert authority.checks == [("education_class", "maintain")]
    assert projection.calls == []
    authority.allowed = False
    assert repo.catalog_creation_references(actor(), "education_class", ("f4:class:new",)) == ()


def test_creation_catalog_rechecks_revocation_before_returning_coordinates():
    repo, _, authority = repository()
    original = authority.require_object

    def revoke_after_object(permit, source_key, *, is_creation):
        original(permit, source_key, is_creation=is_creation)
        authority.allowed = False

    authority.require_object = revoke_after_object
    with pytest.raises(PermissionError):
        repo.catalog_creation_references(actor(), "education_class", ("f4:class:new",))


def _sql_wave_harness(rows, *, fetch_error=None):
    from contextlib import contextmanager

    events, relations = [], []

    class Executor:
        @contextmanager
        def validated_read(self, before_read, after_read):
            events.append("connection_acquired")
            try:
                before_read()
                events.append("transaction_entered")
                yield self
                events.append("transaction_exited")
                after_read()
            finally:
                events.append("connection_released")

        def fetch(self, relation):
            events.append("fetch")
            relations.append(relation)
            if fetch_error is not None:
                raise fetch_error
            return rows

    projection = module().SqlEducationProjection(Executor(), current_sessions=lambda: None)
    return projection, events, relations


def _sql_wave_selections():
    return (
        (
            "class_placement",
            ("class-placement:甲", "class-placement:乙"),
            ("class_source_key", "grade_source_key"),
            {"class_source_key": ["class:甲", "class:乙"]},
        ),
        (
            "teaching_assignment",
            ("assignment:数学",),
            ("offering_source_key", "assignment_source_key"),
            {"offering_source_key": "offering:数学"},
        ),
    )


def test_sql_read_wave_compiles_one_bounded_union_with_each_exact_projection():
    from sqlalchemy.dialects import postgresql

    first, second = (
        {"school_fact_version": 39, "records": []},
        {
            "school_fact_version": 39,
            "records": [{"source_key": "assignment:数学"}],
        },
    )
    projection, events, relations = _sql_wave_harness(
        [{"ordinal": 1, "payload": second}, {"ordinal": 0, "payload": first}]
    )
    checked = []

    def selections():
        events.append("selections")
        return _sql_wave_selections()

    result = projection.read_many(
        school_id=UUID(int=99),
        selections=selections,
        as_of=NOW,
        before_read=lambda: events.append("authorized"),
        after_read=lambda raw: (events.append("revalidated"), checked.append(raw)),
    )
    assert result == (first, second) and checked == [result]
    assert events == [
        "connection_acquired",
        "authorized",
        "transaction_entered",
        "selections",
        "fetch",
        "transaction_exited",
        "revalidated",
        "connection_released",
    ]
    assert len(relations) == 1
    relation = relations[0]
    assert relation.operation_code == "school_facts.education.fixed_wave"
    assert relation.as_of == NOW and relation.page_size == 2
    assert relation.projected_fields == ("ordinal", "payload")
    statement = relation.data_statement
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).count("UNION ALL") == 1
    for index, (part, expected) in enumerate(
        zip(statement.selects, _sql_wave_selections(), strict=True)
    ):
        kind, keys, fields, filters = expected
        assert part.selected_columns["ordinal"].element.value == index
        function = part.selected_columns["payload"].element
        assert function.name == (
            "school_fact_f4_targets" if index == 0 else "school_fact_f5_targets"
        )
        arguments = list(function.clauses)
        assert arguments[0].value == UUID(int=99)
        assert arguments[1].value == kind
        assert tuple(item.value for item in arguments[2].clauses) == keys
        assert tuple(item.value for item in arguments[3].clauses) == fields
        assert arguments[4].value == NOW and arguments[4].type.timezone is True
        assert arguments[5].value is None  # No historical audit or cancellation read.
        assert arguments[6].value == filters
        assert all(key not in str(compiled) for key in keys)


def test_sql_read_wave_before_denial_never_resolves_or_fetches_facts():
    projection, events, relations = _sql_wave_harness([])

    def deny():
        events.append("denied_before")
        raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        projection.read_many(
            school_id=UUID(int=1),
            as_of=NOW,
            selections=lambda: events.append("selections"),
            before_read=deny,
            after_read=lambda _: events.append("after"),
        )
    assert relations == []
    assert events == ["connection_acquired", "denied_before", "connection_released"]


def test_sql_read_wave_after_denial_releases_connection_without_returning_result():
    projection, events, relations = _sql_wave_harness(
        [{"ordinal": 0, "payload": {"school_fact_version": 39, "records": []}}]
    )

    def deny(_raw):
        events.append("denied_after")
        raise PermissionError("revoked during fetch")

    with pytest.raises(PermissionError, match="revoked during fetch"):
        projection.read_many(
            school_id=UUID(int=1),
            as_of=NOW,
            selections=lambda: _sql_wave_selections()[:1],
            before_read=lambda: events.append("authorized"),
            after_read=deny,
        )
    assert len(relations) == 1
    assert events[-3:] == ["transaction_exited", "denied_after", "connection_released"]


@pytest.mark.parametrize(
    "rows",
    [
        [{"ordinal": 0, "payload": {}}, {"ordinal": 0, "payload": {}}],
        [{"ordinal": 0, "payload": {}}],
        [
            {"ordinal": 0, "payload": {}},
            {"ordinal": 1, "payload": {}},
            {"ordinal": 2, "payload": {}},
        ],
        [{"ordinal": 0, "payload": {}}, {"ordinal": 2, "payload": {}}],
        [{"ordinal": False, "payload": {}}, {"ordinal": 1, "payload": {}}],
        [{"ordinal": 0, "payload": {}}, {"ordinal": 1, "payload": None}],
    ],
)
def test_sql_read_wave_rejects_malformed_ordinals_or_payloads_before_release(rows):
    projection, events, _ = _sql_wave_harness(rows)
    with pytest.raises(ValueError, match="Malformed education read wave"):
        projection.read_many(
            school_id=UUID(int=1),
            as_of=NOW,
            selections=_sql_wave_selections,
            before_read=lambda: events.append("authorized"),
            after_read=lambda _: events.append("after"),
        )
    assert "after" not in events
    assert events[-1] == "connection_released"


def test_sql_read_wave_fetch_error_exits_validation_context_without_post_success():
    failure = RuntimeError("connection interrupted")
    projection, events, relations = _sql_wave_harness([], fetch_error=failure)
    with pytest.raises(RuntimeError) as raised:
        projection.read_many(
            school_id=UUID(int=1),
            as_of=NOW,
            selections=_sql_wave_selections,
            before_read=lambda: events.append("authorized"),
            after_read=lambda _: events.append("after"),
        )
    assert raised.value is failure and len(relations) == 1
    assert events == [
        "connection_acquired",
        "authorized",
        "transaction_entered",
        "fetch",
        "connection_released",
    ]


@pytest.mark.parametrize("choice", ["empty", "over_capacity", "unknown_kind"])
def test_sql_read_wave_rejects_unbounded_or_unknown_function_before_fetch(choice):
    selections = _sql_wave_selections()
    if choice == "empty":
        selections = ()
    elif choice == "over_capacity":
        selections = selections * 3
    else:
        selections = (("arbitrary_function", *selections[0][1:]),)
    projection, events, relations = _sql_wave_harness([])
    with pytest.raises(ValueError):
        projection.read_many(
            school_id=UUID(int=1),
            as_of=NOW,
            selections=lambda: selections,
            before_read=lambda: events.append("authorized"),
            after_read=lambda _: events.append("after"),
        )
    assert relations == [] and "after" not in events
    assert events[-1] == "connection_released"
