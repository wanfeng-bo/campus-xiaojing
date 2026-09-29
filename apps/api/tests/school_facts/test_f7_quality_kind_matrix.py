"""Independent 53-kind routing check against the approved F7 review matrix.

This proves contract and consumer registration, not that every kind has a
published record or an effective original-instance grant. Those are verified
by formal maintenance/read evidence in the F7 execution record.
"""

import re
from pathlib import Path
from typing import get_args

from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
from app.composition.school_fact_education_quality import (
    EDUCATION_DEPENDENCY_KINDS,
    EducationQualityBridge,
)
from app.composition.school_fact_family_quality import FamilyQualityReader
from app.composition.school_fact_family_quality_product import FamilyQualityProductAdapter
from app.composition.school_fact_residence_quality import ResidenceQualityBridge
from app.composition.school_fact_residence_quality_product import ResidenceQualityProductAdapter
from app.platform.school_facts.contracts import (
    F2_QUALITY_TARGET_KINDS,
    F7_FAMILY_QUALITY_TARGET_KINDS,
    SchoolFactKind,
)
from app.platform.school_facts.f2_policy import F2_FACT_KINDS
from app.platform.school_facts.f4_policy import F4_FACT_KINDS
from app.platform.school_facts.f5_policy import F5_FACT_KINDS
from app.platform.school_facts.f6_policy import F6_FACT_KINDS


def _approved_owner_matrix() -> dict[str, str]:
    repo = Path(__file__).resolve().parents[4]
    document = (
        repo / "docs/engineering/学校事实F7逐类型质量核查矩阵.md"
    ).read_text(encoding="utf-8")
    entries = re.findall(r"^\| `([a-z][a-z0-9_]*)` \| (F[2-6])\b", document, re.M)
    assert len(entries) == 53
    assert len({kind for kind, _owner in entries}) == len(entries)
    return dict(entries)


def test_all_approved_kinds_have_one_canonical_owner_and_quality_route():
    approved = _approved_owner_matrix()
    canonical = set(get_args(SchoolFactKind.__value__)) - {"fact_quality"}
    assert len(canonical) == 53
    assert set(approved) == canonical

    routed = {
        "F2": F2_QUALITY_TARGET_KINDS,
        "F3": F7_FAMILY_QUALITY_TARGET_KINDS,
        "F4": F4_FACT_KINDS,
        "F5": F5_FACT_KINDS,
        "F6": F6_FACT_KINDS,
    }
    for owner, kinds in routed.items():
        assert {kind for kind, actual_owner in approved.items() if actual_owner == owner} == kinds
    assert not set.intersection(*(set(kinds) for kinds in routed.values()))

    # The governed fact type and its quality target must both be registered.
    assert F2_QUALITY_TARGET_KINDS <= F2_FACT_KINDS
    assert EDUCATION_DEPENDENCY_KINDS == F4_FACT_KINDS | F5_FACT_KINDS


def test_each_owner_has_formal_quality_maintenance_and_read_consumer():
    routes = (
        (DirectoryProductAdapter, "resolve_change", "query"),
        (FamilyQualityProductAdapter, "resolve_change", None),
        (EducationQualityBridge, "require_change_set", "read_dependency"),
        (ResidenceQualityProductAdapter, "resolve_change", "query"),
    )
    for owner, maintenance, read in routes:
        assert callable(getattr(owner, maintenance, None))
        if read:
            assert callable(getattr(owner, read, None))
    assert callable(getattr(FamilyQualityReader, "query_quality_for_target", None))
    assert callable(getattr(ResidenceQualityBridge, "read_content", None))
