from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from app.platform.school_facts.f2_policy import F2FactScope
from tests.school_facts.test_f4_education_reader import actor

AT = datetime(2026, 9, 13, tzinfo=UTC)


def module():
    name = "app.composition.school_fact_education_quality"
    assert find_spec(name) is not None, "Finite F2/F4 quality bridge missing"
    return import_module(name)


class Guard:
    def __init__(self):
        self.allowed = True
        self.calls = []
        self.creation_keys = ("f4:fact_quality:new",)

    def require_current(self, who, kind, *, purpose="read"):
        self.calls.append((kind, purpose))
        if not self.allowed:
            raise PermissionError("revoked")
        scope = F2FactScope(who.school_id, kind, ("quality:one",), self.creation_keys)
        return SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))

    def revalidate(self, auth, who):
        if not self.allowed:
            raise PermissionError("revoked")


class Authority:
    def __init__(self):
        self.field_allowed = True

    def require_scope(self, who, *, fact_kind, action, checked_at):
        return SimpleNamespace(
            scope=SimpleNamespace(
                fact_kind=fact_kind, object_keys=("class:a", "teacher:a"), creation_keys=()
            )
        )

    def require_object(self, p, key, *, is_creation=False):
        if key not in p.scope.object_keys:
            raise PermissionError("object")

    def require_fields(self, p, fields):
        if not self.field_allowed:
            raise PermissionError("fields")

    def revalidate(self, p, *, actor, checked_at):
        if not self.field_allowed:
            raise PermissionError("fields revoked")


class Projection:
    def __init__(self):
        self.calls = []
        self.version = 4
        self.rows = []

    def current_version(self, school):
        return self.version

    def read(self, *, before_read, after_read, **kwargs):
        before_read()
        self.calls.append(kwargs)
        result = {
            "school_fact_version": self.version,
            "records": self.rows,
            "next_valid_boundary": None,
        }
        after_read(result)
        return result


def fixture():
    guard, authority, projection = Guard(), Authority(), Projection()
    bridge = module().EducationQualityBridge(
        f2_guard=guard, education_authority=authority, projection=projection, clock=lambda: AT
    )
    return bridge, guard, authority, projection


def query(bridge):
    return bridge.quality_for(
        actor(),
        target_kind="class_teacher_assignment",
        target_source_keys=("teacher:a",),
        field_names=("teacher_role",),
        owner_kind="education_class",
        owner_source_keys=("class:a",),
        as_of=AT,
    )


def test_quality_cannot_borrow_only_f2_or_only_f4_authority():
    b, g, a, p = fixture()
    g.allowed = False
    with pytest.raises(PermissionError):
        query(b)
    assert not p.calls
    g.allowed = True
    a.field_allowed = False
    with pytest.raises(PermissionError):
        query(b)
    assert not p.calls


def test_quality_passes_exact_sets_to_sql_and_empty_is_unknown():
    b, g, a, p = fixture()
    result = query(b)
    assert result["statements"] == [] and result["coverage"] == "unknown"
    assert p.calls[0]["source_keys"]() == ("quality:one",)
    assert p.calls[0]["owner_source_keys"] == ("class:a",)
    assert p.calls[0]["field_names"] == ("teacher_role",)
    g.allowed = False
    with pytest.raises(PermissionError):
        result["proof"].revalidate()


def test_grade_group_quality_keeps_exact_campus_coordinate_and_authorization():
    b, g, a, p = fixture()
    # The F2 location guard is independent of the F4 year/group read.
    original = g.require_current

    def require_current(who, kind, *, purpose="read"):
        if kind != "location":
            return original(who, kind, purpose=purpose)
        g.calls.append((kind, purpose))
        scope = F2FactScope(who.school_id, kind, ("campus:a",), ())
        return SimpleNamespace(scope_plan=SimpleNamespace(reference=scope.reference))

    g.require_current = require_current
    result = b.quality_for(
        actor(),
        target_kind="education_grade_group",
        target_source_keys=(),
        field_names=("grade_source_key",),
        owner_kind="education_calendar_rule",
        owner_source_keys=("class:a",),
        academic_year=2026,
        campus_source_keys=("campus:a",),
        as_of=AT,
    )
    assert result["coverage"] == "unknown"
    assert p.calls[0]["campus_source_keys"] == ("campus:a",)
    assert ("location", "read") in g.calls


