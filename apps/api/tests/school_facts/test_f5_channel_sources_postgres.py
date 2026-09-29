"""Real schema36 narrow contact provenance and assembled Guardian consumption."""

# ruff: noqa: E501
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_family_teaching_query import (
    FamilyTeachingQueryService,
    FamilyTeachingRepository,
)
from tests.school_facts.test_f3_correction_postgres import guardian
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_writer_postgres import apply

pytestmark = pytest.mark.skipif(
    not os.environ.get("F5_TEACHING_PG"), reason="explicit owned schema36 PG only"
)


def test_formal_calendar_timezone_is_narrow_and_used_by_guardian_assembly(channels):
    engines, candidate, params, raw = channels
    calendars = [r for r in raw["records"] if r["fact_kind"] == "education_calendar_rule"]
    assert calendars
    zones = set()
    for record in calendars:
        for segment in record["segments"]:
            assert set(segment["content"]) == {"display_name", "timezone_name"}
            zones.add(segment["content"]["timezone_name"])
    assert len(zones) == 1
    actor, _ = guardian(engines, source=candidate.source_key)
    service = FamilyTeachingQueryService(
        repository=FamilyTeachingRepository(sessionmaker(engines["reader"])),
        current_actor=lambda a: a == actor,
    )
    result = service.query(actor=actor)
    public = result.public()
    assert result.evidence and result.evidence.valid(actor)
    assert public.get("fact_timezone_name") == next(iter(zones)), {
        "status": public.get("status"),
        "period_status": public.get("period_status"),
        "keys": list(public),
    }
    material = education_model_material(public)
    assert next(iter(zones)) in material["time_labels"]["as_of"]
    for role in ("runtime", "writer"):
        with engines[role].connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(
                    text(
                        "SELECT app.school_fact_family_teaching(:s,:a,:r,:v,NULL,'current',NULL,NULL,false)"
                    ),
                    params,
                )


@pytest.fixture
def channels():
    meta = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert meta["owner"] == "F5 persistence and reader controlled tests"
    engines = {
        role: safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "reader", "runtime", "writer")
    }
    try:
        with engines["migration"].connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) in {
                "20260914_36",
                "20260914_37",
            }
            candidates = c.execute(
                text(
                    "SELECT DISTINCT p.source_key,g.school_id,g.guardian_person_id,g.id,g.version FROM guardian_relationships g JOIN school_people p ON p.id=g.guardian_person_id JOIN account_person_bindings a ON a.person_id=p.id AND a.school_id=p.school_id WHERE g.status='confirmed' AND g.relationship_kind='verified_guardian' AND g.revoked_at IS NULL AND g.authority_version=1 AND g.valid_from<=now() AND (g.valid_to IS NULL OR now()<g.valid_to)"
                )
            ).all()
        assert len(candidates) <= 100
        for candidate in candidates:
            params = dict(
                s=candidate.school_id,
                a=candidate.guardian_person_id,
                r=candidate.id,
                v=candidate.version,
            )
            with engines["reader"].connect() as c:
                raw = c.scalar(
                    text(
                        "SELECT app.school_fact_family_teaching(:s,:a,:r,:v,NULL,'current',NULL,NULL,true)"
                    ),
                    params,
                )
            if raw.get("related_teachers") and not any(
                t["official_contacts"] for t in raw["related_teachers"]
            ):
                teacher = raw["related_teachers"][0]["assignment_source_key"]
                with engines["migration"].connect() as c:
                    org = c.scalar(
                        text(
                            "SELECT o.source_key FROM staff_assignments a JOIN staff_assignment_versions v ON v.assignment_id=a.id JOIN organization_units o ON o.id=v.organization_unit_id WHERE a.source_key=:key AND v.status='confirmed' AND v.revoked_at IS NULL AND v.effective_from<=now() AND (v.effective_to IS NULL OR now()<v.effective_to)"
                        ),
                        {"key": teacher},
                    )
                assert org
                actor, identity, context, writer_service = existing_fixture_writer(engines)
                apply(
                    (engines, actor, identity, context, writer_service),
                    [
                        {
                            "fact_kind": "official_contact",
                            "source_key": "contact:f5-source-test-" + uuid4().hex,
                            "operation": "upsert",
                            "expected_version": 0,
                            "effective_at": datetime.now(UTC) - timedelta(seconds=1),
                            "content": {
                                "owner_kind": "organization",
                                "organization_source_key": org,
                                "contact_kind": "email",
                                "contact_value": "source-proof@example.invalid",
                                "visibility_scope": "public",
                                "lifecycle_status": "active",
                            },
                        }
                    ],
                )
                with engines["reader"].connect() as c:
                    raw = c.scalar(
                        text(
                            "SELECT app.school_fact_family_teaching(:s,:a,:r,:v,NULL,'current',NULL,NULL,true)"
                        ),
                        params,
                    )
            if any(t["official_contacts"] for t in raw.get("related_teachers", [])):
                yield engines, candidate, params, raw
                break
        else:
            raise AssertionError(
                "Owned fixture needs a formally published current teacher organization channel"
            )
    finally:
        for engine in engines.values():
            engine.dispose()


