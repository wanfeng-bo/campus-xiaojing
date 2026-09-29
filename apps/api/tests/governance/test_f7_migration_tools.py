"""Original-instance migration is exact, repeat-safe and preserves recovery inputs."""

import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def scripts_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _snapshot():
    return {
        "system_identifier": "7682019857215991846",
        "database": "campus_ai_school_facts_f1",
        "schema": "20260922_45",
        "anchor": {
            "anchor_id": "f147faa7-783e-47eb-9762-8815a72bf855",
            "school_id": "c1137be2-0734-4b69-8937-37dbd741f141",
        },
        "applying": 0,
        "active_reference": 0,
        "writer_active": 0,
        "writer_connections": 0,
        "non_reader_client_connections": 0,
        "snapshot_horizon": "100:100:",
        "mutation_counters": {"inserted": 1, "updated": 2, "deleted": 3},
        "prepared": 0,
        "governance_login": False,
        "governance_connections": 0,
    }


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    from scripts.f7_engineering import migration as m
    from scripts.f7_engineering import private_paths

    monkeypatch.setattr(m, "ROOT", tmp_path)
    external = tmp_path.parent / (tmp_path.name + "-external")
    monkeypatch.setattr(private_paths, "PRIVATE_ROOT", external)
    monkeypatch.setattr(private_paths, "REPOSITORY_ROOTS", (tmp_path,))
    monkeypatch.setattr(private_paths, "require_external_private_acl", lambda _: None)
    monkeypatch.setattr(m, "_require_private_acl", lambda _: None, raising=False)
    monkeypatch.setattr(m, "_validate_configuration_models", lambda **_: None, raising=False)
    hashes = {}
    for number in range(46, 56):
        path = tmp_path / f"apps/api/alembic/versions/20260924_{number}_test.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(number).encode())
        hashes[path.relative_to(tmp_path).as_posix()] = _digest(path.read_bytes())
    dump = tmp_path / "runtime/f7-execution/backups/recovery/original.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"retained facts and original receipts")
    backup = {
        "backup": str(dump),
        "sha256": _digest(dump.read_bytes()),
        "status": "backup_verified",
        "service_state": "stopped_product_writer",
        "sensitive_backups_private_acl": True,
        "snapshot": _snapshot(),
    }
    proof = {
        "image": "sha256:" + "a" * 64,
        "baseline": m.release.BASELINE,
        "base_image": m.release.BASE,
        "schema_contract": "school-facts-f7-v55",
        "image_manifest_verified": True,
        "files": hashes.copy(),
        "image_files": hashes.copy(),
        "source_sha256": _digest(json.dumps(hashes, sort_keys=True).encode()),
    }
    fallback = {
        "image": proof["image"],
        "source_sha256": proof["source_sha256"],
        "schema_contract": proof["schema_contract"],
        "mode": "f7_packages_disabled",
        "product_packages_enabled": False,
        "writer_packages_enabled": False,
        "legacy_routes_verified": True,
        "own_receipt_verified": True,
        "new_package_prepare_rejected": True,
        "new_package_confirm_rejected": True,
        "scope_status": "final_f7_coordinates",
        "regenerate_after_final_scopes": False,
    }
    private = external / "f7-execution"
    private.mkdir(parents=True)
    for service in ("product", "writer"):
        config = {
            "f7_packages_enabled": False,
            "f5_features_enabled": True,
            "f6_features_enabled": True,
            "reader": {"schema_contract": proof["schema_contract"]},
        }
        if service == "product":
            config.update(
                purpose="school-facts-f7-synthetic",
                runtime={"postgres_schema_contract": proof["schema_contract"]},
            )
        else:
            config["writer"] = {"schema_contract": proof["schema_contract"]}
        path = private / f"fallback-{service}.json"
        path.write_text(json.dumps(config))
        fallback[service + "_config"] = str(path)
        fallback[service + "_config_sha256"] = _digest(path.read_bytes())
        active = private / f"active-{service}.json"
        active.write_text(json.dumps(config | {"f7_packages_enabled": True}))
        fallback[service + "_active_config"] = str(active)
        fallback[service + "_active_config_sha256"] = _digest(active.read_bytes())
    return m, backup, proof, hashes, fallback


