"""The current F6 runtime advances once; historical 44 evidence remains valid."""

import hashlib
import json
from copy import deepcopy

import pytest


def test_current_engineering_contracts_are_45_and_only_exact_migration_is_added():
    from scripts.f6_engineering import publication, release, runtime_governance, runtime_update

    assert {
        publication.SCHEMA,
        release.CONTRACT,
        runtime_governance.SCHEMA_CONTRACT,
        runtime_update.CONTRACT,
    } == {"school-facts-f6-v45"}
    assert release.allowed_addition(
        "apps/api/alembic/versions/20260922_45_school_fact_student_number_lookup.py"
    )
    assert not release.allowed_addition("apps/api/alembic/versions/20260922_45_other.py")
    assert not release.allowed_addition("apps/api/alembic/versions/20260922_46_unreviewed.py")


def test_historical_44_snapshot_requires_explicit_matching_contract(tmp_path, monkeypatch):
    from scripts.f6_engineering import release

    monkeypatch.setattr(release, "OUT", tmp_path)
    monkeypatch.setattr(release, "sources", lambda: {"apps/api/app/test.py": b"fixed"})
    current = release.source()
    old = current | {"schema_contract": "school-facts-f6-v44"}
    release.require_snapshot(old, expected_contract="school-facts-f6-v44")
    with pytest.raises(ValueError):
        release.require_snapshot(old)
    with pytest.raises(ValueError):
        release.require_snapshot(current, expected_contract="school-facts-f6-v44")


def test_f6_backup_accepts_exact_44_without_expanding_old_f5_defaults():
    from scripts.f5_engineering import backup_original as old
    from scripts.f6_engineering import backup_original as current

    from tests.governance.test_f5_backup_transition import snapshot

    value = snapshot() | {"schema": "20260922_44"}
    current.require_snapshot(value)
    with pytest.raises(ValueError):
        old.require_snapshot(value, expected_schema="20260922_44")
    for patch in ({"schema": "20260922_45"}, {"governance_login": True}, {"writer_active": 1}):
        with pytest.raises(ValueError):
            current.require_snapshot(value | patch)
    current.require_snapshot(snapshot() | {"schema": "20260914_41"}, expected_schema="20260914_41")


def test_runner_45_has_exact_closed_original_transition_without_skipping_backup():
    from scripts.f6_engineering import migration

    _, code = migration.retained_runner()
    assert "20260922_44" in code and "20260922_45" in code
    assert "7682019857215991846" in code and "app.assert_school_database_anchor" in code
    _, previous = migration.retained_runner(expected_from="20260914_41", target="20260922_44")
    assert "20260914_41" in previous and "20260922_44" in previous
    for before, after in (("20260914_41", "20260922_45"), ("20260922_45", "20260922_44")):
        with pytest.raises(ValueError):
            migration.retained_runner(expected_from=before, target=after)


def test_44_to_45_requires_fresh_44_backup_and_only_tested_45_hash(tmp_path, monkeypatch):
    from scripts.f5_engineering import migration as retained
    from scripts.f6_engineering import migration

    from tests.governance.test_f5_backup_transition import snapshot

    monkeypatch.setattr(retained, "ROOT", tmp_path)
    filename = "apps/api/alembic/versions/20260922_45_school_fact_student_number_lookup.py"
    path = tmp_path / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixed 45")
    hashes = {filename: hashlib.sha256(path.read_bytes()).hexdigest()}
    dump = tmp_path / "runtime/f6-execution/backups/new44.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"original 44 copy")
    backup = {
        "backup": str(dump),
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "status": "backup_verified",
        "sensitive_backups_private_acl": True,
        "snapshot": snapshot() | {"schema": "20260922_44"},
    }
    proof = {
        "image": "sha256:" + "a" * 64,
        "files": hashes,
        "image_files": hashes,
        "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "baseline": migration.release.BASELINE,
        "implementation_head": migration.release.BASELINE,
        "base_image": migration.release.BASE,
        "schema_contract": "school-facts-f6-v45",
        "image_manifest_verified": True,
        "worktree_still_matches": True,
    }
    assert migration.validate(backup=backup, proof=proof, tested_hashes=hashes) == hashes
    stale = deepcopy(backup)
    stale["snapshot"]["schema"] = "20260914_41"
    with pytest.raises(ValueError):
        migration.validate(backup=stale, proof=proof, tested_hashes=hashes)
    with pytest.raises(ValueError):
        migration.validate(backup=backup, proof=proof, tested_hashes={})


def test_runtime_update_advances_contract_only_preserving_live_scopes_and_ledger():
    from scripts.f6_engineering import runtime_update

    from tests.governance.test_f6_runtime_update import configs, reviewed

    _, old = configs()
    old["reader"]["schema_contract"] = old["runtime"]["postgres_schema_contract"] = (
        "school-facts-f6-v44"
    )
    new = deepcopy(old)
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f6-v45"
    )
    runtime_update.require_config_delta(old, new, product=True, approved_coordinates=reviewed(new))
    assert new["residence"] == old["residence"] and new["model"] == old["model"]
    with pytest.raises(ValueError):
        runtime_update.require_config_delta(
            new, old, product=True, approved_coordinates=reviewed(old)
        )
