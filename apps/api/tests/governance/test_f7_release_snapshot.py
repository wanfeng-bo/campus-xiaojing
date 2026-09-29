"""F7 candidate bytes are explicit and exclude unrelated inherited frontend edits."""

from pathlib import Path, PurePosixPath

import pytest


def test_f7_release_sources_include_current_school_facts_but_not_other_frontend_work():
    from scripts.f7_engineering import release

    sources = release.sources()
    root = Path(__file__).resolve().parents[4]
    assert (
        sources["apps/api/app/db/schema_contract.py"]
        == (root / "apps/api/app/db/schema_contract.py").read_bytes()
    )
    assert "apps/api/alembic/versions/20260924_51_f7_residence_exact_coverage.py" in sources
    assert "apps/api/app/platform/school_facts/f7_receipt_contracts.py" in sources
    assert "scripts/f7_engineering/runtime_governance.py" in sources
    assert "scripts/f7_engineering/pregrant.py" in sources
    assert "apps/web/src/features/school_facts/SchoolFactPackageImport.vue" in sources
    assert sources["apps/web/index.html"] == release.committed_bytes("apps/web/index.html")
    assert sources["apps/web/src/app/DingTalkEntryApp.vue"] == release.committed_bytes(
        "apps/web/src/app/DingTalkEntryApp.vue"
    )
    # A public path-validation helper is executable source, not private material.
    assert sources["scripts/f7_engineering/private_paths.py"] == (
        root / "scripts/f7_engineering/private_paths.py"
    ).read_bytes()
    for name in sources:
        path = PurePosixPath(name)
        assert not {"private", "runtime", ".git", "node_modules"}.intersection(path.parts)
        if path.name.startswith(".env"):
            assert name == "apps/web/.env.example"
            assert sources[name] == release.committed_bytes(name)
        assert path.name not in {
            "product-config.json",
            "writer-config.json",
            "redis-password",
            "postgres-password",
            "rotation-journal.json",
        }


def test_f7_release_rejects_old_schema_and_unreviewed_candidate_manifest():
    from scripts.f7_engineering import release

    assert release.CONTRACT == "school-facts-f7-v56"
    assert release.BASELINE == "a382b8aebded6d3f4c55610066970a0ee54d2dd9"


def test_f7_candidate_must_inherit_the_exact_inspected_f6_rootfs():
    from scripts.f7_engineering import release

    assert hasattr(release, "require_candidate_base_layers")
    original_layers = ["sha256:" + value * 64 for value in ("a", "b")]
    original = {
        "Id": release.BASE,
        "RootFS": {"Type": "layers", "Layers": original_layers},
    }
    candidate = {
        "Id": "sha256:" + "c" * 64,
        "RootFS": {
            "Type": "layers",
            "Layers": [*original_layers, "sha256:" + "f" * 64],
        },
    }
    release.require_candidate_base_layers(original, original, candidate)

    swapped_tag_candidate = {
        **candidate,
        "RootFS": {
            "Type": "layers",
            "Layers": ["sha256:" + "e" * 64, *original_layers[1:], "sha256:" + "f" * 64],
        },
    }
    with pytest.raises(ValueError, match="base layers"):
        release.require_candidate_base_layers(original, original, swapped_tag_candidate)
    with pytest.raises(ValueError, match="base image"):
        release.require_candidate_base_layers(
            original, {**original, "Id": "sha256:" + "d" * 64}, candidate
        )
