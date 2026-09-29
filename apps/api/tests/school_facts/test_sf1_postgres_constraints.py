from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.identity.service import find_or_create_dingtalk_user
from tests.master_data.conftest import PostgresRoles


def _at(month: int, day: int = 1) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _LocationSliceSeed:
    location_id: UUID
    source_key: str
    parent_location_id: UUID | None
    kind: str
    business_code: str
    display_name: str
    aliases: tuple[str, ...]
    display_path: str
    effective_at: datetime
    target_version: int = 1
    expected_version: int = 0


def _insert_formal_location_units(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    slices: tuple[_LocationSliceSeed, ...],
    fact_kind: str = "location",
) -> UUID:
    """Seed exact applied units/lineage required by revision 18 structure slices."""

    assert fact_kind in {"location", "organization"}

    actor_user_id_row = connection.execute(
        """
        INSERT INTO users (
            provider_code, provider_tenant_key, external_subject,
            display_name, is_active, security_version
        ) VALUES ('local_test', %s, %s, '约束测试操作者', true, 1)
        RETURNING id
        """,
        (str(school_id), f"constraint-fixture:{uuid4().hex}"),
    ).fetchone()
    assert actor_user_id_row is not None
    actor_user_id = int(actor_user_id_row[0])
    actor_person_id, actor_unit_id, actor_assignment_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, source_key, status, version)
        VALUES (%s, %s, %s, 'active', 1)
        """,
        (actor_person_id, school_id, f"person:constraint-fixture:{actor_person_id}"),
    )
    connection.execute(
        """
        INSERT INTO organization_units (
            id, school_id, parent_organization_unit_id, source_key, kind, status, version
        ) VALUES (%s, %s, NULL, %s, 'department', 'active', 1)
        """,
        (actor_unit_id, school_id, f"unit:constraint-fixture:{actor_unit_id}"),
    )
    connection.execute(
        """
        INSERT INTO staff_assignments (
            id, school_id, source_key, person_id, organization_unit_id,
            position_title, semantic_role_code, assignment_kind,
            appointment_source, approval_reference, valid_from, valid_to,
            revoked_at, status, version
        ) VALUES (
            %s, %s, %s, %s, %s,
            '约束测试操作者', 'school_facts_admin', 'substantive',
            'test_fixture', %s, %s, NULL, NULL, 'confirmed', 1
        )
        """,
        (
            actor_assignment_id,
            school_id,
            f"assignment:constraint-fixture:{actor_assignment_id}",
            actor_person_id,
            actor_unit_id,
            f"approval:constraint-fixture:{actor_assignment_id}",
            _at(1),
        ),
    )

    batch_id = uuid4()
    idempotency_record_id = uuid4()
    source_sha256 = batch_id.hex * 2
    idempotency_key_hash = uuid4().hex * 2
    plan_sha256 = uuid4().hex * 2
    confirmed_at = datetime.now(UTC).replace(microsecond=0)
    for setting_name, setting_value in (
        ("app.context_purpose", "command"),
        ("app.context_kind", "assignment"),
        ("app.account_user_id", str(actor_user_id)),
        ("app.person_id", str(actor_person_id)),
        ("app.assignment_id", str(actor_assignment_id)),
        ("app.assignment_version", "1"),
    ):
        connection.execute("SELECT set_config(%s, %s, true)", (setting_name, setting_value))
    connection.execute(
        """
        INSERT INTO master_data_import_batches (
            id, school_id, mode, source_kind, source_ref, source_sha256,
            worksheet_names, status, planned_units, succeeded_units,
            failed_units, pending_units, confirmed_by_user_id,
            confirmed_by_person_id, confirmed_by_assignment_id, confirmed_at,
            request_id, idempotency_key_hash, plan_sha256, base_fact_version,
            target_fact_version, confirmation_token_sha256,
            confirmation_expires_at, confirmation_consumed_at,
            confirmation_session_sha256, staged_by_user_id,
            staged_by_person_id, staged_by_assignment_id,
            staged_assignment_version, staged_authorization_version, version
        ) VALUES (
            %s, %s, 'canonical_atomic_changes', 'manual', %s, %s,
            '[]'::jsonb, 'applied', %s, %s,
            0, 0, %s,
            %s, %s, %s,
            %s, %s, %s, 1,
            2, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            1, 1, 1
        )
        """,
        (
            batch_id,
            school_id,
            f"constraint-{fact_kind}:{batch_id}",
            source_sha256,
            len(slices),
            len(slices),
            actor_user_id,
            actor_person_id,
            actor_assignment_id,
            confirmed_at,
            uuid4(),
            idempotency_key_hash,
            plan_sha256,
            uuid4().hex * 2,
            confirmed_at + timedelta(hours=1),
            confirmed_at,
            uuid4().hex * 2,
            actor_user_id,
            actor_person_id,
            actor_assignment_id,
        ),
    )
    for sequence, item in enumerate(slices, start=1):
        unit_id = uuid4()
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                target_id, target_version, version
            ) VALUES (
                %s, %s, %s, %s, %s, 'school_fact', 'applied',
                jsonb_build_object(
                    'fact_kind', CAST(%s AS text),
                    'source_key', CAST(%s AS text),
                    'operation', 'upsert',
                    'effective_at', CAST(%s AS text),
                    'expected_version', CAST(%s AS bigint)
                ), %s, '[]'::jsonb, '[]'::jsonb,
                %s, %s, 1
            )
            """,
            (
                unit_id,
                school_id,
                batch_id,
                sequence,
                sequence,
                fact_kind,
                item.source_key,
                item.effective_at.isoformat(),
                item.expected_version,
                unit_id.hex * 2,
                item.location_id,
                item.target_version,
            ),
        )
        connection.execute(
            """
            INSERT INTO school_fact_change_lineage (
                id, school_id, batch_id, unit_id, fact_kind, fact_source_key,
                operation, target_id, target_version, source_sha256,
                sheet_code, row_number
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                'upsert', %s, %s, %s, 'constraint_fixture', %s
            )
            """,
            (
                uuid4(),
                school_id,
                batch_id,
                unit_id,
                fact_kind,
                item.source_key,
                item.location_id,
                item.target_version,
                source_sha256,
                sequence,
            ),
        )
    sequences = list(range(1, len(slices) + 1))
    connection.execute(
        """
        INSERT INTO command_idempotency_records (
            id, school_id, action_code, actor_user_id, actor_person_id,
            actor_context_type, actor_context_key, actor_assignment_id,
            actor_assignment_version, resource_type, resource_id,
            idempotency_key_hash, request_sha256, status, result_type,
            result_payload
        ) VALUES (
            %s, %s, 'school_facts.confirm', %s, %s,
            'assignment', %s, %s,
            1, 'master_data.import_batch', %s,
            %s, %s, 'succeeded', 'master_data.import_outcome',
            jsonb_build_object(
                'resource_type', 'master_data.import_batch',
                'resource_id', CAST(%s AS text),
                'resource_version', 1,
                'outcome_code', 'applied'
            )
        )
        """,
        (
            idempotency_record_id,
            school_id,
            actor_user_id,
            actor_person_id,
            f"assignment:{actor_assignment_id}:1",
            actor_assignment_id,
            str(batch_id),
            idempotency_key_hash,
            plan_sha256,
            str(batch_id),
        ),
    )
    connection.execute(
        """
        INSERT INTO master_data_import_command_outcomes (
            id, school_id, idempotency_record_id, batch_id, action_code,
            status, batch_version, succeeded_units, failed_units, executed_units
        ) VALUES (
            %s, %s, %s, %s, 'school_facts.confirm',
            'applied', 1, %s, ARRAY[]::integer[], %s
        )
        """,
        (uuid4(), school_id, idempotency_record_id, batch_id, sequences, sequences),
    )
    return batch_id


