# ruff: noqa: F811
"""Receipt metadata is owner-bound and never exposes receipt bodies."""

from uuid import UUID

import pytest
from sqlalchemy import event, text

from app.platform.school_facts.application import SchoolFactChangeRejected
from tests.test_f2_own_receipts import ALLOWED, context, store  # noqa: F401


def lookup(service, locator, **actor):
    return service.locate_own_result_targets(context=context(**actor), locator_sha256=locator)


def test_exact_mixed_receipt_returns_only_complete_coordinates_in_one_select(store):
    service, seed, engine = store
    keys = ALLOWED | {("education_class", "class:a"), ("student_school_relationship", "student:a")}
    locator = seed(1, targets=keys)
    queries = []

    def capture(conn, cursor, statement, parameters, execution_context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    assert lookup(service, locator) == keys
    event.remove(engine, "before_cursor_execute", capture)
    assert len(queries) == 1
    projection = queries[0].split("FROM", 1)[0]
    assert "result_payload" not in projection and "mapping_payload" not in projection
    assert "fact_kind" in projection and "fact_source_key" in projection


@pytest.mark.parametrize(
    "identity", [dict(school=UUID(int=999)), dict(user=8), dict(person=UUID(int=999))]
)
def test_metadata_cannot_cross_school_or_current_person(store, identity):
    service, seed, _ = store
    assert lookup(service, seed(1), **identity) is None


@pytest.mark.parametrize(
    "setup", [dict(status="prepared"), dict(complete=False), dict(has_result=False)]
)
def test_only_complete_applied_receipt_has_metadata(store, setup):
    service, seed, _ = store
    assert lookup(service, seed(1, **setup)) is None


@pytest.mark.parametrize("column", ["confirmed_by_user_id", "staged_by_user_id"])
def test_stager_and_confirmer_must_both_be_current_actor(store, column):
    service, seed, engine = store
    locator = seed(1)
    with engine.begin() as conn:
        conn.execute(text("UPDATE master_data_import_batches SET " + column + "=999"))
    assert lookup(service, locator) is None


def test_wrong_locator_never_falls_back_to_latest_or_list(store):
    service, seed, _ = store
    seed(1)
    assert lookup(service, "f" * 64) is None
    with pytest.raises(SchoolFactChangeRejected):
        lookup(service, "raw locator")


def test_metadata_rejects_inconsistent_result_counters(store):
    service, seed, engine = store
    locator = seed(1)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE master_data_import_batches SET result_payload=:payload"),
            {"payload": '{"target_version":2,"changed_count":9}'},
        )
    assert lookup(service, locator) is None


def test_metadata_rejects_string_counters_instead_of_promoting_them(store):
    service, seed, engine = store
    locator = seed(1)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE master_data_import_batches SET result_payload=:payload"),
            {"payload": '{"target_version":"2","changed_count":"1"}'},
        )
    assert lookup(service, locator) is None


@pytest.mark.parametrize("column", ["confirmed_by_person_id", "staged_by_person_id"])
def test_person_binding_is_required_for_both_sides_of_receipt(store, column):
    service, seed, engine = store
    locator = seed(1)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE master_data_import_batches SET " + column + "=:person"),
            {"person": UUID(int=999).hex},
        )
    assert lookup(service, locator) is None


def test_duplicate_locator_batches_are_not_combined_into_an_invented_receipt(store):
    from app.composition.school_fact_persistence import SchoolFactPersistenceError

    service, seed, _ = store
    locator = seed(1)
    seed(2, locator_value=locator, targets={("education_class", "class:another")})
    with pytest.raises(SchoolFactPersistenceError):
        lookup(service, locator)