def test_exact_tested_transition_and_matching_fallback_required(evidence):
    m, backup, proof, hashes, fallback = evidence
    assert m.validate(backup=backup, proof=proof, tested_hashes=hashes) == hashes
    assert m.require_fallback(fallback, proof=proof)["mode"] == "f7_packages_disabled"
    for field, value in (
        ("image", "sha256:" + "b" * 64),
        ("schema_contract", "school-facts-f6-v45"),
        ("product_packages_enabled", True),
        ("writer_packages_enabled", True),
        ("new_package_confirm_rejected", False),
        ("own_receipt_verified", False),
    ):
        with pytest.raises(ValueError):
            m.require_fallback(fallback | {field: value}, proof=proof)


def test_f7_requires_final_stopped_backup_and_rejects_stale_live_dump(evidence, monkeypatch):
    m, backup, proof, hashes, _ = evidence
    calls = []
    monkeypatch.setattr(m.retained_backup, "backup", lambda **kw: calls.append(kw) or {})
    m.backup_stopped()
    assert calls[0]["_service_state"] == "stopped_product_writer"
    with pytest.raises(ValueError, match="stopped"):
        m.validate(
            backup=backup | {"service_state": "running"},
            proof=proof,
            tested_hashes=hashes,
        )
    for field in ("writer_connections", "non_reader_client_connections"):
        with pytest.raises(ValueError):
            m.validate(
                backup=backup | {"snapshot": _snapshot() | {field: 1}},
                proof=proof,
                tested_hashes=hashes,
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "20260924_52"),
        ("system_identifier", "different-cluster"),
        ("database", "other-project"),
        ("governance_login", True),
        ("governance_connections", 1),
        ("applying", 1),
        ("active_reference", 1),
        ("writer_active", 1),
        ("prepared", 1),
        ("anchor", {"school_id": "c1137be2-0734-4b69-8937-37dbd741f141"}),
    ],
)
def test_backup_must_be_exact_original_and_quiescent(evidence, field, value):
    m, backup, proof, hashes, _ = evidence
    with pytest.raises(ValueError):
        m.validate(
            backup=backup | {"snapshot": _snapshot() | {field: value}},
            proof=proof,
            tested_hashes=hashes,
        )


def test_modified_backup_partial_migrations_and_old_base_are_rejected(evidence):
    m, backup, proof, hashes, _ = evidence
    with pytest.raises(ValueError):
        m.validate(backup=backup, proof=proof, tested_hashes=dict(list(hashes.items())[:-1]))
    with pytest.raises(ValueError):
        m.validate(backup=backup, proof=proof | {"base_image": "old"}, tested_hashes=hashes)
    Path(backup["backup"]).write_bytes(b"changed")
    with pytest.raises(ValueError):
        m.validate(backup=backup, proof=proof, tested_hashes=hashes)


def test_backout_files_cannot_change_or_keep_old_schema(evidence):
    m, _, proof, _, fallback = evidence
    path = Path(fallback["product_config"])
    config = json.loads(path.read_text())
    config["runtime"]["postgres_schema_contract"] = "school-facts-f6-v45"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        m.require_fallback(fallback, proof=proof)
    with pytest.raises(ValueError):
        m.require_fallback(
            fallback | {"product_config_sha256": _digest(path.read_bytes())}, proof=proof
        )


@pytest.mark.parametrize(
    "delta",
    [
        {"scope_status": "provisional_original_F6_coordinates_no_F7_grants"},
        {"scope_status": None},
        {"regenerate_after_final_scopes": True},
        {"regenerate_after_final_scopes": None},
    ],
)
def test_provisional_fallback_never_prepares_migration(evidence, delta):
    m, _, proof, _, fallback = evidence
    with pytest.raises(ValueError):
        m.require_fallback(fallback | delta, proof=proof)


@pytest.mark.parametrize("service", ["product", "writer"])
def test_enabled_counterpart_required_and_only_package_flag_can_differ(evidence, service):
    m, _, proof, _, fallback = evidence
    active = Path(fallback[service + "_active_config"])
    before = active.read_bytes()
    active.unlink()
    with pytest.raises(ValueError):
        m.require_fallback(fallback, proof=proof)
    active.write_bytes(before)
    config = json.loads(before)
    config["new_scope_or_secret_or_ledger"] = "not-in-off-copy"
    active.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        m.require_fallback(fallback, proof=proof)
    rehashed = fallback | {service + "_active_config_sha256": _digest(active.read_bytes())}
    with pytest.raises(ValueError):
        m.require_fallback(rehashed, proof=proof)


