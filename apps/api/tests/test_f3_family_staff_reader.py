from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied, FamilyFactTarget
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_family_staff_reader import FamilyStaffRepository
from app.platform import contracts as pc
from app.platform.school_facts.f3_policy import F3FactScope

NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)
SCHOOL = UUID(int=1)


def actor():
    return pc._issue_staff_actor_context(
        account_user_id=1,
        school_id=pc.SchoolId(SCHOOL),
        person_id=pc.PersonId(UUID(int=2)),
        assignment_id=pc.AssignmentId(UUID(int=3)),
        assignment_version=1,
        authorization_version=2,
    )


class Permit:
    def __init__(self, actor, target, fields):
        self.actor = actor
        self.target = target
        self.scope = type("Scope", (), {"field_names": fields, "is_school_wide": False})()


class Projection:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.read_calls = 0

    def read(self, **kw):
        self.read_calls += 1
        kw.pop("before_read")()
        kw["field_names"] = kw["field_names"]()
        self.calls.append(kw)
        kw.pop("after_read")()
        return self.payload


def config():
    return FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"student": ("student:a", "student:b")},
        creation_catalog={"student": {"student:new": "新增同名学生"}},
    )


def payload(source="student:a", status="available", content=None):
    return {
        "status": status,
        "fact_kind": "student",
        "source_key": source,
        "content": (
            {"person_source_key": "person:a", "status": "active"} if content is None else content
        ),
        "expected_version": 3 if status == "available" else 0,
        "school_fact_version": 7,
        "label": "同名学生",
        "source_reference": "publication:1",
        "source_name": None,
        "source_trace_reference": "synthetic-fixture",
        "source_published_at": NOW.isoformat(),
        "factual_at": NOW.isoformat(),
        "valid_from": NOW.isoformat(),
        "valid_to": None,
    }


def repo(projection, valid=lambda: True):
    def auth(a, action, kind, source, creation, at):
        target = FamilyFactTarget(
            kind, source, ("person_source_key", "status"), a.authorization_version, creation
        )
        return Permit(a, target, ("person_source_key", "status"))

    def revalidate(permit, a, target, at):
        if not valid():
            raise RuntimeError("authority changed")
        assert permit.actor == a and permit.target == target

    return FamilyStaffRepository(
        projection=projection,
        configuration=config(),
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=revalidate,
        clock=lambda: NOW,
    )


def test_selected_query_fields_are_intersected_before_private_projection():
    projection = Projection(payload(content={"status": "active"}))
    repository = repo(projection)
    reference = repository.reference_for(actor=actor(), fact_kind="student", source_key="student:a")
    result = repository.query(
        actor=actor(),
        fact_kind="student",
        reference=reference,
        requested_fields=("status",),
    )
    assert projection.calls[0]["field_names"] == ("status",)
    assert result["records"][0]["content"] == {"status": "active"}


def test_existing_exact_reference_uses_pre_and_post_and_name_never_selects():
    p = Projection(payload())
    r = repo(p)
    a = actor()
    ref = r.reference_for(actor=a, fact_kind="student", source_key="student:a")
    got = r.prepare_target(actor=a, fact_kind="student", target_ref=ref, creation_ref=None)
    assert got.source_key == "student:a" and got.label == "同名学生" and len(p.calls) == 1
    other = r.reference_for(actor=a, fact_kind="student", source_key="student:b")
    assert other != ref


def test_school_reference_resolves_future_key_without_finite_catalog():
    a = actor()
    scope = F3FactScope(
        SCHOOL, "student", field_names=("status",), coverage="school"
    )
    checks = []

    def authorize(current, action, kind, source, creation, at):
        checks.append((action, kind, source, creation))
        assert current == a and scope.allows_key(source, is_creation=creation)
        return type(
            "SchoolPermit",
            (),
            {
                "actor": current,
                "target": FamilyFactTarget(kind, source, ("status",), 2, creation),
                "scope": scope,
            },
        )()

    repository = FamilyStaffRepository(
        projection=Projection(payload("student:later", content={"status": "active"})),
        configuration=FamilyConfiguration(
            approval_reference="school", scope_bindings=(), source_catalog={}
        ),
        reference_key=b"x" * 32,
        authorize_scope=authorize,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )
    reference = repository.reference_for(
        actor=a, fact_kind="student", source_key="student:later", school_wide=True
    )
    assert reference.startswith("schoolv1:f3:student:o:")
    assert repository.source_for_reference(a, reference) == ("student", "student:later")
    assert checks


