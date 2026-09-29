"""Offline fixed 41-to-44 backup/artifact guards."""

import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _scripts_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def test_f6_backup_only_accepts_41_and_retains_closed_original_guards():
    from scripts.f6_engineering.backup_original import require_snapshot

    from tests.governance.test_f5_backup_transition import snapshot

    current = snapshot() | {"schema": "20260914_41"}
    require_snapshot(current, expected_schema="20260914_41")
    for key, value in (
        ("schema", "20260914_40"),
        ("governance_login", True),
        ("governance_connections", 1),
        ("writer_active", 1),
        ("system_identifier", "other"),
    ):
        with pytest.raises(ValueError):
            require_snapshot(current | {key: value}, expected_schema="20260914_41")


def test_f6_runner_only_has_exact_transition_and_anchor():
    from scripts.f6_engineering.migration import retained_runner

    _, code = retained_runner(expected_from="20260914_41", target="20260922_44")
    assert "20260914_41" in code and "20260922_44" in code
    assert "app.assert_school_database_anchor" in code and "7682019857215991846" in code
    assert "campus_ai_governance_writer" in code


def test_migration_validates_all_three_actual_hashes_and_f6_identity(tmp_path, monkeypatch):
    from scripts.f5_engineering import migration as old
    from scripts.f6_engineering import migration as m

    from tests.governance.test_f5_backup_transition import snapshot

    monkeypatch.setattr(old, "ROOT", tmp_path)
    hashes = {}
    for n in (42, 43, 44):
        path = tmp_path / f"apps/api/alembic/versions/20260922_{n}_test.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(n).encode())
        hashes[path.relative_to(tmp_path).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    dump = tmp_path / "runtime/f6-execution/backups/test.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"preserved")
    backup = {
        "backup": str(dump),
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "status": "backup_verified",
        "sensitive_backups_private_acl": True,
        "snapshot": snapshot() | {"schema": "20260914_41"},
    }
    proof = {
        "image": "sha256:" + "a" * 64,
        "files": hashes.copy(),
        "image_files": hashes.copy(),
        "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "baseline": m.release.BASELINE,
        "implementation_head": m.release.F6_COMMITTED_ROOT,
        "base_image": m.release.BASE,
        "schema_contract": "school-facts-f6-v44",
        "image_manifest_verified": True,
        "worktree_still_matches": True,
    }
    transition = {"expected_from": "20260914_41", "target": "20260922_44"}
    assert m.validate(backup=backup, proof=proof, tested_hashes=hashes, **transition) == hashes
    assert (
        m.validate(
            backup=backup,
            proof=proof | {"implementation_head": m.release.BASELINE},
            tested_hashes=hashes,
            **transition,
        )
        == hashes
    )
    for field, value in (
        ("baseline", "old"),
        ("implementation_head", "old"),
        ("implementation_head", "a" * 40),
        ("schema_contract", "school-facts-f5-v41"),
        ("image_manifest_verified", False),
    ):
        with pytest.raises(ValueError):
            m.validate(
                backup=backup, proof=proof | {field: value}, tested_hashes=hashes, **transition
            )
    with pytest.raises(ValueError):
        m.validate(
            backup=backup, proof=proof, tested_hashes=dict(list(hashes.items())[:2]), **transition
        )
    dump.write_bytes(b"modified")
    with pytest.raises(ValueError):
        m.validate(backup=backup, proof=proof, tested_hashes=hashes, **transition)