def test_quality_result_never_accepts_extra_owner_or_field():
    b, g, a, p = fixture()
    p.rows = [
        {
            "source_key": "quality:one",
            "content": {
                "education_target_kind": "education_class",
                "education_target_source_key": "class:other",
                "target_fact_kind": "class_teacher_assignment",
                "field_name": "teacher_role",
            },
        }
    ]
    with pytest.raises(PermissionError):
        query(b)


def test_quality_proof_rejects_school_version_change_even_empty():
    b, g, a, p = fixture()
    result = query(b)
    p.version = 5
    with pytest.raises(module().EducationReadStale):
        result["proof"].revalidate()


@pytest.mark.parametrize("kind", ["content", "f2_permit"])
def test_dependency_deadline_is_checked_after_final_identity_io(kind):
    from datetime import timedelta
    from uuid import UUID

    from app.composition.school_fact_product_identity import (
        ProductIdentity,
        ProductIdentityEvidence,
    )
    from app.composition.school_fact_read_evidence import validation_batch
    from app.platform.assistant.runtime import AssistantRuntimeError
    from app.platform.governance.contracts import GovernanceAuthorizationBasis

    bridge, _, _, projection = fixture()
    now = [AT]
    bridge.clock = lambda: now[0]
    deadline = AT + timedelta(seconds=1)
    governance = GovernanceAuthorizationBasis(
        actor().school_id,
        actor().assignment_id,
        "synthetic",
        UUID(int=4),
        UUID(int=5),
        1,
        1,
        AT,
        deadline,
        (),
    )
    permits = (
        (SimpleNamespace(basis=SimpleNamespace(governance=governance)),)
        if kind == "f2_permit"
        else ()
    )
    proof = module().EducationDependencyProof(
        bridge, actor(), permits, (), 4, deadline if kind == "content" else None
    )
    identity = ProductIdentity(actor(), "synthetic-session", "synthetic-reference")
    calls = []

    def current_version(_):
        calls.append("content")
        return 4

    def final_identity():
        calls.append("identity")
        now[0] = deadline
        return identity

    projection.current_version = current_version
    with pytest.raises(AssistantRuntimeError, match="time boundary"):
        with validation_batch() as batch:
            batch.identity_checks["identity"] = ProductIdentityEvidence(identity, final_identity)
            proof.revalidate()
    assert calls == ["content", "content", "identity"]


def test_dependency_writer_guard_requires_exact_f2_creation_and_f4_target_fields():
    b, g, a, p = fixture()
    content = {
        "education_target_kind": "education_class",
        "education_target_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a",
        "field_name": "teacher_role",
        "owner_kind": "education",
    }
    change = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="f4:fact_quality:new",
        expected_version=0,
        operation="upsert",
        content=SimpleNamespace(model_dump=lambda **kwargs: content),
    )
    plan = SimpleNamespace(changes=(change,))
    proof = b.require_change_set(actor(), plan)
    assert ("fact_quality", "maintain") in g.calls
    a.field_allowed = False
    with pytest.raises(PermissionError):
        proof.revalidate()
    a.field_allowed = True
    change.source_key = "unapproved:new"
    with pytest.raises(PermissionError):
        b.require_change_set(actor(), plan)


