import importlib.util
import sys
from pathlib import Path

import pytest

from tests.governance.test_f4_runtime_preparation import module as preparation

PATH = Path(__file__).resolve().parents[4] / "scripts/f4_engineering/runtime_configuration.py"


def module():
    assert PATH.exists(), "Pure staged runtime configuration builder missing"
    spec = importlib.util.spec_from_file_location("f4_runtime_configuration", PATH)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def test_canonical_manifest_hash_is_stable_across_process_hash_seeds():
    import os
    import subprocess
    from hashlib import sha256

    from tests.governance.test_f4_engineering import foundation

    expected = sha256(module().canonical_manifest_json(foundation()).encode()).hexdigest()
    code = (
        "from hashlib import sha256;"
        "from tests.governance.test_f4_runtime_configuration import module;"
        "from tests.governance.test_f4_engineering import foundation;"
        "print(sha256(module().canonical_manifest_json(foundation()).encode()).hexdigest())"
    )
    api_root = Path(__file__).resolve().parents[2]
    digests = []
    for seed in ("1", "2", "17"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=api_root,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(api_root)},
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        digests.append(result.stdout.strip())
    assert digests == [expected] * 3


def test_unpublished_future_never_becomes_read_object_and_rows_merge():
    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    stages = m.publication_stages(plan)
    assert sum(
        len(stages[x]["changes"]) for x in ("foundation", "education", "dependencies")
    ) == len(plan["canonical_initial_batch"]["changes"])
    rows = m.build_scope_matrix(
        plan,
        plan_sha256=m.plan_digest(plan),
        stage="foundation",
        published_receipts=(),
        existing_receipts=(),
    )
    assert all(r["family"] != "f4" for r in rows)
    assert not any("f4:membership:c-future" in r["object_keys"] for r in rows)
    assert len({(r["family"], r["fact_kind"], r["action"]) for r in rows}) == len(rows)
    assert all(not (set(r["object_keys"]) & set(r["creation_keys"])) for r in rows)


def test_unknown_publication_and_future_stage_order_fail_closed():
    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    with pytest.raises(ValueError):
        m.build_scope_matrix(
            plan,
            plan_sha256=m.plan_digest(plan),
            stage="future_registration",
            published_receipts=(),
            existing_receipts=(),
        )
    with pytest.raises(ValueError):
        m.build_scope_matrix(
            plan,
            plan_sha256=m.plan_digest(plan),
            stage="foundation",
            published_receipts=({"status": "unknown", "targets": []},),
            existing_receipts=(),
        )


def receipt_for(changes, serial=1):
    from uuid import UUID

    return {
        "locator_sha256": str(serial) * 64,
        "confirmed_at": "2026-09-13T13:00:00+00:00",
        "result": {
            "batch_id": str(UUID(int=serial)),
            "target_version": serial,
            "changed_count": len(changes),
            "replayed": False,
        },
        "targets": [
            {
                "fact_kind": c["fact_kind"],
                "source_key": c["source_key"],
                "operation": c["operation"],
                "target_version": c["expected_version"] + 1,
            }
            for c in changes
        ],
    }


def test_published_new_and_verified_old_students_merge_without_other_old_maintenance():
    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    stages = m.publication_stages(plan)
    old = [
        {
            "fact_kind": r["fact_kind"],
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
        }
        for r in plan["scope_matrix_existing_read_rows"]
        for key in r["object_keys"]
    ]
    rows = m.build_scope_matrix(
        plan,
        plan_sha256=m.plan_digest(plan),
        stage="education",
        published_receipts=(receipt_for(stages["foundation"]["changes"]),),
        existing_receipts=(receipt_for(old, 2),),
    )
    student = next(r for r in rows if r["fact_kind"] == "student" and r["action"] == "read")
    assert set(student["object_keys"]) >= {
        "f3:student:a",
        "f3:student:b",
        "f3:student:c",
        "f4:student:d",
        "f4:student:e",
    }
    ssr = next(
        r
        for r in rows
        if r["fact_kind"] == "student_school_relationship" and r["action"] == "maintain"
    )
    assert set(ssr["object_keys"]) >= {
        "f3:student-school:a",
        "f3:student-school:b",
        "f3:student-school:c",
    }
    assert all(
        not any(k.startswith("f3:") for k in r["object_keys"])
        for r in rows
        if r["action"] == "maintain" and r["fact_kind"] != "student_school_relationship"
    )
    plan["approval_reference"] = "changed"
    with pytest.raises(ValueError):
        m.build_scope_matrix(
            plan,
            plan_sha256="0" * 64,
            stage="foundation",
            published_receipts=(),
            existing_receipts=(),
        )