def test_school_key_pages_are_complete_and_field_checked_before_metadata():
    from types import SimpleNamespace

    a = actor()
    keys = tuple(f"student:{i:03d}" for i in range(205))
    projection = Projection(payload())
    calls = []

    def scope_keys(**kw):
        kw["before_read"]()
        after = kw["after_key"]
        offset = keys.index(after) + 1 if after is not None else 0
        page = keys[offset : offset + kw["limit"]]
        calls.append((after, kw["limit"]))
        kw["after_read"]()
        return {
            "keys": list(page),
            "has_more": offset + len(page) < len(keys),
            "next_after": page[-1] if offset + len(page) < len(keys) and page else None,
            "school_fact_version": 10,
        }

    projection.scope_keys = scope_keys
    projection.current_version = lambda _school: 10
    scope = F3FactScope(SCHOOL, "student", field_names=("status",), coverage="school")
    repository = FamilyStaffRepository(
        projection=projection,
        configuration=FamilyConfiguration(approval_reference="school", scope_bindings=()),
        reference_key=b"x" * 32,
        authorize_scope=lambda *_: None,
        revalidate=lambda *_: None,
        authorize_school_scope=lambda *_: SimpleNamespace(scope=scope, actor=a),
        revalidate_school_scope=lambda *_: None,
        clock=lambda: NOW,
    )
    with pytest.raises(FamilyFactAuthorityDenied):
        tuple(repository._school_source_pages(a, "student", ("person_source_key",)))
    assert calls == []
    pages = tuple(repository._school_source_pages(a, "student", ("status",)))
    assert tuple(key for page in pages for key in page) == keys
    assert calls == [(None, 100), (keys[99], 100), (keys[199], 100)]


def test_school_name_query_scans_later_metadata_page_before_empty_conclusion():
    from types import SimpleNamespace

    a = actor()
    keys = tuple(f"person:{i:03d}" for i in range(105))
    projection = Projection(payload())
    projection.current_version = lambda _school: 10
    pages = []

    def scope_keys(**kw):
        kw["before_read"]()
        offset = keys.index(kw["after_key"]) + 1 if kw["after_key"] else 0
        selected = keys[offset : offset + 100]
        more = offset + len(selected) < len(keys)
        pages.append(kw["after_key"])
        kw["after_read"]()
        return {
            "keys": list(selected),
            "has_more": more,
            "next_after": selected[-1] if more else None,
            "school_fact_version": 10,
        }

    projection.scope_keys = scope_keys
    scope = F3FactScope(SCHOOL, "person", field_names=("display_name",), coverage="school")
    repository = FamilyStaffRepository(
        projection=projection,
        configuration=FamilyConfiguration(approval_reference="school", scope_bindings=()),
        reference_key=b"x" * 32,
        authorize_scope=lambda *_: None,
        revalidate=lambda *_: None,
        authorize_school_scope=lambda *_: SimpleNamespace(scope=scope, actor=a),
        revalidate_school_scope=lambda *_: None,
        clock=lambda: NOW,
    )
    candidates = []

    def read_candidates(**kw):
        candidates.append(kw["sources"])
        if "person:104" not in kw["sources"]:
            return (), len(kw["sources"])
        return (
            (
                SimpleNamespace(
                    reference="selected",
                    label="已核正式名称",
                    content={"display_name": "已核正式名称"},
                    expected_version=1,
                    factual_at=NOW,
                    valid_from=NOW,
                    valid_to=None,
                    source_reference="publication:one",
                    source_name=None,
                    source_trace_reference=None,
                    source_published_at=None,
                ),
            ),
            len(kw["sources"]),
        )

    repository._candidate_facts = read_candidates
    result = repository.query(
        actor=a, fact_kind="person", query_text="已核正式名称", requested_fields=("display_name",)
    )
    assert result["status"] == "known" and len(result["records"]) == 1
    assert pages == [None, keys[99]] and len(candidates) == 2


def test_creation_requires_creation_catalog_and_absent_projection():
    p = Projection(payload("student:new", "available_creation", {}))
    r = repo(p)
    a = actor()
    ref = r.reference_for(actor=a, fact_kind="student", source_key="student:new", creation=True)
    got = r.prepare_target(actor=a, fact_kind="student", target_ref=None, creation_ref=ref)
    assert got.expected_version == 0 and got.content == {}
    with pytest.raises(ValueError):
        r.prepare_target(actor=a, fact_kind="student", target_ref=ref, creation_ref=None)