def test_grade_group_collection_final_guard_requires_every_current_exact_member():
    b, _, authority, _ = fixture()
    keys = {"calendar:campus-a", "group:a", "group:b"}
    seen = []

    def require_scope(_actor, *, fact_kind, action, checked_at):
        return SimpleNamespace(
            scope=SimpleNamespace(fact_kind=fact_kind, object_keys=tuple(keys), creation_keys=())
        )

    def require_object(permit, key, *, is_creation=False):
        seen.append((permit.scope.fact_kind, key))
        if key not in keys:
            raise PermissionError("object revoked")

    authority.require_scope = require_scope
    authority.require_object = require_object
    content = {
        "education_target_kind": "education_calendar_rule",
        "education_target_source_key": "calendar:campus-a",
        "target_fact_kind": "education_grade_group",
        "target_source_key": None,
        "field_name": "grade_source_key",
        "owner_kind": "education",
        "coverage_basis": {
            "schema_version": 2,
            "authorized_source_keys": ["group:a", "group:b"],
        },
    }
    change = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="f4:fact_quality:new",
        expected_version=0,
        operation="upsert",
        content=SimpleNamespace(model_dump=lambda **_kw: content),
    )
    b.require_change_set(actor(), SimpleNamespace(changes=(change,)))
    assert ("education_grade_group", "group:a") in seen
    assert ("education_grade_group", "group:b") in seen
    keys.remove("group:b")
    with pytest.raises(PermissionError, match="revoked"):
        b.require_change_set(actor(), SimpleNamespace(changes=(change,)))


def test_dependency_creation_uses_exact_grant_without_engineering_name_prefix():
    b, g, _, _ = fixture()
    g.creation_keys = ("school:quality:2028",)
    content = {
        "education_target_kind": "education_class",
        "education_target_source_key": "class:a",
        "target_fact_kind": "class_teacher_assignment",
        "target_source_key": "teacher:a",
        "field_name": "teacher_role",
        "owner_kind": "education",
    }
    change = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="school:quality:2028",
        expected_version=0,
        operation="upsert",
        content=SimpleNamespace(model_dump=lambda **kw: content),
    )
    b.require_change_set(actor(), SimpleNamespace(changes=(change,))).revalidate()


def test_dependency_product_query_keeps_explicit_year_campus_position_and_current_time(monkeypatch):
    from app.composition import school_fact_education_dependencies as deps

    b, _, _, _ = fixture()
    calls = []

    class Refs:
        def source_for(self, who, kind, reference, **kw):
            return {
                "f4:education_class:opaque": "class:a",
                "campus-ref": "campus:a",
                "position-ref": "position:a",
            }[reference]

    def read(who, **selection):
        calls.append(selection)
        return {
            "records": [],
            "proof": SimpleNamespace(school_fact_version=4, revalidate=lambda: None),
        }

    b.read_dependency = read
    monkeypatch.setattr(deps.EducationReadProof, "combine", lambda rows: None)
    monkeypatch.setattr(
        deps, "EducationQueryProof", lambda *args: SimpleNamespace(revalidate=lambda **kw: None)
    )
    b.education_authority.require_scope = lambda *a, **kw: SimpleNamespace(
        scope=SimpleNamespace(
            object_keys=("class:a",), field_names=("display_name",), is_school_wide=False
        )
    )
    repo = SimpleNamespace(read_records=lambda *a, **kw: SimpleNamespace(proof=None))
    adapter = deps.EducationDependencyProductAdapter(
        bridge=b, references=Refs(), directory_adapter=None, repository=repo
    )
    result = adapter.query_with_proof(
        actor=actor(),
        fact_kind="policy_scope",
        related_field="education_target_ref",
        related_ref="f4:education_class:opaque",
        academic_year=2025,
        campus_ref="campus-ref",
        position_ref="position-ref",
    )
    assert result.public_result["records"] == []
    assert calls[0]["academic_year"] == 2025
    assert calls[0]["campus_source_keys"] == ("campus:a",)
    assert calls[0]["position_source_keys"] == ("position:a",)
    assert calls[0]["as_of"] is None
    with pytest.raises(ValueError):
        adapter.query_with_proof(actor=actor(), fact_kind="policy_scope")


def test_dependency_read_only_descriptor_never_mints_creation_choices():
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter

    b, _, _, _ = fixture()
    actual = b.require_dependency_scope

    def only_read(who, kind, purpose):
        if purpose == "maintain":
            raise PermissionError("not granted")
        return actual(who, kind, purpose)

    b.require_dependency_scope = only_read
    adapter = EducationDependencyProductAdapter(
        bridge=b,
        references=None,
        directory_adapter=SimpleNamespace(describe=lambda **kw: {"fields": []}),
        repository=None,
    )
    result = adapter.describe(actor=actor(), fact_kind="fact_quality")
    assert result["creation_choices"] == []
    assert result["query_support"]["all_authorized"] is False


