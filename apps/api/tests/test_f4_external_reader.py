from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.composition.school_fact_education_external import (
    EducationExternalReader,
    EducationRelatedReadIncomplete,
)
from app.composition.school_fact_education_reader import EducationReadStale


class Evidence:
    valid = True

    def check(self, actor):
        return self.valid

    @contextmanager
    def capture(self, **kwargs):
        yield SimpleNamespace(seal=lambda: self)


class Query:
    def __init__(self):
        self.calls = []

    def reference_for(self, *, actor, fact_kind, source_key, creation=False):
        return f"f2:{fact_kind}:{source_key}"

    def source_for(self, *, actor, fact_kind, reference, creation=False):
        return reference.split(":", 2)[2]

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="known",
            has_more=False,
            records=(
                SimpleNamespace(
                    content={"display_name": "合成陈老师"},
                    fact_ref=kwargs["reference"],
                    publication_ref="publication:synthetic",
                    valid_to=None,
                ),
            ),
        )


def test_official_contacts_keep_each_authorized_channel_publication_without_more_reads():
    from datetime import UTC, datetime

    class Contacts(Query):
        def query(self, **kwargs):
            self.calls.append(kwargs)
            kind = kwargs["fact_kind"]
            if kind == "official_contact":
                return SimpleNamespace(
                    status="known",
                    has_more=False,
                    records=tuple(
                        SimpleNamespace(
                            content={
                                "contact_kind": "email",
                                "contact_value": value,
                                "service_hours": None,
                            },
                            source_kind="manual_entry",
                            publication_ref="private-publication-reference",
                            published_at=datetime(2026, 8, day, tzinfo=UTC),
                            valid_from=datetime(2026, 9, day, tzinfo=UTC),
                            valid_to=None,
                        )
                        for day, value in ((1, "a@example.invalid"), (2, "b@example.invalid"))
                    ),
                )
            content = (
                {"organization_ref": "f2:organization:org"}
                if kind == "assignment"
                else {"display_name": "教务处"}
            )
            return SimpleNamespace(
                status="known", has_more=False, records=(SimpleNamespace(content=content),)
            )

    query, evidence = Contacts(), Evidence()
    reader = EducationExternalReader(directory=query, evidence=evidence)
    result = reader.official_contacts(object(), assignment_keys=("teacher",), as_of=None)
    assert len(query.calls) == 3
    for day, row in enumerate(result["records"], 1):
        item = row["publication_evidence"][0]
        assert item["applies_to"] == "官方联系渠道"
        assert item["basis"]["published_at"] == f"2026-08-0{day}T00:00:00+00:00"
        assert item["basis"]["recorded_at"] is None
        assert item["effective_at"] == f"2026-09-0{day}T00:00:00+00:00"
        assert "private-publication-reference" not in str(row)
    evidence.valid = False
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()


def test_related_name_uses_exact_object_and_minimal_database_projection():
    query, evidence, actor = Query(), Evidence(), object()
    reader = EducationExternalReader(directory=query, evidence=evidence)
    result = reader.read_records(
        actor, "person", source_keys=("teacher",), fields=("display_name",), as_of=None
    )
    assert result["records"] == [
        {"source_key": "teacher", "content": {"display_name": "合成陈老师"}}
    ]
    assert query.calls[0]["content_fields"] == ("display_name",)
    assert query.calls[0]["reference"] == "f2:person:teacher"
    result["proof"].revalidate()
    evidence.valid = False
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()


def test_external_reader_never_accepts_private_contact_or_unbounded_objects():
    reader = EducationExternalReader(directory=Query(), evidence=Evidence())
    with pytest.raises(ValueError):
        reader.read_records(
            object(),
            "person_contact",
            source_keys=("teacher",),
            fields=("contact_value",),
            as_of=None,
        )
    with pytest.raises(ValueError):
        reader.read_records(
            object(), "person", source_keys=(), fields=("display_name",), as_of=None
        )


def test_reader_does_not_treat_denial_as_no_record():
    query = Query()
    query.query = lambda **kwargs: SimpleNamespace(status="forbidden", records=(), has_more=False)
    reader = EducationExternalReader(directory=query, evidence=Evidence())
    with pytest.raises(PermissionError):
        reader.read_records(
            object(), "person", source_keys=("teacher",), fields=("display_name",), as_of=None
        )