def test_post_read_authority_change_and_overbroad_projection_fail_closed():
    a = actor()
    refbase = repo(Projection(payload())).reference_for(
        actor=a, fact_kind="student", source_key="student:a"
    )
    with pytest.raises(RuntimeError, match="changed"):
        repo(Projection(payload()), lambda: False).prepare_target(
            actor=a, fact_kind="student", target_ref=refbase, creation_ref=None
        )
    bad = payload()
    bad["content"] = {"display_name": "secret"}
    with pytest.raises(ValueError, match="unapproved"):
        repo(Projection(bad)).prepare_target(
            actor=a, fact_kind="student", target_ref=refbase, creation_ref=None
        )


def test_query_and_describe_keep_public_reference_time_source_and_unknown_coverage():
    p = Projection(payload())
    r = repo(p)
    a = actor()
    ref = r.reference_for(actor=a, fact_kind="student", source_key="student:a")
    result = r.query(actor=a, fact_kind="student", reference=ref, limit=1)
    assert result["coverage"] == "unknown" and result["records"][0]["reference"] == ref
    assert (
        "source_key" not in result["records"][0]
        and result["records"][0]["source_reference"] == "publication:1"
    )
    described = r.describe(actor=a, fact_kind="student")
    assert "person_ref" in described["fields"] and described["coverage"] == "unknown"
    with pytest.raises(ValueError):
        r.query(actor=a, fact_kind="student", limit=21)


def test_two_creation_reservations_are_independent_and_read_is_separate_action():
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"student": ("student:a",)},
        creation_catalog={"student": {"student:new": "A", "student:new2": "B"}},
    )
    actions = []

    def auth(a, action, kind, source, creation, at):
        actions.append((action, source, creation))
        target = FamilyFactTarget(
            kind, source, ("person_source_key", "status"), a.authorization_version, creation
        )
        return Permit(a, target, ("person_source_key", "status"))

    def revalidate(*_args):
        return True

    for source in ("student:new", "student:new2"):
        p = Projection(payload(source, "available_creation", {}))
        r = FamilyStaffRepository(
            projection=p,
            configuration=cfg,
            reference_key=b"x" * 32,
            authorize_scope=auth,
            revalidate=revalidate,
            clock=lambda: NOW,
        )
        ref = r.reference_for(actor=actor(), fact_kind="student", source_key=source, creation=True)
        assert (
            r.prepare_target(
                actor=actor(), fact_kind="student", target_ref=None, creation_ref=ref
            ).expected_version
            == 0
        )
    p = Projection(payload("student:a"))
    r = FamilyStaffRepository(
        projection=p,
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=revalidate,
        clock=lambda: NOW,
    )
    ref = r.reference_for(actor=actor(), fact_kind="student", source_key="student:a")
    r.query(actor=actor(), fact_kind="student", reference=ref)
    assert actions == [
        ("maintain", "student:new", True),
        ("maintain", "student:new2", True),
        ("read", "student:a", False),
    ]


def test_subset_scope_filters_objects_before_sql_and_uses_subset_fields():
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"student": ("student:a", "student:b")},
        source_labels={"student": {"student:a": "同名", "student:b": "同名"}},
    )
    calls = []

    class P:
        def read(self, **kw):
            kw.pop("before_read")()
            fields = kw.pop("field_names")()
            calls.append((kw["source_key"], fields))
            kw.pop("after_read")()
            return payload(kw["source_key"], content={"status": "active"})

    def auth(a, action, kind, source, creation, at):
        if source != "student:a":
            raise FamilyFactAuthorityDenied("denied")
        target = FamilyFactTarget(kind, source, ("status",), a.authorization_version, creation)
        return Permit(a, target, ("status",))

    r = FamilyStaffRepository(
        projection=P(),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_: True,
        clock=lambda: NOW,
    )
    result = r.query(actor=actor(), fact_kind="student")
    assert len(result["records"]) == 1 and calls == [("student:a", ("status",))]
    assert result["records"][0]["content"] == {"status": "active"}


def test_unknown_name_ambiguous_limit_and_creation_choices_are_structured_and_scoped():
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"student": ("student:a", "student:b")},
        source_labels={"student": {"student:a": "A", "student:b": "B"}},
        creation_catalog={"student": {"student:new": "N", "student:new2": "N2"}},
    )

    def auth(a, action, kind, source, creation, at):
        if source.endswith("2"):
            raise FamilyFactAuthorityDenied("denied")
        target = FamilyFactTarget(
            kind, source, ("person_source_key", "status"), a.authorization_version, creation
        )
        return Permit(a, target, ("person_source_key", "status"))

    r = FamilyStaffRepository(
        projection=Projection(payload()),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_: True,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="no authorized formal name"):
        r.query(actor=actor(), fact_kind="student", query_text="missing")
    limited = r.query(actor=actor(), fact_kind="student", limit=1)
    assert limited["status"] == "ambiguous" and limited["has_more"] and limited["truncated"]
    assert len(r.creation_choices(actor=actor(), fact_kind="student")) == 1