def _insert_confirmed_location_slices(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    slices: tuple[_LocationSliceSeed, ...],
) -> None:
    batch_id = _insert_formal_location_units(
        connection,
        school_id=school_id,
        slices=slices,
    )
    for item in slices:
        connection.execute(
            """
            INSERT INTO campus_location_versions (
                id, school_id, location_id, business_code, display_name, aliases,
                display_path, parent_location_id, kind, valid_from, valid_to,
                source_kind, source_ref, status, version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                'manual', %s, 'confirmed', %s
            )
            """,
            (
                uuid4(),
                school_id,
                item.location_id,
                item.business_code,
                item.display_name,
                list(item.aliases),
                item.display_path,
                item.parent_location_id,
                item.kind,
                item.effective_at,
                f"change-set:{batch_id}",
                item.target_version,
            ),
        )


def _commit_dingtalk_mapping(
    postgres_roles: PostgresRoles,
    *,
    identity: DingTalkIdentity,
    request_id: str,
    started: Event | None = None,
    flushed: Event | None = None,
    release: Event | None = None,
) -> tuple[int, int]:
    engine = create_engine(
        "postgresql+psycopg://",
        connect_args=postgres_roles.runtime_connection_parameters,
        poolclass=NullPool,
    )
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        backend_pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        if started is not None:
            started.set()
        user = find_or_create_dingtalk_user(
            session,
            identity=identity,
            request_id=request_id,
        )
        assert user.id is not None
        user_id = int(user.id)
        if flushed is not None:
            flushed.set()
        if release is not None and not release.wait(timeout=6):
            raise AssertionError("timed out waiting to release first identity mapping transaction")
        transaction.commit()
        return user_id, int(backend_pid)
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise
    finally:
        session.close()
        connection.close()
        engine.dispose()


def _insert_school(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO schools (
            id, source_key, school_code, display_name, timezone_name, status, version
        ) VALUES (%s, %s, %s, %s, 'Asia/Shanghai', 'active', 1)
        """,
        (school_id, source_key, f"legacy-{source_key}", f"学校-{source_key}"),
    )


def _insert_source(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_id: UUID,
    source_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO school_fact_sources (
            id, school_id, source_key, source_kind, source_ref, schema_version
        ) VALUES (%s, %s, %s, 'manual', %s, 1)
        """,
        (source_id, school_id, source_key, f"manual:{source_key}"),
    )


def _insert_person_and_student(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    person_id: UUID,
    student_id: UUID,
    key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, source_key, status, version)
        VALUES (%s, %s, %s, 'active', 1)
        """,
        (person_id, school_id, f"person:{key}"),
    )
    connection.execute(
        """
        INSERT INTO students (id, school_id, person_id, source_key, status, version)
        VALUES (%s, %s, %s, %s, 'active', 1)
        """,
        (student_id, school_id, person_id, f"student:{key}"),
    )


def _insert_room(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_id: UUID,
    room_id: UUID,
    key: str,
    capacity: int,
    room_business_code: str | None = None,
    room_display_name: str | None = None,
    room_aliases: tuple[str, ...] = (),
    room_display_path: str | None = None,
) -> None:
    campus_id = uuid4()
    building_id = uuid4()
    connection.execute(
        """
        INSERT INTO campus_locations (id, school_id, parent_location_id, source_key, kind, status)
        VALUES
            (%s, %s, NULL, %s, 'campus', 'active'),
            (%s, %s, %s, %s, 'building', 'active'),
            (%s, %s, %s, %s, 'dorm_room', 'active')
        """,
        (
            campus_id,
            school_id,
            f"campus:{key}",
            building_id,
            school_id,
            campus_id,
            f"building:{key}",
            room_id,
            school_id,
            building_id,
            f"room:{key}",
        ),
    )
    _insert_confirmed_location_slices(
        connection,
        school_id=school_id,
        slices=(
            _LocationSliceSeed(
                location_id=campus_id,
                source_key=f"campus:{key}",
                parent_location_id=None,
                kind="campus",
                business_code=f"CAMPUS-{key}",
                display_name=f"校区-{key}",
                aliases=(),
                display_path=f"/校区-{key}",
                effective_at=_at(1),
            ),
            _LocationSliceSeed(
                location_id=building_id,
                source_key=f"building:{key}",
                parent_location_id=campus_id,
                kind="building",
                business_code=f"BUILDING-{key}",
                display_name=f"楼栋-{key}",
                aliases=(),
                display_path=f"/校区-{key}/楼栋-{key}",
                effective_at=_at(1),
            ),
            _LocationSliceSeed(
                location_id=room_id,
                source_key=f"room:{key}",
                parent_location_id=building_id,
                kind="dorm_room",
                business_code=room_business_code or f"ROOM-{key}",
                display_name=room_display_name or f"宿舍-{key}",
                aliases=room_aliases,
                display_path=room_display_path or f"/校区-{key}/楼栋-{key}/宿舍-{key}",
                effective_at=_at(1),
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO dorm_room_profile_versions (
            id, school_id, room_location_id, authorized_bed_capacity,
            accommodation_zone, is_available, accessibility_tags,
            valid_from, valid_to, version, source_id, import_batch_id
        ) VALUES (%s, %s, %s, %s, 'general', true, ARRAY[]::varchar[],
                  %s, NULL, 1, %s, NULL)
        """,
        (uuid4(), school_id, room_id, capacity, _at(1), source_id),
    )


def _insert_campus(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    key: str,
) -> UUID:
    campus_id = uuid4()
    source_key = f"campus:{key}"
    connection.execute(
        """
        INSERT INTO campus_locations (
            id, school_id, parent_location_id, source_key, kind, status
        ) VALUES (%s, %s, NULL, %s, 'campus', 'active')
        """,
        (campus_id, school_id, source_key),
    )
    _insert_confirmed_location_slices(
        connection,
        school_id=school_id,
        slices=(
            _LocationSliceSeed(
                location_id=campus_id,
                source_key=source_key,
                parent_location_id=None,
                kind="campus",
                business_code=f"CAMPUS-{key}",
                display_name=f"校区-{key}",
                aliases=(),
                display_path=f"/校区-{key}",
                effective_at=_at(1),
            ),
        ),
    )
    return campus_id