def test_dependency_prepare_builds_original_f2_kind_in_atomic_schema_three():
    from uuid import UUID

    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter

    b, _, _, _ = fixture()
    captured = []
    b.creation_coordinates = lambda *args: {"school_fact_version": 4}
    b.require_change_set = lambda who, plan: (
        captured.append(plan) or SimpleNamespace(revalidate=lambda: None)
    )
    content = {
        "policy_source_key": "policy:one",
        "education_target_kind": "education_class",
        "education_target_source_key": "class:a",
        "academic_year": 2026,
    }
    refs = SimpleNamespace(
        source_for=lambda *args, **kw: "school:scope:new",
        resolve_fields=lambda *args, **kw: content,
        public_fields=lambda who, raw: {
            "policy_ref": "f2:policy:opaque",
            "education_target_ref": "f4:education_class:opaque",
        },
    )
    adapter = EducationDependencyProductAdapter(
        bridge=b, references=refs, directory_adapter=None, repository=None
    )
    payload = FactChangeInput.model_validate_json(
        '{"fact_kind":"policy_scope","creation_ref":"f2create:policy_scope:opaque","operation":"upsert","fields":{},"reason":"核对制度适用范围"}'
    )
    result = adapter.resolve_change(
        actor=actor(), payload=payload, request_id=UUID(int=9), effective_at=AT
    )
    assert result.change_set.schema_version == 3
    assert result.change_set.changes[0].fact_kind == "policy_scope"
    assert result.change_set.changes[0].source_key == "school:scope:new"
    assert captured == [result.change_set]
    assert result.summary.before is None


def test_dependency_existing_read_requires_no_product_hint_and_rechecks_both_owners():
    b, g, a, p = fixture()
    selection_calls = []

    def selection(*, before_read, after_read, **kw):
        before_read()
        selection_calls.append(kw)
        raw = {
            "school_fact_version": 4,
            "records": [
                {
                    "source_key": "quality:one",
                    "version": 1,
                    "owner_kind": "education_class",
                    "owner_key": "class:a",
                    "target_kind": "class_teacher_assignment",
                    "target_key": "teacher:a",
                    "replacement_key": None,
                    "field_name": "teacher_role",
                    "academic_year": 2026,
                    "position_key": None,
                    "campus_key": None,
                    "policy_key": None,
                }
            ],
        }
        after_read(raw)
        return raw

    p.selection = selection
    p.rows = [
        {
            "source_key": "quality:one",
            "version": 1,
            "content": {
                "education_target_kind": "education_class",
                "education_target_source_key": "class:a",
                "target_fact_kind": "class_teacher_assignment",
                "target_source_key": "teacher:a",
                "field_name": "teacher_role",
                "academic_year": 2026,
                "owner_kind": "education",
            },
        }
    ]
    result = b.read_for_source(actor(), "fact_quality", "quality:one", AT)
    assert result["row"]["version"] == 1
    for operation in ("end", "revoke"):
        change = SimpleNamespace(
            fact_kind="fact_quality",
            source_key="quality:one",
            expected_version=1,
            operation=operation,
            content=None,
            effective_at=AT,
        )
        b.require_change_set(actor(), SimpleNamespace(changes=(change,))).revalidate()

    assert (
        not b.known_dependencies or ("1", "fact_quality", "quality:one") not in b.known_dependencies
    )
    a.field_allowed = False
    with pytest.raises(PermissionError):
        b.read_for_source(actor(), "fact_quality", "quality:one", AT)
    g.allowed = False
    count = len(selection_calls)
    with pytest.raises(PermissionError):
        b.read_for_source(actor(), "fact_quality", "quality:one", AT)
    assert len(selection_calls) == count


