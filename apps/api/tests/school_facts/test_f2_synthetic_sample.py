import importlib.util
import sys
from pathlib import Path

from app.platform.master_data_imports.school_facts_json import SchoolFactJsonPlanBuilder
from app.platform.school_facts.f2_policy import F2_FACT_KINDS


def generator():
    path = Path(__file__).resolve().parents[4] / "scripts/generate_school_facts_f2_sample.py"
    spec = importlib.util.spec_from_file_location("f2_sample", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_approved_synthetic_sample_covers_families_and_preserves_full_text():
    module = generator()
    inputs = module.ExistingF1Inputs(
        history_from="2025-01-01T00:00:00Z",
        history_to="2026-01-01T00:00:00Z",
        base_version=1,
        school_version=1,
        school_content={
            "business_code": "SYNTHETIC",
            "display_name": "合成学校",
            "timezone_name": "Asia/Shanghai",
            "lifecycle_status": "active",
        },
    )
    first, second = module.build_sample(inputs), module.build_sample(inputs)
    assert first == second
    plan = SchoolFactJsonPlanBuilder().build(first)
    assert {r.fact_kind for r in plan.changes} == F2_FACT_KINDS
    assert not any(r.fact_kind == "login_mapping" for r in plan.changes)
    school = next(r for r in plan.changes if r.fact_kind == "school")
    assert school.expected_version == 1
    assert school.source_key == inputs.school_key
    policies = [r for r in plan.changes if r.fact_kind == "policy_version"]
    assert {len(r.content.content_text) for r in policies} == {32768, 50000, 100000}
    assert all(r.content.content_text.endswith(module.END_MARKER) for r in policies)
    end = module.build_service_end(
        base_version=2, target_version=1, effective_at="2026-09-06T01:00:00Z"
    )
    assert end.changes[0].operation == "end"
    correction = module.build_correction(base_version=3, effective_at="2026-09-06T01:00:00Z")
    assert {
        r.content.quality_kind
        for r in (*plan.changes, *correction.changes)
        if r.fact_kind == "fact_quality"
    } == {
        "source",
        "coverage",
        "negative",
        "dispute",
        "review",
        "correction",
    }


def test_generated_confirmed_assignment_slots_have_disjoint_periods():
    """Adjacent planned terms must satisfy the existing PostgreSQL slot exclusion."""
    from itertools import combinations

    module = generator()
    inputs = module.ExistingF1Inputs(
        history_from="2026-09-05T00:00:01Z",
        history_to="2026-09-05T12:00:00Z",
        base_version=8,
        school_version=2,
        school_content={
            "business_code": "SYNTHETIC",
            "display_name": "合成学校",
            "timezone_name": "Asia/Shanghai",
            "lifecycle_status": "active",
        },
    )
    assignments = [r for r in module.build_sample(inputs).changes if r.fact_kind == "assignment"]
    assert len(assignments) == 3
    for left, right in combinations(assignments, 2):
        left_slot = (
            left.content.person_source_key,
            left.content.organization_source_key,
            left.content.semantic_role_code,
        )
        right_slot = (
            right.content.person_source_key,
            right.content.organization_source_key,
            right.content.semantic_role_code,
        )
        if left_slot != right_slot:
            continue
        assert (
            left.content.valid_to is not None and left.content.valid_to <= right.effective_at
        ) or (right.content.valid_to is not None and right.content.valid_to <= left.effective_at), (
            f"Overlapping confirmed slot: {left.source_key} / {right.source_key}"
        )