def test_school_creation_choice_uses_live_key_and_current_school_authority():
    scope = F3FactScope(
        actor().school_id, "student", field_names=("person_source_key", "status"),
        coverage="school",
    )
    school = type("SchoolPermit", (), {"scope": scope})()
    r = FamilyStaffRepository(
        projection=Projection(payload()), configuration=config(), reference_key=b"x" * 32,
        authorize_scope=lambda a, action, kind, key, creation, at: type(
            "TargetPermit", (), {
                "scope": scope,
                "target": FamilyFactTarget(
                    kind, key, scope.field_names, a.authorization_version, creation
                ),
            }
        )(), revalidate=lambda *_: True,
        authorize_school_scope=lambda *_: school,
        revalidate_school_scope=lambda *_: None, clock=lambda: NOW,
    )
    choices = r.creation_choices(actor=actor(), fact_kind="student")
    assert len(choices) == 1
    assert choices[0]["creation_ref"].startswith("schoolv1:f3:student:c:")


def test_public_field_references_hide_canonical_keys_and_preserve_null():
    class External:
        def resolve(self, **_kw):
            raise AssertionError("read projection must not resolve")

        def reference_for(self, *, actor, fact_kind, source_key):
            assert fact_kind == "location" and source_key == "location:a"
            return "f2:location:opaque"

    r = FamilyStaffRepository(
        projection=Projection(payload()),
        configuration=config(),
        reference_key=b"x" * 32,
        authorize_scope=lambda *_args: Permit(actor(), None, ("person_source_key", "status")),
        revalidate=lambda *_args: None,
        external_references=External(),
        clock=lambda: NOW,
    )
    view = r.public_field_references(
        actor=actor(),
        fact_kind="student_school_relationship",
        fields={
            "student_source_key": "student:a",
            "primary_campus_source_key": "location:a",
            "status": "active",
        },
    )
    assert view == {
        "student_ref": r.reference_for(actor=actor(), fact_kind="student", source_key="student:a"),
        "primary_campus_ref": "f2:location:opaque",
        "status": "active",
    }
    cleared = r.public_field_references(
        actor=actor(),
        fact_kind="student_contact",
        fields={"contact_person_source_key": None},
    )
    assert cleared == {"contact_person_ref": None}
    assert "_source_key" not in str(view) + str(cleared)


@pytest.mark.parametrize("person_allowed", [True, False])
def test_region_maintenance_label_reads_only_the_exact_current_authorized_person(person_allowed):
    calls = []
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"person_region": ("region:a",), "person": ("person:a", "person:b")},
    )

    class P:
        def read(self, **kw):
            kw.pop("before_read")()
            kw.pop("field_names")()
            calls.append((kw["fact_kind"], kw["source_key"]))
            content = (
                {"person_source_key": "person:a", "district_name": "合成区县"}
                if kw["fact_kind"] == "person_region"
                else {"display_name": "当前正式姓名"}
            )
            kw.pop("after_read")()
            return {
                **payload(kw["source_key"], content=content),
                "fact_kind": kw["fact_kind"],
                "label": "区县资料",
            }

    def auth(a, action, kind, source, creation, at):
        assert action == "read" and source in {"region:a", "person:a"}
        if kind == "person" and not person_allowed:
            raise FamilyFactAuthorityDenied("name not authorized")
        fields = (
            ("person_source_key", "district_name") if kind == "person_region" else ("display_name",)
        )
        return Permit(a, FamilyFactTarget(kind, source, fields, a.authorization_version), fields)

    r = FamilyStaffRepository(
        projection=P(),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )
    ref = r.reference_for(actor=actor(), fact_kind="person_region", source_key="region:a")
    target = r.prepare_target(
        actor=actor(), fact_kind="person_region", target_ref=ref, creation_ref=None
    )
    assert target.reference == ref and target.content["person_source_key"] == "person:a"
    if person_allowed:
        assert target.label == "当前正式姓名的区县资料"
        assert calls == [("person_region", "region:a"), ("person", "person:a")]
    else:
        assert "姓名未获权读取" in target.label and "当前正式姓名" not in target.label
        assert calls == [("person_region", "region:a")]


