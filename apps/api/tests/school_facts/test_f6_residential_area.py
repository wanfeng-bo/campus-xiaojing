"""R02: a residence area inside an ordinary building, on the owned fixture only."""

from uuid import uuid4

import psycopg
import pytest

from app.platform.school_facts.contracts import safe_content_payload
from tests.school_facts.test_f6_reader_postgres import _reader_publication, targets
from tests.school_facts.test_f6_reader_postgres import reader_fixture as reader_fixture
from tests.school_facts.test_f6_residence_contracts import PROFILE, change
from tests.school_facts.test_f6_sample_publication_postgres import entry
from tests.school_facts.test_sf1_postgres_constraints import (
    _at,
    _insert_confirmed_location_slices,
    _LocationSliceSeed,
)


def test_residential_area_is_an_explicit_n25_usage_not_a_display_name_guess():
    result = change(content={**PROFILE, "usage_kind": "residential_area"})
    assert safe_content_payload(result.content)["usage_kind"] == "residential_area"
    assert safe_content_payload(change().content)["usage_kind"] == "building"


@pytest.mark.integration
def test_real_reader_residence_area_does_not_declare_the_whole_building(reader_fixture):
    c, data = reader_fixture
    # Fixture construction only. Every row and role switch is rolled back by
    # reader_fixture; tested projections execute as the real restricted Reader.
    c.execute("RESET SESSION AUTHORIZATION")
    c.execute("SET SESSION AUTHORIZATION campus_ai_migration")
    c.execute("SET ROLE campus_ai_db_owner")
    c.execute("SET CONSTRAINTS ALL DEFERRED")
    room, building, version = c.execute(
        "SELECT id,parent_location_id,version FROM campus_locations "
        "WHERE school_id=%s AND source_key=%s",
        (data["school"], data["room_key"]),
    ).fetchone()
    area = uuid4()
    area_key = "f6-reader-residential-area"
    c.execute(
        "INSERT INTO campus_locations(id,school_id,parent_location_id,source_key,kind,status) "
        "VALUES(%s,%s,%s,%s,'area','active')",
        (area, data["school"], building, area_key),
    )
    _insert_confirmed_location_slices(
        c,
        school_id=data["school"],
        slices=(
            _LocationSliceSeed(
                area,
                area_key,
                building,
                "area",
                "AREA-ONE",
                "综合楼东侧寄宿区域",
                (),
                "/综合楼/东侧寄宿区域",
                _at(1),
            ),
        ),
    )
    c.execute(
        "UPDATE campus_location_versions SET valid_to=%s "
        "WHERE school_id=%s AND location_id=%s AND version=%s",
        (_at(2), data["school"], room, version),
    )
    _insert_confirmed_location_slices(
        c,
        school_id=data["school"],
        slices=(
            _LocationSliceSeed(
                room,
                data["room_key"],
                area,
                "dorm_room",
                "ROOM-AREA",
                "区域内301室",
                (),
                "/综合楼/东侧寄宿区域/301室",
                _at(2),
                target_version=version + 1,
                expected_version=version,
            ),
        ),
    )
    c.execute(
        "UPDATE campus_locations SET parent_location_id=%s,version=%s WHERE school_id=%s AND id=%s",
        (area, version + 1, data["school"], room),
    )
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # N25 location identity is immutable: end the original fixture use and
    # publish a distinct area use through the sole formal Writer.
    # Each formal location fixture allocates its own confirmer. Restore the
    # same original batch actor that _reader_publication selects as template;
    # never weaken the real import confirmer-context check.
    confirmer = c.execute(
        "SELECT confirmed_by_user_id,confirmed_by_person_id,confirmed_by_assignment_id "
        "FROM master_data_import_batches WHERE school_id=%s LIMIT 1",
        (data["school"],),
    ).fetchone()
    for setting, value in (
        ("app.context_purpose", "command"),
        ("app.context_kind", "assignment"),
        ("app.account_user_id", str(confirmer[0])),
        ("app.person_id", str(confirmer[1])),
        ("app.assignment_id", str(confirmer[2])),
        ("app.assignment_version", "1"),
    ):
        c.execute("SELECT set_config(%s,%s,true)", (setting, value))
    new_profile_key = "f6-reader-area-profile"
    _reader_publication(
        c,
        data,
        [
            dict(
                fact_kind="dormitory_building_profile",
                source_key=data["keys"]["dormitory_building_profile"],
                operation="end",
                expected_version=1,
                effective_at=_at(2),
                content=None,
            ),
            entry(
                "dormitory_building_profile",
                new_profile_key,
                dict(
                    location_source_key=area_key,
                    usage_kind="residential_area",
                    display_name="综合楼东侧寄宿区域",
                    is_available=True,
                ),
                at=_at(2),
            ),
        ],
    )
    old = targets(c, data, "dormitory_building_profile", ["usage_kind"], at=_at(3))
    assert old["records"] == []
    data = {**data, "keys": {**data["keys"], "dormitory_building_profile": new_profile_key}}
    assert c.execute("SELECT current_user").fetchone()[0] == "campus_ai_school_fact_reader"
    n25 = targets(
        c,
        data,
        "dormitory_building_profile",
        ["usage_kind", "display_name", "location_source_key"],
        at=_at(3),
    )
    assert n25["records"][0]["content"] == {
        "usage_kind": "residential_area",
        "display_name": "综合楼东侧寄宿区域",
        "location_source_key": area_key,
    }
    room_result = targets(
        c, data, "dorm_room", ["room_name", "building_name", "authorized_bed_capacity"], at=_at(3)
    )
    assert room_result["records"][0]["content"] == {
        "room_name": "区域内301室",
        "building_name": "综合楼东侧寄宿区域",
        "authorized_bed_capacity": 1,
    }
    result = c.execute(
        "SELECT app.school_fact_family_residence(%s,%s,%s,1,%s)",
        (data["school"], data["adult"], data["relation"], _at(3)),
    ).fetchone()[0]
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["building_name"] == "综合楼东侧寄宿区域"
    assert record["room_name"] == "区域内301室"
    assert "authorized_bed_capacity" not in record and "bed_source_key" not in record
    with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification), c.transaction():
        targets(
            c, {**data, "school": uuid4()}, "dormitory_building_profile", ["usage_kind"], at=_at(3)
        )