@pytest.mark.parametrize("selected", [("display_name",), ("teacher_role", "assignment_source_key")])
def test_dependency_coordinate_basis_uses_one_current_authorized_field(selected):
    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter

    b, _, a, _ = fixture()
    a.require_scope = lambda *args, **kwargs: SimpleNamespace(
        scope=SimpleNamespace(object_keys=("teacher:a",), field_names=selected)
    )
    a.require_fields = EducationAuthority.require_fields
    observed = []
    proof = SimpleNamespace(revalidate=lambda **kwargs: observed.append("release"))

    def read(who, kind, **kwargs):
        assert kwargs["source_keys"] == ("teacher:a",)
        assert kwargs["fields"] == (sorted(selected)[0],)
        a.require_fields(a.require_scope(), kwargs["fields"])
        observed.append(kwargs)
        return SimpleNamespace(proof=proof)

    adapter = EducationDependencyProductAdapter(
        bridge=b,
        references=None,
        directory_adapter=None,
        repository=SimpleNamespace(read_records=read),
    )
    result = adapter._coordinate_basis(actor(), "class_teacher_assignment", "teacher:a", None)
    result.proof.revalidate()
    assert observed[-1] == "release"
    a.require_scope = lambda *args, **kwargs: SimpleNamespace(
        scope=SimpleNamespace(object_keys=("teacher:a",), field_names=())
    )
    with pytest.raises(PermissionError):
        adapter._coordinate_basis(actor(), "class_teacher_assignment", "teacher:a", None)
    with pytest.raises(PermissionError):
        adapter._coordinate_basis(actor(), "class_teacher_assignment", "teacher:other", None)


def test_quality_for_combines_same_records_self_owner_without_global_promotion():
    b, _, _, _ = fixture()
    calls = []

    def read(who, **selection):
        calls.append(selection)
        own = selection["owner_kind"] == "class_teacher_assignment"
        return {
            "records": [
                {
                    "source_key": "quality:self",
                    "content": {
                        "education_target_kind": "class_teacher_assignment",
                        "education_target_source_key": "teacher:a",
                        "target_fact_kind": "class_teacher_assignment",
                        "target_source_key": None,
                        "quality_kind": "source",
                        "source_status": "available",
                        "field_name": "teacher_role",
                    },
                }
            ]
            if own
            else [],
            "statements": [],
            "school_fact_version": 4,
            "proof": SimpleNamespace(revalidate=lambda: None),
            "next_valid_boundary": None,
        }

    b.read_dependency = read
    result = b.quality_for(
        actor(),
        target_kind="class_teacher_assignment",
        target_source_keys=("teacher:a",),
        field_names=("teacher_role",),
        owner_kind="education_class",
        owner_source_keys=("class:a",),
        include_target_owner=True,
    )
    assert len(calls) == 2
    assert calls[1]["owner_kind"] == "class_teacher_assignment"
    assert calls[1]["owner_source_keys"] == ("teacher:a",)
    assert result["records"][0]["applies_to_source_key"] == "teacher:a"
    assert result["records"][0]["content"]["target_source_key"] is None
    result["proof"].revalidate()


@pytest.mark.parametrize("fault", ["revoke", "identity", "version", "time"])
def test_coordinate_basis_keeps_real_reader_release_checks(fault):
    from datetime import timedelta

    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter
    from tests.school_facts.test_f4_education_reader import NOW, repository

    repo, projection, authority = repository()
    authority.require_fields = EducationAuthority.require_fields
    projection.records = [
        {
            "source_key": "f4:class:a",
            "fact_kind": "education_class",
            "version": 1,
            "segments": [{"content": {"display_name": "不应进入质量回答的对象字段"}}],
            "next_valid_boundary": (NOW + timedelta(seconds=1)).isoformat(),
        }
    ]
    adapter = EducationDependencyProductAdapter(
        bridge=SimpleNamespace(education_authority=authority, clock=repo.clock),
        repository=repo,
        references=None,
        directory_adapter=None,
    )
    basis = adapter._coordinate_basis(actor(), "education_class", "f4:class:a", None)
    assert projection.calls[0]["field_names"] == ("display_name",)
    assert projection.calls[0]["source_keys"] == ("f4:class:a",)
    if fault == "revoke":
        authority.allowed = False
    elif fault == "version":
        projection.version += 1
    elif fault == "time":
        repo.clock = lambda: NOW + timedelta(seconds=2)
    with pytest.raises((PermissionError, module().EducationReadStale)):
        basis.proof.revalidate(actor=actor(4) if fault == "identity" else actor())