def _replace_confirmed_location_slice(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    location_id: UUID,
    business_code: str,
    display_name: str,
    aliases: tuple[str, ...],
    display_path: str,
    effective_at: datetime,
) -> None:
    stable = connection.execute(
        """
        SELECT source_key, parent_location_id, kind, version
        FROM campus_locations
        WHERE school_id = %s AND id = %s
        """,
        (school_id, location_id),
    ).fetchone()
    assert stable is not None
    source_key, parent_location_id, kind, expected_version = stable
    target_version = int(expected_version) + 1
    item = _LocationSliceSeed(
        location_id=location_id,
        source_key=str(source_key),
        parent_location_id=parent_location_id,
        kind=str(kind),
        business_code=business_code,
        display_name=display_name,
        aliases=aliases,
        display_path=display_path,
        effective_at=effective_at,
        target_version=target_version,
        expected_version=int(expected_version),
    )
    connection.execute(
        """
        UPDATE campus_location_versions
        SET valid_to = %s
        WHERE school_id = %s AND location_id = %s AND version = %s
        """,
        (effective_at, school_id, location_id, expected_version),
    )
    batch_id = _insert_formal_location_units(
        connection,
        school_id=school_id,
        slices=(item,),
    )
    connection.execute(
        """
        UPDATE campus_locations
        SET version = %s
        WHERE school_id = %s AND id = %s AND version = %s
        """,
        (target_version, school_id, location_id, expected_version),
    )
    connection.execute(
        """
        INSERT INTO campus_location_versions (
            id, school_id, location_id, business_code, display_name, aliases,
            display_path, parent_location_id, kind, valid_from, valid_to,
            source_kind, source_ref, status, version
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL,
            'manual', %s, 'confirmed', %s
        )
        """,
        (
            uuid4(),
            school_id,
            location_id,
            business_code,
            display_name,
            list(aliases),
            display_path,
            parent_location_id,
            kind,
            effective_at,
            f"change-set:{batch_id}",
            target_version,
        ),
    )


def _insert_bare_dorm_room(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    room_id: UUID,
    key: str,
) -> None:
    campus_id, building_id = uuid4(), uuid4()
    connection.execute(
        """
        INSERT INTO campus_locations (id, school_id, parent_location_id, source_key, kind, status)
        VALUES
            (%s, %s, NULL, %s, 'campus', 'active'),
            (%s, %s, %s, %s, 'building', 'active'),
            (%s, %s, %s, %s, 'dorm_room', 'active')
        """,
        (
            campus_id,
            school_id,
            f"campus:{key}",
            building_id,
            school_id,
            campus_id,
            f"building:{key}",
            room_id,
            school_id,
            building_id,
            f"room:{key}",
        ),
    )