def test_private_acl_and_full_config_validation_are_mandatory(evidence, monkeypatch):
    m, _, proof, _, fallback = evidence
    calls = []

    def deny(_):
        raise ValueError("Private ACL rejected")

    monkeypatch.setattr(m, "_require_private_acl", deny)
    monkeypatch.setattr(m.private_paths, "require_external_private_acl", deny)
    monkeypatch.setattr(m, "_validate_configuration_models", lambda **kw: calls.append(kw))
    with pytest.raises(ValueError, match="ACL"):
        m.require_fallback(fallback, proof=proof)
    assert calls == []
    seen = []
    monkeypatch.setattr(m, "_require_private_acl", lambda paths: seen.extend(paths))
    monkeypatch.setattr(
        m.private_paths, "require_external_private_acl", lambda paths: seen.extend(paths)
    )
    m.require_fallback(fallback, proof=proof)
    assert set(map(str, seen)) >= {
        fallback[key]
        for key in (
            "product_config",
            "writer_config",
            "product_active_config",
            "writer_active_config",
        )
    }
    assert len(calls) == 1 and calls[0]["proof"] == proof


def test_config_paths_cannot_escape_private_roots_even_with_valid_hash(evidence, tmp_path):
    m, _, proof, _, fallback = evidence
    escaped = tmp_path / "public.json"
    escaped.write_bytes(Path(fallback["product_active_config"]).read_bytes())
    with pytest.raises(ValueError):
        m.require_fallback(fallback | {"product_active_config": str(escaped)}, proof=proof)


def test_actual_acl_probe_is_read_only_and_fails_closed_without_printing_details(
    monkeypatch, tmp_path
):
    import base64
    from types import SimpleNamespace

    from scripts.f7_engineering import migration as m

    monkeypatch.setattr(m, "os", SimpleNamespace(name="nt"))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=b'{"private_acl_verified":true,"paths_checked":1}'
        )

    monkeypatch.setattr(m.subprocess, "run", run)
    m._require_private_acl((tmp_path / "private.json",))
    code = base64.b64decode(calls[0][0][-1]).decode("utf-16le")
    assert "[IO.File]::GetAccessControl($path)" in code and "GetAccessRules($true,$true" in code
    assert "S-1-5-18" in code and "WindowsIdentity" in code
    assert "Set-Acl" not in code and "SetAccessControl" not in code and "Get-Content" not in code
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stdout=b"private-value-that-must-not-be-in-error",
        ),
    )
    with pytest.raises(ValueError, match="ACL") as error:
        m._require_private_acl((tmp_path / "private.json",))
    assert "private-value" not in str(error.value)


def test_complete_configuration_check_is_pinned_networkless_and_redacts_failures(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from scripts.f7_engineering import migration as m

    names = ("product_active_config", "product_config", "writer_active_config", "writer_config")
    paths = {name: tmp_path / (name + ".json") for name in names}
    value = {name + "_sha256": "a" * 64 for name in names}
    proof = {"image": "sha256:" + "b" * 64, "image_files": {"source": "digest"}}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b'{"configurations_valid":true}')

    monkeypatch.setattr(m.subprocess, "run", run)
    m._validate_configuration_models(value=value, proof=proof, paths=paths)
    argv = calls[0]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--log-driver") + 1] == "none"
    assert proof["image"] in argv and argv.count("--mount") == 4
    assert "model_validate_json" in argv[-1] and "create_school_fact" not in argv[-1]
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stdout=b"config includes a secret value",
        ),
    )
    with pytest.raises(ValueError) as error:
        m._validate_configuration_models(value=value, proof=proof, paths=paths)
    assert "secret" not in str(error.value)


