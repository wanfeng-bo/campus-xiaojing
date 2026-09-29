"""Execute temporal Writer SQL against isolated in-memory relational fixtures.

SQLite only substitutes UUID transport and the PostgreSQL timestamptz cast;
these tests do not claim PostgreSQL permission or transaction evidence.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeV2

SCHOOL = uuid4()
SEP = datetime(2026, 9, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 6, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)
NOV = datetime(2026, 11, 1, tzinfo=UTC)


class RelationalSession:
    def __init__(self):
        self.connection = create_engine("sqlite://").connect()
        for ddl in (
            "CREATE TABLE school_fact_change_lineage (school_id TEXT, fact_kind TEXT, "
            "fact_source_key TEXT, target_id TEXT, target_version INTEGER, "
            "operation TEXT, unit_id TEXT, batch_id TEXT)",
            "CREATE TABLE master_data_import_batches (school_id TEXT, id TEXT, "
            "base_fact_version INTEGER)",
            "CREATE TABLE master_data_import_units (school_id TEXT, id TEXT, payload TEXT)",
            "CREATE TABLE official_contact_points (school_id TEXT, id TEXT, version INTEGER, "
            "valid_from TEXT, valid_to TEXT, lifecycle_status TEXT)",
            "CREATE TABLE organization_locations (school_id TEXT, id TEXT, version INTEGER, "
            "valid_from TEXT, valid_to TEXT, revoked_at TEXT)",
        ):
            self.connection.execute(text(ddl))

    def execute(self, query, parameters=None) -> Any:
        converted = {
            k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v
            for k, v in (parameters or {}).items()
        }
        return self.connection.execute(
            text(
                str(query)
                .replace("::timestamptz", "")
                .replace("CAST(:until AS timestamptz)", ":until")
            ),
            converted,
        )

    def fact(self, key, start, end=None, *, version=1, kind="organization_location", revoked=None):
        identifier = uuid4()
        table = (
            "official_contact_points" if kind == "official_contact" else "organization_locations"
        )
        self.execute(
            f"INSERT INTO {table} VALUES (:school, :id, :version, :start, :end, :extra)",
            dict(
                school=SCHOOL,
                id=identifier,
                version=version,
                start=start,
                end=end,
                extra="active" if kind == "official_contact" else revoked,
            ),
        )
        self.lineage(key, identifier, version, "upsert", start, kind)
        return identifier

    def lineage(self, key, identifier, version, operation, at, kind="organization_location"):
        import json

        unit = uuid4()
        batch = uuid4()
        self.execute(
            "INSERT INTO master_data_import_batches SELECT :school,:id,"
            "COALESCE(MAX(base_fact_version),0)+1 FROM master_data_import_batches",
            dict(school=SCHOOL, id=batch),
        )
        self.execute(
            "INSERT INTO master_data_import_units VALUES (:school, :id, :payload)",
            dict(school=SCHOOL, id=unit, payload=json.dumps({"effective_at": at.isoformat()})),
        )
        self.execute(
            "INSERT INTO school_fact_change_lineage VALUES "
            "(:school,:kind,:key,:id,:version,:op,:unit,:batch)",
            dict(
                school=SCHOOL,
                kind=kind,
                key=key,
                id=identifier,
                version=version,
                op=operation,
                unit=unit,
                batch=batch,
            ),
        )


def correction(at=NOW):
    return CanonicalFactChangeV2.model_validate(
        dict(
            fact_kind="fact_quality",
            source_key="fix:one",
            operation="upsert",
            expected_version=0,
            effective_at=at,
            origin=dict(sheet_code="quality", row_number=2),
            content=dict(
                quality_kind="correction",
                target_fact_kind="organization_location",
                owner_kind="school",
                target_source_key="old",
                replacement_source_key="new",
                basis_reference="approved",
                responsibility="directory",
                verified_at=at,
                evidence_reference="review:one",
            ),
        )
    )


def validate(db, at=NOW):
    change = correction(at)
    assert change.content is not None
    SqlAlchemySchoolFactPersistence._validate_quality_targets(
        cast(Session, db), SCHOOL, change, change.content.model_dump(exclude_none=True)
    )


@pytest.mark.parametrize("at,end", [(NOW, OCT), (SEP, NOW)])
def test_service_uses_applicable_contact_slice_even_after_future_publication(at, end):
    db = RelationalSession()
    old = db.fact("channel", SEP, OCT, kind="official_contact")
    db.fact("channel", OCT, kind="official_contact", version=2)
    writer = object.__new__(SqlAlchemySchoolFactPersistence)
    captured = {}
    original_execute = db.execute

    def execute(query, parameters=None):
        if str(query).startswith("INSERT INTO school_services"):
            captured.update(query.compile().params)
            return SimpleNamespace()
        if "FROM organization_units" in str(query):
            return SimpleNamespace(scalar_one_or_none=lambda: uuid4())
        return original_execute(query, parameters)

    db.execute = execute
    change = CanonicalFactChangeV2.model_validate(
        dict(
            fact_kind="school_service",
            source_key="service",
            operation="upsert",
            expected_version=0,
            effective_at=at,
            origin=dict(sheet_code="service", row_number=2),
            content=dict(
                display_name="Service",
                organization_source_key="org",
                audience_description="staff",
                service_summary="Information",
                basis_reference="basis",
                official_contact_source_key="channel",
                valid_to=end,
            ),
        )
    )
    writer._upsert_f2(
        session=cast(Session, db),
        school_id=SCHOOL,
        batch_id=uuid4(),
        source_id=uuid4(),
        change=change,
        content={},
        current_id=None,
        current_version=0,
    )
    assert captured["official_contact_id"] == old


@pytest.mark.parametrize(
    "old_end,new_start,new_end",
    [(OCT, OCT, None), (OCT, SEP, None), (NOW, OCT, None), (NOW, SEP, NOW)],
)
def test_correction_rejects_misdated_terminal_or_unusable_replacement(old_end, new_start, new_end):
    db = RelationalSession()
    old = db.fact("old", SEP, old_end)
    db.lineage("old", old, 2, "end", old_end)
    db.fact("new", new_start, new_end)
    with pytest.raises(SchoolFactChangeRejected):
        validate(db)


def test_historical_correction_resolves_past_replacement_even_when_latest_is_ended():
    db = RelationalSession()
    old = db.fact("old", SEP, NOW)
    db.lineage("old", old, 2, "end", NOW)
    db.fact("new", NOW, OCT)
    future = db.fact("new", OCT, NOV, version=2)
    db.lineage("new", future, 3, "end", NOV)
    validate(db)


def test_correction_rejects_replacement_revoked_at_boundary():
    db = RelationalSession()
    old = db.fact("old", SEP, NOW)
    db.lineage("old", old, 2, "end", NOW)
    db.fact("new", SEP, revoked=NOW)
    with pytest.raises(SchoolFactChangeRejected):
        validate(db)


@pytest.mark.parametrize("until", [None, NOV])
def test_service_cannot_outlive_its_specific_contact_content_version(until):
    db = RelationalSession()
    db.fact("channel", SEP, OCT, kind="official_contact")
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._f2_content_at(
            cast(Session, db),
            SCHOOL,
            "official_contact",
            "channel",
            NOW,
            until=until,
            require_whole_window=True,
        )


@pytest.mark.parametrize("scenario", ["overlap", "missing", "wrong_school", "inactive", "terminal"])
def test_contact_reference_fails_closed_on_unusable_or_ambiguous_slice(scenario):
    db = RelationalSession()
    identifier = db.fact("channel", SEP, kind="official_contact")
    school = SCHOOL
    key = "channel"
    if scenario == "overlap":
        db.fact("channel", NOW, kind="official_contact", version=2)
    elif scenario == "missing":
        key = "missing"
    elif scenario == "wrong_school":
        school = uuid4()
    elif scenario == "inactive":
        db.execute("UPDATE official_contact_points SET lifecycle_status='inactive'")
    else:
        db.lineage("channel", identifier, 2, "end", NOW, "official_contact")
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._f2_content_at(
            cast(Session, db),
            school,
            "official_contact",
            key,
            NOW,
        )


@pytest.mark.parametrize("at,accepted", [(NOW, False), (OCT, True)])
def test_same_batch_order_does_not_make_a_future_correction_effective_early(at, accepted):
    from app.platform.master_data_imports.models import MasterDataImportUnit
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV2

    db = RelationalSession()
    old = db.fact("old", SEP)
    fix = correction(at)
    end = CanonicalFactChangeV2.model_validate(
        dict(
            fact_kind="organization_location",
            source_key="old",
            operation="end",
            content=None,
            expected_version=1,
            effective_at=OCT,
            origin=dict(sheet_code="office", row_number=2),
        )
    )
    replacement = CanonicalFactChangeV2.model_validate(
        dict(
            fact_kind="organization_location",
            source_key="new",
            operation="upsert",
            expected_version=0,
            effective_at=OCT,
            origin=dict(sheet_code="office", row_number=3),
            content=dict(
                organization_source_key="org",
                location_source_key="loc",
                purpose="office",
                is_primary=True,
                basis_reference="approved",
            ),
        )
    )
    plan = CanonicalFactChangeSetV2(
        source_sha256="a" * 64, base_version=1, changes=(fix, replacement, end)
    )
    ordered = SqlAlchemySchoolFactPersistence._ordered_change_pairs(
        tuple(MasterDataImportUnit() for _ in range(3)),
        plan,
        {},
    )
    assert [change.source_key for _, change in ordered] == ["old", "new", "fix:one"]
    for _, change in ordered:
        if change.source_key == "old":
            db.execute(
                "UPDATE organization_locations SET valid_to=:at WHERE id=:id", dict(at=OCT, id=old)
            )
            db.lineage("old", old, 2, "end", OCT)
        elif change.source_key == "new":
            db.fact("new", OCT)
        elif accepted:
            validate(db, at)
        else:
            with pytest.raises(SchoolFactChangeRejected):
                validate(db, at)


def test_historical_replacement_resolution_retains_the_applicable_content_version():
    db = RelationalSession()
    first = db.fact("new", SEP, OCT)
    db.fact("new", OCT, version=2)
    assert SqlAlchemySchoolFactPersistence._f2_content_at(
        cast(Session, db),
        SCHOOL,
        "organization_location",
        "new",
        NOW,
    ) == (first, 1)


def test_correction_publication_evidence_reconstructs_versions_after_later_backdated_update():
    db = RelationalSession()
    old = db.fact("old", SEP, NOW)
    db.lineage("old", old, 2, "end", NOW)
    replacement = db.fact("new", SEP)
    validate(db)
    db.lineage("fix:one", uuid4(), 1, "upsert", NOW, "fact_quality")
    # Later permitted content changes can mutate the previous slice's endpoint.
    # They must not rewrite what a previously published correction referred to.
    earlier = datetime(2026, 9, 5, tzinfo=UTC)
    db.execute(
        "UPDATE organization_locations SET valid_to=:at WHERE id=:id",
        dict(at=earlier, id=replacement),
    )
    db.fact("new", earlier, version=2)
    evidence = db.execute(
        "SELECT l.fact_source_key,l.target_id,l.target_version,l.operation FROM "
        "school_fact_change_lineage l JOIN master_data_import_batches b ON "
        "b.school_id=l.school_id AND b.id=l.batch_id JOIN master_data_import_units u "
        "ON u.school_id=l.school_id AND u.id=l.unit_id WHERE l.school_id=:school "
        "AND l.fact_kind='organization_location' AND b.base_fact_version <= "
        "(SELECT qb.base_fact_version FROM school_fact_change_lineage q JOIN "
        "master_data_import_batches qb ON qb.school_id=q.school_id AND qb.id=q.batch_id "
        "WHERE q.school_id=:school AND q.fact_kind='fact_quality' AND "
        "q.fact_source_key='fix:one') AND (u.payload->>'effective_at')::timestamptz <= :at "
        "ORDER BY l.fact_source_key,l.target_version",
        dict(school=SCHOOL, at=NOW),
    ).all()
    assert [
        (row.fact_source_key, row.target_id, row.target_version, row.operation) for row in evidence
    ] == [
        ("new", str(replacement), 1, "upsert"),
        ("old", str(old), 1, "upsert"),
        ("old", str(old), 2, "end"),
    ]


@pytest.mark.parametrize("at,accepted", [(SEP, False), (NOW, True), (OCT, True)])
def test_policy_reference_uses_initial_declared_start_not_system_creation(at, accepted):
    db = RelationalSession()
    identifier = uuid4()
    db.execute("CREATE TABLE school_policies (school_id TEXT, id TEXT, created_at TEXT)")
    db.execute(
        "INSERT INTO school_policies VALUES (:school,:id,:created)",
        dict(school=SCHOOL, id=identifier, created=OCT),
    )
    db.lineage("policy", identifier, 1, "upsert", NOW, "policy")
    if accepted:
        assert SqlAlchemySchoolFactPersistence._f2_content_at(
            cast(Session, db), SCHOOL, "policy", "policy", at
        ) == (identifier, 1)
        SqlAlchemySchoolFactPersistence._require_f2_reference_interval(
            cast(Session, db), SCHOOL, "policy", identifier, at, None
        )
    else:
        with pytest.raises(SchoolFactChangeRejected):
            SqlAlchemySchoolFactPersistence._f2_content_at(
                cast(Session, db), SCHOOL, "policy", "policy", at
            )
        with pytest.raises(SchoolFactChangeRejected):
            SqlAlchemySchoolFactPersistence._require_f2_reference_interval(
                cast(Session, db), SCHOOL, "policy", identifier, at, None
            )
