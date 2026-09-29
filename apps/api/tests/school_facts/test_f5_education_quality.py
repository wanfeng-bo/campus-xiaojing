import pytest

from tests.school_facts.test_f4_education_quality import AT, actor, fixture


def query(bridge, **kwargs):
    return bridge.quality_for(
        actor(),
        target_kind="teaching_assignment",
        target_source_keys=("teacher:a",),
        field_names=("teaching_role",),
        owner_kind="teaching_offering",
        owner_source_keys=("class:a",),
        as_of=AT,
        **kwargs,
    )


def test_f5_quality_uses_own_target_fields_without_expanding_f4():
    from app.platform.school_facts.f4_policy import F4_FACT_KINDS

    b, g, a, p = fixture()
    result = query(b, teaching_roles=("lead",))
    assert result["coverage"] == "unknown"
    assert p.calls[0]["teaching_roles"] == ("lead",)
    assert "teaching_assignment" not in F4_FACT_KINDS
    a.field_allowed = False
    with pytest.raises(PermissionError):
        query(b, teaching_roles=("lead",))


def test_wrong_role_quality_is_not_a_negative_for_requested_teaching_role():
    b, g, a, p = fixture()
    p.rows = [
        {
            "source_key": "quality:one",
            "content": {
                "education_target_kind": "teaching_offering",
                "education_target_source_key": "class:a",
                "target_fact_kind": "teaching_assignment",
                "teaching_role": "co_teacher",
                "negative_code": "no_teaching_assignment",
            },
        }
    ]
    with pytest.raises(PermissionError):
        query(b, teaching_roles=("lead",))


def test_new_dependency_references_accept_f5_and_keep_exact_reference_kind():
    from app.composition.school_fact_education_quality import EducationDependencyReferenceAdapter

    fields = EducationDependencyReferenceAdapter.reference_fields(
        {"education_target_kind": "teaching_offering", "target_fact_kind": "teaching_assignment"}
    )
    assert fields["education_target_ref"] == ("education_target_source_key", "teaching_offering")


def test_unverified_future_quality_is_not_current_confirmed_evidence():
    b, g, a, p = fixture()
    p.rows = [
        {
            "source_key": "quality:one",
            "content": {
                "education_target_kind": "teaching_offering",
                "education_target_source_key": "class:a",
                "target_fact_kind": "teaching_assignment",
                "teaching_role": "lead",
                "negative_code": "no_teaching_assignment",
                "verified_at": "2099-01-01T00:00:00Z",
            },
        }
    ]
    with pytest.raises(PermissionError):
        query(b, teaching_roles=("lead",))


