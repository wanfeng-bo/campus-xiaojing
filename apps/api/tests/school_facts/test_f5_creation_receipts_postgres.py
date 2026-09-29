"""Owned fixture only: original creation proof, never a new daily read grant."""

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.school_facts.test_f5_writer_reader_postgres import writer  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="owned only")


def test_original_creation_audit_is_readable_but_later_update_is_not(writer):  # noqa: F811
    engines, actor, identity, _, service = writer
    key = "f5:receipt-owned:" + uuid4().hex
    targets = frozenset({("teaching_course", key)})
    locators = []
    contexts = []
    for expected in (0, 1):
        context = compose_command_context(actor, uuid4(), "owned-creation-receipt:" + uuid4().hex)
        contexts.append(context)
        with engines["reader"].connect() as conn:
            base = conn.scalar(
                text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
            )
        digest = sha256(uuid4().bytes).hexdigest()
        locator = sha256(uuid4().bytes).hexdigest()
        locators.append(locator)
        plan = CanonicalFactChangeSetV3.model_validate_json(
            json.dumps(
                {
                    "schema_version": 3,
                    "base_version": base,
                    "source_sha256": digest,
                    "changes": [
                        {
                            "schema_version": 3,
                            "origin": {"sheet_code": "owned-audit", "row_number": 1},
                            "fact_kind": "teaching_course",
                            "source_key": key,
                            "operation": "upsert",
                            "expected_version": expected,
                            "effective_at": datetime.now(UTC).isoformat(),
                            "content": {"display_name": "合成回执课程" + str(expected)},
                        }
                    ],
                }
            )
        )
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=digest,
            template_version="owned",
            mapping_version="owned",
            mapping_config_sha256=digest,
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
        applied = service.confirm(
            credential=prepared.credential,
            change_set=plan,
            context=context,
            session_binding=identity.session_nonce_hash,
        )
        assert applied.changed_count == 1
    original = service.check_own_result_by_locator(
        context=contexts[0],
        locator_sha256=locators[0],
        allowed_targets=targets,
        creation_only_targets=targets,
    )
    assert original is not None and original.targets[0].creation_receipt is True
    assert (
        service.check_own_result_by_locator(
            context=contexts[1],
            locator_sha256=locators[1],
            allowed_targets=targets,
            creation_only_targets=targets,
        )
        is None
    )
    page = service.list_own_results(
        context=contexts[1], allowed_targets=targets, creation_only_targets=targets
    )
    assert [item.locator_sha256 for item in page.items] == [locators[0]]
    assert service.list_own_results(context=contexts[1], allowed_targets=frozenset()).items == ()
    print(
        "F5_CREATION_RECEIPT_PG", original.result.batch_id, "creation=yes update=no latest=creation"
    )
