import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def inputs(tmp_path):
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "f5_migration_guard", root / "scripts/f5_engineering/migration.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.ROOT = tmp_path
    hashes = {}
    for n in range(32, 36):
        name = f"apps/api/alembic/versions/20260914_{n}_test.py"
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(str(n).encode())
        hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    backup_file = tmp_path / "runtime/f5-execution/backups/test.dump"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_bytes(b"preserved")
    backup = {
        "backup": str(backup_file),
        "sha256": hashlib.sha256(b"preserved").hexdigest(),
        "status": "backup_verified",
        "sensitive_backups_private_acl": True,
        "snapshot": {
            "schema": "20260913_31",
            "system_identifier": "7682019857215991846",
            "database": "campus_ai_school_facts_f1",
            "anchor": {"school_id": "c1137be2-0734-4b69-8937-37dbd741f141"},
            "governance_login": False,
            "governance_connections": 0,
            "writer_active": 0,
            "applying": 0,
            "active_reference": 0,
            "prepared": 0,
        },
    }
    proof = {
        "image": "sha256:" + "a" * 64,
        "files": hashes.copy(),
        "image_files": hashes.copy(),
        "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "baseline": "677a82fcb5d207a3d4e9919e0a8f49a318897577",
        "base_image": "sha256:360bc7ae0ca58f85e7e90397d30d3e246788acfe98521998c221f1fece327a20",
        "schema_contract": "school-facts-f5-v35",
        "image_manifest_verified": True,
        "worktree_still_matches": True,
    }
    return m, backup, proof, hashes


def test_exact_candidate_and_verified_backup(inputs):
    m, backup, proof, hashes = inputs
    assert m.validate(backup=backup, proof=proof, tested_hashes=hashes) == hashes


@pytest.mark.parametrize(
    "fault",
    [
        "status",
        "acl",
        "writer_active",
        "active_reference",
        "prepared",
        "schema",
        "system_identifier",
        "backup_bytes",
        "migration_hash",
        "source_hash",
        "image_hash",
        "base_image",
        "baseline",
        "schema_contract",
        "image_manifest_verified",
    ],
)
def test_migration_rejects_misaligned_evidence(inputs, fault):
    m, backup, proof, hashes = inputs
    if fault == "status":
        backup["status"] = "unknown"
    elif fault == "acl":
        backup["sensitive_backups_private_acl"] = False
    elif fault in {"writer_active", "active_reference", "prepared"}:
        backup["snapshot"][fault] = 1
    elif fault in {"schema", "system_identifier"}:
        backup["snapshot"][fault] = "wrong"
    elif fault == "backup_bytes":
        Path(backup["backup"]).write_bytes(b"changed")
    elif fault == "migration_hash":
        hashes[next(iter(hashes))] = "bad"
    elif fault == "source_hash":
        proof["source_sha256"] = "bad"
    elif fault == "image_hash":
        proof["image"] = "sha256:" + "z" * 64
    else:
        proof[fault] = False if fault == "image_manifest_verified" else "wrong"
    with pytest.raises(ValueError):
        m.validate(backup=backup, proof=proof, tested_hashes=hashes)