def test_grade_arrangement_uses_exact_ssr_fields_and_fresh_release_proof():
    from datetime import UTC, datetime

    now = datetime(2027, 9, 2, tzinfo=UTC)
    calls = []
    allowed = {"student_source_key", "enrollment_status", "valid_to"}

    def before_scope(*args):
        if not allowed:
            raise PermissionError("revoked")
        return SimpleNamespace(scope=SimpleNamespace(field_names=allowed), target="ssr:a")

    def projection(**kwargs):
        kwargs["before_read"]()
        assert set(kwargs["field_names"]()) == {
            "student_source_key",
            "enrollment_status",
            "valid_to",
        }
        calls.append(kwargs["source_key"])
        kwargs["after_read"]()
        return {
            "fact_kind": "student_school_relationship",
            "source_key": "ssr:a",
            "version": 2,
            "school_fact_version": 10,
            "segments": [
                {
                    "effective_at": "2027-01-01T00:00:00Z",
                    "valid_to": "2027-09-01T00:00:00Z",
                    "content": {
                        "student_source_key": "student:a",
                        "enrollment_status": "suspended",
                        "valid_to": "2027-09-01T00:00:00Z",
                    },
                },
                {
                    "effective_at": "2027-09-01T00:00:00Z",
                    "valid_to": None,
                    "content": {
                        "student_source_key": "student:a",
                        "enrollment_status": "enrolled",
                        "valid_to": None,
                    },
                },
            ],
        }

    repo = SimpleNamespace(
        _clock=lambda: now,
        evidence=SimpleNamespace(version=lambda actor: 10),
        _authorize_scope=before_scope,
        _revalidate=lambda *args: None,
        _projection=SimpleNamespace(education_timeline=projection),
    )
    reader = EducationExternalReader(
        directory=Query(), evidence=Evidence(), family=SimpleNamespace(repository=repo)
    )
    result = reader.grade_arrangement(
        SimpleNamespace(school_id="school"),
        student_source_key="student:a",
        relationship_keys=("ssr:a",),
    )
    assert (
        result["status"] == "enrolled"
        and result["last_suspended_until"] == "2027-09-01T00:00:00+00:00"
    )
    result["proof"].revalidate()
    assert calls == ["ssr:a", "ssr:a"]
    allowed.clear()
    with pytest.raises(PermissionError):
        result["proof"].revalidate()


@pytest.mark.parametrize(
    "difference", [None, "actor", "student", "time", "source", "version", "natural_boundary"]
)
def test_separate_grade_history_proofs_share_only_identical_release_reads(difference):
    from dataclasses import asdict
    from datetime import timedelta

    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform import contracts as actors
    from tests.test_f2_read_evidence import NOW, actor

    calls, version, allowed, state, now = [], [10], [True], ["enrolled"], [NOW]

    def authorize(selected, action, kind, source, creation, checked_at):
        calls.append("authorize")
        if not allowed[0]:
            raise PermissionError("revoked")
        return SimpleNamespace(
            scope=SimpleNamespace(
                field_names=("student_source_key", "enrollment_status", "valid_to")
            ),
            target=source,
        )

    def projection(**kwargs):
        kwargs["before_read"]()
        calls.append("read")
        kwargs["after_read"]()
        return {
            "fact_kind": "student_school_relationship", "source_key": kwargs["source_key"],
            "version": version[0], "school_fact_version": 10,
            "segments": [{
                "effective_at": (NOW - timedelta(days=1)).isoformat(),
                "valid_to": (NOW + timedelta(seconds=1)).isoformat(),
                "content": {
                    "student_source_key": "student:a",
                    "enrollment_status": state[0], "valid_to": None,
                },
            }],
        }

    repo = SimpleNamespace(
        _clock=lambda: now[0], evidence=SimpleNamespace(version=lambda _: 10),
        _authorize_scope=authorize, _revalidate=lambda *args: calls.append("revalidate"),
        _projection=SimpleNamespace(education_timeline=projection),
    )
    reader = EducationExternalReader(
        directory=Query(), evidence=Evidence(), family=SimpleNamespace(repository=repo)
    )
    selected = actor()
    first = reader.grade_arrangement(
        selected, student_source_key="student:a", relationship_keys=("ssr:a",)
    )
    if difference == "version":
        version[0] += 1
    other = actors._issue_staff_actor_context(
        **(asdict(selected) | {"authorization_version": selected.authorization_version + 1})
    ) if difference == "actor" else selected
    # An empty relationship selection exercises another student without inventing a row.
    if difference == "student":
        repo.query = lambda **kwargs: {"status": "no_current_records", "records": []}
        repo.reference_for = lambda **kwargs: "ref"
    second = reader.grade_arrangement(
        other, student_source_key="student:b" if difference == "student" else "student:a",
        relationship_keys=() if difference == "student" else (
            "ssr:b" if difference == "source" else "ssr:a",
        ),
        as_of=NOW if difference == "time" else None,
    )
    calls.clear()
    if difference in {"version", "natural_boundary"}:
        with pytest.raises(EducationReadStale):
            with validation_batch():
                first["proof"].revalidate()
                second["proof"].revalidate()
                if difference == "natural_boundary":
                    now[0] += timedelta(seconds=1)
        return
    with validation_batch():
        first["proof"].revalidate()
        second["proof"].revalidate()
    expected = 1 if difference in {None, "student"} else 2
    assert calls.count("read") == expected
    assert calls.count("authorize") == expected == calls.count("revalidate")
    # Neither a later send nor a withdrawal can reuse the completed batch.
    calls.clear()
    first["proof"].revalidate()
    assert calls.count("read") == 1
    allowed[0] = False
    with pytest.raises(PermissionError):
        first["proof"].revalidate()


