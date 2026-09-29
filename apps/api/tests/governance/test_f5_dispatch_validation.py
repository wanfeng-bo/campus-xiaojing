import json
from uuid import UUID

import pytest

from tests.governance.test_f5_engineering import foundation


def test_swapped_or_merged_staff_identities_rejected():
    from scripts.f5_engineering.dispatch_validation import require_fixed_identities

    with pytest.raises(ValueError):
        require_fixed_identities(
            {
                "assignment:f1-maintainer": "c6887625-5d0e-45c4-8625-e7c8c553a451",
                "f3:assignment:teacher-parent": "c6887625-5d0e-45c4-8625-e7c8c553a451",
            }
        )


def test_publication_status_without_matching_digest_never_adds_objects():
    from scripts.f5_engineering.dispatch_validation import checked_publications
    from scripts.f5_engineering.prepare_samples import build_plan

    with pytest.raises(ValueError):
        checked_publications(
            build_plan(),
            [
                {
                    "status": "succeeded",
                    "stage": "foundation",
                    "journal": {"state": "succeeded"},
                    "result": {"batch_id": str(UUID(int=1))},
                }
            ],
        )


def test_forged_governance_receipt_not_accepted_for_next_grants():
    from hashlib import sha256

    from scripts.f4_engineering.runtime_configuration import canonical_manifest_json
    from scripts.f5_engineering.dispatch_validation import checked_governance_phase

    manifest = foundation()
    evidence = {
        "status": "succeeded",
        "manifest_sha256": sha256(canonical_manifest_json(manifest).encode()).hexdigest(),
        "commands": {},
    }
    with pytest.raises(ValueError):
        checked_governance_phase(manifest, evidence)


def complete_publication():
    from scripts.f5_engineering.prepare_samples import build_plan

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV3, canonical_plan_sha256

    p = build_plan()
    cs = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps({**p["batches"]["foundation"], "base_version": 7})
    )
    applied = {
        "batch_id": str(UUID(int=1)),
        "target_version": 8,
        "changed_count": len(cs.changes),
        "replayed": False,
    }
    r = {
        "status": "succeeded",
        "stage": "foundation",
        "journal": {
            "state": "succeeded",
            "change_set": cs.model_dump(mode="json", exclude_unset=True),
            "plan_sha256": canonical_plan_sha256(cs),
            "receipt": applied.copy(),
        },
        "result": applied,
    }
    return p, r


def test_complete_exact_publication_and_identity_pass():
    from scripts.f5_engineering.dispatch_validation import (
        FIXED_IDENTITIES,
        checked_publications,
        require_fixed_identities,
    )

    p, r = complete_publication()
    require_fixed_identities(FIXED_IDENTITIES.copy())
    assert checked_publications(p, [r]) == {
        (c["fact_kind"], c["source_key"]) for c in p["batches"]["foundation"]["changes"]
    }
    r["result"]["batch_id"] = str(UUID(int=2))
    with pytest.raises(ValueError):
        checked_publications(p, [r])


def test_dispatch_preserves_other_config_and_uses_real_batch_id(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "runtime/f5-execution/original_dispatch.py"
    if not path.exists():
        pytest.skip("Root finite host dispatch artifact unavailable")
    spec = importlib.util.spec_from_file_location("f5_dispatch_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {
        "reader": {"other": "kept"},
        "education": {},
        "runtime": {"other": "kept"},
        "model": {"model": "existing", "budget_microyuan": 88},
        "writer": {},
        "directory": {"old": "kept"},
        "family": {"old": "kept"},
    }
    from copy import deepcopy

    original = deepcopy(base)
    p, r = complete_publication()
    monkeypatch.setattr(
        module, "bases", lambda: {module.PRODUCT: deepcopy(base), module.WRITER: deepcopy(base)}
    )
    monkeypatch.setattr(module, "plan", lambda state: p)
    monkeypatch.setattr(module, "read", lambda path: r)
    result = module.current_configs({"publication_receipts": ["receipt.json"]}, scoped=False)
    assert (
        result[module.PRODUCT]["family_leadership"]["publication_reference"]
        == "正式发布回执:" + r["result"]["batch_id"]
    )
    assert result[module.PRODUCT]["directory"] == base["directory"]
    assert result[module.PRODUCT]["model"]["budget_microyuan"] == 88
    assert result[module.WRITER]["family"] == base["family"]
    assert base == original