def _insert_bed(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_id: UUID,
    room_id: UUID,
    bed_id: UUID,
    key: str,
    available_from: datetime | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO dorm_beds (id, school_id, room_location_id, source_key)
        VALUES (%s, %s, %s, %s)
        """,
        (bed_id, school_id, room_id, f"bed:{key}"),
    )
    connection.execute(
        """
        INSERT INTO dorm_bed_versions (
            id, school_id, bed_id, business_code, display_name, availability,
            valid_from, valid_to, version, source_id, import_batch_id
        ) VALUES (%s, %s, %s, %s, %s, 'available', %s, NULL, 1, %s, NULL)
        """,
        (
            uuid4(),
            school_id,
            bed_id,
            f"BED-{key}",
            f"{key}号床",
            available_from or _at(1),
            source_id,
        ),
    )


def _insert_student_school_relationship(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_id: UUID,
    student_id: UUID,
    key: str,
    valid_from: datetime,
    valid_to: datetime | None,
    enrollment_status: str = "enrolled",
    residence_mode: str = "boarding",
    version: int = 1,
) -> UUID:
    relationship_id = uuid4()
    campus_id = connection.execute(
        """
        SELECT id FROM campus_locations
        WHERE school_id = %s AND kind = 'campus'
        ORDER BY id LIMIT 1
        """,
        (school_id,),
    ).fetchone()
    assert campus_id is not None, "student relationship seed requires one campus"
    connection.execute(
        """
        INSERT INTO student_school_relationships (
            id, school_id, student_id, student_number, enrollment_status,
            residence_mode, primary_campus_location_id,
            administrative_campus_location_id, valid_from, valid_to, version,
            source_id, import_batch_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        """,
        (
            relationship_id,
            school_id,
            student_id,
            f"STU-{key}",
            enrollment_status,
            residence_mode,
            campus_id[0],
            campus_id[0],
            valid_from,
            valid_to,
            version,
            source_id,
        ),
    )
    return relationship_id


def _insert_residence(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    source_id: UUID,
    student_id: UUID,
    bed_id: UUID,
    valid_from: datetime,
    valid_to: datetime | None = None,
    residence_id: UUID | None = None,
    revoked_at: datetime | None = None,
) -> UUID:
    residence_id = residence_id or uuid4()
    connection.execute(
        """
        INSERT INTO student_bed_residences (
            id, school_id, student_id, bed_id, valid_from, valid_to, version,
            source_id, import_batch_id, revoked_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, NULL, %s)
        """,
        (
            residence_id,
            school_id,
            student_id,
            bed_id,
            valid_from,
            valid_to,
            source_id,
            revoked_at,
        ),
    )
    return residence_id


@pytest.mark.integration
def test_stable_source_keys_are_immutable(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id = uuid4()
    _insert_school(connection, school_id=school_id, source_key="immutable")

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), connection.transaction():
        connection.execute(
            "UPDATE schools SET source_key = 'changed' WHERE id = %s",
            (school_id,),
        )


@pytest.mark.integration
def test_business_identifiers_change_by_version_without_replacing_stable_roots_or_fks(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id = uuid4(), uuid4()
    room_id, bed_id = uuid4(), uuid4()
    residence_id: UUID
    _insert_school(connection, school_id=school_id, source_key="versioned-identifiers")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="versioned-identifiers",
    )
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=person_id,
        student_id=student_id,
        key="versioned-identifiers",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="versioned-identifiers",
        capacity=1,
        room_business_code="ROOM-OLD",
        room_display_name="旧宿舍",
        room_aliases=("旧宿舍",),
        room_display_path="/旧宿舍",
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=bed_id,
        key="versioned-identifiers",
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    with connection.transaction():
        connection.execute(
            """
            INSERT INTO school_versions (
                id, school_id, business_code, display_name, short_name,
                timezone_name, lifecycle_status, valid_from, valid_to,
                version, source_id, import_batch_id
            ) VALUES
                (%s, %s, 'SCH-OLD', '旧校名', NULL, 'Asia/Shanghai', 'active',
                 %s, %s, 1, %s, NULL),
                (%s, %s, 'SCH-NEW', '新校名', NULL, 'Asia/Shanghai', 'active',
                 %s, NULL, 2, %s, NULL)
            """,
            (uuid4(), school_id, _at(1), _at(3), source_id, uuid4(), school_id, _at(3), source_id),
        )
        _replace_confirmed_location_slice(
            connection,
            school_id=school_id,
            location_id=room_id,
            business_code="ROOM-NEW",
            display_name="新宿舍",
            aliases=("新宿舍",),
            display_path="/新宿舍",
            effective_at=_at(3),
        )
        _insert_student_school_relationship(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            key="OLD",
            valid_from=_at(1),
            valid_to=_at(3),
        )
        residence_id = _insert_residence(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            bed_id=bed_id,
            valid_from=_at(2),
            valid_to=_at(3),
        )
        _insert_student_school_relationship(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            key="NEW",
            valid_from=_at(3),
            valid_to=None,
            enrollment_status="transferred_out",
            residence_mode="day",
            version=2,
        )
        connection.execute(
            "UPDATE dorm_bed_versions SET valid_to = %s WHERE bed_id = %s",
            (_at(3), bed_id),
        )
        connection.execute(
            """
            INSERT INTO dorm_bed_versions (
                id, school_id, bed_id, business_code, display_name, availability,
                valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES (%s, %s, %s, 'BED-NEW', '新床位号', 'inactive',
                      %s, NULL, 2, %s, NULL)
            """,
            (uuid4(), school_id, bed_id, _at(3), source_id),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    roots = connection.execute(
        """
        SELECT school.id, school.source_key,
               student.id, student.source_key, student.person_id,
               room.id, room.source_key,
               bed.id, bed.source_key, bed.room_location_id,
               residence.student_id, residence.bed_id
        FROM schools AS school
        JOIN students AS student ON student.id = %s
        JOIN campus_locations AS room ON room.id = %s
        JOIN dorm_beds AS bed ON bed.id = %s
        JOIN student_bed_residences AS residence ON residence.id = %s
        WHERE school.id = %s
        """,
        (student_id, room_id, bed_id, residence_id, school_id),
    ).fetchone()
    assert roots == (
        school_id,
        "versioned-identifiers",
        student_id,
        "student:versioned-identifiers",
        person_id,
        room_id,
        "room:versioned-identifiers",
        bed_id,
        "bed:versioned-identifiers",
        room_id,
        student_id,
        bed_id,
    )
    assert connection.execute(
        "SELECT business_code FROM school_versions WHERE school_id = %s ORDER BY valid_from",
        (school_id,),
    ).fetchall() == [("SCH-OLD",), ("SCH-NEW",)]
    assert connection.execute(
        "SELECT student_number FROM student_school_relationships "
        "WHERE student_id = %s ORDER BY valid_from",
        (student_id,),
    ).fetchall() == [("STU-OLD",), ("STU-NEW",)]
    assert connection.execute(
        "SELECT business_code FROM campus_location_versions "
        "WHERE location_id = %s ORDER BY valid_from",
        (room_id,),
    ).fetchall() == [("ROOM-OLD",), ("ROOM-NEW",)]
    assert connection.execute(
        "SELECT business_code FROM dorm_bed_versions WHERE bed_id = %s ORDER BY valid_from",
        (bed_id,),
    ).fetchall() == [("BED-versioned-identifiers",), ("BED-NEW",)]


@pytest.mark.integration
def test_staff_number_changes_by_profile_version_without_breaking_assignment_fk(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, unit_id, assignment_id = uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="staff-number-version")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="staff-number-version",
    )
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, source_key, status, version)
        VALUES (%s, %s, 'person:staff-number-version', 'active', 1)
        """,
        (person_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO organization_units (
            id, school_id, parent_organization_unit_id, source_key, kind, status, version
        ) VALUES (%s, %s, NULL, 'unit:staff-number-version', 'department', 'active', 1)
        """,
        (unit_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO staff_assignments (
            id, school_id, source_key, person_id, organization_unit_id,
            position_title, semantic_role_code, assignment_kind,
            appointment_source, approval_reference, valid_from, valid_to,
            revoked_at, status, version
        ) VALUES (
            %s, %s, 'assignment:staff-number-version', %s, %s,
            '教师', 'teacher', 'substantive', 'school_record',
            'approval:staff-number-version', %s, NULL, NULL, 'confirmed', 1
        )
        """,
        (assignment_id, school_id, person_id, unit_id, _at(1)),
    )
    connection.execute(
        """
        INSERT INTO school_person_profile_versions (
            id, school_id, person_id, business_code, person_kind,
            lifecycle_status, valid_from, valid_to, version,
            source_id, import_batch_id
        ) VALUES
            (%s, %s, %s, 'STAFF-OLD', 'staff', 'active', %s, %s, 1, %s, NULL),
            (%s, %s, %s, 'STAFF-NEW', 'staff', 'active', %s, NULL, 2, %s, NULL)
        """,
        (
            uuid4(),
            school_id,
            person_id,
            _at(1),
            _at(3),
            source_id,
            uuid4(),
            school_id,
            person_id,
            _at(3),
            source_id,
        ),
    )

    assert connection.execute(
        """
        SELECT business_code
        FROM school_person_profile_versions
        WHERE school_id = %s AND person_id = %s
        ORDER BY valid_from
        """,
        (school_id, person_id),
    ).fetchall() == [("STAFF-OLD",), ("STAFF-NEW",)]
    assert connection.execute(
        """
        SELECT person.id, person.source_key,
               assignment.id, assignment.person_id, assignment.organization_unit_id
        FROM school_people AS person
        JOIN staff_assignments AS assignment
          ON assignment.school_id = person.school_id
         AND assignment.person_id = person.id
        WHERE person.school_id = %s AND person.id = %s
        """,
        (school_id, person_id),
    ).fetchone() == (
        person_id,
        "person:staff-number-version",
        assignment_id,
        person_id,
        unit_id,
    )


@pytest.mark.integration
def test_dorm_bed_room_ownership_is_immutable(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    first_room_id, second_room_id, bed_id = uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="immutable-bed-room")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="immutable-bed-room",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=first_room_id,
        key="immutable-bed-room-one",
        capacity=1,
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=second_room_id,
        key="immutable-bed-room-two",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=first_room_id,
        bed_id=bed_id,
        key="immutable-bed-room",
    )

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), connection.transaction():
        connection.execute(
            "UPDATE dorm_beds SET room_location_id = %s WHERE id = %s",
            (second_room_id, bed_id),
        )


@pytest.mark.integration
def test_dorm_room_profile_ownership_is_immutable(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    first_room_id, second_room_id = uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="immutable-room-profile")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="immutable-room-profile",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=first_room_id,
        key="immutable-room-profile",
        capacity=1,
    )
    _insert_bare_dorm_room(
        connection,
        school_id=school_id,
        room_id=second_room_id,
        key="immutable-room-profile-target",
    )
    profile_id = connection.execute(
        "SELECT id FROM dorm_room_profile_versions WHERE room_location_id = %s",
        (first_room_id,),
    ).fetchone()
    assert profile_id is not None

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), connection.transaction():
        connection.execute(
            "UPDATE dorm_room_profile_versions SET room_location_id = %s WHERE id = %s",
            (second_room_id, profile_id[0]),
        )


@pytest.mark.integration
def test_dorm_bed_version_ownership_is_immutable(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    room_id, first_bed_id, second_bed_id = uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="immutable-bed-version")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="immutable-bed-version",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="immutable-bed-version",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=first_bed_id,
        key="immutable-bed-version",
    )
    connection.execute(
        """
        INSERT INTO dorm_beds (id, school_id, room_location_id, source_key)
        VALUES (%s, %s, %s, 'bed:immutable-bed-version-target')
        """,
        (second_bed_id, school_id, room_id),
    )
    version_id = connection.execute(
        "SELECT id FROM dorm_bed_versions WHERE bed_id = %s",
        (first_bed_id,),
    ).fetchone()
    assert version_id is not None

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), connection.transaction():
        connection.execute(
            "UPDATE dorm_bed_versions SET bed_id = %s WHERE id = %s",
            (second_bed_id, version_id[0]),
        )


