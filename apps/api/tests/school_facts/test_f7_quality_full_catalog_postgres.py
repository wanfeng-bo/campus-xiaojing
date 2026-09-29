"""F7 quality target compatibility for every approved canonical fact kind.

The parametrized contract test checks each kind's accepted type and a valid
representative owner-coordinate shape. The PostgreSQL test checks the installed
target constraint. Neither
test substitutes for a formal Writer receipt plus an authorized Reader result
for every kind; those are separate Q1 acceptance evidence.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_package_authorization import PackageFactPartition
from app.platform.school_facts.contracts import (
    F2_QUALITY_TARGET_KINDS,
    F7_FAMILY_QUALITY_TARGET_KINDS,
    CanonicalFactChangeV1,
)
from app.platform.school_facts.f3_policy import F3_FACT_FIELDS
from app.platform.school_facts.f4_policy import F4_FACT_KINDS
from app.platform.school_facts.f5_policy import F5_FACT_KINDS
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from tests.school_facts.test_f3_family_postgres import safe_test_engine
from tests.school_facts.test_sf3_http import _context


def _approved_kinds() -> tuple[tuple[str, str], ...]:
    # The runtime test helper mounts apps/api only. The separate host-side
    # matrix test checks these owner sets against the approved document.
    owners = {
        "F2": F2_QUALITY_TARGET_KINDS,
        "F3": F7_FAMILY_QUALITY_TARGET_KINDS,
        "F4": F4_FACT_KINDS,
        "F5": F5_FACT_KINDS,
        "F6": F6_FACT_KINDS,
    }
    entries = tuple((kind, owner) for owner, kinds in owners.items() for kind in sorted(kinds))
    assert len(entries) == len({kind for kind, _owner in entries}) == 53
    return entries


@pytest.mark.parametrize("kind,owner", _approved_kinds())
def test_each_kind_has_a_typed_quality_publication_shape(kind: str, owner: str) -> None:
    """The typed contract accepts one representative source claim per kind."""
    at = datetime(2026, 9, 24, tzinfo=UTC)
    content: dict[str, object] = {
        "quality_kind": "source",
        "source_status": "available",
        "target_fact_kind": kind,
        "target_source_key": f"f7:target:{kind}",
        "basis_reference": "隔离合成核查依据",
        "responsibility": "隔离质量责任者",
        "verified_at": at,
    }
    if owner == "F3":
        assert kind in F7_FAMILY_QUALITY_TARGET_KINDS
        content.update(
            owner_kind="person",
            person_source_key="f7:person:quality-owner",
            field_name=sorted(F3_FACT_FIELDS[kind])[0],
        )
    elif owner in {"F4", "F5"}:
        content.update(
            owner_kind="education",
            education_target_kind=kind,
            education_target_source_key=f"f7:target:{kind}",
        )
    elif kind == "student_bed_residence":
        content.update(owner_kind="person", person_source_key="f7:person:resident")
    elif owner == "F6" and kind in {"dorm_room", "dorm_bed"}:
        content.update(owner_kind="location", location_source_key="f7:location:room")
    else:
        content["owner_kind"] = "school"
    change = CanonicalFactChangeV1.model_validate(
        {
            "schema_version": 4,
            "operation": "upsert",
            "fact_kind": "fact_quality",
            "source_key": "f7:quality:" + uuid4().hex,
            "expected_version": 0,
            "effective_at": at,
            "origin": {"sheet_code": "f7_quality_catalog", "row_number": 1},
            "content": content,
        }
    )
    assert change.content is not None
    assert change.content.target_fact_kind == kind
    assert change.content.owner_kind == content["owner_kind"]


@pytest.mark.parametrize("kind,owner", _approved_kinds())
def test_each_quality_target_keeps_its_owner_for_create_update_and_correction(
    kind: str, owner: str
) -> None:
    """A correction/end must not escape the target's deterministic owner guard."""
    expected = {
        "F2": "directory", "F3": "family_quality", "F4": "education_quality",
        "F5": "education_quality", "F6": "residence_quality",
    }[owner]
    partition = PackageFactPartition(
        directory_configuration=None,
        family_configuration=None,
        education_configuration=None,
        existing_quality_kind=lambda _actor, _key: kind,
    )
    actor = _context().actor
    for version, content in (
        (0, SimpleNamespace(target_fact_kind=kind)),
        (1, SimpleNamespace(target_fact_kind=kind)),
        (1, None),
    ):
        change = SimpleNamespace(
            fact_kind="fact_quality", source_key=f"f7:quality:{kind}",
            expected_version=version, content=content,
        )
        assert tuple(partition.group(actor, (change,))) == (expected,)


@pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)
def test_installed_quality_target_constraint_contains_exact_approved_catalog() -> None:
    """The isolated installed schema, not only Python, recognizes every kind."""
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    engine = safe_test_engine(config_path, "migration")
    try:
        with engine.connect() as connection:
            database, system = connection.execute(
                text("SELECT current_database(),system_identifier::text FROM pg_control_system()")
            ).one()
            assert (database, system) == (config["database"], config["system_identifier"])
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260924_55"
            )
            definition = connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid=c.conrelid "
                    "WHERE t.relname='school_fact_qualities' "
                    "AND c.conname='ck_school_fact_qualities_target'"
                )
            )
            assert isinstance(definition, str)
            quoted = set(re.findall(r"'([a-z][a-z0-9_]*)'", definition))
            assert quoted == {kind for kind, _owner in _approved_kinds()}
    finally:
        engine.dispose()
