"""Own-history reads execute owner/scope predicates before returning receipt data."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Uuid, create_engine
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_persistence import (
    SchoolFactPersistenceError,
    SqlAlchemySchoolFactPersistence,
)
from app.platform import contracts
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
SCHOOL = UUID(int=11)
PERSON = UUID(int=13)
ALLOWED = frozenset({("official_contact", "contact:approved")})


def context(*, user=7, person=PERSON, school=SCHOOL):
    actor = contracts._issue_staff_actor_context(
        account_user_id=user,
        school_id=contracts.SchoolId(school),
        person_id=contracts.PersonId(person),
        assignment_id=contracts.AssignmentId(UUID(int=14)),
        assignment_version=1,
        authorization_version=1,
    )
    return contracts._issue_command_execution_context(
        actor=actor, request_id=uuid4(), idempotency_key="read-own-history"
    )


@pytest.fixture
def store(monkeypatch):
    metadata = MetaData()
    batch = Table(
        "master_data_import_batches",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("school_id", Uuid),
        Column("mode", String),
        Column("status", String),
        Column("staged_by_user_id", Integer),
        Column("staged_by_person_id", Uuid),
        Column("confirmed_by_user_id", Integer),
        Column("confirmed_by_person_id", Uuid),
        Column("confirmed_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True)),
        Column("confirmation_session_sha256", String),
        Column("mapping_payload", JSON),
        Column("result_payload", JSON),
        Column("target_fact_version", Integer),
        Column("planned_units", Integer),
        Column("succeeded_units", Integer),
        Column("failed_units", Integer),
        Column("pending_units", Integer),
    )
    lineage = Table(
        "school_fact_change_lineage",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("school_id", Uuid),
        Column("batch_id", Uuid),
        Column("fact_kind", String),
        Column("fact_source_key", String),
        Column("operation", String),
        Column("target_version", Integer),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    factory = sessionmaker(engine)
    # SQLite cannot install PostgreSQL execution envelopes; real ownership/scope
    # SELECTs still execute against a database, with no stub result rows.
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_set_local_request_context", lambda *_: None
    )

    def unused_envelope(_context: contracts.CommandExecutionContext) -> Never:
        raise AssertionError("SQLite scope tests do not issue execution envelopes")

    persistence = SqlAlchemySchoolFactPersistence(factory, unused_envelope)

    def seed(
        index,
        *,
        user=7,
        person=PERSON,
        school=SCHOOL,
        targets=ALLOWED,
        status="applied",
        has_result=True,
        complete=True,
        locator_value=...,
        omit_locator=False,
    ):
        identifier = UUID(int=100 + index)
        locator = (
            sha256(f"original-plan-{index}".encode()).hexdigest()
            if locator_value is ...
            else locator_value
        )
        count = len(targets) + (0 if complete else 1)
        with engine.begin() as connection:
            connection.execute(
                batch.insert().values(
                    id=identifier,
                    school_id=school,
                    mode="canonical_atomic_changes",
                    status=status,
                    staged_by_user_id=user,
                    staged_by_person_id=person,
                    confirmed_by_user_id=user,
                    confirmed_by_person_id=person,
                    confirmed_at=NOW + timedelta(seconds=index),
                    created_at=NOW,
                    confirmation_session_sha256=sha256(b"previous-login").hexdigest(),
                    mapping_payload={
                        **({} if omit_locator else {"result_locator_sha256": locator}),
                        "private": "never-return",
                    },
                    result_payload={"target_version": index + 1, "changed_count": count}
                    if has_result
                    else None,
                    target_fact_version=index + 1,
                    planned_units=count,
                    succeeded_units=count,
                    failed_units=0,
                    pending_units=0,
                )
            )
            for kind, key in targets:
                connection.execute(
                    lineage.insert().values(
                        id=uuid4(),
                        school_id=school,
                        batch_id=identifier,
                        fact_kind=kind,
                        fact_source_key=key,
                        operation="upsert",
                        target_version=index + 1,
                    )
                )
        return locator

    yield SchoolFactChangeService(reader=persistence, writer=persistence), seed, engine
    engine.dispose()


def test_own_result_survives_new_login_without_old_session_or_cleartext_locator(store):
    service, seed, _ = store
    locator = seed(1)
    result = service.check_own_result_by_locator(
        context=context(),
        locator_sha256=locator,
        allowed_targets=ALLOWED,
    )
    assert result is not None and result.locator_sha256 == locator
    assert result.result.target_version == 2 and result.result.changed_count == 1
    assert [(t.fact_kind, t.source_key) for t in result.targets] == list(ALLOWED)
    assert "previous-login" not in repr(result) and "never-return" not in repr(result)


@pytest.mark.parametrize(
    "wrong", [dict(user=8), dict(person=UUID(int=99)), dict(school=UUID(int=99))]
)
def test_own_result_rejects_different_owner_even_with_valid_locator(store, wrong):
    service, seed, _ = store
    locator = seed(1)
    assert (
        service.check_own_result_by_locator(
            context=context(**wrong),
            locator_sha256=locator,
            allowed_targets=ALLOWED,
        )
        is None
    )


def test_scope_filters_all_targets_before_limit_and_never_returns_partial_batch(store):
    service, seed, _ = store
    permitted = seed(1)
    seed(2, targets=ALLOWED | {("person_contact", "private:unapproved")})
    seed(3, targets=frozenset({("official_contact", "contact:other")}))
    page = service.list_own_results(
        context=context(),
        allowed_targets=ALLOWED,
        limit=1,
    )
    assert [item.locator_sha256 for item in page.items] == [permitted]
    assert page.has_more is False


def test_directory_is_bounded_and_excludes_incomplete_or_unconfirmed_results(store):
    service, seed, _ = store
    seed(1)
    second = seed(2)
    seed(3, status="ready_for_confirmation")
    seed(4, has_result=False)
    seed(5, complete=False)
    seed(6, targets=frozenset())
    page = service.list_own_results(
        context=context(),
        allowed_targets=ALLOWED,
        limit=1,
    )
    assert [item.locator_sha256 for item in page.items] == [second]
    assert page.has_more is True


@pytest.mark.parametrize("locator_fields", [{"locator_value": None}, {"omit_locator": True}])
@pytest.mark.parametrize("recoverable_count", [0, 1, 2])
def test_legal_optional_locator_batches_do_not_consume_receipt_page(
    store, locator_fields, recoverable_count
):
    service, seed, _ = store
    eligible = [seed(index + 1) for index in range(recoverable_count)]
    # These newer complete batches belong to the same current actor and target.
    # The original provenance contract allows them to lack a recovery locator.
    for index in range(3, 6):
        seed(index, **locator_fields)
    page = service.list_own_results(context=context(), allowed_targets=ALLOWED, limit=1)
    assert [item.locator_sha256 for item in page.items] == eligible[-1:]
    assert page.has_more is (recoverable_count > 1)
    if eligible:
        exact = service.check_own_result_by_locator(
            context=context(), allowed_targets=ALLOWED, locator_sha256=eligible[-1]
        )
        assert exact == page.items[0]


@pytest.mark.parametrize("malformed", ["", "g" * 64, "a" * 63, 12])
def test_nonnull_malformed_locator_still_rejects_the_entire_receipt_page(store, malformed):
    service, seed, _ = store
    seed(1)
    seed(2, locator_value=malformed)
    with pytest.raises(SchoolFactPersistenceError):
        service.list_own_results(context=context(), allowed_targets=ALLOWED, limit=1)


@pytest.mark.parametrize("limit", [0, 21, True])
def test_own_history_requires_bounded_limit(store, limit):
    service, _, _ = store
    with pytest.raises(SchoolFactChangeRejected):
        service.list_own_results(
            context=context(),
            allowed_targets=ALLOWED,
            limit=limit,
        )


def test_own_history_requires_trusted_context(store):
    service, _, _ = store
    for invalid in (None, object()):
        with pytest.raises(SchoolFactChangeRejected):
            service.list_own_results(
                context=invalid,
                allowed_targets=ALLOWED,
            )