def test_coordinate_basis_never_projects_before_exact_current_permission():
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter
    from tests.school_facts.test_f4_education_reader import repository

    repo, projection, authority = repository()
    adapter = EducationDependencyProductAdapter(
        bridge=SimpleNamespace(education_authority=authority, clock=repo.clock),
        repository=repo,
        references=None,
        directory_adapter=None,
    )
    with pytest.raises(PermissionError):
        adapter._coordinate_basis(actor(), "education_class", "f4:class:foreign", None)
    authority.allowed = False
    with pytest.raises(PermissionError):
        adapter._coordinate_basis(actor(), "education_class", "f4:class:a", None)
    assert projection.calls == []


def test_quality_query_does_not_return_coordinate_basis_content():
    from app.composition.school_fact_education_authority import EducationAuthority
    from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter
    from tests.school_facts.test_f4_education_reader import repository

    repo, projection, authority = repository()
    authority.require_fields = EducationAuthority.require_fields
    projection.records = [
        {
            "source_key": "f4:class:a",
            "fact_kind": "education_class",
            "version": 1,
            "segments": [{"content": {"display_name": "只供坐标核验的字段值"}}],
        }
    ]
    permission = SimpleNamespace(revalidate=lambda: None, school_fact_version=10)
    bridge = SimpleNamespace(
        education_authority=authority,
        clock=repo.clock,
        read_for_source=lambda *_args: {
            "proof": permission,
            "content": {
                "education_target_kind": "education_class",
                "education_target_source_key": "f4:class:a",
                "target_fact_kind": "education_class",
            },
        },
        read_dependency=lambda *_args, **_kwargs: {"records": [], "proof": permission},
    )
    adapter = EducationDependencyProductAdapter(
        bridge=bridge,
        repository=repo,
        directory_adapter=None,
        references=SimpleNamespace(source_for=lambda *_: "quality:one"),
    )
    result = adapter.query_with_proof(actor=actor(), fact_kind="fact_quality", reference="opaque")
    assert projection.calls[0]["field_names"] == ("display_name",)
    assert "只供坐标核验的字段值" not in str(result.public_result)
    assert "segments" not in str(result.public_result)
    assert result.public_result["records"] == []


@pytest.mark.parametrize("fault", ["version", "object", "revoke", "time"])
def test_quality_join_retains_each_permission_version_and_boundary(fault):
    from datetime import timedelta

    bridge, _, _, _ = fixture()
    released = []
    active = [True]
    now = [AT]
    deadline = AT + timedelta(seconds=1)

    def read(_actor, **selection):
        own = selection["owner_kind"] == "class_teacher_assignment"

        def revalidate():
            released.append(own)
            if own and not active[0]:
                raise PermissionError("revoked")
            if own and now[0] >= deadline:
                raise module().EducationReadStale("boundary")

        return {
            "records": [
                {
                    "source_key": "quality:self",
                    "content": {
                        "education_target_source_key": "teacher:other"
                        if fault == "object"
                        else "teacher:a",
                    },
                }
            ]
            if own
            else [],
            "school_fact_version": 5 if own and fault == "version" else 4,
            "proof": SimpleNamespace(revalidate=revalidate),
            "next_valid_boundary": deadline.isoformat() if own else None,
        }

    bridge.read_dependency = read
    kwargs = dict(
        target_kind="class_teacher_assignment",
        target_source_keys=("teacher:a",),
        field_names=("teacher_role",),
        owner_kind="education_class",
        owner_source_keys=("class:a",),
        include_target_owner=True,
    )
    if fault in {"version", "object"}:
        with pytest.raises((PermissionError, module().EducationReadStale)):
            bridge.quality_for(actor(), **kwargs)
        return
    result = bridge.quality_for(actor(), **kwargs)
    assert result["next_valid_boundary"] == deadline
    assert set(released) == {False, True}
    if fault == "revoke":
        active[0] = False
    else:
        now[0] = deadline
    with pytest.raises((PermissionError, module().EducationReadStale)):
        result["proof"].revalidate()