@pytest.mark.parametrize(
    ("current", "status", "publications", "supported"),
    [
        (True, "available", 1, True),
        (True, "no_current_records", 0, True),
        (True, "no_current_records", 1, False),
        (True, "available", 2, False),
        (False, "available", 2, True),
        (False, "no_current_records", 1, True),
    ],
)
def test_f3_evidence_support_requires_a_complete_natural_time_boundary(
    current, status, publications, supported
):
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_reader import F3EvidenceBackend

    backend = F3EvidenceBackend(sessions=None, thin=None)
    backend.version = lambda _actor: 7
    backend.clock = lambda: NOW
    deadline = NOW + timedelta(seconds=2) if status == "available" else None
    metadata = (("person:a", "digest", status, deadline, publications),)
    with backend.capture(actor=actor(), current_time=current) as capture:
        capture.decisions["scope"] = ("request", "allowed")
        backend.record(
            SimpleNamespace(metadata=lambda _relation: metadata),
            school_id=SCHOOL,
            fact_kind="person",
            sources=("person:a",),
            fields=("display_name",),
            as_of=NOW,
        )
        proof = capture.seal()
    assert (proof is not None) == supported
    if proof is not None:
        assert proof.valid_before == (deadline if current else None)
        assert proof.probes[0][1] == (1, metadata)


def test_f3_deadline_crossed_during_authority_finish_rejects_unmatched_source():
    from types import SimpleNamespace

    from app.composition.school_fact_family_staff_reader import F3EvidenceBackend

    backend = F3EvidenceBackend(sessions=None, thin=None)
    backend.version = lambda _actor: 7
    now = [NOW]
    backend.clock = lambda: now[0]
    deadline = NOW + timedelta(seconds=2)
    metadata = (("person:not-matching-name", "digest", "available", deadline, 1),)
    with backend.capture(actor=actor()) as capture:
        capture.decisions["scope"] = ("request", "allowed")
        backend.record(
            SimpleNamespace(metadata=lambda _relation: metadata),
            school_id=SCHOOL,
            fact_kind="person",
            sources=("person:not-matching-name",),
            fields=("display_name",),
            as_of=NOW,
        )
        proof = capture.seal()
    assert proof is not None
    backend.metadata = lambda _probe: (1, metadata)
    backend.decision_key = lambda request: request
    calls = []

    def decisions(requests):
        calls.append(requests)
        if len(calls) == 2:
            now[0] = deadline
        return tuple("allowed" for _ in requests)

    backend.decisions = decisions
    from contextlib import nullcontext

    backend.new_decision_round = lambda _actor: SimpleNamespace(
        activate=nullcontext, finish=lambda: None, clear=lambda: None
    )
    assert not proof.check(actor())
    assert len(calls) == 2


def test_f3_signature_is_scoped_and_exposes_no_plaintext_projection():
    from sqlalchemy.dialects import postgresql

    from app.composition.school_fact_family_staff_reader import F3SignatureCompiler

    compiler = F3SignatureCompiler()
    relation = compiler.compile(
        family_signature=True,
        school_id=SCHOOL,
        fact_kind="student",
        sources=("student:a", "student:b"),
        fields=("person_source_key",),
        as_of=NOW,
    )
    assert relation.projected_fields == (
        "source_key",
        "signature",
        "status",
        "valid_to",
        "publication_count",
    )
    sql = str(relation.metadata_statement.compile(dialect=postgresql.dialect()))
    assert "MATERIALIZED" in sql and "school_fact_f3_staff_target" in sql
    assert "school_fact_change_lineage.fact_source_key = f3_authorized_targets.source_key" in sql
    assert "master_data_import_units" not in sql and "sha256" in sql
    with pytest.raises(ValueError, match="Invalid fixed"):
        compiler.compile(
            family_signature=True,
            school_id=SCHOOL,
            fact_kind="student",
            sources=("student:a",),
            fields=("display_name",),
            as_of=NOW,
        )


def test_one_exact_scope_authorizes_all_its_members_once_per_short_read():
    from app.composition.school_fact_family_authority import StaffFamilyFactPermit
    from app.platform.school_facts.f3_policy import F3FactScope

    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"person": ("person:a", "person:b")},
    )
    scope = F3FactScope(SCHOOL, "person", ("person:a", "person:b"), (), ("display_name",))
    calls = []

    def authorize(a, action, kind, source, creation, at):
        calls.append(("before", source))
        return StaffFamilyFactPermit(
            a,
            action,
            FamilyFactTarget(kind, source, scope.field_names, a.authorization_version),
            UUID(int=44),
            scope,
            None,
        )

    r = FamilyStaffRepository(
        projection=CandidateProjection({"person:a": "同名", "person:b": "同名"}),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=authorize,
        revalidate=lambda permit, *_: calls.append(("after", permit.target.source_key)),
        clock=lambda: NOW,
    )
    result = r.query(actor=actor(), fact_kind="person", query_text="同名")
    assert len(result["records"]) == 2
    assert calls == [("before", "person:a"), ("after", "person:a")]
    r.query(actor=actor(), fact_kind="person", query_text="同名")
    assert len(calls) == 4  # The next read acquires a new permit.


