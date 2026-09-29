from __future__ import annotations

import base64
import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_facts import (
    HmacSchoolFactConfirmationCredentialIssuer,
    InMemoryPrivateSchoolFactSourceStore,
    InMemorySchoolFactPlanStore,
)
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_facts import (
    SchoolFactRequestIdentity,
    create_school_fact_router,
)
from app.platform.contracts import StaffActorContext
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeV1
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    context_for,
    make_owner_factory,
    make_school_fact_persistence_factory,
    seed_staff,
)
from tests.school_facts.test_sf4_simulated_workbook import _load_generator


def _apply_excel(
    client: TestClient,
    *,
    content: bytes,
    filename: str,
    base_version: int,
    idempotency_prefix: str,
) -> tuple[dict[str, object], dict[str, object]]:
    upload = client.post(
        "/api/v1/school-facts/sources",
        json={
            "filename_summary": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert upload.status_code == 201, upload.text
    preflight = client.post(
        "/api/v1/school-facts/plans/excel",
        json={
            "source_ref": upload.json()["source_ref"],
            "base_version": base_version,
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["is_clean"] is True, preflight.json()
    assert preflight.json()["issues"] == []
    plan_ref = preflight.json()["plan_ref"]
    prepared = client.post(
        f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
        headers={"Idempotency-Key": f"{idempotency_prefix}-prepare"},
    )
    assert prepared.status_code == 200, prepared.text
    applied = client.post(
        f"/api/v1/school-facts/plans/{plan_ref}/confirm",
        headers={"Idempotency-Key": f"{idempotency_prefix}-confirm"},
        json={"credential": prepared.json()["credential"]},
    )
    assert applied.status_code == 200, applied.text
    return preflight.json(), applied.json()


def _expected_unit_row(
    *,
    batch_id: UUID,
    sequence: int,
    change: CanonicalFactChangeV1,
) -> tuple[object, ...]:
    private_payload = change.model_dump(mode="json", exclude_none=True)
    persisted_payload = {
        "schema_version": change.schema_version,
        "operation": change.operation,
        "fact_kind": change.fact_kind,
        "source_key": change.source_key,
        "expected_version": change.expected_version,
        "effective_at": private_payload["effective_at"],
        "origin": change.origin.model_dump(mode="json", exclude_none=True),
    }
    encoded = json.dumps(
        private_payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        batch_id,
        sequence,
        "applied",
        persisted_payload,
        sha256(encoded).hexdigest(),
        change.expected_version + 1,
    )


def test_sf4_workbooks_use_formal_http_channel_and_match_declared_ground_truth(
    isolated_migration_schema: PostgresRoles,
) -> None:
    generator = _load_generator()
    ground_truth = generator.build_ground_truth()
    factory, engine = make_owner_factory(isolated_migration_schema)
    persistence_engine = None
    try:
        school_id, user_id, person_id, assignment_id = seed_staff(
            factory,
            school_source_key="sf4:school:xinglan",
            bootstrap_valid_from=datetime(2023, 12, 31, 0, 0, tzinfo=UTC),
        )
        persistence_factory, persistence_engine = make_school_fact_persistence_factory(
            isolated_migration_schema,
            factory,
        )
        # 正式通道需要预绑定学校租户和一个可信 actor；它们是明确披露的技术前置，
        # 不属于 ground truth 的 455 条 SF4 变更。
        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 0
            )
            assert session.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM schools WHERE id = :school_id), "
                    "(SELECT count(*) FROM school_people WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM organization_units "
                    " WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM staff_assignments "
                    " WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM account_person_bindings "
                    " WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM students WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM campus_locations WHERE school_id = :school_id), "
                    "(SELECT count(*) FROM school_policies WHERE school_id = :school_id)"
                ),
                {"school_id": school_id},
            ).one() == (1, 1, 1, 1, 1, 0, 0, 0)
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_people "
                        "WHERE school_id = :school_id AND source_key LIKE 'sf4:%'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 0
            )

        with factory.begin() as session:
            session.execute(
                text(
                    "INSERT INTO users "
                    "(provider_code, provider_tenant_key, external_subject, display_name, "
                    "is_active, security_version) VALUES "
                    "('sf4_synthetic', 'sf4-xinglan.invalid', :subject, :display_name, "
                    "true, 1)"
                ),
                [
                    {
                        "subject": f"fictional-staff-{index:03d}",
                        "display_name": f"SF4 登录显示缓存 {index:03d}",
                    }
                    for index in range(1, 25)
                ],
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key="sf4-router-bootstrap",
        )
        actor = context.actor
        assert isinstance(actor, StaffActorContext)
        signing_key = os.environ.get("RLS_CONTEXT_SIGNING_KEY")
        assert signing_key
        signer = ContextProofSigner(signing_key.encode("utf-8"))
        persistence = SqlAlchemySchoolFactPersistence(
            persistence_factory,
            lambda execution_context: signer.sign_execution_context(
                execution_context,
                session_nonce_hash=sha256(b"sf4-isolated-postgres-session").hexdigest(),
                proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
            confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                b"sf4-formal-confirmation-signing-key-32-bytes"
            ),
        )
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: ground_truth.as_of,
        )

        def identity() -> SchoolFactRequestIdentity:
            return SchoolFactRequestIdentity(
                actor=actor,
                session_binding="sf4-formal-http-session",
                authorized_actions=frozenset({"school_facts.manage"}),
            )

        app = FastAPI()
        app.middleware("http")(assign_request_id)
        app.add_exception_handler(ApiError, api_error_handler)
        app.add_exception_handler(
            RequestValidationError,
            request_validation_error_handler,
        )
        app.include_router(
            create_school_fact_router(
                change_service=service,
                source_store=InMemoryPrivateSchoolFactSourceStore(clock=lambda: ground_truth.as_of),
                plan_store=InMemorySchoolFactPlanStore(clock=lambda: ground_truth.as_of),
                identity_dependency=identity,
                clock=lambda: ground_truth.as_of,
            )
        )

        with TestClient(app) as client:
            base_preflight, base_result = _apply_excel(
                client,
                content=generator.BASE_WORKBOOK_PATH.read_bytes(),
                filename="sf4-fictional-base-school-facts-v2.xlsx",
                base_version=1,
                idempotency_prefix="sf4-base",
            )
            lifecycle_preflight, lifecycle_result = _apply_excel(
                client,
                content=generator.LIFECYCLE_WORKBOOK_PATH.read_bytes(),
                filename="sf4-fictional-lifecycle-school-facts-v2.xlsx",
                base_version=2,
                idempotency_prefix="sf4-lifecycle",
            )

        base_changes = base_preflight["changes"]
        lifecycle_changes = lifecycle_preflight["changes"]
        assert isinstance(base_changes, list)
        assert isinstance(lifecycle_changes, list)
        assert len(base_changes) == len(ground_truth.base_changes)
        assert len(lifecycle_changes) == len(ground_truth.lifecycle_changes)
        assert base_result["changed_count"] == len(ground_truth.base_changes)
        assert base_result["target_version"] == 2
        assert lifecycle_result["changed_count"] == len(ground_truth.lifecycle_changes)
        assert lifecycle_result["target_version"] == 3
        batch_ids = (
            UUID(str(base_result["batch_id"])),
            UUID(str(lifecycle_result["batch_id"])),
        )

        expected_lineage = Counter(
            (
                change.fact_kind,
                change.source_key,
                change.operation,
                change.expected_version + 1,
            )
            for change in ground_truth.all_changes
        )
        with factory() as session:
            actual_lineage = Counter(
                session.execute(
                    text(
                        "SELECT fact_kind, fact_source_key, operation, target_version "
                        "FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id AND batch_id = ANY(:batch_ids)"
                    ),
                    {"school_id": school_id, "batch_ids": list(batch_ids)},
                )
                .tuples()
                .all()
            )
            assert actual_lineage == expected_lineage
            batch_rows = (
                session.execute(
                    text(
                        "SELECT id, status, planned_units, succeeded_units, failed_units, "
                        "pending_units, base_fact_version, target_fact_version "
                        "FROM master_data_import_batches "
                        "WHERE school_id = :school_id AND id = ANY(:batch_ids) "
                        "ORDER BY base_fact_version"
                    ),
                    {"school_id": school_id, "batch_ids": list(batch_ids)},
                )
                .tuples()
                .all()
            )
            assert batch_rows == [
                (
                    batch_ids[0],
                    "applied",
                    len(ground_truth.base_changes),
                    len(ground_truth.base_changes),
                    0,
                    0,
                    1,
                    2,
                ),
                (
                    batch_ids[1],
                    "applied",
                    len(ground_truth.lifecycle_changes),
                    len(ground_truth.lifecycle_changes),
                    0,
                    0,
                    2,
                    3,
                ),
            ]
            unit_rows = (
                session.execute(
                    text(
                        "SELECT batch_id, sequence, status, payload, payload_sha256, "
                        "target_version "
                        "FROM master_data_import_units "
                        "WHERE school_id = :school_id AND batch_id = ANY(:batch_ids) "
                        "ORDER BY batch_id, sequence"
                    ),
                    {"school_id": school_id, "batch_ids": list(batch_ids)},
                )
                .tuples()
                .all()
            )
            expected_units = sorted(
                [
                    _expected_unit_row(
                        batch_id=batch_id,
                        sequence=sequence,
                        change=change,
                    )
                    for batch_id, changes in (
                        (batch_ids[0], ground_truth.base_changes),
                        (batch_ids[1], ground_truth.lifecycle_changes),
                    )
                    for sequence, change in enumerate(changes, start=1)
                ],
                key=lambda row: (row[0], row[1]),
            )
            assert unit_rows == expected_units

            # 场景 1：一人多岗、跨组织兼职、临时代任、历史任职。
            assert (
                session.execute(
                    text(
                        "SELECT a.source_key, o.source_key, a.assignment_kind "
                        "FROM staff_assignments a "
                        "JOIN school_people p ON p.id = a.person_id "
                        "JOIN organization_units o ON o.id = a.organization_unit_id "
                        "WHERE a.school_id = :school_id "
                        "AND p.source_key = 'sf4:person:staff:001' "
                        "ORDER BY a.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                (
                    "sf4:assignment:main:001",
                    "sf4:org:east-teaching",
                    "substantive",
                ),
                (
                    "sf4:assignment:staff001:parttime",
                    "sf4:org:west-teaching",
                    "part_time",
                ),
            ]
            assert session.execute(
                text(
                    "SELECT assignment_kind, valid_to FROM staff_assignments "
                    "WHERE school_id = :school_id "
                    "AND source_key = 'sf4:assignment:staff002:acting'"
                ),
                {"school_id": school_id},
            ).one() == ("acting", datetime(2026, 9, 30, 0, 0, tzinfo=UTC))
            assert (
                session.execute(
                    text(
                        "SELECT valid_to < :as_of FROM staff_assignments "
                        "WHERE school_id = :school_id "
                        "AND source_key = 'sf4:assignment:staff003:history'"
                    ),
                    {"school_id": school_id, "as_of": ground_truth.as_of},
                ).scalar_one()
                is True
            )

            # 场景 2：重名、联系方式缺失、联系方式历史。
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_person_names "
                        "WHERE school_id = :school_id AND display_name = '模拟重名'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 2
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM person_contact_points c "
                        "JOIN school_people p ON p.id = c.person_id "
                        "WHERE c.school_id = :school_id "
                        "AND p.source_key = 'sf4:person:staff:024'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 0
            )
            assert (
                session.execute(
                    text(
                        "SELECT l.fact_source_key, c.contact_value, c.is_primary, "
                        "c.valid_from, c.valid_to "
                        "FROM school_fact_change_lineage l "
                        "JOIN person_contact_points c ON c.id = l.target_id "
                        "WHERE l.school_id = :school_id "
                        "AND l.fact_kind = 'person_contact' "
                        "AND l.operation = 'upsert' "
                        "AND l.fact_source_key LIKE 'sf4:person-contact:staff023:%' "
                        "ORDER BY l.fact_source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                (
                    "sf4:person-contact:staff023:new",
                    "000-2300-0002",
                    True,
                    datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
                    None,
                ),
                (
                    "sf4:person-contact:staff023:old",
                    "000-2300-0001",
                    False,
                    datetime(2025, 6, 1, 0, 0, tzinfo=UTC),
                    datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
                ),
            ]

            # 场景 3：地点更名、别名歧义、地点失效。
            assert (
                session.execute(
                    text(
                        "SELECT v.display_name, v.valid_from, v.valid_to "
                        "FROM campus_location_versions v "
                        "JOIN campus_locations l ON l.id = v.location_id "
                        "WHERE v.school_id = :school_id "
                        "AND l.source_key = 'sf4:location:learning-center' "
                        "ORDER BY v.valid_from"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                (
                    "旧学习中心",
                    datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
                    datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
                ),
                (
                    "星澜学习中心",
                    datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
                    None,
                ),
            ]
            assert (
                session.execute(
                    text(
                        "SELECT a.alias, l.source_key "
                        "FROM campus_location_aliases a "
                        "JOIN campus_locations l ON l.id = a.location_id "
                        "WHERE a.school_id = :school_id AND a.alias = '活动室' "
                        "ORDER BY l.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                ("活动室", "sf4:location:common-room-a"),
                ("活动室", "sf4:location:common-room-b"),
            ]
            assert (
                session.execute(
                    text(
                        "SELECT status FROM campus_locations "
                        "WHERE school_id = :school_id "
                        "AND source_key = 'sf4:location:retired-store'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == "inactive"
            )

            # 场景 4：空床与不可用床。
            assert (
                session.execute(
                    text(
                        "SELECT v.availability FROM dorm_beds b "
                        "JOIN dorm_bed_versions v ON v.school_id = b.school_id "
                        "AND v.bed_id = b.id "
                        "WHERE b.school_id = :school_id "
                        "AND b.source_key = 'sf4:bed:60' "
                        "AND v.valid_from <= :as_of "
                        "AND (v.valid_to IS NULL OR v.valid_to > :as_of)"
                    ),
                    {"school_id": school_id, "as_of": ground_truth.as_of},
                ).scalar_one()
                == "unavailable"
            )
            available_empty_beds = session.execute(
                text(
                    "SELECT count(*) FROM dorm_beds b "
                    "JOIN dorm_bed_versions v ON v.school_id = b.school_id "
                    "AND v.bed_id = b.id "
                    "WHERE b.school_id = :school_id AND v.availability = 'available' "
                    "AND v.valid_from <= :as_of "
                    "AND (v.valid_to IS NULL OR v.valid_to > :as_of) "
                    "AND NOT EXISTS ("
                    " SELECT 1 FROM student_bed_residences r "
                    " WHERE r.school_id = b.school_id AND r.bed_id = b.id "
                    " AND r.valid_from <= :as_of "
                    " AND (r.valid_to IS NULL OR r.valid_to > :as_of) "
                    " AND (r.revoked_at IS NULL OR r.revoked_at > :as_of))"
                ),
                {"school_id": school_id, "as_of": ground_truth.as_of},
            ).scalar_one()
            assert available_empty_beds > 0

            # 场景 5、6、7、8：future、连续调宿、同床连续切片、有头有尾。
            residence_rows = (
                session.execute(
                    text(
                        "SELECT l.fact_source_key, s.source_key AS student_source_key, "
                        "b.source_key AS bed_source_key, "
                        "r.valid_from, r.valid_to, r.revoked_at "
                        "FROM school_fact_change_lineage l "
                        "JOIN student_bed_residences r ON r.id = l.target_id "
                        "JOIN students s ON s.id = r.student_id "
                        "JOIN dorm_beds b ON b.id = r.bed_id "
                        "WHERE l.school_id = :school_id "
                        "AND l.fact_kind = 'student_bed_residence' "
                        "AND l.operation = 'upsert'"
                    ),
                    {"school_id": school_id},
                )
                .mappings()
                .all()
            )
            by_source = {row["fact_source_key"]: row for row in residence_rows}
            assert by_source["sf4:residence:s050:future"]["valid_from"] > ground_truth.as_of
            transfer_a = by_source["sf4:residence:s001:a"]
            transfer_b = by_source["sf4:residence:s001:b"]
            transfer_c = by_source["sf4:residence:current:001"]
            assert (
                transfer_a["student_source_key"],
                transfer_b["student_source_key"],
                transfer_c["student_source_key"],
            ) == ("sf4:student:001",) * 3
            assert (
                transfer_a["bed_source_key"],
                transfer_b["bed_source_key"],
                transfer_c["bed_source_key"],
            ) == ("sf4:bed:56", "sf4:bed:57", "sf4:bed:31")
            assert transfer_a["valid_to"] == transfer_b["valid_from"]
            assert transfer_b["valid_to"] < transfer_c["valid_from"]

            ended_residence = by_source["sf4:residence:s049:ended"]
            assert ended_residence["student_source_key"] == "sf4:student:049"
            assert ended_residence["valid_to"] == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
            assert ended_residence["valid_to"] < ground_truth.as_of

            same_bed_history = by_source["sf4:residence:s002:same-bed"]
            same_bed_current = by_source["sf4:residence:current:002"]
            assert (
                same_bed_history["student_source_key"],
                same_bed_current["student_source_key"],
            ) == ("sf4:student:002", "sf4:student:002")
            assert (
                same_bed_history["bed_source_key"],
                same_bed_current["bed_source_key"],
            ) == ("sf4:bed:01", "sf4:bed:01")
            assert same_bed_history["valid_to"] == same_bed_current["valid_from"]
            bounded = by_source["sf4:residence:s051:bounded"]
            assert bounded["valid_from"] is not None and bounded["valid_to"] is not None
            assert bounded["valid_from"] == datetime(2025, 1, 10, 0, 0, tzinfo=UTC)
            assert bounded["valid_to"] == datetime(2025, 6, 30, 0, 0, tzinfo=UTC)
            assert bounded["valid_from"] < bounded["valid_to"]
            assert bounded["valid_to"] < ground_truth.as_of

            # 场景 9、10：走读校区字段与跨校区住宿是两个独立事实。
            assert (
                session.execute(
                    text(
                        "SELECT s.source_key, rel.residence_mode, "
                        "primary_campus.source_key, administrative_campus.source_key "
                        "FROM student_school_relationships rel "
                        "JOIN students s ON s.id = rel.student_id "
                        "JOIN campus_locations primary_campus "
                        "ON primary_campus.id = rel.primary_campus_location_id "
                        "JOIN campus_locations administrative_campus "
                        "ON administrative_campus.id = rel.administrative_campus_location_id "
                        "WHERE rel.school_id = :school_id AND rel.residence_mode = 'day' "
                        "ORDER BY s.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                (
                    f"sf4:student:{index:03d}",
                    "day",
                    "sf4:campus:west",
                    "sf4:campus:west",
                )
                for index in range(53, 61)
            ]
            assert session.execute(
                text(
                    "SELECT primary_campus.source_key, residence_bed.source_key, "
                    "residence_campus.source_key "
                    "FROM students s "
                    "JOIN student_school_relationships rel ON rel.student_id = s.id "
                    "JOIN campus_locations primary_campus "
                    "ON primary_campus.id = rel.primary_campus_location_id "
                    "JOIN student_bed_residences residence ON residence.student_id = s.id "
                    "JOIN dorm_beds residence_bed ON residence_bed.id = residence.bed_id "
                    "JOIN campus_locations room ON room.id = residence_bed.room_location_id "
                    "JOIN campus_locations floor ON floor.id = room.parent_location_id "
                    "JOIN campus_locations building ON building.id = floor.parent_location_id "
                    "JOIN campus_locations residence_campus "
                    "ON residence_campus.id = building.parent_location_id "
                    "WHERE s.school_id = :school_id AND s.source_key = 'sf4:student:001' "
                    "AND residence.valid_from <= :as_of "
                    "AND (residence.valid_to IS NULL OR residence.valid_to > :as_of) "
                    "AND residence.revoked_at IS NULL"
                ),
                {"school_id": school_id, "as_of": ground_truth.as_of},
            ).one() == ("sf4:campus:east", "sf4:bed:31", "sf4:campus:west")

            # 场景 11：一位联系人、祖辈联系人，不产生家庭结构字段。
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM student_contact_relationships rel "
                        "JOIN students s ON s.id = rel.student_id "
                        "WHERE rel.school_id = :school_id "
                        "AND s.source_key = 'sf4:student:001'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 1
            )
            assert (
                session.execute(
                    text(
                        "SELECT relationship_kind FROM student_contact_relationships rel "
                        "JOIN students s ON s.id = rel.student_id "
                        "WHERE rel.school_id = :school_id "
                        "AND s.source_key = 'sf4:student:002'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == "grandfather"
            )

            # 场景 12：制度多版本与虚构 restricted 正文哨兵。
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM school_policy_versions v "
                        "JOIN school_policies p ON p.id = v.policy_id "
                        "WHERE v.school_id = :school_id "
                        "AND p.source_key = 'sf4:policy:01'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 3
            )
            assert session.execute(
                text(
                    "SELECT classification, content_text FROM school_policy_versions v "
                    "JOIN school_fact_change_lineage l ON l.target_id = v.id "
                    "WHERE l.school_id = :school_id "
                    "AND l.fact_source_key = 'sf4:policy-version:02:restricted'"
                ),
                {"school_id": school_id},
            ).one() == (
                "restricted",
                "FICTIONAL-RESTRICTED-SENTINEL-SF4：仅验证存储，不声明权限保护。",
            )

            # 场景 13：撤销与原计划结束时间分别保存。
            assignment_expiry, assignment_revoked = session.execute(
                text(
                    "SELECT v.planned_valid_to, v.revoked_at "
                    "FROM staff_assignment_versions v "
                    "JOIN staff_assignments a ON a.id = v.assignment_id "
                    "WHERE v.school_id = :school_id "
                    "AND a.source_key = 'sf4:assignment:main:004'"
                ),
                {"school_id": school_id},
            ).one()
            assert assignment_expiry == datetime(2027, 6, 30, 0, 0, tzinfo=UTC)
            assert assignment_revoked == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
            policy_expiry, policy_revoked = session.execute(
                text(
                    "SELECT v.effective_to, v.revoked_at FROM school_policy_versions v "
                    "JOIN school_fact_change_lineage l ON l.target_id = v.id "
                    "WHERE l.school_id = :school_id "
                    "AND l.fact_source_key = 'sf4:policy-version:03:revoked' "
                    "AND l.operation = 'upsert'"
                ),
                {"school_id": school_id},
            ).one()
            assert policy_expiry == datetime(2027, 7, 15, 0, 0, tzinfo=UTC)
            assert policy_revoked == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

            # 场景 14、16：全校组织显式双校区，专属组织只关联一个校区。
            organization_link_counts = dict(
                session.execute(
                    text(
                        "SELECT o.source_key, count(*) "
                        "FROM organization_campus_relationships rel "
                        "JOIN organization_units o ON o.id = rel.organization_unit_id "
                        "WHERE rel.school_id = :school_id "
                        "GROUP BY o.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            )
            assert organization_link_counts == {
                "sf4:org:school-office": 2,
                "sf4:org:east-teaching": 1,
                "sf4:org:west-teaching": 1,
                "sf4:org:east-dorm": 1,
                "sf4:org:west-dorm": 1,
            }

            campus_staff_counts = dict(
                session.execute(
                    text(
                        "SELECT campus.source_key, count(DISTINCT a.person_id) "
                        "FROM campus_locations campus "
                        "JOIN organization_campus_relationships rel "
                        "ON rel.campus_location_id = campus.id "
                        "JOIN organization_units o ON o.id = rel.organization_unit_id "
                        "JOIN staff_assignments a ON a.organization_unit_id = o.id "
                        "WHERE campus.school_id = :school_id "
                        "AND ((campus.source_key = 'sf4:campus:east' "
                        "AND o.source_key LIKE 'sf4:org:east%') "
                        "OR (campus.source_key = 'sf4:campus:west' "
                        "AND o.source_key LIKE 'sf4:org:west%')) "
                        "AND a.valid_from <= :as_of "
                        "AND (a.valid_to IS NULL OR a.valid_to > :as_of) "
                        "AND (a.revoked_at IS NULL OR a.revoked_at > :as_of) "
                        "GROUP BY campus.source_key"
                    ),
                    {"school_id": school_id, "as_of": ground_truth.as_of},
                )
                .tuples()
                .all()
            )
            assert set(campus_staff_counts) == {
                "sf4:campus:east",
                "sf4:campus:west",
            }
            assert all(count > 0 for count in campus_staff_counts.values())

            campus_student_counts = dict(
                session.execute(
                    text(
                        "SELECT campus.source_key, count(DISTINCT rel.student_id) "
                        "FROM student_school_relationships rel "
                        "JOIN campus_locations campus "
                        "ON campus.id = rel.primary_campus_location_id "
                        "WHERE rel.school_id = :school_id "
                        "AND rel.valid_from <= :as_of "
                        "AND (rel.valid_to IS NULL OR rel.valid_to > :as_of) "
                        "GROUP BY campus.source_key"
                    ),
                    {"school_id": school_id, "as_of": ground_truth.as_of},
                )
                .tuples()
                .all()
            )
            assert set(campus_student_counts) == {
                "sf4:campus:east",
                "sf4:campus:west",
            }
            assert all(count > 0 for count in campus_student_counts.values())

            campus_dorm_counts = dict(
                session.execute(
                    text(
                        "SELECT campus.source_key, count(*) "
                        "FROM campus_locations building "
                        "JOIN campus_locations campus "
                        "ON campus.id = building.parent_location_id "
                        "WHERE building.school_id = :school_id "
                        "AND building.kind = 'building' "
                        "GROUP BY campus.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            )
            assert campus_dorm_counts == {
                "sf4:campus:east": 1,
                "sf4:campus:west": 1,
            }

            # 场景 15：制度适用范围分别只指向校区或组织。
            assert (
                session.execute(
                    text(
                        "SELECT scope.source_key, policy.source_key, "
                        "campus.source_key, organization.source_key "
                        "FROM school_policy_scopes scope "
                        "JOIN school_policies policy ON policy.id = scope.policy_id "
                        "LEFT JOIN campus_locations campus "
                        "ON campus.id = scope.campus_location_id "
                        "LEFT JOIN organization_units organization "
                        "ON organization.id = scope.organization_unit_id "
                        "WHERE scope.school_id = :school_id "
                        "ORDER BY scope.source_key"
                    ),
                    {"school_id": school_id},
                )
                .tuples()
                .all()
            ) == [
                (
                    "sf4:policy-scope:01:east",
                    "sf4:policy:01",
                    "sf4:campus:east",
                    None,
                ),
                (
                    "sf4:policy-scope:02:west-dorm",
                    "sf4:policy:02",
                    None,
                    "sf4:org:west-dorm",
                ),
            ]

            current_residents = session.execute(
                text(
                    "SELECT count(DISTINCT student_id) FROM student_bed_residences "
                    "WHERE school_id = :school_id AND valid_from <= :as_of "
                    "AND (valid_to IS NULL OR valid_to > :as_of) "
                    "AND (revoked_at IS NULL OR revoked_at > :as_of)"
                ),
                {"school_id": school_id, "as_of": ground_truth.as_of},
            ).scalar_one()
            assert current_residents == ground_truth.current_resident_count() == 48
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM students "
                        "WHERE school_id = :school_id AND source_key LIKE 'sf4:student:%'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 60
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM dorm_beds "
                        "WHERE school_id = :school_id AND source_key LIKE 'sf4:bed:%'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 60
            )
    finally:
        if persistence_engine is not None:
            persistence_engine.dispose()  # type: ignore[union-attr]
        engine.dispose()  # type: ignore[union-attr]