def test_f5_dependency_migration_has_narrow_role_and_verification_boundaries():
    from pathlib import Path

    sql = (
        Path(__file__).parents[2] / "alembic/versions/20260914_35_f5_education_dependencies.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260914_34"' in sql
    assert "p_teaching_roles text[]" in sql
    assert "s.verified_at<=p_at" in sql
    assert "q.teaching_role=ANY(p_teaching_roles)" in sql
    assert "SELECT verified_at FROM selected" in sql
    assert "GRANT SELECT(teaching_role)" in sql
    assert "CREATE OR REPLACE" not in sql


@pytest.mark.parametrize(
    "enabled,owner,target,expected",
    [
        (True, "teaching_offering", "teaching_assignment", "school_fact_f5_dependencies"),
        (True, "education_class", "class_teacher_assignment", "school_fact_f4_dependencies"),
    ],
)
def test_projection_dispatches_to_exact_family_with_roles(enabled, owner, target, expected):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.composition.school_fact_education_quality import SqlEducationDependencyProjection

    statements = []

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield self
            after()

        def fetch(self, relation):
            statements.append(str(relation.data_statement))
            return [{"payload": {"records": [], "school_fact_version": 1}}]

    projection = SqlEducationDependencyProjection(
        SimpleNamespace(executor=Executor()), f5_enabled=enabled
    )
    selection = dict(
        school_id=actor().school_id,
        kind="fact_quality",
        source_keys=lambda: ("quality:one",),
        owner_kind=owner,
        owner_source_keys=("class:a",),
        target_kind=target,
        target_source_keys=("teacher:a",),
        field_names=("teaching_role",),
        as_of=AT,
        academic_year=None,
        policy_source_keys=(),
        campus_source_keys=(),
        position_source_keys=(),
        teaching_roles=("lead",),
        before_read=lambda: None,
        after_read=lambda _: None,
    )
    projection.read(**selection)
    assert expected in statements[0]
    projection.f5_enabled = False
    if target == "teaching_assignment":
        with pytest.raises(PermissionError):
            projection.read(**selection)


def test_negative_maintenance_requires_exact_offering_maintenance_authority():
    from types import SimpleNamespace

    b, g, a, p = fixture()
    content = dict(
        education_target_kind="teaching_offering",
        education_target_source_key="class:a",
        target_fact_kind="teaching_assignment",
        owner_kind="education",
        teaching_role="lead",
        negative_code="no_teaching_assignment",
    )
    change = SimpleNamespace(
        fact_kind="fact_quality",
        source_key="f4:fact_quality:new",
        expected_version=0,
        operation="upsert",
        content=SimpleNamespace(model_dump=lambda **kwargs: content),
    )
    original = a.require_scope

    def require(who, **kwargs):
        if kwargs["fact_kind"] == "teaching_offering" and kwargs["action"] == "maintain":
            raise PermissionError("only offering read")
        return original(who, **kwargs)

    a.require_scope = require
    with pytest.raises(PermissionError, match="only offering read"):
        b.require_change_set(actor(), SimpleNamespace(changes=(change,)))


def test_period_metadata_sql_has_exact_candidates_and_never_projects_contact_payload():
    import ast
    from pathlib import Path

    module = ast.parse(
        (
            Path(__file__).parents[2] / "alembic/versions/20260914_35_f5_education_dependencies.py"
        ).read_text(encoding="utf-8")
    )
    sql = next(
        ast.literal_eval(n.value)
        for n in module.body
        if isinstance(n, ast.Assign) and n.targets[0].id == "EXTERNAL_BOUNDARY_SQL"
    )
    assert "l.fact_source_key=ANY(p_keys)" in sql
    assert "o.source_key=ANY(p_organizations)" in sql
    assert "visibility_scope='public'" in sql
    assert "contact_value" not in sql
    assert "SELECT *" not in sql


def test_external_metadata_uses_exact_current_scopes_and_rereads_before_release():
    from types import SimpleNamespace

    from app.composition.school_fact_education_external import SqlEducationExternalBoundaries
    from app.composition.school_fact_education_reader import EducationReadStale
    from app.platform.school_facts.f2_policy import F2FactScope

    calls = []
    version = [10]
    scopes = {
        "person": ("person:a",),
        "organization": ("org:a",),
        "official_contact": ("contact:future",),
    }

    class Guard:
        def require_current(self, a, kind, **kwargs):
            return SimpleNamespace(
                scope_plan=SimpleNamespace(
                    reference=F2FactScope(a.school_id, kind, scopes[kind]).reference
                )
            )

        def revalidate(self, *_):
            pass

    class Session:
        def __enter__(self):
            calls.append("open")
            return self

        def __exit__(self, *_):
            calls.append("close")

        def scalar(self, statement, args):
            calls.append(args)
            return {
                "school_fact_version": version[0],
                "next_valid_boundary": "2028-01-01T00:00:00Z",
            }

    reader = SqlEducationExternalBoundaries(sessions=Session, guard=Guard())
    with pytest.raises(PermissionError):
        reader.read(actor(), "person", ("person:other",), AT)
    assert calls == []
    result = reader.read(actor(), "official_contact", None, AT, organizations=("org:a",))
    assert calls[1]["keys"] == ["contact:future"]
    assert calls[1]["orgs"] == ["org:a"]
    assert calls[-1] == "close"
    version[0] = 11
    with pytest.raises(EducationReadStale):
        result["proof"].revalidate()
    assert calls[-1] == "close"
