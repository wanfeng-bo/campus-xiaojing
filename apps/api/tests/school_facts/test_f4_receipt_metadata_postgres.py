"""Actual Writer-bound PostgreSQL receipt metadata; no original database access."""

# ruff: noqa: F811
import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.school_facts.test_f4_writer_postgres import writer  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("F4_WRITER_PG"), reason="owned F4 PG only")


def test_real_writer_metadata_is_complete_owner_bound_and_cross_family_compatible(writer):
    engines, actor, identity, context, service = writer

    def prepare(kinds):
        with engines["reader"].connect() as c:
            base = c.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
            )
        token = uuid4().hex
        changes = [
            {
                "schema_version": 3,
                "fact_kind": kind,
                "source_key": kind + ":" + token,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": datetime.now(UTC).isoformat(),
                "content": {
                    "display_name": "回执兼容合成对象",
                    **(
                        {"person_kind": "other", "lifecycle_status": "active"}
                        if kind == "person"
                        else {}
                    ),
                },
                "origin": {"sheet_code": "metadata", "row_number": index + 1},
            }
            for index, kind in enumerate(kinds)
        ]
        plan = CanonicalFactChangeSetV3.model_validate_json(
            json.dumps(
                {
                    "schema_version": 3,
                    "base_version": base,
                    "source_sha256": sha256(token.encode()).hexdigest(),
                    "changes": changes,
                }
            )
        )
        locator = sha256(("receipt:" + token).encode()).hexdigest()
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=plan.source_sha256,
            template_version="school_facts_v1",
            mapping_version="admin_v1",
            mapping_config_sha256="c" * 64,
            source_size_bytes=None,
            worksheet_names=(),
            result_locator_sha256=locator,
        )
        prepared = service.prepare_confirmation(
            change_set=plan,
            context=context,
            session_binding=identity.session_nonce_hash,
            provenance=provenance,
        )
        return plan, locator, prepared

    for kinds in (("education_class", "person"), ("person",)):
        plan, locator, prepared = prepare(kinds)
        # A real staged, unconfirmed batch is not a receipt and yields no coordinates.
        assert service.locate_own_result_targets(context=context, locator_sha256=locator) is None
        result = service.confirm(
            change_set=plan,
            credential=prepared.credential,
            context=context,
            session_binding=identity.session_nonce_hash,
        )
        expected = frozenset((c.fact_kind, c.source_key) for c in plan.changes)
        assert (
            service.locate_own_result_targets(context=context, locator_sha256=locator) == expected
        )
        receipt = service.check_own_result_by_locator(
            context=context, locator_sha256=locator, allowed_targets=expected
        )
        assert receipt.result.batch_id == result.batch_id and len(receipt.targets) == len(expected)
        assert (
            service.check_own_result_by_locator(
                context=context, locator_sha256=locator, allowed_targets=frozenset()
            )
            is None
        )
        bound = service._reader.for_request(context)
        for school, user, person in (
            (uuid4(), actor.account_user_id, actor.person_id),
            (actor.school_id, actor.account_user_id + 100000, actor.person_id),
            (actor.school_id, actor.account_user_id, uuid4()),
        ):
            with pytest.raises(SchoolFactChangeRejected):
                bound.find_own_result_targets(school, user, person, locator)
        assert service.locate_own_result_targets(context=context, locator_sha256="f" * 64) is None

    # Even a missing locator must not skip the database's current identity check.
    from sqlalchemy.exc import DBAPIError

    from app.platform.contracts import _issue_command_execution_context, _issue_staff_actor_context

    stale_actor = _issue_staff_actor_context(
        account_user_id=actor.account_user_id,
        school_id=actor.school_id,
        person_id=actor.person_id,
        assignment_id=actor.assignment_id,
        assignment_version=actor.assignment_version + 1,
        authorization_version=actor.authorization_version,
    )
    stale_context = _issue_command_execution_context(
        actor=stale_actor, request_id=uuid4(), idempotency_key="stale-missing-receipt"
    )
    with pytest.raises((SchoolFactChangeRejected, DBAPIError)):
        service.locate_own_result_targets(context=stale_context, locator_sha256="e" * 64)