@pytest.mark.parametrize("outcome", ["known", "forbidden", "ambiguous", "number_hidden"])
def test_region_student_number_follows_only_unique_authorized_exact_relations(outcome):
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={
            "student": ("student:a",),
            "student_school_relationship": ("enrollment:a",),
        },
    )
    r = FamilyStaffRepository(
        projection=Projection(payload()),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=lambda *_: None,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )
    calls = []

    def query(**kw):
        calls.append(kw)
        if kw["fact_kind"] == "student":
            assert kw["related_field"] == "person_ref" and kw["related_ref"] == "current-person-ref"
            return {"status": "known", "records": [{"reference": "current-student-ref"}]}
        assert kw["related_field"] == "student_ref" and kw["related_ref"] == "current-student-ref"
        return {
            "status": "known" if outcome == "number_hidden" else outcome,
            "records": [
                {"content": {} if outcome == "number_hidden" else {"student_number": "test-A001"}}
            ],
        }

    r.query = query
    assert r._student_number_for_person(actor(), "current-person-ref") == (
        "test-A001" if outcome == "known" else None
    )
    assert len(calls) == 2 and all(call["limit"] == 1 for call in calls)


def test_candidate_limit_does_not_skip_unmatched_authorized_group_evidence():
    from contextlib import contextmanager

    from app.composition.school_fact_family_staff_reader import SqlFamilyStaffProjection

    probes = []
    fetches = []

    class Transaction:
        def fetch(self, relation):
            fetches.append(relation)
            return [{"source_key": "person:a", "label": "same", "payload": {}}]

    class Executor:
        @contextmanager
        def validated_read(self, before, after):
            before()
            yield Transaction()
            after()

    class Evidence:
        def record(self, transaction, **kwargs):
            probes.append(kwargs)

    projection = SqlFamilyStaffProjection(Executor(), Evidence())
    rows = projection.candidates(
        school_id=SCHOOL,
        fact_kind="person",
        factual_at=NOW,
        match_field="display_name",
        match_value="same",
        limit=1,
        source_groups=lambda: (
            (("person:a",), ("display_name",)),
            (("person:b",), ("display_name", "person_kind")),
        ),
        before_read=lambda: None,
        after_read=lambda: None,
    )
    assert len(rows) == len(fetches) == 1
    assert [probe["sources"] for probe in probes] == [("person:a",), ("person:b",)]


@pytest.mark.parametrize("read_allowed", [True, False])
def test_empty_related_match_distinguishes_scoped_absence_from_no_read_permission(read_allowed):
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"person": ("person:c",), "person_region": ("region:a",)},
    )

    def authorize(a, action, kind, source, creation, at):
        if kind == "person_region" and not read_allowed:
            raise FamilyFactAuthorityDenied("not granted")
        fields = ("person_source_key",) if kind == "person_region" else ("display_name",)
        return Permit(a, FamilyFactTarget(kind, source, fields, a.authorization_version), fields)

    r = FamilyStaffRepository(
        projection=CandidateProjection({}, {("region:a", "person_source_key"): "person:a"}),
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=authorize,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )
    ref = r.reference_for(actor=actor(), fact_kind="person", source_key="person:c")
    result = r.query(
        actor=actor(), fact_kind="person_region", related_field="person_ref", related_ref=ref
    )
    assert result["status"] == ("no_current_records" if read_allowed else "forbidden")
    assert result["records"] == [] and result["coverage"] == "unknown"


def test_projection_cannot_return_fields_outside_signed_scope():
    def auth(a, action, kind, source, creation, at):
        target = FamilyFactTarget(kind, source, ("status",), a.authorization_version, creation)
        return Permit(a, target, ("status",))

    r = FamilyStaffRepository(
        projection=Projection(payload()),
        configuration=config(),
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_args: None,
        clock=lambda: NOW,
    )
    reference = r.reference_for(actor=actor(), fact_kind="student", source_key="student:a")
    with pytest.raises(ValueError, match="unapproved"):
        r.query(actor=actor(), fact_kind="student", reference=reference)


