"""A complete claim needs a typed, versioned mother-set proof."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.platform.school_facts.contracts import CanonicalFactChangeV1
from app.platform.school_facts.f7_coverage_basis import mother_set_digest

AT = datetime(2026, 9, 24, tzinfo=UTC)


def _change(*, basis=None, target_kind="student", target_key="f3:student-a"):
    return {
        "schema_version": 4,
        "operation": "upsert",
        "fact_kind": "fact_quality",
        "source_key": "f7:quality:coverage-a",
        "expected_version": 0,
        "effective_at": AT,
        "origin": {"sheet_code": "f7", "row_number": 1},
        "content": {
            "quality_kind": "coverage",
            "target_fact_kind": target_kind,
            "owner_kind": "person",
            "person_source_key": "person:a",
            "target_source_key": target_key,
            "basis_reference": "正式核查记录",
            "responsibility": "学校正式维护人",
            "verified_at": AT,
            "coverage_status": "complete",
            **({"coverage_basis": basis} if basis is not None else {}),
        },
    }


def _basis():
    return {
        "schema_version": 1,
        "school_id": uuid4(),
        "owner_kind": "person",
        "owner_source_key": "person:a",
        "target_fact_kind": "student",
        "target_source_key": "f3:student-a",
        "field_name": None,
        "purpose": None,
        "contact_kind": None,
        "valid_from": AT,
        "valid_to": None,
        "school_fact_version": 12,
        "member_count": 1,
        "member_sha256": "0" * 64,
    }


def test_v4_complete_requires_typed_exact_basis():
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(_change())
    parsed = CanonicalFactChangeV1.model_validate(_change(basis=_basis()))
    assert parsed.content.coverage_basis.school_fact_version == 12


@pytest.mark.parametrize("mutation", [
    {"owner_source_key": "person:b"},
    {"target_fact_kind": "guardian_relationship"},
    {"target_source_key": "f3:student-b"},
    {"field_name": "status"},
    {"valid_from": "2026-09-25T00:00:00Z"},
    {"member_sha256": "not-a-digest"},
    {"member_count": -1},
])
def test_v4_complete_rejects_basis_coordinate_tampering(mutation):
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(_change(basis=_basis() | mutation))


def test_non_complete_cannot_borrow_complete_basis():
    content = _change(basis=_basis())
    content["content"]["coverage_status"] = "partial"
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(content)


def test_digest_distinguishes_equal_count_different_members_and_versions():
    first = mother_set_digest((("one", 1), ("two", 1)))
    assert first == mother_set_digest((("two", 1), ("one", 1)))
    assert first != mother_set_digest((("one", 1), ("three", 1)))
    assert first != mother_set_digest((("one", 1), ("two", 2)))


def test_verified_basis_accepts_nullable_coordinates_omitted_by_writer_json():
    from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis

    school = uuid4()
    raw = _basis() | {
        "school_id": str(school), "valid_from": AT.isoformat(),
        "member_sha256": mother_set_digest((("f3:student-a", 3),))[1],
    }
    for nullable in ("field_name", "purpose", "contact_kind", "valid_to"):
        raw.pop(nullable)
    assert verify_coverage_basis(
        raw, school_id=school, owner_kind="person", owner_source_key="person:a",
        target_fact_kind="student", target_source_key="f3:student-a", field_name=None,
        purpose=None, contact_kind=None, factual_at=AT, school_fact_version=13,
        members=(("f3:student-a", 3),),
    )
    # Another school's directory change increments the broad revision but does
    # not change this exact mother set or its current permission and time checks.
    assert verify_coverage_basis(
        raw, school_id=school, owner_kind="person", owner_source_key="person:a",
        target_fact_kind="student", target_source_key="f3:student-a", field_name=None,
        purpose=None, contact_kind=None, factual_at=AT, school_fact_version=17,
        members=(("f3:student-a", 3),),
    )


def test_writer_recomputes_exact_member_under_school_lock(monkeypatch):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school = uuid4()
    basis = _basis() | {
        "school_id": school,
        "member_count": 1,
        "member_sha256": mother_set_digest((("f3:student-a", 3),))[1],
    }
    change = CanonicalFactChangeV1.model_validate(_change(basis=basis))
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence,
        "_current_global_fact_version",
        staticmethod(lambda session, school_id: 12),
    )

    class Session:
        def __init__(self, version):
            self.version = version
            self.queries = []

        def execute(self, statement, parameters):
            self.queries.append((str(statement), parameters))
            return self

        def scalar_one_or_none(self):
            return self.version

    good = Session(3)
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        good, school, change, {"person_id": uuid4()}
    )
    assert len(good.queries) == 1
    assert "source_key=:target" in good.queries[0][0]
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(4), school, change, {"person_id": uuid4()}
        )


@pytest.mark.parametrize("kind", [
    "student_school_relationship", "login_mapping", "student_contact",
    "guardian_relationship", "person_region",
])
def test_writer_rechecks_each_exact_family_target_and_owner(monkeypatch, kind):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school, owner, target_id = uuid4(), uuid4(), uuid4()
    key = f"{kind}:a"
    basis = _basis() | {
        "school_id": school, "target_fact_kind": kind,
        "target_source_key": key,
        "member_sha256": mother_set_digest(((key, 3),))[1],
    }
    change = CanonicalFactChangeV1.model_validate(
        _change(basis=basis, target_kind=kind, target_key=key)
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_current_global_fact_version",
        staticmethod(lambda session, school_id: 12),
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_f3_quality_target_at",
        staticmethod(lambda session, school_id, selected_kind, selected_key, at: target_id),
    )

    class Session:
        def __init__(self, version=3):
            self.version = version
            self.queries = []

        def execute(self, statement, parameters):
            self.queries.append((str(statement), parameters))
            return self

        def scalar_one_or_none(self):
            return self.version

    good = Session()
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        good, school, change, {"person_id": owner}
    )
    assert len(good.queries) == 1
    assert good.queries[0][1]["owner"] == owner
    assert good.queries[0][1]["target_id"] == target_id
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(4), school, change, {"person_id": owner}
        )


def test_writer_rejects_legacy_complete_without_typed_basis():
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school = uuid4()
    content = _change()
    content["schema_version"] = 2
    content["content"].update(
        target_fact_kind="organization_location",
        owner_kind="organization",
        organization_source_key="org:a",
        target_source_key=None,
        person_source_key=None,
        purpose="office",
    )
    old = CanonicalFactChangeV1.model_validate(content)
    with pytest.raises(SchoolFactChangeRejected, match="受控母集"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            object(), school, old, {}
        )


def test_writer_rechecks_f2_exact_target_version_under_school_lock(monkeypatch):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school = uuid4()
    basis = _basis() | {
        "school_id": school, "owner_kind": "organization", "owner_source_key": "org:a",
        "target_fact_kind": "official_contact", "target_source_key": "contact:a",
        "member_sha256": mother_set_digest((("contact:a", 4),))[1],
    }
    material = _change(basis=basis, target_kind="official_contact", target_key="contact:a")
    material["content"].update(
        owner_kind="organization", organization_source_key="org:a", person_source_key=None,
    )
    change = CanonicalFactChangeV1.model_validate(material)
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_current_global_fact_version",
        staticmethod(lambda session, school_id: 12),
    )
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_f2_content_at",
        staticmethod(lambda session, school_id, kind, key, at: (uuid4(), session.version)),
    )

    class Session:
        def __init__(self, version):
            self.version = version

    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        Session(4), school, change, {"organization_unit_id": uuid4()}
    )
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(5), school, change, {"organization_unit_id": uuid4()}
        )


def test_v4_f2_owner_collection_allows_verified_empty_set():
    basis = _basis() | {
        "owner_kind": "organization",
        "owner_source_key": "org:a",
        "target_fact_kind": "organization_location",
        "target_source_key": None,
        "purpose": "office",
        "member_count": 0,
        "member_sha256": mother_set_digest(())[1],
    }
    material = _change(basis=basis, target_kind="organization_location", target_key=None)
    material["content"].update(
        owner_kind="organization",
        organization_source_key="org:a",
        person_source_key=None,
        target_source_key=None,
        purpose="office",
    )
    parsed = CanonicalFactChangeV1.model_validate(material)
    assert parsed.content.coverage_basis.member_count == 0


def _grade_group_collection_basis(school, members):
    count, digest = mother_set_digest(members)
    return {
        "schema_version": 2,
        "school_id": school,
        "owner_kind": "education",
        "owner_source_key": "calendar:campus-a",
        "target_fact_kind": "education_grade_group",
        "target_source_key": None,
        "field_name": "grade_source_key",
        "purpose": None,
        "contact_kind": None,
        "campus_source_key": "campus:a",
        "academic_year": 2026,
        "authorized_source_keys": [key for key, _version in members],
        "valid_from": AT,
        "valid_to": None,
        "school_fact_version": 12,
        "member_count": count,
        "member_sha256": digest,
    }


def _grade_group_collection_change(basis):
    payload = _change(basis=basis, target_kind="education_grade_group", target_key=None)
    payload["content"].update(
        owner_kind="education", person_source_key=None,
        education_target_kind="education_calendar_rule",
        education_target_source_key="calendar:campus-a",
        target_source_key=None, field_name="grade_source_key",
        campus_source_key="campus:a", academic_year=2026,
    )
    return payload


def test_grade_group_collection_requires_exact_campus_year_and_authorized_set():
    basis = _grade_group_collection_basis(uuid4(), (("group:a", 3),))
    parsed = CanonicalFactChangeV1.model_validate(_grade_group_collection_change(basis))
    assert parsed.content.coverage_basis.schema_version == 2
    for mutation in (
        {"campus_source_key": "campus:b"},
        {"academic_year": 2027},
        {"authorized_source_keys": []},
        {"target_source_key": "group:a"},
    ):
        with pytest.raises(ValidationError):
            CanonicalFactChangeV1.model_validate(
                _grade_group_collection_change(basis | mutation)
            )


def test_writer_rechecks_whole_grade_group_collection_not_authorized_subset(monkeypatch):
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school = uuid4()
    basis = _grade_group_collection_basis(school, (("group:a", 3), ("group:b", 2)))
    change = CanonicalFactChangeV1.model_validate(_grade_group_collection_change(basis))
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_current_global_fact_version",
        staticmethod(lambda session, school_id: 12),
    )

    class Session:
        def __init__(self, members, *, future_boundary=False):
            self.members = members
            self.future_boundary = future_boundary
            self.queries = []

        def execute(self, statement, parameters):
            self.queries.append((str(statement), parameters))
            return self

        def all(self):
            return [SimpleNamespace(source_key=key, version=version)
                    for key, version in self.members]

        def scalar_one_or_none(self):
            return 1 if self.future_boundary else None

    good = Session((("group:a", 3), ("group:b", 2)))
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        good, school, change, {"education_target_id": uuid4()}
    )
    assert "school_education_facts" in good.queries[0][0]
    assert good.queries[0][1]["campus"] == "campus:a"
    assert good.queries[0][1]["academic_year"] == "2026"
    for members in (
        (("group:a", 3),),
        (("group:a", 3), ("group:b", 2), ("group:c", 1)),
        (("group:a", 3), ("group:b", 3)),
    ):
        with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
            SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
                Session(members), school, change, {"education_target_id": uuid4()}
            )
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session((("group:a", 3), ("group:b", 2)), future_boundary=True),
            school, change, {"education_target_id": uuid4()},
        )


def test_grade_group_collection_release_requires_same_school_revision_and_exact_coordinates():
    from app.platform.school_facts.f7_coverage_basis import (
        verify_grade_group_collection_basis,
    )

    school = uuid4()
    members = (("group:a", 3), ("group:b", 2))
    basis = _grade_group_collection_basis(school, members)
    common = {
        "school_id": school, "owner_source_key": "calendar:campus-a",
        "campus_source_key": "campus:a", "academic_year": 2026,
        "field_name": "grade_source_key", "factual_at": AT,
        "school_fact_version": 13, "members": members,
    }
    assert verify_grade_group_collection_basis(basis, **common)
    for changed in (
        {"campus_source_key": "campus:b"},
        {"academic_year": 2027},
        {"field_name": "display_name"},
        {"school_fact_version": 14},
        {"members": (("group:a", 3),)},
        {"members": (("group:a", 3), ("group:b", 3))},
    ):
        assert not verify_grade_group_collection_basis(basis, **(common | changed))


def test_authorized_grade_group_query_consumes_complete_only_in_exact_scope():
    from types import SimpleNamespace

    from app.composition.school_fact_education_views import EducationMaintenanceViews
    from tests.test_secure_reader_projection import _actor

    person = _actor()
    basis = _grade_group_collection_basis(person.school_id, (("group:a", 3),))
    rows = ({
        "source_key": "group:a", "fact_kind": "education_grade_group",
        "version": 3, "segments": [{
            "effective_at": AT.isoformat(), "valid_to": None,
            "content": {"display_name": "初中年级组", "grade_source_key": "grade:7",
                        "campus_source_key": "campus:a", "academic_year": 2026},
        }],
    },)
    calls = []

    class Repository:
        version = 13
        denied = False
        authority = SimpleNamespace(require_scope=lambda *args, **kwargs: SimpleNamespace(
            scope=SimpleNamespace(object_keys=("calendar:campus-a",))
        ))

        def clock(self):
            return AT

        def read_records(self, _actor, kind, **kwargs):
            calls.append((kind, kwargs))
            proof = SimpleNamespace(
                school_fact_version=self.version,
                revalidate=lambda actor=None: (_ for _ in ()).throw(PermissionError("revoked"))
                if self.denied else None,
            )
            return SimpleNamespace(records=rows, as_of=AT,
                                   school_fact_version=self.version, proof=proof)

        def _reference(self, _actor, kind, key, creation):
            return f"f4:{kind}:{key}"

    repository = Repository()
    quality_denied = {"value": False}

    def quality_for(_actor, **kwargs):
        if quality_denied["value"]:
            from app.composition.school_fact_education_quality import EducationDependencyDenied
            raise EducationDependencyDenied("quality not granted")
        calls.append(("quality", kwargs))
        return {
            "school_fact_version": repository.version,
            "proof": SimpleNamespace(school_fact_version=repository.version,
                                     revalidate=lambda: None),
            "statements": [{"content": {
                "quality_kind": "coverage", "coverage_status": "complete",
                "education_target_source_key": "calendar:campus-a",
                "education_target_kind": "education_calendar_rule",
                "academic_year": 2026,
                "target_fact_kind": "education_grade_group",
                "target_source_key": None, "field_name": "grade_source_key",
                "coverage_basis": basis,
            }}],
        }

    adapter = SimpleNamespace(
        repository=repository,
        external=SimpleNamespace(quality_for=quality_for),
        resolve_reference=lambda *_args: "campus:a",
        public_fields=lambda _actor, content: content,
    )
    view = EducationMaintenanceViews(adapter, directory=None, family=None)
    kwargs = {"actor": person, "fact_kind": "education_grade_group",
              "campus_ref": "f2:location:a", "academic_year": 2026, "limit": 20}
    answer = view.query_with_proof(**kwargs)
    assert answer.public_result["coverage"]["status"] == "complete"
    assert calls[0][1]["filter"] == {
        "campus_source_key": "campus:a", "academic_year": 2026,
    }
    repository.version = 14
    stale = view.query_with_proof(**kwargs)
    assert stale.public_result["coverage"]["status"] == "unknown"
    quality_denied["value"] = True
    no_quality_grant = view.query_with_proof(**kwargs)
    assert no_quality_grant.public_result["coverage"]["status"] == "unknown"
    repository.denied = True
    with pytest.raises(PermissionError, match="revoked"):
        view.query_with_proof(**kwargs)


def test_grade_group_year_filter_is_only_a_bounded_exact_integer():
    from app.composition.school_fact_education_reader import _read_filter

    assert _read_filter("education_grade_group", {"academic_year": 2026}) == {
        "academic_year": 2026,
    }
    for kind, value in (
        ("education_grade_group", "2026"),
        ("education_grade_group", [2026]),
        ("education_grade_group", True),
        ("education_grade_group", 1899),
        ("education_class", 2026),
    ):
        with pytest.raises(ValueError):
            _read_filter(kind, {"academic_year": value})


def test_writer_rechecks_all_f2_owner_members_not_only_an_authorized_page(monkeypatch):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    school = uuid4()
    basis = _basis() | {
        "school_id": school,
        "owner_kind": "organization",
        "owner_source_key": "org:a",
        "target_fact_kind": "organization_location",
        "target_source_key": None,
        "purpose": "office",
        "member_count": 2,
        "member_sha256": mother_set_digest((("office:a", 1), ("office:b", 1)))[1],
    }
    material = _change(basis=basis, target_kind="organization_location", target_key=None)
    material["content"].update(
        owner_kind="organization",
        organization_source_key="org:a",
        person_source_key=None,
        target_source_key=None,
        purpose="office",
    )
    change = CanonicalFactChangeV1.model_validate(material)
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence,
        "_current_global_fact_version",
        staticmethod(lambda session, school_id: 12),
    )

    class Session:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, statement, parameters):
            return self

        def all(self):
            return self.rows

    from types import SimpleNamespace

    initial = (SimpleNamespace(source_key="office:a", version=1),
               SimpleNamespace(source_key="office:b", version=1))
    SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
        Session(initial), school, change, {"organization_unit_id": uuid4()}
    )
    replacement = (SimpleNamespace(source_key="office:a", version=1),
                   SimpleNamespace(source_key="office:c", version=1))
    with pytest.raises(SchoolFactChangeRejected, match="母集已变化"):
        SqlAlchemySchoolFactPersistence._verify_complete_coverage_basis(
            Session(replacement), school, change, {"organization_unit_id": uuid4()}
        )


def test_f2_product_builds_complete_proof_from_authorized_full_owner_page():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_f2_query_repository import (
        F2FactQueryResult,
        F2MaintenanceTarget,
        F2PublicFact,
    )
    from tests.test_secure_reader_projection import _actor

    actor = _actor()
    current = F2PublicFact(
        "f2:organization_membership:signed-a", "organization_membership",
        {"organization_ref": "f2:organization:signed", "membership_kind": "member"},
        3, AT, None, "manual", "publication:a", AT,
    )

    class Repository:
        def prepare_target(self, **kwargs):
            return F2MaintenanceTarget(
                "quality:new", 0, 12, {},
                F2PublicFact("f2create:fact_quality:signed", "fact_quality", {},
                             0, AT, None, "pending", "publication:pending", AT),
            )

        def resolve_field_references(self, **kwargs):
            return {
                key: value for key, value in kwargs["fields"].items()
                if key != "organization_ref"
            } | {"organization_source_key": "org:a"}

        def reference_for(self, **kwargs):
            assert kwargs["fact_kind"] == "organization"
            return "f2:organization:signed"

        def query(self, **kwargs):
            assert kwargs["fact_kind"] == "organization_membership"
            assert kwargs["related_ref"] == "f2:organization:signed"
            return F2FactQueryResult("known", (current,), 1, False, None, AT,
                                     factual_at=AT, read_version=12)

        def source_for(self, **kwargs):
            assert kwargs["fact_kind"] == "organization_membership"
            return "member:a"

    payload = FactChangeInput.model_validate({
        "fact_kind": "fact_quality", "creation_ref": "f2create:fact_quality:signed",
        "fields": {
            "quality_kind": "coverage", "coverage_status": "complete",
            "target_fact_kind": "organization_membership", "owner_kind": "organization",
            "organization_ref": "f2:organization:signed", "basis_reference": "学校核查",
            "responsibility": "维护人", "verified_at": AT.isoformat(),
        },
        "reason": "按学校正式名单核对",
    })
    resolved = DirectoryProductAdapter(Repository()).resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT,
    )
    assert resolved.change_set.schema_version == 4
    proof = resolved.change_set.changes[0].content.coverage_basis
    assert proof.owner_source_key == "org:a"
    assert proof.member_sha256 == mother_set_digest((("member:a", 3),))[1]


def test_f2_product_builds_exact_target_proof_from_authorized_selection():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_f2_query_repository import F2MaintenanceTarget, F2PublicFact
    from tests.test_secure_reader_projection import _actor

    actor = _actor()
    target = F2PublicFact(
        "f2:official_contact:signed", "official_contact", {"channel": "email"},
        4, AT, None, "manual", "publication:contact", AT,
    )

    class Repository:
        def prepare_target(self, **kwargs):
            if kwargs["fact_kind"] == "official_contact":
                assert kwargs["target_ref"] == "f2:official_contact:signed"
                return F2MaintenanceTarget("contact:a", 4, 12, {}, target)
            return F2MaintenanceTarget(
                "quality:new", 0, 12, {},
                F2PublicFact("f2create:fact_quality:signed", "fact_quality", {},
                             0, AT, None, "pending", "publication:pending", AT),
            )

        def resolve_field_references(self, **kwargs):
            return {key: value for key, value in kwargs["fields"].items()
                    if key != "organization_ref"} | {"organization_source_key": "org:a"}

        def reference_for(self, **kwargs):
            assert kwargs == {"actor": actor, "fact_kind": "official_contact",
                             "source_key": "contact:a"}
            return "f2:official_contact:signed"

    payload = FactChangeInput.model_validate({
        "fact_kind": "fact_quality", "creation_ref": "f2create:fact_quality:signed",
        "fields": {
            "quality_kind": "coverage", "coverage_status": "complete",
            "target_fact_kind": "official_contact", "target_source_key": "contact:a",
            "owner_kind": "organization", "organization_ref": "f2:organization:signed",
            "basis_reference": "学校核查", "responsibility": "维护人",
            "verified_at": AT.isoformat(),
        },
        "reason": "核对正式联系方式",
    })
    resolved = DirectoryProductAdapter(Repository()).resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT,
    )
    proof = resolved.change_set.changes[0].content.coverage_basis
    assert proof.target_source_key == "contact:a"
    assert proof.member_sha256 == mother_set_digest((("contact:a", 4),))[1]


def test_f2_end_stays_on_existing_contract_without_complete_proof():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_f2_query_repository import F2MaintenanceTarget, F2PublicFact
    from tests.test_secure_reader_projection import _actor

    class Repository:
        def prepare_target(self, **kwargs):
            return F2MaintenanceTarget(
                "contact:a", 4, 12, {},
                F2PublicFact("f2:official_contact:signed", "official_contact", {},
                             4, AT, None, "manual", "publication:contact", AT),
            )

    payload = FactChangeInput.model_validate({
        "fact_kind": "official_contact", "target_ref": "f2:official_contact:signed",
        "operation": "end", "fields": {}, "reason": "正式结束",
    })
    resolved = DirectoryProductAdapter(Repository()).resolve_change(
        actor=_actor(), payload=payload, request_id=uuid4(), effective_at=AT,
    )
    assert resolved.change_set.schema_version == 2
    assert resolved.change_set.changes[0].content is None


def test_f2_exact_quality_only_promotes_when_current_target_matches_proof():
    from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
    from app.composition.school_fact_directory_product import FactQueryInput
    from app.composition.school_fact_f2_query_repository import F2FactQueryResult, F2PublicFact
    from tests.test_secure_reader_projection import _actor

    actor = _actor()
    basis = _basis() | {
        "school_id": str(actor.school_id), "owner_kind": "organization",
        "owner_source_key": "org:a", "target_fact_kind": "official_contact",
        "valid_from": AT.isoformat(),
        "target_source_key": "contact:a", "member_count": 1,
        "member_sha256": mother_set_digest((("contact:a", 4),))[1],
    }
    quality = F2PublicFact(
        "f2:fact_quality:signed", "fact_quality", {
            "owner_kind": "organization", "organization_ref": "f2:organization:signed",
            "target_fact_kind": "official_contact",
            "target_ref": "f2:official_contact:signed",
            "quality_kind": "coverage", "coverage_status": "complete",
            "verified_at": AT, "coverage_basis": basis,
        }, 1, AT, None, "manual", "publication:quality", AT,
    )

    class Repository:
        version = 4

        def query(self, **kwargs):
            if kwargs["fact_kind"] == "fact_quality":
                return F2FactQueryResult("known", (quality,), 1, False, None, AT,
                                         factual_at=AT, read_version=13)
            contact = F2PublicFact(
                "f2:official_contact:signed", "official_contact",
                {"organization_ref": "f2:organization:signed", "channel": "email"},
                self.version, AT, None, "manual", "publication:contact", AT,
            )
            return F2FactQueryResult("known", (contact,), 1, False, None, AT,
                                     factual_at=AT, read_version=13)

        def source_for(self, **kwargs):
            return {"organization": "org:a", "official_contact": "contact:a"}[
                kwargs["fact_kind"]
            ]

    repository = Repository()
    from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis
    assert verify_coverage_basis(
        basis, school_id=actor.school_id, owner_kind="organization", owner_source_key="org:a",
        target_fact_kind="official_contact", target_source_key="contact:a", field_name=None,
        purpose=None, contact_kind=None, factual_at=AT, school_fact_version=13,
        members=(("contact:a", 4),),
    )
    request = FactQueryInput(
        fact_kind="official_contact", reference="f2:official_contact:signed"
    )
    verified = DirectoryProductAdapter(repository).query(actor=actor, payload=request)
    assert verified["quality"]["coverage"] == "complete", verified["quality"]["statements"]
    repository.version = 5
    changed = DirectoryProductAdapter(repository).query(actor=actor, payload=request)
    assert changed["quality"]["coverage"] == "unknown"


def test_f7_education_quality_sql_only_adds_basis_to_existing_authorized_rows():
    from pathlib import Path

    migration = (
        Path(__file__).parents[2]
        / "alembic/versions/20260924_50_f7_education_quality_coverage_basis.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260924_49"' in migration
    assert "coverage_basis" in migration
    assert "school_fact_f4_dependencies" in migration
    assert "school_fact_f5_dependencies" in migration
    assert "f5_child_projection" in migration
    assert "CREATE OR REPLACE FUNCTION" in migration
    assert "GRANT EXECUTE" not in migration or "campus_ai_school_fact_reader" in migration
