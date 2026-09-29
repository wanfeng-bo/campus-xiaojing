"""Short, explicitly owned PostgreSQL concurrency and stale-confirmation evidence."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_education_maintenance import snapshot_at
from app.domains.students.teaching_change_planner import plan_teaching_change
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f5_teaching_postgres import insert_version
from tests.school_facts.test_f5_writer_reader_postgres import writer  # noqa: F401


def test_waiting_cross_root_source_is_excluded_after_first_commit(writer):  # noqa: F811
    engines, *_ = writer
    payload = {
        "offering_source_key": "f5-concurrency:" + uuid4().hex,
        "source_kind": "administrative_class",
        "class_source_key": "class:isolated-concurrency",
    }
    ready = Queue()

    def candidate():
        with engines["migration"].connect() as second:
            transaction = second.begin()
            second.execute(text("SET LOCAL statement_timeout='6000ms'"))
            ready.put(second.scalar(text("SELECT pg_backend_pid()")))
            try:
                insert_version(second, "offering_member_source", payload)
                transaction.commit()
                return "unexpected_commit"
            except DBAPIError as error:
                transaction.rollback()
                return getattr(error.orig, "sqlstate", None)

    with engines["migration"].connect() as first, ThreadPoolExecutor(max_workers=1) as pool:
        transaction = first.begin()
        version = insert_version(first, "offering_member_source", payload)
        future = pool.submit(candidate)
        pid = ready.get(timeout=3)
        deadline = monotonic() + 3
        waiting = False
        while monotonic() < deadline:
            # Refresh statistics inside this transaction; the first insert remains uncommitted.
            first.execute(text("SELECT pg_stat_clear_snapshot()"))
            waiting = (
                first.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))>0"),
                    {"pid": pid},
                )
                is True
            )
            if waiting:
                break
            sleep(0.03)
        assert waiting and not future.done(), (
            future.result() if future.done() else "no_block_observed"
        )
        transaction.commit()
        assert future.result(timeout=4) == "23P01"
        assert (
            first.scalar(
                text("SELECT count(*) FROM school_education_versions WHERE id=:id"), {"id": version}
            )
            == 1
        )
        assert (
            first.scalar(
                text(
                    "SELECT count(*) FROM school_education_versions "
                    "WHERE fact_kind='offering_member_source' "
                    "AND payload->>'offering_source_key'=:key"
                ),
                {"key": payload["offering_source_key"]},
            )
            == 1
        )
        # This storage-only candidate has no publication lineage or reference rows.
        root = first.scalar(
            text("SELECT fact_id FROM school_education_versions WHERE id=:id"), {"id": version}
        )
        first.execute(text("DELETE FROM school_education_versions WHERE id=:id"), {"id": version})
        first.execute(text("DELETE FROM school_education_facts WHERE id=:id"), {"id": root})
        first.commit()


def test_writer_rejects_original_preview_after_shared_offering_expands_impact(writer, monkeypatch):  # noqa: F811
    engines, actor, identity, context, service = writer
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    validate = SqlAlchemySchoolFactPersistence._validate_education_state

    def diagnostic(*args, **kwargs):
        try:
            return validate(*args, **kwargs)
        except SchoolFactChangeRejected as error:
            raise AssertionError(str(error)) from error

    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_validate_education_state", staticmethod(diagnostic)
    )
    original_preflight = service.preflight

    def checked_preflight(**kwargs):
        report = original_preflight(**kwargs)
        assert report.is_clean, report.model_dump(mode="json")
        return report

    service.preflight = checked_preflight
    prefix = "f5-impact:" + uuid4().hex + ":"
    now = datetime.now(UTC)
    start = now - timedelta(minutes=1)
    boundary = now + timedelta(hours=1)
    course, group, offer1, source1, offer2, source2 = (
        prefix + suffix for suffix in ("course", "group", "offer1", "source1", "offer2", "source2")
    )

    def entry(kind, key, content):
        return dict(
            fact_kind=kind,
            source_key=key,
            operation="upsert",
            expected_version=0,
            effective_at=start,
            content=content,
        )

    organization = prefix + "organization"
    apply(
        writer,
        [
            entry(
                "organization",
                organization,
                {
                    "kind": "department",
                    "display_name": "Owned concurrency group",
                    "display_path": "Owned concurrency group",
                    "status": "active",
                },
            )
        ],
    )
    apply(
        writer,
        [
            entry("teaching_course", course, {"display_name": "Owned impact course"}),
            entry(
                "teaching_group",
                group,
                {
                    "organization_source_key": organization,
                    "group_mode": "shared",
                },
            ),
            entry(
                "teaching_offering",
                offer1,
                {"course_source_key": course, "offering_status": "active"},
            ),
            entry(
                "offering_member_source",
                source1,
                {
                    "offering_source_key": offer1,
                    "source_kind": "independent_group",
                    "group_source_key": group,
                },
            ),
        ],
    )
    selections = {
        "teaching_group": [group],
        "teaching_offering": [offer1, offer2],
        "offering_member_source": [source1, source2],
    }

    def snapshots():
        result = []
        with engines["reader"].connect() as reader:
            for kind, keys in selections.items():
                raw = reader.scalar(
                    text(
                        "SELECT app.school_fact_f5_targets(:school,:kind,CAST(:keys AS text[]),"
                        "CAST(:fields AS text[]),:at,NULL,CAST(:filter AS jsonb))"
                    ),
                    {
                        "school": actor.school_id,
                        "kind": kind,
                        "keys": keys,
                        "fields": sorted(F5_FACT_FIELDS[kind]),
                        "filter": "{}",
                        "at": now,
                    },
                )
                for row in raw["records"]:
                    snapshot = snapshot_at(row, kind=kind, at=now)
                    if snapshot is not None:
                        result.append(snapshot)
        return tuple(result)

    primary = dict(
        schema_version=3,
        fact_kind="teaching_group",
        source_key=group,
        operation="end",
        expected_version=1,
        effective_at=boundary,
        origin={"sheet_code": "education", "row_number": 1},
        content=None,
    )

    def impact(rows):
        return plan_teaching_change(
            (primary,),
            rows,
            now=now,
            authorized_keys=frozenset(k for keys in selections.values() for k in keys),
            complete_snapshot=True,
        )

    before = impact(snapshots())
    assert not before.issues
    assert {c["source_key"] for c in before.changes} == {group, offer1, source1}
    with engines["reader"].connect() as reader:
        base = reader.scalar(
            text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
        )
    plan = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps(
            dict(
                schema_version=3,
                base_version=base,
                source_sha256=sha256(uuid4().bytes).hexdigest(),
                changes=before.changes,
            ),
            default=str,
        )
    )
    prepared = service.prepare_confirmation(
        change_set=plan, context=context, session_binding=identity.session_nonce_hash
    )
    added = apply(
        writer,
        [
            entry(
                "teaching_offering",
                offer2,
                {"course_source_key": course, "offering_status": "active"},
            ),
            entry(
                "offering_member_source",
                source2,
                {
                    "offering_source_key": offer2,
                    "source_kind": "independent_group",
                    "group_source_key": group,
                },
            ),
        ],
    )
    after = impact(snapshots())
    assert not after.issues
    assert {c["source_key"] for c in after.changes} == {group, offer1, source1, offer2, source2}
    with pytest.raises(SchoolFactChangeRejected):
        service.confirm(
            credential=prepared.credential,
            change_set=plan,
            context=context,
            session_binding=identity.session_nonce_hash,
        )
    # The rejected original preview cannot partially end the old group or its sources.
    rows = snapshots()
    assert {row.source_key for row in rows} == {group, offer1, source1, offer2, source2}
    assert all(row.version == 1 and all(s.valid_to is None for s in row.segments) for row in rows)
    with engines["migration"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM master_data_import_batches WHERE id=:id"),
                {"id": added.batch_id},
            )
            == "applied"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_batches "
                    "WHERE source_sha256=:digest AND status='applied'"
                ),
                {"digest": plan.source_sha256},
            )
            == 0
        )