@pytest.mark.parametrize("include_boundaries", [None, True, False])
def test_fixed_history_future_boundary_metadata_is_optional_and_defaults_on(include_boundaries):
    from datetime import UTC, datetime, timedelta

    at = datetime(2027, 9, 2, tzinfo=UTC)
    calls = []
    boundary_proof = object()

    def read_boundary(*args):
        calls.append(args)
        return {"next_valid_boundary": at + timedelta(days=1), "proof": boundary_proof}

    query = Query()
    reader = EducationExternalReader(
        directory=query, evidence=Evidence(), boundary_reader=SimpleNamespace(read=read_boundary)
    )
    options = {} if include_boundaries is None else {"include_boundaries": include_boundaries}
    result = reader.read_records(
        object(),
        "person",
        source_keys=("teacher",),
        fields=("display_name",),
        as_of=at,
        **options,
    )
    assert query.calls[0]["factual_at"] == at
    assert query.calls[0]["content_fields"] == ("display_name",)
    assert result["records"][0]["content"] == {"display_name": "合成陈老师"}
    if include_boundaries is False:
        assert not calls
        assert set(result) == {"records", "proof"}
    else:
        assert len(calls) == 1
        assert result["boundary_proof"] is boundary_proof
        assert result["next_valid_boundary"] == at + timedelta(days=1)
    result["proof"].revalidate()


@pytest.mark.parametrize(
    "change", ["field_revision", "authority", "empty_membership", "school_version"]
)
def test_fixed_history_without_future_boundaries_keeps_real_read_evidence(change):
    from datetime import UTC, datetime, timedelta

    from app.composition.school_fact_read_capture import F2EvidenceBackend, _capture
    from tests.test_f2_directory_product import actor

    now = datetime(2027, 9, 2, tzinfo=UTC)
    at = now - timedelta(days=5)
    state = SimpleNamespace(
        now=now,
        revision=7,
        row_revision=3,
        authority="read-field",
        empty=change == "empty_membership",
    )

    class Backend:
        capture = F2EvidenceBackend.capture

        def clock(self):
            return state.now

        def version(self, selected):
            assert selected == actor()
            return state.revision

        def decision_key(self, request):
            return request

        def decision(self, request):
            assert request == "person.display_name"
            return state.authority

        def metadata_key(self, probe):
            return probe.key

        def metadata(self, probe):
            assert probe.current_time is False and probe.arguments["as_of"] == at
            return (0, ()) if state.empty else (1, (("teacher", state.row_revision),))

    backend = Backend()

    class HistoricalQuery(Query):
        def query(self, **kwargs):
            assert kwargs["factual_at"] == at and kwargs["content_fields"] == ("display_name",)
            captured = _capture.get()
            assert captured.current_time is False
            captured.record_decision("person.display_name", state.authority)
            relation = object()
            captured.relations[id(relation)] = (relation, {"as_of": at}, False)
            total, metadata = backend.metadata(
                SimpleNamespace(current_time=False, arguments={"as_of": at})
            )
            captured.record_page(relation, total, metadata, ())
            result = super().query(**kwargs)
            if state.empty:
                result.records = ()
                result.status = "no_current_records"
            return result

    def unused_boundary(*args):
        pytest.fail("A fixed historical field read must not need future boundary metadata")

    reader = EducationExternalReader(
        directory=HistoricalQuery(),
        evidence=backend,
        boundary_reader=SimpleNamespace(read=unused_boundary),
    )

    def read():
        return reader.read_records(
            actor(),
            "person",
            source_keys=("teacher",),
            fields=("display_name",),
            as_of=at,
            include_boundaries=False,
        )

    if state.empty:
        with pytest.raises(EducationRelatedReadIncomplete) as error:
            read()
        assert error.value.status == "not_recorded"
        assert error.value.kind == "person"
        assert error.value.fields == ("display_name",)
        return

    result = read()
    assert bool(result["records"]) is not state.empty
    state.now += timedelta(days=10)
    result["proof"].revalidate()
    if change == "field_revision":
        state.row_revision += 1
    elif change == "authority":
        state.authority = "denied"
    elif change == "school_version":
        state.revision += 1
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()