def test_runner_preserves_anchor_single_forward_upgrade_and_no_data_restore():
    from scripts.f7_engineering import migration as m

    _, code = m.retained_runner()
    assert "20260922_45" in code and "20260924_55" in code
    assert code.count("app.assert_school_database_anchor") == 2
    assert "campus_ai_governance_writer" in code and "pg_prepared_xacts" in code
    assert "'upgrade'" in code and "downgrade" not in code
    assert "pg_restore" not in code and "DELETE FROM" not in code
    _, bounded = m.retained_runner(
        expected_snapshot_horizon="100:100:",
        expected_mutation_counters={"inserted": 1, "updated": 2, "deleted": 3},
    )
    assert "non_reader" not in bounded
    assert "usename<>'campus_ai_school_fact_reader'" in bounded
    assert "'100:100:'" in bounded and "sys.exit(24)" in bounded
    assert "pg_current_snapshot" in bounded and "tup_inserted,tup_updated,tup_deleted" in bounded
    assert "(1, 2, 3)" in bounded and "pg_current_wal_lsn" not in bounded
    for horizon, counters in (
        ("bad'; dangerous", {"inserted": 1, "updated": 2, "deleted": 3}),
        ("100:100:", {"inserted": 1, "updated": -1, "deleted": 3}),
        ("100:100:", {"inserted": 1, "updated": True, "deleted": 3}),
    ):
        with pytest.raises(ValueError):
            m.retained_runner(
                expected_snapshot_horizon=horizon, expected_mutation_counters=counters
            )
    for start, end in (("20260924_52", m.TO), (m.FROM, "20260922_44")):
        with pytest.raises(ValueError):
            m.retained_runner(expected_from=start, target=end)


def test_execute_rebuilds_argv_rechecks_artifacts_and_never_retries_unknown(tmp_path, monkeypatch):
    from scripts.f7_engineering import migration as m

    prepared = {
        "from": m.FROM,
        "to": m.TO,
        "status": "prepared_not_executed",
        "backup_evidence": "backup.json",
        "proof_evidence": "proof.json",
        "fallback_evidence": "fallback.json",
        "migration_hashes": {},
        "argv": ["approved-exact-command"],
    }
    checks, calls = [], []
    monkeypatch.setattr(m, "prepare", lambda **kw: checks.append(kw) or prepared.copy())
    monkeypatch.setattr(m, "_require_stopped_services", lambda _: None)
    monkeypatch.setattr(
        m,
        "_execute_retained",
        lambda value, **kw: (
            calls.append(value.copy()) or {"status": "unknown_query_original_revision"}
        ),
    )
    with pytest.raises(ValueError):
        m.execute(
            prepared | {"argv": ["arbitrary-command"]}, evidence_path=tmp_path / "result.json"
        )
    assert not calls and checks
    result = m.execute(prepared, evidence_path=tmp_path / "result.json")
    assert result["status"] == "unknown_query_original_revision" and len(calls) == 1
    with pytest.raises(ValueError):
        m.execute(prepared, evidence_path=tmp_path / "result.json")
    existing = tmp_path / "prior.json"
    existing.write_text('{"status":"unknown_query_original_revision"}')
    with pytest.raises(ValueError):
        m.execute(prepared, evidence_path=existing)
    assert len(calls) == 1


def test_prepare_is_side_effect_free_and_keeps_no_logging_exact_runner(
    evidence, tmp_path, monkeypatch
):
    m, backup, proof, hashes, fallback = evidence
    for name, value in (("backup", backup), ("proof", proof), ("fallback", fallback)):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    monkeypatch.setattr(m.release, "require_snapshot", lambda _: None)
    value = m.prepare(
        backup_path=tmp_path / "backup.json",
        proof_path=tmp_path / "proof.json",
        fallback_path=tmp_path / "fallback.json",
        tested_hashes=hashes,
    )
    assert value["status"] == "prepared_not_executed"
    assert value["services_restarted"] is False
    argv = value["argv"]
    assert argv[argv.index("--network") + 1] == "campus-school-facts-f1-private"
    assert argv[argv.index("--log-driver") + 1] == "none"
    assert proof["image"] in argv and "pg_restore" not in " ".join(argv)
    assert "old F6-v45 image is incompatible" in value["rollback"]


def test_running_replaced_or_foreign_services_reject_before_execution(tmp_path, monkeypatch):
    import copy

    from scripts.f7_engineering import migration as m

    names = [
        "campus-school-facts-f1-product-1",
        "campus-school-facts-f1-writer-1",
        "campus-school-facts-f1-postgres-1",
    ]
    before = {
        "services": [{"name": name, "id": name + "-id", "image": "old-image"} for name in names]
    }
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(before))
    rows = [
        {
            "Name": item["name"],
            "Id": item["id"],
            "Image": item["image"],
            "State": {"Running": False},
        }
        for item in before["services"]
    ]
    rows[2]["State"]["Running"] = True
    monkeypatch.setattr(m.retained_backup, "capture", lambda _: json.dumps(rows).encode())
    m._require_stopped_services(path)
    for mutate in (
        lambda row: row["State"].update(Running=True),
        lambda row: row.update(Id="replacement"),
        lambda row: row.update(Name="other-project"),
        lambda row: row.update(Image="unverified-image"),
    ):
        changed = copy.deepcopy(rows)
        mutate(changed[0])
        monkeypatch.setattr(
            m.retained_backup, "capture", lambda _, v=changed: json.dumps(v).encode()
        )
        with pytest.raises(ValueError):
            m._require_stopped_services(path)
    changed = copy.deepcopy(rows)
    changed[2]["Id"] = "replacement-postgres"
    monkeypatch.setattr(m.retained_backup, "capture", lambda _, v=changed: json.dumps(v).encode())
    with pytest.raises(ValueError):
        m._require_stopped_services(path)


