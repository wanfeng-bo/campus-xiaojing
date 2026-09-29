"""Real schema25 SQL in the disposable cluster; authority remains a test double."""

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.composition.school_fact_persistence import SchoolFactPersistenceError
from app.platform.master_data_imports.models import MasterDataImportBatch
from tests.test_f2_own_receipt_visibility import official
from tests.test_f2_own_receipt_visibility import receipt_visibility as receipt_visibility


def _publish_batches(fixture, locator_values):
    """Seed old serializer variants before terminal immutability takes effect."""
    keys = tuple(f"receipt:optional-locator:{index}" for index in range(len(locator_values)))
    fixture.authority.grants = {"official_contact": keys}
    locators, identifiers = [], []
    current = 0

    def legacy_mapping(_mapper, _connection, batch):
        identifiers.append(batch.id)
        value = locator_values[current]
        mapping = dict(batch.mapping_payload)
        if value == "missing":
            mapping.pop("result_locator_sha256")
        elif value != "valid":
            mapping["result_locator_sha256"] = value
        batch.mapping_payload = mapping

    def ordered_confirmation(_mapper, _connection, batch):
        if batch.status == "applied":
            batch.confirmed_at = fixture.now - timedelta(seconds=len(keys) - current)

    event.listen(MasterDataImportBatch, "before_insert", legacy_mapping)
    event.listen(MasterDataImportBatch, "before_update", ordered_confirmation)
    try:
        for current, key in enumerate(keys):
            content = official()
            content["contact_value"] = f"000-0000000{current + 2}"
            locators.append(fixture.publish("official_contact", key, content))
    finally:
        event.remove(MasterDataImportBatch, "before_insert", legacy_mapping)
        event.remove(MasterDataImportBatch, "before_update", ordered_confirmation)
    return locators, identifiers


def _preserved_rows(roles, identifiers):
    roles.verify_disposable_cluster()
    with roles.admin_connection() as db:
        return db.execute(
            "SELECT id,mapping_payload,result_payload,target_fact_version,planned_units,"
            "succeeded_units,status FROM public.master_data_import_batches "
            "WHERE id=ANY(%s) ORDER BY id",
            (identifiers,),
        ).fetchall()


@pytest.mark.parametrize("recoverable_count", [0, 1, 2])
def test_missing_and_json_null_locators_are_excluded_before_real_pg_page_limit(
    receipt_visibility, school_facts_f1_schema, recoverable_count
):
    fixture, roles = receipt_visibility, school_facts_f1_schema
    locators, identifiers = _publish_batches(
        fixture, ["valid"] * recoverable_count + ["missing", None]
    )
    before = _preserved_rows(roles, identifiers)
    eligible = locators[:recoverable_count][::-1]
    first = fixture.kernel.list_own_results(
        context=fixture.context, session_binding="new-login-session", limit=1
    )
    assert [item.locator_sha256 for item in first.items] == eligible[:1]
    assert first.has_more is (recoverable_count > 1)
    both = fixture.kernel.list_own_results(
        context=fixture.context, session_binding="new-login-session", limit=2
    )
    assert [item.locator_sha256 for item in both.items] == eligible
    assert both.has_more is False
    # Actual cursor counts prove exclusion before materialization and LIMIT.
    assert fixture.receipt_rows == [recoverable_count, recoverable_count]
    assert _preserved_rows(roles, identifiers) == before
    assert fixture.reader_engine.pool.checkedout() == fixture.writer_engine.pool.checkedout() == 0


def test_non_null_malformed_locator_remains_a_hard_pg_result_failure(
    receipt_visibility, school_facts_f1_schema
):
    fixture, roles = receipt_visibility, school_facts_f1_schema
    _locators, identifiers = _publish_batches(fixture, ["not-a-valid-digest"])
    before = _preserved_rows(roles, identifiers)
    with pytest.raises(SchoolFactPersistenceError):
        fixture.kernel.list_own_results(
            context=fixture.context, session_binding="new-login-session", limit=1
        )
    assert fixture.receipt_rows == [1]
    assert _preserved_rows(roles, identifiers) == before
    assert fixture.reader_engine.pool.checkedout() == fixture.writer_engine.pool.checkedout() == 0