def _assert_same_school_foreign_keys_positive_versions_and_half_open_periods(
    connection: psycopg.Connection[Any],
) -> None:
    school_a, school_b = uuid4(), uuid4()
    source_a, source_b = uuid4(), uuid4()
    person_a, student_a = uuid4(), uuid4()
    room_b, bed_b = uuid4(), uuid4()
    _insert_school(connection, school_id=school_a, source_key="cross-a")
    _insert_school(connection, school_id=school_b, source_key="cross-b")
    _insert_source(connection, school_id=school_a, source_id=source_a, source_key="src-a")
    _insert_source(connection, school_id=school_b, source_id=source_b, source_key="src-b")
    _insert_person_and_student(
        connection,
        school_id=school_a,
        person_id=person_a,
        student_id=student_a,
        key="cross-a",
    )
    _insert_campus(connection, school_id=school_a, key="cross-a")
    _insert_room(
        connection,
        school_id=school_b,
        source_id=source_b,
        room_id=room_b,
        key="cross-b",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_b,
        source_id=source_b,
        room_id=room_b,
        bed_id=bed_b,
        key="cross-b",
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        _insert_residence(
            connection,
            school_id=school_a,
            source_id=source_a,
            student_id=student_a,
            bed_id=bed_b,
            valid_from=_at(2),
        )

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        _insert_student_school_relationship(
            connection,
            school_id=school_a,
            source_id=source_a,
            student_id=student_a,
            key="bad-version",
            valid_from=_at(2),
            valid_to=None,
            version=0,
        )

    with pytest.raises(psycopg.errors.InvalidParameterValue), connection.transaction():
        _insert_student_school_relationship(
            connection,
            school_id=school_a,
            source_id=source_a,
            student_id=student_a,
            key="empty-range",
            valid_from=_at(2),
            valid_to=_at(2),
        )


@pytest.mark.integration
def test_same_school_foreign_keys_positive_versions_and_half_open_periods(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    connection = psycopg.connect(
        **isolated_revision19_schema.migration_connection_parameters,
        autocommit=False,
    )
    try:
        connection.execute("SET ROLE campus_ai_db_owner")
        _assert_same_school_foreign_keys_positive_versions_and_half_open_periods(connection)
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.integration
def test_personal_contact_defaults_to_school_visibility_and_consent_is_optional(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id, person_id = uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="contact-visibility")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="contact-visibility",
    )
    connection.execute(
        """
        INSERT INTO school_people (id, school_id, source_key, status, version)
        VALUES (%s, %s, 'person:contact-visibility', 'active', 1)
        """,
        (person_id, school_id),
    )

    default_contact_id = uuid4()
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO person_contact_points (
                id, school_id, person_id, contact_kind, contact_value, is_primary,
                valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES (%s, %s, %s, 'mobile', '13800000000', true,
                      %s, NULL, 1, %s, NULL)
            """,
            (default_contact_id, school_id, person_id, _at(1), source_id),
        )
        default_scope = connection.execute(
            "SELECT visibility_scope, consent_reference FROM person_contact_points WHERE id = %s",
            (default_contact_id,),
        ).fetchone()
    assert default_scope == ("school_authenticated", None)

    for scope, audience, value in (
        ("private", None, "private@example.test"),
        ("school_authenticated", None, "school@example.test"),
        ("declared_audience", "staff_directory", "audience@example.test"),
    ):
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO person_contact_points (
                    id, school_id, person_id, contact_kind, contact_value, is_primary,
                    visibility_scope, audience_code, consent_reference,
                    valid_from, valid_to, version, source_id, import_batch_id
                ) VALUES (%s, %s, %s, 'email', %s, false, %s, %s, NULL,
                          %s, NULL, 1, %s, NULL)
                """,
                (uuid4(), school_id, person_id, value, scope, audience, _at(1), source_id),
            )

    for scope, audience, value in (
        ("declared_audience", None, "missing-audience@example.test"),
        ("private", "staff_directory", "private-with-audience@example.test"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO person_contact_points (
                    id, school_id, person_id, contact_kind, contact_value, is_primary,
                    visibility_scope, audience_code, consent_reference,
                    valid_from, valid_to, version, source_id, import_batch_id
                ) VALUES (%s, %s, %s, 'email', %s, false, %s, %s, NULL,
                          %s, NULL, 1, %s, NULL)
                """,
                (uuid4(), school_id, person_id, value, scope, audience, _at(1), source_id),
            )

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        connection.execute(
            """
            INSERT INTO official_contact_points (
                id, school_id, owner_kind, organization_unit_id, location_id,
                contact_kind, contact_value, service_hours, lifecycle_status,
                visibility_scope, audience_code,
                valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES (%s, %s, 'school', NULL, NULL,
                      'phone', '010-12345678', NULL, 'active',
                      'restricted', NULL,
                      %s, NULL, 1, %s, NULL)
            """,
            (uuid4(), school_id, _at(1), source_id),
        )


@pytest.mark.integration
def test_identity_subject_uniqueness_is_scoped_to_provider_tenant_namespace(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    subject = f"shared-subject-{uuid4()}"

    with connection.transaction():
        connection.execute(
            """
            INSERT INTO users (
                provider_code, provider_tenant_key, external_subject, display_name
            ) VALUES
                ('dingtalk', 'school-a', %s, '甲校用户'),
                ('dingtalk', 'school-b', %s, '乙校用户')
            """,
            (subject, subject),
        )

    with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
        connection.execute(
            """
            INSERT INTO users (
                provider_code, provider_tenant_key, external_subject, display_name
            ) VALUES ('dingtalk', 'school-a', %s, '甲校重复用户')
            """,
            (subject,),
        )


@pytest.mark.integration
def test_concurrent_first_identity_mapping_converges_on_one_namespaced_user(
    current_master_data_schema: PostgresRoles,
) -> None:
    token = uuid4().hex
    corporation_id = f"concurrent-corp-{token}"
    personnel_id = f"concurrent-person-{token}"
    legacy_subject = f"dingtalk:{corporation_id}:{personnel_id}"
    identity = DingTalkIdentity(
        corporation_id=corporation_id,
        personnel_id=personnel_id,
        display_name="并发首次映射",
    )
    first_flushed, second_started, release_first = Event(), Event(), Event()
    request_ids = (f"identity-a-{token[:20]}", f"identity-b-{token[:20]}")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                _commit_dingtalk_mapping,
                current_master_data_schema,
                identity=identity,
                request_id=request_ids[0],
                flushed=first_flushed,
                release=release_first,
            )
            if not first_flushed.wait(timeout=3):
                release_first.set()
                first.result(timeout=3)
                raise AssertionError("first identity mapping did not reach the flush barrier")
            second = executor.submit(
                _commit_dingtalk_mapping,
                current_master_data_schema,
                identity=identity,
                request_id=request_ids[1],
                started=second_started,
            )
            assert second_started.wait(timeout=3)
            sleep(0.2)
            assert second.done() is False
            release_first.set()
            first_result = first.result(timeout=6)
            second_result = second.result(timeout=6)

        assert first_result[1] != second_result[1]
        assert first_result[0] == second_result[0]
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            row = connection.execute(
                """
                SELECT id, count(*) OVER ()
                FROM users
                WHERE provider_code = 'dingtalk'
                  AND provider_tenant_key = %s
                  AND external_subject = %s
                """,
                (corporation_id, personnel_id),
            ).fetchone()
            assert row == (first_result[0], 1)
            audit_count = connection.execute(
                """
                SELECT count(*)
                FROM audit_logs
                WHERE event_type = 'identity.mapping.created'
                  AND target_id = %s
                """,
                (str(first_result[0]),),
            ).fetchone()
            assert audit_count == (1,)
    finally:
        release_first.set()
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE users
                    SET provider_code = 'legacy',
                        provider_tenant_key = 'legacy',
                        external_subject = %s
                    WHERE provider_code = 'dingtalk'
                      AND provider_tenant_key = %s
                      AND external_subject = %s
                    """,
                    (legacy_subject, corporation_id, personnel_id),
                )


@pytest.mark.integration
def test_concurrent_exact_legacy_claim_preserves_authorization_and_person_binding(
    current_master_data_schema: PostgresRoles,
) -> None:
    token = uuid4().hex
    corporation_id = f"legacy-corp-{token}"
    personnel_id = f"legacy-person-{token}"
    legacy_subject = f"dingtalk:{corporation_id}:{personnel_id}"
    school_id, person_id, binding_id = uuid4(), uuid4(), uuid4()
    identity = DingTalkIdentity(
        corporation_id=corporation_id,
        personnel_id=personnel_id,
        display_name="并发旧身份认领",
    )
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        with connection.transaction():
            legacy_user_id = connection.execute(
                """
                INSERT INTO users (
                    provider_code, provider_tenant_key, external_subject, display_name
                ) VALUES ('legacy', 'legacy', %s, '旧身份')
                RETURNING id
                """,
                (legacy_subject,),
            ).fetchone()
            assert legacy_user_id is not None
            user_id = int(legacy_user_id[0])
            connection.execute(
                """
                INSERT INTO schools (
                    id, source_key, school_code, display_name,
                    timezone_name, status, version
                ) VALUES (%s, %s, %s, '旧身份学校', 'Asia/Shanghai', 'active', 1)
                """,
                (school_id, f"legacy:schools:{school_id}", f"LEGACY-{token}"),
            )
            connection.execute(
                """
                INSERT INTO school_people (id, school_id, source_key, status, version)
                VALUES (%s, %s, %s, 'active', 1)
                """,
                (person_id, school_id, f"legacy:school_people:{person_id}"),
            )
            connection.execute(
                """
                INSERT INTO account_person_bindings (
                    id, school_id, source_key, account_user_id, person_id,
                    valid_from, valid_to, status, version
                ) VALUES (%s, %s, %s, %s, %s, %s, NULL, 'confirmed', 1)
                """,
                (
                    binding_id,
                    school_id,
                    f"legacy:account_person_bindings:{binding_id}",
                    user_id,
                    person_id,
                    _at(1),
                ),
            )
            role_id_row = connection.execute(
                """
                INSERT INTO roles (code, name, is_system)
                VALUES (%s, '并发认领保留角色', false)
                RETURNING id
                """,
                (f"identity-claim-{token}",),
            ).fetchone()
            assert role_id_row is not None
            role_id = int(role_id_row[0])
            connection.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                (user_id, role_id),
            )

    first_flushed, second_started, release_first = Event(), Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                _commit_dingtalk_mapping,
                current_master_data_schema,
                identity=identity,
                request_id=f"legacy-a-{token[:20]}",
                flushed=first_flushed,
                release=release_first,
            )
            if not first_flushed.wait(timeout=3):
                release_first.set()
                first.result(timeout=3)
                raise AssertionError("first legacy claim did not reach the flush barrier")
            second = executor.submit(
                _commit_dingtalk_mapping,
                current_master_data_schema,
                identity=identity,
                request_id=f"legacy-b-{token[:20]}",
                started=second_started,
            )
            assert second_started.wait(timeout=3)
            sleep(0.2)
            assert second.done() is False
            release_first.set()
            first_result = first.result(timeout=6)
            second_result = second.result(timeout=6)

        assert first_result[1] != second_result[1]
        assert first_result[0] == second_result[0] == user_id
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            users = connection.execute(
                """
                SELECT id
                FROM users
                WHERE provider_code = 'dingtalk'
                  AND provider_tenant_key = %s
                  AND external_subject = %s
                """,
                (corporation_id, personnel_id),
            ).fetchall()
            assert users == [(user_id,)]
            assert connection.execute(
                "SELECT user_id, role_id FROM user_roles WHERE user_id = %s",
                (user_id,),
            ).fetchall() == [(user_id, role_id)]
            binding = connection.execute(
                """
                SELECT id, school_id, account_user_id, person_id,
                       valid_from, valid_to, status, version
                FROM account_person_bindings
                WHERE id = %s
                """,
                (binding_id,),
            ).fetchone()
            assert binding == (
                binding_id,
                school_id,
                user_id,
                person_id,
                _at(1),
                None,
                "confirmed",
                1,
            )
    finally:
        release_first.set()
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE users
                    SET provider_code = 'legacy',
                        provider_tenant_key = 'legacy',
                        external_subject = %s,
                        display_name = '旧身份'
                    WHERE id = %s
                    """,
                    (legacy_subject, user_id),
                )


@pytest.mark.integration
def test_approved_assignment_student_and_bed_vocabularies_are_enforced_by_postgres(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id, unit_id = uuid4(), uuid4(), uuid4()
    room_id, bed_id = uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="approved-vocabularies")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="approved-vocabularies",
    )
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=person_id,
        student_id=student_id,
        key="approved-vocabularies",
    )
    connection.execute(
        """
        INSERT INTO organization_units (
            id, school_id, parent_organization_unit_id, source_key, kind, status, version
        ) VALUES (%s, %s, NULL, 'unit:approved-vocabularies', 'department', 'active', 1)
        """,
        (unit_id, school_id),
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="approved-vocabularies",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=bed_id,
        key="approved-vocabularies",
    )

    with connection.transaction():
        connection.execute(
            """
            INSERT INTO staff_assignments (
                id, school_id, source_key, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference, valid_from, valid_to,
                revoked_at, status, version
            ) VALUES (%s, %s, 'assignment:approved-vocabularies', %s, %s,
                      '教师', 'teacher', 'substantive', 'manual', 'approval-1',
                      %s, NULL, NULL, 'confirmed', 1)
            """,
            (uuid4(), school_id, person_id, unit_id, _at(1)),
        )
        _insert_student_school_relationship(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            key="suspended",
            valid_from=_at(1),
            valid_to=None,
            enrollment_status="suspended",
            residence_mode="day",
        )
        connection.execute(
            """
            UPDATE student_school_relationships
            SET enrollment_status = 'transferred_out'
            WHERE school_id = %s
            """,
            (school_id,),
        )
        connection.execute(
            "UPDATE dorm_bed_versions SET availability = 'inactive' WHERE bed_id = %s",
            (bed_id,),
        )

    invalid_updates: tuple[tuple[LiteralString, tuple[UUID]], ...] = (
        (
            "UPDATE staff_assignments SET assignment_kind = 'regular' WHERE school_id = %s",
            (school_id,),
        ),
        (
            "UPDATE student_school_relationships SET enrollment_status = 'leave' "
            "WHERE school_id = %s",
            (school_id,),
        ),
        (
            "UPDATE student_school_relationships SET enrollment_status = 'transferred' "
            "WHERE school_id = %s",
            (school_id,),
        ),
        (
            "UPDATE dorm_bed_versions SET availability = 'retired' WHERE bed_id = %s",
            (bed_id,),
        ),
    )
    for statement, parameters in invalid_updates:
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(statement, parameters)


@pytest.mark.integration
def test_only_enrolled_boarders_can_use_an_available_bed_within_room_capacity(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id = uuid4(), uuid4()
    room_id, bed_id = uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="boarding")
    _insert_source(connection, school_id=school_id, source_id=source_id, source_key="boarding")
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=person_id,
        student_id=student_id,
        key="boarding",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="boarding",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=bed_id,
        key="one",
    )
    _insert_student_school_relationship(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        key="day",
        valid_from=_at(1),
        valid_to=_at(2),
        residence_mode="day",
    )

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        _insert_residence(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            bed_id=bed_id,
            valid_from=_at(1),
            valid_to=_at(2),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    _insert_student_school_relationship(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        key="boarding",
        valid_from=_at(2),
        valid_to=None,
    )
    _insert_residence(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        bed_id=bed_id,
        valid_from=_at(2),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        _insert_bed(
            connection,
            school_id=school_id,
            source_id=source_id,
            room_id=room_id,
            bed_id=uuid4(),
            key="over-capacity",
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.integration
def test_upstream_facts_cannot_invalidate_an_existing_residence(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id = uuid4(), uuid4()
    room_id, bed_id = uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="reverse-validation")
    _insert_source(
        connection,
        school_id=school_id,
        source_id=source_id,
        source_key="reverse-validation",
    )
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=person_id,
        student_id=student_id,
        key="reverse-validation",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="reverse-validation",
        capacity=1,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=bed_id,
        key="reverse-validation",
    )
    relationship_id = _insert_student_school_relationship(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        key="reverse-validation",
        valid_from=_at(1),
        valid_to=None,
    )
    _insert_residence(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        bed_id=bed_id,
        valid_from=_at(2),
    )
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.execute("SET CONSTRAINTS ALL DEFERRED")

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        connection.execute(
            "UPDATE student_school_relationships SET residence_mode = 'day' WHERE id = %s",
            (relationship_id,),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        connection.execute(
            "UPDATE dorm_bed_versions SET availability = 'unavailable' WHERE bed_id = %s",
            (bed_id,),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        connection.execute(
            """
            UPDATE dorm_room_profile_versions
            SET is_available = false
            WHERE room_location_id = %s
            """,
            (room_id,),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.integration
def test_concurrent_relationship_change_and_residence_insert_cannot_both_commit(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    school_id, source_id = uuid4(), uuid4()
    race_key = f"residence-race:{school_id.hex}"
    person_id, student_id = uuid4(), uuid4()
    room_id, bed_id = uuid4(), uuid4()
    contender_started = Event()
    contender_pid: dict[str, int] = {}
    guarded_tables = (
        "student_bed_residences",
        "student_school_relationships",
        "dorm_bed_versions",
        "dorm_room_profile_versions",
    )
    ledger_cleanup_tables = (
        "school_fact_change_lineage",
        "master_data_import_command_outcomes",
        "master_data_import_units",
        "master_data_import_batches",
        "command_idempotency_records",
    )
    setup_committed = False
    updater: psycopg.Connection[Any] | None = None
    started = False
    blocked = False
    outcome = "not-started"

    def insert_residence() -> str:
        connection = psycopg.connect(
            **current_master_data_schema.migration_connection_parameters,
            autocommit=False,
        )
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute("SET LOCAL lock_timeout = '5s'")
        pid_row = connection.execute("SELECT pg_backend_pid()").fetchone()
        assert pid_row is not None
        contender_pid["value"] = int(pid_row[0])
        contender_started.set()
        try:
            _insert_residence(
                connection,
                school_id=school_id,
                source_id=source_id,
                student_id=student_id,
                bed_id=bed_id,
                valid_from=_at(2),
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            connection.commit()
            return "committed"
        except psycopg.errors.CheckViolation:
            connection.rollback()
            return "rejected"
        finally:
            connection.close()

    try:
        with psycopg.connect(
            **current_master_data_schema.migration_connection_parameters,
            autocommit=False,
        ) as setup:
            setup.execute("SET ROLE campus_ai_db_owner")
            _insert_school(setup, school_id=school_id, source_key=race_key)
            _insert_source(
                setup,
                school_id=school_id,
                source_id=source_id,
                source_key=race_key,
            )
            _insert_person_and_student(
                setup,
                school_id=school_id,
                person_id=person_id,
                student_id=student_id,
                key=race_key,
            )
            _insert_room(
                setup,
                school_id=school_id,
                source_id=source_id,
                room_id=room_id,
                key=race_key,
                capacity=1,
            )
            _insert_bed(
                setup,
                school_id=school_id,
                source_id=source_id,
                room_id=room_id,
                bed_id=bed_id,
                key=race_key,
            )
            relationship_id = _insert_student_school_relationship(
                setup,
                school_id=school_id,
                source_id=source_id,
                student_id=student_id,
                key=race_key,
                valid_from=_at(1),
                valid_to=None,
            )
        setup_committed = True

        updater = psycopg.connect(
            **current_master_data_schema.migration_connection_parameters,
            autocommit=False,
        )
        updater.execute("SET ROLE campus_ai_db_owner")
        updater.execute("SET LOCAL lock_timeout = '5s'")
        updater_pid = updater.execute("SELECT pg_backend_pid()").fetchone()
        assert updater_pid is not None
        updater.execute(
            "UPDATE student_school_relationships SET residence_mode = 'day' WHERE id = %s",
            (relationship_id,),
        )
        updater.execute("SET CONSTRAINTS ALL IMMEDIATE")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(insert_residence)
            started = contender_started.wait(timeout=2)
            if started:
                deadline = monotonic() + 2
                with current_master_data_schema.admin_connection() as observer:
                    while monotonic() < deadline and not future.done():
                        wait_row = observer.execute(
                            """
                            SELECT wait_event_type, pg_blocking_pids(%s)
                            FROM pg_stat_activity
                            WHERE pid = %s
                            """,
                            (contender_pid["value"], contender_pid["value"]),
                        ).fetchone()
                        if (
                            wait_row is not None
                            and wait_row[0] == "Lock"
                            and int(updater_pid[0]) in wait_row[1]
                        ):
                            blocked = True
                            break
                        sleep(0.02)

            if blocked:
                updater.commit()
            else:
                updater.rollback()
            outcome = future.result(timeout=6)
    finally:
        if updater is not None:
            updater.rollback()
            updater.close()
        if setup_committed:
            with psycopg.connect(
                **current_master_data_schema.migration_connection_parameters,
                autocommit=False,
            ) as cleanup:
                cleanup.execute("SET ROLE campus_ai_db_owner")
                for table_name in guarded_tables:
                    cleanup.execute(
                        f"ALTER TABLE public.{table_name} DISABLE TRIGGER "
                        f"trg_sf1_reject_delete_{table_name}"
                    )
                for table_name in ledger_cleanup_tables:
                    cleanup.execute(f"ALTER TABLE public.{table_name} DISABLE ROW LEVEL SECURITY")
                cleanup.execute(
                    "ALTER TABLE public.campus_location_versions DISABLE TRIGGER "
                    "trg_sf1_reject_delete_campus_location_versions"
                )
                cleanup.execute(
                    "ALTER TABLE public.staff_assignments DISABLE TRIGGER "
                    "trg_sf1_reject_delete_staff_assignments"
                )
                cleanup.execute(
                    "ALTER TABLE public.school_fact_change_lineage DISABLE TRIGGER "
                    "trg_sf2_reject_lineage_update"
                )
                cleanup.execute(
                    "ALTER TABLE public.master_data_import_command_outcomes DISABLE TRIGGER "
                    "reject_master_data_import_outcome_row_mutation"
                )
                cleanup.execute(
                    "DELETE FROM student_bed_residences WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM student_school_relationships WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM dorm_bed_versions WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute("DELETE FROM dorm_beds WHERE school_id = %s", (school_id,))
                cleanup.execute(
                    "DELETE FROM dorm_room_profile_versions WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM campus_location_versions WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM school_fact_change_lineage WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM master_data_import_command_outcomes WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM master_data_import_units WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM master_data_import_batches WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute(
                    "DELETE FROM command_idempotency_records WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute("DELETE FROM staff_assignments WHERE school_id = %s", (school_id,))
                cleanup.execute("DELETE FROM organization_units WHERE school_id = %s", (school_id,))
                for location_kind in ("dorm_room", "building", "campus"):
                    cleanup.execute(
                        "DELETE FROM campus_locations WHERE school_id = %s AND kind = %s",
                        (school_id, location_kind),
                    )
                cleanup.execute("DELETE FROM students WHERE school_id = %s", (school_id,))
                cleanup.execute("DELETE FROM school_people WHERE school_id = %s", (school_id,))
                cleanup.execute(
                    "DELETE FROM users WHERE provider_code = 'local_test' "
                    "AND provider_tenant_key = %s",
                    (str(school_id),),
                )
                cleanup.execute(
                    "DELETE FROM school_fact_sources WHERE school_id = %s",
                    (school_id,),
                )
                cleanup.execute("DELETE FROM schools WHERE id = %s", (school_id,))
                for table_name in ledger_cleanup_tables:
                    cleanup.execute(f"ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY")
                    cleanup.execute(f"ALTER TABLE public.{table_name} FORCE ROW LEVEL SECURITY")
                cleanup.execute(
                    "ALTER TABLE public.master_data_import_command_outcomes ENABLE TRIGGER "
                    "reject_master_data_import_outcome_row_mutation"
                )
                cleanup.execute(
                    "ALTER TABLE public.school_fact_change_lineage ENABLE TRIGGER "
                    "trg_sf2_reject_lineage_update"
                )
                cleanup.execute(
                    "ALTER TABLE public.staff_assignments ENABLE TRIGGER "
                    "trg_sf1_reject_delete_staff_assignments"
                )
                cleanup.execute(
                    "ALTER TABLE public.campus_location_versions ENABLE TRIGGER "
                    "trg_sf1_reject_delete_campus_location_versions"
                )
                for table_name in guarded_tables:
                    cleanup.execute(
                        f"ALTER TABLE public.{table_name} ENABLE TRIGGER "
                        f"trg_sf1_reject_delete_{table_name}"
                    )

    assert started is True
    assert blocked is True
    assert outcome == "rejected"


@pytest.mark.integration
def test_student_and_bed_residence_periods_cannot_overlap(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id = uuid4(), uuid4()
    room_id, first_bed, second_bed = uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="overlap")
    _insert_source(connection, school_id=school_id, source_id=source_id, source_key="overlap")
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=person_id,
        student_id=student_id,
        key="overlap",
    )
    _insert_room(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        key="overlap",
        capacity=2,
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=first_bed,
        key="overlap-one",
    )
    _insert_bed(
        connection,
        school_id=school_id,
        source_id=source_id,
        room_id=room_id,
        bed_id=second_bed,
        key="overlap-two",
    )
    _insert_student_school_relationship(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        key="overlap",
        valid_from=_at(1),
        valid_to=None,
    )
    _insert_residence(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=student_id,
        bed_id=first_bed,
        valid_from=_at(2),
        valid_to=_at(4),
    )

    with pytest.raises(psycopg.errors.ExclusionViolation), connection.transaction():
        _insert_residence(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=student_id,
            bed_id=second_bed,
            valid_from=_at(3),
            valid_to=_at(5),
        )

    second_person_id, second_student_id = uuid4(), uuid4()
    _insert_person_and_student(
        connection,
        school_id=school_id,
        person_id=second_person_id,
        student_id=second_student_id,
        key="overlap-second-student",
    )
    _insert_student_school_relationship(
        connection,
        school_id=school_id,
        source_id=source_id,
        student_id=second_student_id,
        key="overlap-second-student",
        valid_from=_at(1),
        valid_to=None,
    )
    with pytest.raises(psycopg.errors.ExclusionViolation), connection.transaction():
        _insert_residence(
            connection,
            school_id=school_id,
            source_id=source_id,
            student_id=second_student_id,
            bed_id=first_bed,
            valid_from=_at(3),
            valid_to=_at(5),
        )


@pytest.mark.integration
def test_authoritative_history_rejects_physical_delete(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    connection = current_owner_connection
    school_id, source_id, policy_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    _insert_school(connection, school_id=school_id, source_key="history")
    _insert_source(connection, school_id=school_id, source_id=source_id, source_key="history")
    connection.execute(
        """
        INSERT INTO school_policies (id, school_id, source_key)
        VALUES (%s, %s, 'policy:history')
        """,
        (policy_id, school_id),
    )
    connection.execute(
        """
        INSERT INTO school_policy_versions (
            id, school_id, policy_id, business_code, title, category_code,
            classification, published_at, effective_from, effective_to,
            content_text, version, source_id, import_batch_id
        ) VALUES (%s, %s, %s, 'POL-1', '制度一', 'general', 'internal',
                  %s, %s, NULL, NULL, 1, %s, NULL)
        """,
        (version_id, school_id, policy_id, _at(1), _at(1), source_id),
    )

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), connection.transaction():
        connection.execute("DELETE FROM school_policy_versions WHERE id = %s", (version_id,))


@pytest.mark.integration
def test_every_authoritative_history_table_uses_the_delete_guard(
    current_owner_connection: psycopg.Connection[Any],
) -> None:
    expected = {
        "school_versions",
        "school_person_names",
        "school_person_profile_versions",
        "account_person_bindings",
        "person_contact_points",
        "official_contact_points",
        "organization_unit_versions",
        "staff_assignments",
        "campus_location_versions",
        "campus_location_aliases",
        "dorm_room_profile_versions",
        "dorm_bed_versions",
        "student_school_relationships",
        "student_bed_residences",
        "student_contact_relationships",
        "school_policy_versions",
    }
    guarded = {
        row[0]
        for row in current_owner_connection.execute(
            """
            SELECT relation.relname
            FROM pg_catalog.pg_trigger AS trigger
            JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger.tgrelid
            JOIN pg_catalog.pg_proc AS procedure ON procedure.oid = trigger.tgfoid
            WHERE NOT trigger.tgisinternal
              AND procedure.proname = 'sf1_reject_history_delete'
            """
        ).fetchall()
    }
    assert expected <= guarded