def test_real_channel_publication_is_its_own_exact_applied_lineage(channels):
    engines, candidate, params, raw = channels
    contacts = [c for t in raw["related_teachers"] for c in t["official_contacts"]]
    assert contacts
    with engines["migration"].connect() as c:
        for channel in contacts:
            assert set(channel["source"]) == {"kind", "label", "published_at"}
            stamps = c.scalars(
                text(
                    "SELECT l.created_at FROM official_contact_points c JOIN school_fact_change_lineage l ON l.school_id=c.school_id AND l.target_id=c.id AND l.fact_kind='official_contact' AND l.target_version=c.version JOIN master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id WHERE c.school_id=:s AND c.contact_kind=:kind AND c.contact_value=:value AND b.status='applied'"
                ),
                dict(
                    s=candidate.school_id,
                    kind=channel["contact_kind"],
                    value=channel["contact_value"],
                ),
            ).all()
            assert datetime.fromisoformat(channel["source"]["published_at"]) in stamps
    with engines["reader"].connect() as c:
        without = c.scalar(
            text(
                "SELECT app.school_fact_family_teaching(:s,:a,:r,:v,NULL,'current',NULL,NULL,false)"
            ),
            params,
        )
    assert all(not t["official_contacts"] for t in without["related_teachers"])


def test_real_guardian_assembly_keeps_channel_source_attached_and_not_teaching_source(channels):
    engines, candidate, _, raw = channels
    actor, _ = guardian(engines, source=candidate.source_key)
    service = FamilyTeachingQueryService(
        repository=FamilyTeachingRepository(sessionmaker(engines["reader"])),
        current_actor=lambda a: a == actor,
    )
    result = service.query(actor=actor, include_official_contacts=True)
    assert result.evidence and result.evidence.valid(actor)
    material = education_model_material(result.public())
    assert "sources" not in material
    actual = [
        c for item in material["items"] for t in item["teachers"] for c in t["official_contacts"]
    ]
    expected = [c for t in raw["related_teachers"] for c in t["official_contacts"]]
    assert actual
    for channel in actual:
        matching = [
            c
            for c in expected
            if c["owner"] == channel["owner"] and c["contact_value"] == channel["contact_value"]
        ]
        assert (
            matching and channel["source"]["published_at"] == matching[0]["source"]["published_at"]
        )
        assert "publication_reference" not in str(channel) and "batch_id" not in str(channel)
    pairs = [
        (t["basis"]["published_at"], c["source"]["published_at"])
        for item in material["items"]
        for t in item["teachers"]
        for c in t["official_contacts"]
    ]
    assert any(a != b for a, b in pairs), (
        "Fixture must prove F2 channel publication is distinct from teaching publication"
    )