class CandidateProjection:
    def __init__(self, names, related=None):
        self.names = names
        self.related = related or {}
        self.candidate_calls = []
        self.read_calls = 0

    def candidates(self, **kw):
        kw.pop("before_read")()
        groups = kw.pop("source_groups")()
        sources = tuple(source for group, _fields in groups for source in group)
        fields_by_source = {source: fields for group, fields in groups for source in group}
        self.candidate_calls.append(
            (sources, kw["match_field"], kw["match_value"], kw["factual_at"], kw["limit"])
        )
        if kw["match_field"] == "display_name":
            matched = [source for source in sources if self.names.get(source) == kw["match_value"]]
            kind = "person"
        else:
            matched = [
                source
                for source in sources
                if self.related.get((source, kw["match_field"])) == kw["match_value"]
            ]
            kind = kw["fact_kind"]
        rows = []
        for source in matched[: kw["limit"]]:
            content = (
                {"display_name": self.names[source]}
                if kind == "person"
                else {
                    "person_source_key": self.related[(source, "person_source_key")],
                    "status": "active",
                }
            )
            content = {
                key: value for key, value in content.items() if key in fields_by_source[source]
            }
            value = payload(source, content=content)
            value["fact_kind"] = kind
            value["label"] = "人员 · " + self.names[source] if kind == "person" else "学生"
            rows.append({"source_key": source, "label": value["label"], "payload": value})
        kw.pop("after_read")()
        return tuple(rows)

    def read(self, **kw):
        self.read_calls += 1
        kw.pop("before_read")()
        source = kw["source_key"]
        kw.pop("field_names")()
        kw.pop("after_read")()
        kind = kw["fact_kind"]
        content = (
            {"display_name": self.names[source]}
            if kind == "person"
            else {
                "person_source_key": self.related[(source, "person_source_key")],
                "status": "active",
            }
        )
        value = payload(source, content=content)
        value["fact_kind"] = kind
        value["label"] = "人员 · " + self.names[source] if kind == "person" else "学生"
        return value


class NumberProjection:
    def __init__(self):
        self.calls = []

    def candidates(self, **kw):
        kw.pop("before_read")()
        groups = kw.pop("source_groups")()
        self.calls.append((kw, groups))
        kw.pop("after_read")()
        return ()

    def read(self, **kw):
        pytest.fail("Number selection must not enumerate complete records")


def number_repository(projection, fields=("student_number", "enrollment_status")):
    def auth(a, action, kind, source, creation, at):
        assert action == "read" and kind == "student_school_relationship"
        target = FamilyFactTarget(kind, source, fields, a.authorization_version, creation)
        return Permit(a, target, fields)

    return FamilyStaffRepository(
        projection=projection,
        configuration=FamilyConfiguration(
            approval_reference="F3",
            scope_bindings=(),
            source_catalog={"student_school_relationship": ("school:a", "school:b")},
        ),
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )


def test_number_selection_is_exact_timed_and_limited_before_projection():
    projection = NumberProjection()
    at = NOW - timedelta(days=3)
    result = number_repository(projection).query(
        actor=actor(),
        fact_kind="student_school_relationship",
        student_number="F3-A001",
        requested_fields=("enrollment_status",),
        as_of=at,
        limit=2,
    )
    assert result["records"] == []
    call, groups = projection.calls[0]
    assert call["match_field"] == "student_number"
    assert call["match_value"] == "F3-A001" and call["factual_at"] == at
    assert call["limit"] == 3
    assert groups == ((("school:a", "school:b"), ("student_number", "enrollment_status")),)


def test_number_filter_requires_field_permission_before_any_projection():
    projection = NumberProjection()
    with pytest.raises(FamilyFactAuthorityDenied):
        number_repository(projection, ("enrollment_status",)).query(
            actor=actor(),
            fact_kind="student_school_relationship",
            student_number="F3-A001",
        )
    assert projection.calls == []


@pytest.mark.parametrize(
    "selection",
    [
        {"fact_kind": "person", "student_number": "F3-A001"},
        {"fact_kind": "student_school_relationship", "student_number": " "},
        {
            "fact_kind": "student_school_relationship",
            "student_number": "F3-A001",
            "query_text": "甲",
        },
        {
            "fact_kind": "student_school_relationship",
            "student_number": "F3-A001",
            "reference": "opaque",
        },
    ],
)
def test_invalid_number_selection_never_reaches_projection(selection):
    projection = NumberProjection()
    with pytest.raises(ValueError):
        number_repository(projection).query(actor=actor(), **selection)
    assert projection.calls == []