def _pregrant(evidence):
    from copy import deepcopy

    from tests.governance.test_f7_runtime_update import configurations

    m, _, _, _, old_evidence = evidence
    old_product, product, old_writer, writer = configurations()
    result = {key: value for key, value in old_evidence.items() if "config" not in key}
    result.update(
        mode="f7_pregrant_recovery",
        scope_status="retained_f6_coordinates",
        regenerate_after_final_scopes=True,
    )
    result["approved_stage_budget_sha256"] = m._digest(
        json.dumps(
            product["model"]["stage_budget"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    for service, original, updated in (
        ("product", old_product, product),
        ("writer", old_writer, writer),
    ):
        updated = deepcopy(updated)
        updated["f7_packages_enabled"] = False
        for suffix, config, folder in (
            ("_original_config", original, m.ROOT / "infra/school-facts-f1/private"),
            ("_config", updated, m.private_paths.PRIVATE_ROOT / "f7-execution"),
        ):
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{service}-config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result[service + suffix] = str(path)
            result[service + suffix + "_sha256"] = m._digest(path.read_bytes())
    return result


def test_pregrant_recovery_needs_no_new_scope_ids_but_is_never_final(evidence):
    m, _, proof, _, _ = evidence
    value = _pregrant(evidence)
    assert m.require_pregrant_recovery(value, proof=proof) is value
    with pytest.raises(ValueError):
        m.require_fallback(value, proof=proof)
    with pytest.raises(ValueError):
        m.require_pregrant_recovery(value | {"scope_status": "final_f7_coordinates"}, proof=proof)


@pytest.mark.parametrize("field", ["scope", "ledger", "secret", "on", "old_image"])
def test_pregrant_recovery_preserves_exact_original_authority_and_private_state(evidence, field):
    m, _, proof, _, _ = evidence
    value = _pregrant(evidence)
    path = Path(value["product_config"])
    config = json.loads(path.read_text())
    if field == "scope":
        config["family"]["scope_bindings"] = ["fabricated-f7-scope"]
    elif field == "ledger":
        config["model"]["state_directory"] = "fresh-ledger"
    elif field == "secret":
        config["reader"]["password"] = "replacement"
    elif field == "on":
        config["f7_packages_enabled"] = True
    else:
        value["image"] = m.release.BASE
    path.write_text(json.dumps(config))
    value["product_config_sha256"] = m._digest(path.read_bytes())
    with pytest.raises(ValueError):
        m.require_pregrant_recovery(value, proof=proof)


def test_pregrant_recovery_prepares_migration_without_asserting_final_activation(
    evidence, tmp_path, monkeypatch
):
    m, backup, proof, hashes, _ = evidence
    recovery = _pregrant(evidence)
    for name, value in (("backup", backup), ("proof", proof), ("fallback", recovery)):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    monkeypatch.setattr(m.release, "require_snapshot", lambda _: None)
    value = m.prepare(
        backup_path=tmp_path / "backup.json",
        proof_path=tmp_path / "proof.json",
        fallback_path=tmp_path / "fallback.json",
        tested_hashes=hashes,
    )
    assert value["fallback_mode"] == "f7_pregrant_recovery"
    assert value["activation_allowed"] is False
    assert value["status"] == "prepared_not_executed"


def test_pregrant_cannot_substitute_fake_original_file(evidence):
    m, _, proof, _, _ = evidence
    value = _pregrant(evidence)
    original = Path(value["product_original_config"])
    copy = m.private_paths.PRIVATE_ROOT / "f7-execution/fake-original.json"
    copy.write_bytes(original.read_bytes())
    with pytest.raises(ValueError):
        m.require_pregrant_recovery(value | {"product_original_config": str(copy)}, proof=proof)