@pytest.mark.parametrize("stage", ["foundation", "education", "future_registration"])
def test_ssr_lifecycle_matrix_has_all_fields_without_mutating_pinned_facts(stage):
    import json

    from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    original = json.dumps(plan, sort_keys=True)
    stages = m.publication_stages(plan)
    existing = [
        {"fact_kind": r["fact_kind"], "source_key": k, "operation": "upsert", "expected_version": 0}
        for r in plan["scope_matrix_existing_read_rows"]
        for k in r["object_keys"]
    ]
    published = tuple(
        receipt_for(stages[name]["changes"], i + 1)
        for i, name in enumerate(m.STAGES[: m.STAGES.index(stage)])
    )
    rows = m.build_scope_matrix(
        plan,
        plan_sha256=m.plan_digest(plan),
        stage=stage,
        published_receipts=published,
        existing_receipts=(receipt_for(existing, 99),),
    )
    ssr = [r for r in rows if r["fact_kind"] == "student_school_relationship"]
    assert {r["action"] for r in ssr} == {"read", "maintain"}
    assert all(set(r["field_names"]) == F3_FACT_FIELDS["student_school_relationship"] for r in ssr)
    assert all("f3:student-school:a" in r["object_keys"] for r in ssr)
    assert not any(r["fact_kind"] == "student_contact" for r in rows)
    assert json.dumps(plan, sort_keys=True) == original


def test_real_manifest_receipts_keep_legacy_config_and_do_not_roll_deadline():
    from dataclasses import asdict
    from datetime import timedelta
    from hashlib import sha256
    from types import SimpleNamespace
    from uuid import UUID

    from app.composition import school_fact_f4_engineering_cli as cli
    from app.platform.governance.contracts import GovernanceWriteResult
    from tests.governance.test_f4_engineering import SCHOOL, STAFF, T0

    m = module()
    row = cli.ScopeMatrixRow(
        key="person",
        family="f3",
        fact_kind="person",
        action="read",
        object_keys=("f3:person:student-a", "f4:person:a:current"),
        field_names=("display_name",),
    )
    manifest = cli.build_duties_and_scopes(
        school_id=SCHOOL,
        actor_reference="operator",
        approval_reference="approved",
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=10),
        grant_epoch=T0,
        approved_assignment_ids=frozenset({STAFF}),
        rows=(row,),
    )
    returned = {
        c.idempotency_key: GovernanceWriteResult(UUID(int=100 + i), 1, None, False)
        for i, c in enumerate(manifest.materialize())
    }
    service = SimpleNamespace(receipt=lambda c, **kw: returned[c.idempotency_key])
    grants = cli.build_grants(
        manifest,
        [
            cli.GrantBinding(
                key="person-grant",
                assignment_id=STAFF,
                duty_key="f3_person_reader",
                scope_key="person",
            )
        ],
        grant_valid_from=T0 + timedelta(days=1),
        execution_valid_to=T0 + timedelta(days=1, minutes=10),
        service=service,
        credential="unit",
    )
    returned.update(
        {
            c.idempotency_key: GovernanceWriteResult(UUID(int=200), 1, 2, False)
            for c in grants.materialize()
        }
    )

    def evidence(man):
        return {
            "status": "succeeded",
            "manifest_sha256": sha256(m.canonical_manifest_json(man).encode()).hexdigest(),
            "commands": {
                c.idempotency_key: {
                    "state": "succeeded",
                    "digest": m.f4_governance_command_digest(c),
                    "receipt": asdict(returned[c.idempotency_key]),
                }
                for c in man.materialize()
            },
        }

    original = {
        "directory": {"old": "unchanged"},
        "family": {"old": "private"},
        "education": {"approval_reference": "approved", "scope_bindings": []},
    }
    kwargs = dict(
        original=original,
        matrix=[row.model_dump(mode="json")],
        school_id=SCHOOL,
        assignment_ids=(STAFF,),
        grant_epoch=T0,
        grant_valid_to=T0 + timedelta(days=7),
    )
    with pytest.raises(ValueError):
        m.build_runtime_configuration(**kwargs, governance_receipts=())
    result = m.build_runtime_configuration(
        **kwargs, governance_receipts=((manifest, evidence(manifest)), (grants, evidence(grants)))
    )
    assert result["directory"] == original["directory"] and result["family"] == original["family"]
    assert not original["education"]["scope_bindings"]
    from app.composition.school_fact_education_external import EducationExternalReader
    from app.composition.school_fact_family_settings import FamilyConfiguration

    family = SimpleNamespace(
        configuration=FamilyConfiguration.model_validate(result["education"]["dependency_family"]),
        repository=SimpleNamespace(reference_for=lambda **kw: "f3:authorized"),
    )
    reader = EducationExternalReader(
        directory=SimpleNamespace(
            reference_for=lambda **kw: pytest.fail("must not add F2 person scope")
        ),
        evidence=None,
        family=family,
    )
    assert (
        reader.reference_for(actor=None, fact_kind="person", source_key="f3:person:student-a")
        == "f3:authorized"
    )
    assert (
        reader.reference_for(actor=None, fact_kind="person", source_key="f4:person:a:current")
        == "f3:authorized"
    )
    bad = evidence(grants)
    next(iter(bad["commands"].values()))["state"] = "confirming"
    with pytest.raises(ValueError):
        m.build_runtime_configuration(
            **kwargs, governance_receipts=((manifest, evidence(manifest)), (grants, bad))
        )