def family_name_with_boundary():
    from datetime import timedelta
    from uuid import UUID

    from app.composition.school_fact_education_external import SqlEducationExternalBoundaries
    from app.composition.school_fact_family_settings import FamilyConfiguration
    from app.platform.school_facts.f2_policy import F2FactScope
    from tests.governance.test_f3_family_authority import NOW, actor, dependency_components
    from tests.school_facts.test_f4_education_gateway import related_family_reader

    components, governance, inherited = dependency_components()
    external, name_reads = related_family_reader()
    external.evidence.check = lambda selected: selected == actor()
    configuration = FamilyConfiguration(
        approval_reference="existing-exact-read",
        scope_bindings=(),
        read_dependency_bindings=components.read_dependency_bindings,
        source_catalog={"person": ("person:old-student", "person:unapproved")},
    )
    external.family.configuration = configuration
    external.family.authority = components
    repository = external.family.repository
    repository._configuration = configuration
    repository._authorize_scope = components.require_scope
    repository._revalidate = components.revalidate_permit
    state = SimpleNamespace(now=NOW, version=10, boundary=NOW + timedelta(days=1), after_sql=None)
    repository._clock = lambda: state.now
    sql_calls, directory_calls = [], []

    class DirectoryGuard:
        def require_current(self, selected, kind, **kwargs):
            directory_calls.append((kind, selected.assignment_id))
            return SimpleNamespace(
                scope_plan=SimpleNamespace(
                    reference=F2FactScope(
                        selected.school_id, kind, ("person:teacher", "person:old-student")
                    ).reference
                )
            )

        def revalidate(self, *_):
            pass

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def scalar(self, statement, args):
            sql_calls.append(args)
            if state.after_sql is not None:
                state.after_sql()
            return {"school_fact_version": state.version, "next_valid_boundary": state.boundary}

    external.boundary_reader = SqlEducationExternalBoundaries(
        sessions=Session, guard=DirectoryGuard(), family=external.family
    )
    return (
        external,
        inherited,
        state,
        sql_calls,
        directory_calls,
        name_reads,
        governance,
        actor(),
        UUID(int=82),
    )


def test_f3_name_and_boundary_use_original_owner_and_only_requested_name_field():
    from dataclasses import replace

    external, inherited, state, sql, f2, names, _, selected, _ = family_name_with_boundary()
    inherited.scope = replace(inherited.scope, field_names=("display_name",))
    result = external.read_records(
        selected,
        "person",
        source_keys=("person:old-student",),
        fields=("display_name",),
        as_of=state.now,
    )
    assert len(result["records"]) == 1
    assert set(result["records"][0]["content"]) == {"display_name"}
    assert names == [("person", "person:old-student", ("display_name",))]
    assert not f2
    assert sql[0]["keys"] == ["person:old-student"]
    assert result["next_valid_boundary"] == state.boundary
    result["boundary_proof"].revalidate()
    assert len(sql) == 2


def test_mixed_person_boundary_checks_each_internal_owner_before_same_finite_sql():
    external, _, state, sql, f2, _, governance, selected, dependency_scope = (
        family_name_with_boundary()
    )
    result = external.boundary_reader.read(
        selected, "person", ("person:old-student", "person:teacher"), state.now
    )
    assert sql[0]["keys"] == ["person:old-student", "person:teacher"]
    assert len(f2) == 1 and governance.calls == [dependency_scope]
    result["proof"].revalidate()
    assert len(sql) == 2 and len(f2) == 2


@pytest.mark.parametrize("failure", ("revoked", "expired", "unapproved", "after_sql"))
def test_f3_boundary_requires_current_exact_owner_and_never_falls_back_to_f2(failure):
    from datetime import timedelta

    external, inherited, state, sql, f2, _, _, selected, _ = family_name_with_boundary()
    source = "person:old-student"
    if failure == "revoked":
        inherited.valid = False
    elif failure == "expired":
        state.now += timedelta(days=7)
    elif failure == "unapproved":
        source = "person:unapproved"
    else:
        state.after_sql = lambda: setattr(inherited, "valid", False)
    with pytest.raises(PermissionError):
        external.boundary_reader.read(selected, "person", (source,), state.now)
    assert not f2
    assert len(sql) == (1 if failure == "after_sql" else 0)


@pytest.mark.parametrize("change", ("version", "boundary"))
def test_f3_boundary_proof_rechecks_metadata_before_release(change):
    from datetime import timedelta

    external, _, state, sql, _, _, _, selected, _ = family_name_with_boundary()
    result = external.boundary_reader.read(selected, "person", ("person:old-student",), state.now)
    if change == "version":
        state.version += 1
    else:
        state.boundary += timedelta(days=1)
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()
    assert len(sql) == 2