def test_formal_person_name_uses_authorized_sql_candidates_and_tracks_rename_and_homonyms():
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"person": ("person:a", "person:b")},
    )
    projection = CandidateProjection({"person:a": "新姓名", "person:b": "新姓名"})

    def auth(a, action, kind, source, creation, at):
        target = FamilyFactTarget(
            kind, source, ("display_name",), a.authorization_version, creation
        )
        return Permit(a, target, ("display_name",))

    r = FamilyStaffRepository(
        projection=projection,
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_args: None,
        clock=lambda: NOW,
    )
    assert (
        r.query(actor=actor(), fact_kind="person", query_text="旧姓名")["status"]
        == "object_unresolved"
    )
    same_name = r.query(actor=actor(), fact_kind="person", query_text="新姓名", limit=1)
    assert same_name["status"] == "ambiguous" and same_name["has_more"]
    assert projection.candidate_calls[0][1:3] == ("display_name", "旧姓名")
    assert projection.candidate_calls[1][0] == ("person:a", "person:b")
    assert projection.read_calls == 0


def test_candidate_task_fields_narrow_sql_before_read_without_granting_hidden_match_fields():
    projection = CandidateProjection({"person:a": "正式姓名"})
    captured = []
    original = projection.candidates

    def candidates(**kw):
        groups = kw["source_groups"]

        def selected():
            values = groups()
            captured.extend(values)
            return values

        kw["source_groups"] = selected
        return original(**kw)

    projection.candidates = candidates
    allowed = {"names": True}

    def auth(a, action, kind, source, creation, at):
        fields = ("display_name", "business_code") if allowed["names"] else ("business_code",)
        target = FamilyFactTarget(kind, source, fields, a.authorization_version, creation)
        return Permit(a, target, fields)

    repository = FamilyStaffRepository(
        projection=projection,
        configuration=FamilyConfiguration(
            approval_reference="F3", scope_bindings=(), source_catalog={"person": ("person:a",)}
        ),
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_: None,
        clock=lambda: NOW,
    )
    result = repository.query(
        actor=actor(), fact_kind="person", query_text="正式姓名", requested_fields=("display_name",)
    )
    assert result["status"] == "known"
    assert captured == [(("person:a",), ("display_name",))]
    allowed["names"] = False
    denied = repository.query(
        actor=actor(), fact_kind="person", query_text="正式姓名", requested_fields=("display_name",)
    )
    assert denied["status"] == "forbidden" and denied["records"] == []


def test_related_query_requires_both_current_scopes_and_relation_field_before_sql():
    cfg = FamilyConfiguration(
        approval_reference="F3",
        scope_bindings=(),
        source_catalog={"person": ("person:a",), "student": ("student:a",)},
    )
    projection = CandidateProjection(
        {"person:a": "学生甲"},
        {("student:a", "person_source_key"): "person:a"},
    )
    revoked = {"person": False}
    include_relation = {"value": True}

    def auth(a, action, kind, source, creation, at):
        if kind == "person" and revoked["person"]:
            raise FamilyFactAuthorityDenied("related object revoked")
        fields = (
            ("person_source_key", "status")
            if kind == "student" and include_relation["value"]
            else ("status",)
            if kind == "student"
            else ("display_name",)
        )
        target = FamilyFactTarget(kind, source, fields, a.authorization_version, creation)
        return Permit(a, target, fields)

    r = FamilyStaffRepository(
        projection=projection,
        configuration=cfg,
        reference_key=b"x" * 32,
        authorize_scope=auth,
        revalidate=lambda *_args: None,
        clock=lambda: NOW,
    )
    person_ref = r.reference_for(actor=actor(), fact_kind="person", source_key="person:a")
    found = r.query(
        actor=actor(),
        fact_kind="student",
        related_field="person_ref",
        related_ref=person_ref,
    )
    assert found["status"] == "known" and len(found["records"]) == 1
    assert projection.candidate_calls[-1][1:3] == ("person_source_key", "person:a")
    assert projection.read_calls == 0
    include_relation["value"] = False
    assert (
        r.query(
            actor=actor(),
            fact_kind="student",
            related_field="person_ref",
            related_ref=person_ref,
        )["status"]
        == "forbidden"
    )
    revoked["person"] = True
    with pytest.raises(FamilyFactAuthorityDenied):
        r.query(
            actor=actor(),
            fact_kind="student",
            related_field="person_ref",
            related_ref=person_ref,
        )