def test_apply_result_and_confirmed_journal_break_creation_read_scope_cycle():
    import json
    from uuid import UUID

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV3, canonical_plan_sha256
    from app.platform.school_facts.ports import SchoolFactApplyResult

    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    canonical = m.publication_stages(plan)["foundation"]
    canonical["base_version"] = 10
    cs = CanonicalFactChangeSetV3.model_validate_json(json.dumps(canonical))
    result = SchoolFactApplyResult(UUID(int=45), 11, len(cs.changes), False)
    journal = {
        "state": "succeeded",
        "change_set": canonical,
        "plan_sha256": canonical_plan_sha256(cs),
        "receipt": {
            "batch_id": str(result.batch_id),
            "target_version": 11,
            "changed_count": len(cs.changes),
        },
    }
    proof = {"stage": "foundation", "journal": journal, "result": result}
    old = [
        {
            "fact_kind": r["fact_kind"],
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
        }
        for r in plan["scope_matrix_existing_read_rows"]
        for key in r["object_keys"]
    ]
    kwargs = dict(
        plan_sha256=m.plan_digest(plan),
        stage="education",
        published_receipts=(proof,),
        existing_receipts=(receipt_for(old, 2),),
    )
    rows = m.build_scope_matrix(plan, **kwargs)
    assert any(r["fact_kind"] == "student" and r["object_keys"] for r in rows)
    journal["plan_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        m.build_scope_matrix(plan, **kwargs)


def test_fixed_epoch_and_pin_reject_renewal_or_mutated_plan():
    from datetime import datetime, timedelta
    from uuid import UUID

    m = module()
    at = datetime.fromisoformat("2026-09-13T13:00:00+00:00")
    with pytest.raises(ValueError):
        m.build_runtime_configuration(
            original={"education": {"approval_reference": "approved"}},
            matrix=[],
            school_id=UUID(int=1),
            assignment_ids=(UUID(int=2),),
            grant_epoch=at,
            grant_valid_to=at + timedelta(days=8),
            governance_receipts=(),
        )
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    pin = m.plan_digest(plan)
    metadata = m.stage_metadata(plan, plan_sha256=pin, stage="foundation")
    assert metadata["change_count"] > 0 and metadata["plan_sha256"] == pin
    labels = m.configuration_labels(plan)
    assert "合成" in labels["person"]["f4:person:a:current"]
    plan["canonical_initial_batch"]["changes"][0]["content"]["display_name"] = "changed"
    with pytest.raises(ValueError):
        m.build_scope_matrix(
            plan, plan_sha256=pin, stage="foundation", published_receipts=(), existing_receipts=()
        )


def test_future_cancel_scope_can_be_installed_before_m13_and_stays_identical_after_m14():
    m = module()
    plan = preparation.build_plan(member_at="2026-09-13T13:00:00+00:00")
    stages = m.publication_stages(plan)
    old = [
        {
            "fact_kind": r["fact_kind"],
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
        }
        for r in plan["scope_matrix_existing_read_rows"]
        for key in r["object_keys"]
    ]
    published = tuple(
        receipt_for(stages[name]["changes"], serial=i + 2) for i, name in enumerate(m.STAGES[:3])
    )
    kwargs = dict(plan_sha256=m.plan_digest(plan), existing_receipts=(receipt_for(old),))
    with pytest.raises(ValueError):
        m.build_scope_matrix(
            plan, stage="future_cancellation", published_receipts=published, **kwargs
        )
    registration = m.build_scope_matrix(
        plan, stage="future_registration", published_receipts=published, **kwargs
    )
    future = stages["future_registration"]["changes"][0]["source_key"]
    assert any(future in row["creation_keys"] for row in registration)
    assert not any(future in row["object_keys"] for row in registration)
    published += (receipt_for(stages["future_registration"]["changes"], serial=6),)
    before = m.build_scope_matrix(
        plan, stage="future_cancellation", published_receipts=published, **kwargs
    )
    assert {row["action"] for row in before if future in row["object_keys"]} == {"read", "maintain"}
    after = m.build_scope_matrix(
        plan,
        stage="future_cancellation",
        published_receipts=published
        + (receipt_for(stages["future_cancellation"]["changes"], serial=7),),
        **kwargs,
    )
    assert before == after
