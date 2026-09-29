"""F7 local Product/Writer update guards without touching the original instance."""

import json
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def repository_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def module():
    return import_module("scripts.f7_engineering.runtime_update")


def configurations():
    old_product = {
        "purpose": "school-facts-f6-synthetic",
        "reader": {"schema_contract": "school-facts-f6-v45", "password": "unchanged"},
        "runtime": {"postgres_schema_contract": "school-facts-f6-v45"},
        "directory": {"scope_bindings": ["old"]},
        "family": {"scope_bindings": ["old"]},
        "education": {"scope_bindings": ["old"]},
        "residence": {"scope_bindings": ["old"]},
        "f5_features_enabled": True,
        "f6_features_enabled": True,
        "model": {
            "state_directory": "original-ledger",
            "budget": {"cap_fen": 5000},
            "stage_budget": {
                "stage_id": "school-facts-f6",
                "cap_microyuan": 3_000_000,
                "approval_reference": "old",
            },
        },
    }
    new_product = deepcopy(old_product)
    new_product["purpose"] = "school-facts-f7-synthetic"
    new_product["f7_packages_enabled"] = True
    new_product["reader"]["schema_contract"] = "school-facts-f7-v55"
    new_product["runtime"]["postgres_schema_contract"] = "school-facts-f7-v55"
    new_product["model"]["stage_budget"] = {
        "stage_id": "school-facts-f7",
        "cap_microyuan": 3_000_000,
        "approval_reference": "f7-approved",
    }
    old_writer = {
        "reader": {"schema_contract": "school-facts-f6-v45", "password": "unchanged"},
        "writer": {"schema_contract": "school-facts-f6-v45"},
        "directory": {"scope_bindings": ["old"]},
        "family": {"scope_bindings": ["old"]},
        "education": {"scope_bindings": ["old"]},
        "residence": {"scope_bindings": ["old"]},
        "f5_features_enabled": True,
        "f6_features_enabled": True,
    }
    new_writer = deepcopy(old_writer)
    new_writer["f7_packages_enabled"] = True
    new_writer["reader"]["schema_contract"] = "school-facts-f7-v55"
    new_writer["writer"]["schema_contract"] = "school-facts-f7-v55"
    return old_product, new_product, old_writer, new_writer


def test_f7_update_only_changes_exact_contract_stage_and_reviewed_scopes():
    old_product, new_product, old_writer, new_writer = configurations()
    m = module()
    product_review = {
        "model.stage_budget": m.coordinate_digest(new_product["model"]["stage_budget"])
    }
    m.require_config_delta(
        old_product, new_product, product=True, approved_coordinates=product_review
    )
    m.require_config_delta(old_writer, new_writer, product=False)
    m.require_feature_pair({"Product": new_product, "Writer": new_writer})

    new_product["family"]["scope_bindings"] = ["reviewed"]
    with pytest.raises(ValueError):
        m.require_config_delta(
            old_product, new_product, product=True, approved_coordinates=product_review
        )
    product_review["family.scope_bindings"] = m.coordinate_digest(["reviewed"])
    m.require_config_delta(
        old_product, new_product, product=True, approved_coordinates=product_review
    )


@pytest.mark.parametrize("product", [False, True])
def test_f7_update_cannot_review_changed_family_read_dependency_bindings(product):
    m = module()
    old_product, new_product, old_writer, new_writer = configurations()
    old, final = (old_product, new_product) if product else (old_writer, new_writer)
    final["family"]["read_dependency_bindings"] = [
        {
            "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
            "operation_code": "school_facts.f3.student.read",
            "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
            "object_keys": ["f3:student:b"],
        }
    ]
    approved = {
        "family.read_dependency_bindings": m.coordinate_digest(
            final["family"]["read_dependency_bindings"]
        )
    }
    if product:
        approved["model.stage_budget"] = m.coordinate_digest(
            final["model"]["stage_budget"]
        )

    with pytest.raises(ValueError, match="Only exact reviewed F7 coordinates"):
        m.require_config_delta(
            old, final, product=product, approved_coordinates=approved
        )


def test_f7_reviewed_nested_education_dependencies_are_required_for_rollout_and_recovery():
    m = module()
    old_product, product, old_writer, writer = configurations()
    for is_product, old, final in (
        (True, old_product, product),
        (False, old_writer, writer),
    ):
        old["education"]["dependency_directory"] = {
            "scope_bindings": ["old"],
            "source_catalog": {"existing": "old"},
            "creation_catalog": {"existing": "old"},
        }
        old["education"]["dependency_family"] = {"scope_bindings": ["old"]}
        final["education"]["dependency_directory"] = {
            "scope_bindings": ["old", "new-read"],
            "source_catalog": {"existing": "old", "new-read": "source"},
            "creation_catalog": {"existing": "old", "new-read": "creation"},
        }
        final["education"]["dependency_family"] = {
            "scope_bindings": ["old", "new-student"]
        }
        approved = {
            path: m.coordinate_digest(m._get(final, path))
            for path in (
                "education.dependency_directory.scope_bindings",
                "education.dependency_directory.source_catalog",
                "education.dependency_directory.creation_catalog",
                "education.dependency_family.scope_bindings",
            )
        }
        if is_product:
            approved["model.stage_budget"] = m.coordinate_digest(
                final["model"]["stage_budget"]
            )
        m.require_config_delta(
            old, final, product=is_product, approved_coordinates=approved
        )
        with pytest.raises(ValueError):
            m.require_config_delta(
                old,
                final,
                product=is_product,
                approved_coordinates={
                    k: v for k, v in approved.items() if not k.endswith("source_catalog")
                },
            )

        off = deepcopy(final)
        off["f7_packages_enabled"] = False
        off["education"]["dependency_directory"] = deepcopy(
            old["education"]["dependency_directory"]
        )
        off["education"]["dependency_family"] = deepcopy(
            old["education"]["dependency_family"]
        )
        activation_approved = {
            k: v for k, v in approved.items() if k != "model.stage_budget"
        }
        m.require_recovered_activation_config_delta(
            off, final, product=is_product, approved_coordinates=activation_approved
        )
        changed = deepcopy(final)
        changed["education"]["dependency_directory"]["unreviewed"] = True
        with pytest.raises(ValueError):
            m.require_recovered_activation_config_delta(
                off, changed, product=is_product, approved_coordinates=activation_approved
            )


@pytest.mark.parametrize(
    "change", ["ledger", "password", "global", "old_stage", "wrong_scope", "flag", "budget"]
)
def test_f7_update_rejects_unreviewed_or_security_relevant_change(change):
    old_product, new_product, _, _ = configurations()
    m = module()
    reviewed = {"model.stage_budget": m.coordinate_digest(new_product["model"]["stage_budget"])}
    if change == "ledger":
        new_product["model"]["state_directory"] = "fresh-ledger"
    elif change == "password":
        new_product["reader"]["password"] = "changed"
    elif change == "global":
        new_product["model"]["budget"]["cap_fen"] = 10000
    elif change == "old_stage":
        new_product["model"]["stage_budget"]["stage_id"] = "school-facts-f6"
    elif change == "wrong_scope":
        new_product["education"]["scope_bindings"] = ["unreviewed"]
    elif change == "flag":
        new_product["f6_features_enabled"] = False
    else:
        new_product["model"]["stage_budget"]["cap_microyuan"] = 4_000_000
    with pytest.raises(ValueError):
        m.require_config_delta(
            old_product, new_product, product=True, approved_coordinates=reviewed
        )


def test_f7_update_uses_exact_schema_and_both_original_services(monkeypatch):
    m = module()
    calls = []
    monkeypatch.setattr(
        m.retained, "update_services", lambda **kw: calls.append(kw) or {"status": "prepared"}
    )
    monkeypatch.setattr(m, "_require_start_authority", lambda **kw: None, raising=False)
    assert m.update_services(
        proof={}, configurations={}, evidence_path=None, expected_images={}
    ) == {"status": "prepared"}
    assert calls[0]["_family"] == "f7" and calls[0]["_config_check"] is m.require_config_delta
    assert callable(calls[0]["_quiescent_check"])
    assert calls[0]["_surface_check"] is m.verify_f7_surface
    with pytest.raises(ValueError):
        m.update_services(
            proof={}, configurations={}, evidence_path=None, expected_images={}, product_only=True
        )
    sql = []
    helper = SimpleNamespace(
        DOCKER=[],
        safe_run=lambda *a, **kw: (
            sql.append(kw["data"].decode()) or b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"
        ),
    )
    m.require_quiescent(helper)
    assert "='20260924_55'" in sql[0] and "BEGIN READ ONLY" in sql[0]
    seen = []
    monkeypatch.setattr(m, "_require_start_unchanged", lambda **kw: seen.append(kw))
    calls[0]["_quiescent_check"](helper)
    assert seen[0]["activation"] == "f7_on"


def test_f7_forward_backout_only_closes_new_package_intake(monkeypatch):
    m = module()
    _, product, _, writer = configurations()
    product_off, writer_off = deepcopy(product), deepcopy(writer)
    product_off["f7_packages_enabled"] = False
    writer_off["f7_packages_enabled"] = False
    m.require_forward_backout_config_delta(product, product_off, product=True)
    m.require_forward_backout_config_delta(writer, writer_off, product=False)
    m.require_feature_pair({"Product": product_off, "Writer": writer_off})
    with pytest.raises(ValueError):
        m.require_feature_pair({"Product": product_off, "Writer": writer})
    changed = deepcopy(product_off)
    changed["model"]["state_directory"] = "new-ledger"
    with pytest.raises(ValueError):
        m.require_forward_backout_config_delta(product, changed, product=True)

    calls = []
    monkeypatch.setattr(m.retained, "update_services", lambda **kw: calls.append(kw))
    m.disable_package_intake(
        proof={"image": "same-image", "schema_contract": m.CONTRACT},
        configurations={"Product": product_off, "Writer": writer_off},
        evidence_path=None,
        expected_images={"Product": "same-image", "Writer": "same-image"},
    )
    assert calls[0]["_family"] == "f7"
    assert calls[0]["_config_check"] is m.require_forward_backout_config_delta
    surface_calls = []
    monkeypatch.setattr(m, "verify_f7_surface", lambda *a, **kw: surface_calls.append((a, kw)))
    calls[0]["_surface_check"]("helper", "Writer", enabled=False)
    assert surface_calls == [(("helper", "Writer"), {"enabled": False, "expected_contract": m.CONTRACT})]
    with pytest.raises(ValueError):
        m.disable_package_intake(
            proof={"image": "same-image", "schema_contract": m.CONTRACT},
            configurations={"Product": product_off, "Writer": writer},
            evidence_path=None,
            expected_images={"Product": "same-image", "Writer": "same-image"},
        )
    with pytest.raises(ValueError):
        m.disable_package_intake(
            proof={"image": "same-image", "schema_contract": m.CONTRACT},
            configurations={"Product": product_off, "Writer": writer_off},
            evidence_path=None,
            expected_images={"Product": "old-image", "Writer": "same-image"},
        )


@pytest.mark.parametrize("product_replaced", [False, True])
def test_forward_backout_failure_closes_owned_writer_and_product(
    monkeypatch, tmp_path, product_replaced
):
    m = module()
    _, product, _, writer = configurations()
    for config in (product, writer):
        config["f7_packages_enabled"] = False
    image = "sha256:" + "a" * 64
    live = {
        "Product": {
            "Id": "product-original",
            "Image": image,
            "State": {"Running": True},
        },
        "Writer": {
            "Id": "writer-original",
            "Image": image,
            "State": {"Running": True},
        },
    }
    stopped = []

    def safe_run(argv, **kw):
        assert argv[:2] == ["docker", "stop"]
        stopped.append(argv[-1])
        next(row for row in live.values() if row["Id"] == argv[-1])["State"]["Running"] = False

    helper = SimpleNamespace(
        PRODUCT="Product",
        WRITER="Writer",
        DOCKER=["docker"],
        inspect=lambda name: live[name],
        safe_run=safe_run,
    )
    evidence = tmp_path / "forward-backout.json"

    def second_service_fails(**kw):
        live["Writer"] = {
            "Id": "writer-candidate",
            "Image": image,
            "State": {"Running": True},
        }
        rows = [{"name": "Writer", "new_id": "writer-candidate", "phase": "verified"}]
        if product_replaced:
            live["Product"] = {
                "Id": "product-candidate",
                "Image": image,
                "State": {"Running": True},
            }
            rows.append(
                {"name": "Product", "new_id": "product-candidate", "phase": "created"}
            )
        evidence.write_text(json.dumps({"image": image, "services": rows}))
        raise RuntimeError("second service failed")

    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "update_services", second_service_fails)
    with pytest.raises(RuntimeError, match="second service failed"):
        m.disable_package_intake(
            proof={"image": image},
            configurations={"Product": product, "Writer": writer},
            evidence_path=evidence,
            expected_images={"Product": image, "Writer": image},
            execute=True,
        )
    assert set(stopped) == {
        "writer-candidate",
        "product-candidate" if product_replaced else "product-original",
    }
    assert stopped[0] == "writer-candidate"
    assert not any(row["State"]["Running"] for row in live.values())
    result = json.loads(evidence.read_text())
    assert result["status"] == "stopped"
    if not product_replaced:
        assert result["original_product_phase"] == "stopped"


def test_forward_backout_failure_does_not_stop_replaced_product(monkeypatch, tmp_path):
    m = module()
    _, product, _, writer = configurations()
    for config in (product, writer):
        config["f7_packages_enabled"] = False
    image = "sha256:" + "a" * 64
    live = {
        "Product": {
            "Id": "product-original",
            "Image": image,
            "State": {"Running": True},
        },
        "Writer": {
            "Id": "writer-original",
            "Image": image,
            "State": {"Running": True},
        },
    }
    stopped = []

    def safe_run(argv, **kw):
        stopped.append(argv[-1])
        live["Writer"]["State"]["Running"] = False

    helper = SimpleNamespace(
        PRODUCT="Product",
        WRITER="Writer",
        DOCKER=["docker"],
        inspect=lambda name: live[name],
        safe_run=safe_run,
    )
    evidence = tmp_path / "forward-backout.json"

    def second_service_fails(**kw):
        live["Writer"] = {
            "Id": "writer-candidate",
            "Image": image,
            "State": {"Running": True},
        }
        live["Product"] = {
            "Id": "foreign-product",
            "Image": image,
            "State": {"Running": True},
        }
        evidence.write_text(
            json.dumps(
                {
                    "image": image,
                    "services": [
                        {"name": "Writer", "new_id": "writer-candidate", "phase": "verified"}
                    ],
                }
            )
        )
        raise RuntimeError("second service failed")

    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "update_services", second_service_fails)
    with pytest.raises(RuntimeError, match="reconciliation"):
        m.disable_package_intake(
            proof={"image": image},
            configurations={"Product": product, "Writer": writer},
            evidence_path=evidence,
            expected_images={"Product": image, "Writer": image},
            execute=True,
        )
    assert stopped == ["writer-candidate"]
    assert live["Product"]["State"]["Running"] is True
    result = json.loads(evidence.read_text())
    assert result["status"] == "closure_unknown"
    assert result["original_product_phase"] == "closure_unknown"


def test_forward_backout_failure_accepts_exact_preserved_stopped_product(
    monkeypatch, tmp_path
):
    m = module()
    _, product, _, writer = configurations()
    for config in (product, writer):
        config["f7_packages_enabled"] = False
    image = "sha256:" + "a" * 64
    live = {
        "Product": {"Id": "product-original", "Image": image, "State": {"Running": True}},
        "Writer": {"Id": "writer-original", "Image": image, "State": {"Running": True}},
    }
    stopped = []

    def safe_run(argv, **kw):
        stopped.append(argv[-1])
        live["Writer"]["State"]["Running"] = False

    helper = SimpleNamespace(
        PRODUCT="Product",
        WRITER="Writer",
        DOCKER=["docker"],
        inspect=lambda name: live[name],
        safe_run=safe_run,
    )
    evidence = tmp_path / "forward-backout.json"

    def product_create_fails(**kw):
        live["Writer"] = {
            "Id": "writer-candidate", "Image": image, "State": {"Running": True}
        }
        live["Product-before-f7"] = {
            "Id": "product-original", "Image": image, "State": {"Running": False}
        }
        del live["Product"]
        evidence.write_text(
            json.dumps(
                {
                    "image": image,
                    "services": [
                        {"name": "Writer", "new_id": "writer-candidate", "phase": "verified"},
                        {
                            "name": "Product", "old_id": "product-original",
                            "backup_container": "Product-before-f7", "phase": "preserved",
                        },
                    ],
                }
            )
        )
        raise RuntimeError("product create failed")

    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "update_services", product_create_fails)
    with pytest.raises(RuntimeError, match="product create failed"):
        m.disable_package_intake(
            proof={"image": image},
            configurations={"Product": product, "Writer": writer},
            evidence_path=evidence,
            expected_images={"Product": image, "Writer": image},
            execute=True,
        )
    assert stopped == ["writer-candidate"]
    result = json.loads(evidence.read_text())
    assert result["status"] == "stopped"
    assert result["original_product_phase"] == "preserved_stopped"


def test_f7_runtime_probe_checks_actual_enabled_and_disabled_routes():
    m = module()
    calls = []
    helper = SimpleNamespace(
        DOCKER=["docker"],
        PRODUCT="Product",
        WRITER="Writer",
        safe_run=lambda args, **kw: calls.append(args) or b"",
    )
    m.verify_f7_surface(helper, "Product", enabled=True)
    m.verify_f7_surface(helper, "Product", enabled=False)
    m.verify_f7_surface(helper, "Writer", enabled=False)
    assert "plans/package" in calls[0][-1]
    assert "f7_packages_enabled'] is True" in calls[0][-1]
    assert "{x for x in p if x.startswith(prefix+'/')}=={result}" in calls[1][-1]
    assert "school-facts-f7-v55" in calls[2][-1]
    with pytest.raises(ValueError):
        m.verify_f7_surface(helper, "Other", enabled=True)


@pytest.mark.parametrize("schema_repair", [False, True])
def test_f7_stopped_rollout_requires_verified_migration_and_same_stopped_originals(
    monkeypatch, tmp_path, schema_repair
):
    m = module()
    old_product, product, old_writer, writer = configurations()
    if schema_repair:
        old_product, old_writer = deepcopy(product), deepcopy(writer)
        product["reader"]["schema_contract"] = m.REPAIRED_CONTRACT
        product["runtime"]["postgres_schema_contract"] = m.REPAIRED_CONTRACT
        writer["reader"]["schema_contract"] = m.REPAIRED_CONTRACT
        writer["writer"]["schema_contract"] = m.REPAIRED_CONTRACT
    names = ("campus-school-facts-f1-writer-1", "campus-school-facts-f1-product-1")
    old = {
        name: {
            "Id": name + "-id",
            "Image": m.release.BASE,
            "State": {"Running": False, "Restarting": False},
        }
        for name in names
    }
    old["campus-school-facts-f1-postgres-1"] = {
        "Id": "pg-id",
        "Image": "sha256:" + "b" * 64,
        "State": {"Running": True, "Restarting": False},
    }
    helper = SimpleNamespace(
        WRITER=names[0],
        PRODUCT=names[1],
        inspect=lambda name: old[name],
        require_candidate_image=lambda image, proof: None,
        validate_runtime_config=lambda image, config, product: None,
        read_json=lambda path: old_product if "product" in str(path) else old_writer,
        recreate_command=lambda *args: ["create"],
    )
    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.release, "require_snapshot", lambda proof: None)
    monkeypatch.setattr(m, "require_quiescent", lambda h, contract=m.CONTRACT: None)
    monkeypatch.setattr(m.migration, "migration_hashes", lambda **kw: {"migration": "tested"})
    monkeypatch.setattr(m.migration, "validate", lambda **kwargs: None)
    monkeypatch.setattr(m, "_require_start_authority", lambda **kw: None, raising=False)
    proof = {"image": "sha256:" + "c" * 64, "source_sha256": "source", "schema_contract": m.REPAIRED_CONTRACT if schema_repair else m.CONTRACT}
    backup = tmp_path / "backup.json"
    backup.write_text(
        json.dumps(
            {
                "status": "backup_verified",
                "services": [
                    {"name": "/" + name, "id": old[name]["Id"], "image": old[name]["Image"]}
                    for name in (*names, "campus-school-facts-f1-postgres-1")
                ],
            }
        ),
        encoding="utf-8",
    )
    migration = tmp_path / "migration.json"
    migration.write_text(
        json.dumps(
            {
                "status": "migrated_checked",
                "exit_code": 0,
                "from": "20260924_55" if schema_repair else "20260922_45",
                "to": "20260924_56" if schema_repair else "20260924_55",
                "image": proof["image"],
                "source_sha256": "source",
                "backup_evidence": str(backup),
                "migration_hashes": {"migration": "tested"},
            }
        ),
        encoding="utf-8",
    )
    configs = {names[0]: writer, names[1]: product}
    expected = {name: old[name]["Image"] for name in names}
    reviewed = {
        names[1]: {"model.stage_budget": m.coordinate_digest(product["model"]["stage_budget"])}
    }
    args = dict(
        proof=proof,
        configurations=configs,
        expected_images=expected,
        backup_evidence=backup,
        migration_evidence=migration,
        evidence_path=tmp_path / "update.json",
        approved_coordinates=None if schema_repair else reviewed,
    )
    assert m.update_stopped_services(**args)["status"] == "prepared"
    old[names[0]]["State"]["Running"] = True
    with pytest.raises(ValueError, match="stopped"):
        m.update_stopped_services(**args)
    old[names[0]]["State"]["Running"] = False
    migration.write_text(
        migration.read_text(encoding="utf-8").replace(
            "migrated_checked", "unknown_query_original_revision"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="migration"):
        m.update_stopped_services(**args)


@pytest.mark.parametrize("activation", ["f7_on", "pregrant_recovery"])
@pytest.mark.parametrize("fail_product", [False, True])
def test_f7_stopped_rollout_preserves_old_pair_and_closes_partial_new_pair(
    monkeypatch, tmp_path, fail_product, activation
):
    m = module()
    old_product, product, old_writer, writer = configurations()
    if activation == "pregrant_recovery":
        product["f7_packages_enabled"] = writer["f7_packages_enabled"] = False
    names = ("campus-school-facts-f1-writer-1", "campus-school-facts-f1-product-1")
    original = {
        name: {
            "Id": name + "-old",
            "Image": m.release.BASE,
            "State": {"Running": False, "Restarting": False},
            "NetworkSettings": {
                "Networks": {
                    "campus-school-facts-f1-private": {"Aliases": []},
                    **(
                        {"campus-school-facts-f1-model-egress": {"Aliases": ["model"]}}
                        if name == names[1]
                        else {}
                    ),
                }
            },
        }
        for name in names
    }
    pg = "campus-school-facts-f1-postgres-1"
    original[pg] = {
        "Id": "pg-id",
        "Image": "sha256:" + "b" * 64,
        "State": {"Running": True, "Restarting": False},
    }
    live = dict(original)
    active_config = {names[0]: old_writer, names[1]: old_product}
    preserved = {}
    commands = []
    image = "sha256:" + "c" * 64

    def name_from_path(path):
        return names[1] if "product" in str(path) else names[0]

    def safe_run(args, timeout=45):
        commands.append(tuple(args))
        op = args[1]
        if op == "ps":
            return b""
        if op == "rename":
            _, _, name, backup_name = args
            preserved[backup_name] = live.pop(name)
            return b""
        if op == "create":
            name = args[args.index("--name") + 1]
            new = deepcopy(original[name])
            new["Id"], new["Image"] = name + "-new", image
            live[name] = new
            return (new["Id"] + "\n").encode()
        if op == "start":
            live[args[2]]["State"]["Running"] = True
            return b""
        if op == "stop":
            target = next(value for value in live.values() if value["Id"] == args[-1])
            target["State"]["Running"] = False
            return b""
        if op == "network":
            live[args[-1]]["NetworkSettings"]["Networks"][args[-2]] = {"Aliases": ["model"]}
            return b""
        if op == "exec":
            return b""
        raise AssertionError(args)

    helper = SimpleNamespace(
        DOCKER=["docker"],
        WRITER=names[0],
        PRODUCT=names[1],
        inspect=lambda name: live[name],
        safe_run=safe_run,
        require_candidate_image=lambda candidate, proof: None,
        read_json=lambda path: active_config[name_from_path(path)],
        atomic_private_config=lambda path, value: active_config.__setitem__(
            name_from_path(path), value
        ),
        validate_runtime_config=lambda candidate, config, product: None,
        recreate_command=lambda name, old, candidate: ["docker", "create", "--name", name],
        verify_manifest=lambda name, proof: (
            (_ for _ in ()).throw(ValueError("bad candidate"))
            if fail_product and name == names[1]
            else None
        ),
        verify_service_surface=lambda name, family_enabled: None,
    )
    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "require_preserved_shape", lambda old, new: None)
    monkeypatch.setattr(m.release, "require_snapshot", lambda proof: None)
    monkeypatch.setattr(m, "require_quiescent", lambda h, contract=m.CONTRACT: None)
    monkeypatch.setattr(m.migration, "migration_hashes", lambda **kw: {"migration": "tested"})
    monkeypatch.setattr(m.migration, "validate", lambda **kwargs: None)
    monkeypatch.setattr(
        m,
        "_require_start_authority",
        lambda **kw: {
            "approved_stage_budget_sha256": m.coordinate_digest(product["model"]["stage_budget"])
        },
    )
    start_checks = []
    monkeypatch.setattr(m, "_require_start_unchanged", lambda **kw: start_checks.append(kw))
    proof = {"image": image, "source_sha256": "source", "schema_contract": m.CONTRACT}
    backup = tmp_path / "backup.json"
    backup.write_text(
        json.dumps(
            {
                "status": "backup_verified",
                "services": [
                    {
                        "name": "/" + name,
                        "id": original[name]["Id"],
                        "image": original[name]["Image"],
                    }
                    for name in (*names, pg)
                ],
            }
        ),
        encoding="utf-8",
    )
    migration = tmp_path / "migration.json"
    migration.write_text(
        json.dumps(
            {
                "status": "migrated_checked",
                "exit_code": 0,
                "from": "20260922_45",
                "to": "20260924_55",
                "image": image,
                "source_sha256": "source",
                "backup_evidence": str(backup),
                "migration_hashes": {"migration": "tested"},
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "rollout.json"
    kwargs = dict(
        proof=proof,
        configurations={names[0]: writer, names[1]: product},
        expected_images={name: m.release.BASE for name in names},
        backup_evidence=backup,
        migration_evidence=migration,
        evidence_path=evidence,
        execute=True,
        activation=activation,
        approved_coordinates=(
            {
                names[1]: {
                    "model.stage_budget": m.coordinate_digest(product["model"]["stage_budget"])
                }
            }
            if activation == "f7_on"
            else None
        ),
    )
    if fail_product:
        with pytest.raises(RuntimeError, match="reconciliation"):
            m.update_stopped_services(**kwargs)
        assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "stopped"
        assert not live[names[0]]["State"]["Running"]
        assert not live[names[1]]["State"]["Running"]
    else:
        assert m.update_stopped_services(**kwargs)["status"] == "verified"
        assert all(live[name]["State"]["Running"] for name in names)
    assert len(preserved) == 2
    assert all(not value["State"]["Running"] for value in preserved.values())
    assert live[pg]["Id"] == "pg-id"
    assert not any(item[1] in {"rm", "volume"} for item in commands)
    assert len(start_checks) == 4
    assert all(check["activation"] == activation for check in start_checks)
    probes = [item[-1] for item in commands if item[1] == "exec"]
    if not fail_product:
        assert any("f7_packages_enabled'] is " + str(activation == "f7_on") in p for p in probes)
    claim = evidence.with_name(evidence.name + ".claim")
    assert claim.is_file()
    assert json.loads(claim.read_text())["activation"] == activation


@pytest.fixture
def startup_authority(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m.private_paths, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(m.private_paths, "REPOSITORY_ROOTS", ())
    monkeypatch.setattr(m.private_paths, "require_external_private_acl", lambda _: None)
    _, product, _, writer = configurations()
    configs = {
        "campus-school-facts-f1-product-1": product,
        "campus-school-facts-f1-writer-1": writer,
    }
    value = {"mode": "f7_packages_disabled", "scope_status": "final_f7_coordinates"}
    private = tmp_path / "private"
    private.mkdir()
    for name, config in (("product", product), ("writer", writer)):
        for suffix, enabled in (("_active_config", True), ("_config", False)):
            path = private / (name + suffix + ".json")
            path.write_text(json.dumps(config | {"f7_packages_enabled": enabled}))
            value[name + suffix] = str(path)
            value[name + suffix + "_sha256"] = m.migration._digest(path.read_bytes())
    fallback = tmp_path / "recovery.json"
    fallback.write_text(json.dumps(value))
    calls = []
    monkeypatch.setattr(m.migration, "require_fallback", lambda v, **kw: calls.append("final") or v)
    monkeypatch.setattr(
        m.migration, "require_pregrant_recovery", lambda v, **kw: calls.append("pregrant") or v
    )
    helper = SimpleNamespace(
        PRODUCT="campus-school-facts-f1-product-1", WRITER="campus-school-facts-f1-writer-1"
    )
    return m, configs, value, fallback, helper, calls


def test_final_start_requires_live_authority_verification_not_a_success_boolean(startup_authority):
    m, configs, _, fallback, helper, calls = startup_authority
    with pytest.raises(ValueError, match="activation"):
        m._require_start_authority(
            proof={},
            configurations=configs,
            recovery_evidence=fallback,
            activation_evidence={"all_grants_valid": True},
            helper=helper,
            activation="f7_on",
        )
    assert calls == ["final"]


def test_final_start_binds_actual_configs_to_full_final_pair(startup_authority, monkeypatch):
    m, configs, _, fallback, helper, _ = startup_authority
    checks = []
    monkeypatch.setattr(
        m, "_require_current_activation", lambda **kw: checks.append(kw), raising=False
    )
    m._require_start_authority(
        proof={},
        configurations=configs,
        recovery_evidence=fallback,
        activation_evidence={"governance_phases": []},
        helper=helper,
        activation="f7_on",
    )
    assert checks and checks[0]["configurations"] == configs
    changed = deepcopy(configs)
    changed[helper.PRODUCT]["family"]["scope_bindings"] = ["different"]
    with pytest.raises(ValueError, match="configuration"):
        m._require_start_authority(
            proof={},
            configurations=changed,
            recovery_evidence=fallback,
            activation_evidence={},
            helper=helper,
            activation="f7_on",
        )
    assert len(checks) == 1


def test_pregrant_recovery_start_cannot_enable_intake_and_binds_migration(startup_authority):
    m, configs, value, path, helper, calls = startup_authority
    value.update(mode="f7_pregrant_recovery", scope_status="retained_f6_coordinates")
    path.write_text(json.dumps(value))
    migrated = {
        "fallback_evidence": str(path),
        "fallback_sha256": m.migration._digest(path.read_bytes()),
        "fallback_mode": "f7_pregrant_recovery",
    }
    off = {key: config | {"f7_packages_enabled": False} for key, config in configs.items()}
    m._require_start_authority(
        proof={},
        configurations=off,
        recovery_evidence=path,
        activation_evidence=None,
        helper=helper,
        activation="pregrant_recovery",
        migration_result=migrated,
    )
    assert calls == ["pregrant"]
    for changed in (configs, {**off, helper.PRODUCT: off[helper.PRODUCT] | {"purpose": "wrong"}}):
        with pytest.raises(ValueError):
            m._require_start_authority(
                proof={},
                configurations=changed,
                recovery_evidence=path,
                activation_evidence=None,
                helper=helper,
                activation="pregrant_recovery",
                migration_result=migrated,
            )
    with pytest.raises(ValueError, match="migration"):
        m._require_start_authority(
            proof={},
            configurations=off,
            recovery_evidence=path,
            activation_evidence=None,
            helper=helper,
            activation="pregrant_recovery",
            migration_result=migrated | {"fallback_sha256": "changed"},
        )
    with pytest.raises(ValueError):
        m._require_start_authority(
            proof={},
            configurations=configs,
            recovery_evidence=path,
            activation_evidence={},
            helper=helper,
            activation="f7_on",
            migration_result=migrated,
        )


def test_final_start_rechecks_current_authority_and_refuses_changed_copy(
    startup_authority, monkeypatch
):
    m, configs, value, fallback, helper, _ = startup_authority
    checks = []

    def private_documents(value, *, names):
        docs = {}
        for name in names:
            raw = Path(value[name]).read_bytes()
            if m.migration._digest(raw) != value[name + "_sha256"]:
                raise ValueError("Changed private copy")
            docs[name] = json.loads(raw)
        return {}, docs

    monkeypatch.setattr(m.migration, "_private_configurations", private_documents)
    monkeypatch.setattr(m, "_require_current_activation", lambda **kw: checks.append(kw))
    args = dict(
        proof={},
        configurations=configs,
        recovery_evidence=fallback,
        activation_evidence={},
        helper=helper,
        activation="f7_on",
        reviewed=value,
    )
    m._require_start_unchanged(**args)
    assert len(checks) == 1
    Path(value["writer_config"]).write_text("{}")
    with pytest.raises(ValueError):
        m._require_start_unchanged(**args)
    assert len(checks) == 1


def test_live_updater_cannot_bypass_final_governance_gate(monkeypatch):
    m = module()
    calls = []
    monkeypatch.setattr(m.retained, "update_services", lambda **kw: calls.append(kw))
    with pytest.raises(ValueError):
        m.update_services(proof={}, configurations={}, expected_images={}, evidence_path=None)
    assert not calls


def test_on_activation_hook_delegates_exact_inputs_and_propagates_rejection(monkeypatch):
    from scripts.f7_engineering import activation

    m = module()
    values = dict(
        proof={"source_sha256": "reviewed"},
        configurations={"Product": {}},
        evidence={"migration_evidence": "fixed"},
        helper=object(),
    )
    calls = []
    monkeypatch.setattr(activation, "require_current_activation", lambda **kw: calls.append(kw))
    m._require_current_activation(**values)
    assert calls == [values]

    def denied(**kw):
        raise ValueError("Current grant revoked")

    monkeypatch.setattr(activation, "require_current_activation", denied)
    with pytest.raises(ValueError, match="revoked"):
        m._require_current_activation(**values)


def test_recovered_f7_off_to_on_only_adds_reviewed_coordinates():
    m = module()
    _, product, _, writer = configurations()
    for is_product, final in ((True, product), (False, writer)):
        off = deepcopy(final)
        off["f7_packages_enabled"] = False
        final["family"]["scope_bindings"] = ["old", "formally-issued"]
        approved = {"family.scope_bindings": m.coordinate_digest(final["family"]["scope_bindings"])}
        m.require_recovered_activation_config_delta(
            off, final, product=is_product, approved_coordinates=approved
        )
        with pytest.raises(ValueError):
            m.require_recovered_activation_config_delta(off, final, product=is_product)
        with pytest.raises(ValueError):
            m.require_recovered_activation_config_delta(
                final, final, product=is_product, approved_coordinates=approved
            )
        for path, replacement in (
            ("reader", {"schema_contract": "school-facts-f6-v45"}),
            ("reader", off["reader"] | {"password": "changed"}),
        ):
            with pytest.raises(ValueError):
                m.require_recovered_activation_config_delta(
                    off,
                    final | {path: replacement},
                    product=is_product,
                    approved_coordinates=approved,
                )


@pytest.mark.parametrize("is_product", [True, False])
def test_active_f7_coordinate_update_keeps_on_state_and_all_other_configuration(is_product):
    m = module()
    _, product, _, writer = configurations()
    base = product if is_product else writer
    base["education"]["dependency_family"] = {"scope_bindings": ["old"]}
    final = deepcopy(base)
    final["family"]["scope_bindings"] = ["old", "formally-issued"]
    final["education"]["dependency_family"]["scope_bindings"] = ["old", "formally-issued"]
    approved = {path: m.coordinate_digest(m._get(final, path)) for path in (
        "family.scope_bindings", "education.dependency_family.scope_bindings",
    )}
    m.require_active_scope_config_delta(base, final, product=is_product,
                                       approved_coordinates=approved)
    with pytest.raises(ValueError):
        m.require_active_scope_config_delta(base, final, product=is_product)
    with pytest.raises(ValueError):
        m.require_active_scope_config_delta(base, final, product=is_product,
                                           approved_coordinates={**approved,
                                               "family.scope_bindings": "0" * 64})
    for mutated in (
        final | {"f7_packages_enabled": False},
        final | {"f6_features_enabled": False},
        final | {"reader": {**final["reader"], "password": "changed"}},
        final | {"reader": {**final["reader"], "schema_contract": "different"}},
    ):
        with pytest.raises(ValueError):
            m.require_active_scope_config_delta(base, mutated, product=is_product,
                                               approved_coordinates=approved)
    with pytest.raises(ValueError):
        m.require_active_scope_config_delta(base | {"f7_packages_enabled": False}, final,
                                           product=is_product, approved_coordinates=approved)
    if is_product:
        for key, value in (("state_directory", "other-ledger"), ("model_name", "other-model"),
                           ("stage_budget", {"cap_microyuan": 6_000_000})):
            changed = deepcopy(final)
            changed["model"][key] = value
            with pytest.raises(ValueError):
                m.require_active_scope_config_delta(base, changed, product=True,
                    approved_coordinates=approved | {
                        "model." + key: m.coordinate_digest(value),
                    })


def test_first_phase_scope_install_keeps_package_intake_off_and_requires_exact_review():
    m = module()
    _, product, _, writer = configurations()
    for is_product, final in ((True, product), (False, writer)):
        base = deepcopy(final)
        base["f7_packages_enabled"] = False
        interim = deepcopy(base)
        interim["family"]["scope_bindings"] = ["old", "formally-issued"]
        approved = {
            "family.scope_bindings": m.coordinate_digest(
                interim["family"]["scope_bindings"]
            )
        }
        m.require_first_phase_off_config_delta(
            base, interim, product=is_product, approved_coordinates=approved
        )
        with pytest.raises(ValueError):
            m.require_first_phase_off_config_delta(base, interim, product=is_product)
        with pytest.raises(ValueError):
            m.require_first_phase_off_config_delta(
                base,
                interim | {"f7_packages_enabled": True},
                product=is_product,
                approved_coordinates=approved,
            )
        changed = deepcopy(interim)
        changed["family"]["private_contact"] = "not reviewed"
        with pytest.raises(ValueError):
            m.require_first_phase_off_config_delta(
                base, changed, product=is_product, approved_coordinates=approved
            )


def test_first_phase_scope_install_requires_formal_grants_but_keeps_actual_pair_off(monkeypatch):
    from scripts.f7_engineering import activation

    m = module()
    _, product, _, writer = configurations()
    for value in (product, writer):
        value["f7_packages_enabled"] = False
    configs = {"Product": product, "Writer": writer}
    proof = {"image": "sha256:" + "a" * 64}
    expected = {name: proof["image"] for name in configs}
    seen = []
    monkeypatch.setattr(
        activation,
        "require_current_interim_scopes",
        lambda **kw: seen.append(kw),
    )
    m._require_current_interim(
        proof=proof,
        configurations=configs,
        evidence={"phase_folders": ["formal-first-phase"]},
        helper=SimpleNamespace(PRODUCT="Product", WRITER="Writer"),
    )
    assert all(
        value["f7_packages_enabled"] is False
        for value in seen[0]["configurations"].values()
    )
    assert all(value["f7_packages_enabled"] is False for value in configs.values())

    calls = []
    monkeypatch.setattr(
        m.retained, "retained_helper", lambda: SimpleNamespace(PRODUCT="Product", WRITER="Writer")
    )
    monkeypatch.setattr(
        m.retained, "update_services", lambda **kw: calls.append(kw) or {"status": "prepared"}
    )
    assert m.install_first_phase_scopes(
        proof=proof,
        configurations=configs,
        evidence_path=None,
        expected_images=expected,
        activation_evidence={"phase_folders": ["formal-first-phase"]},
    ) == {"status": "prepared"}
    assert calls[0]["_config_check"] is m.require_first_phase_off_config_delta
    assert calls[0]["_feature_check"] is m.require_feature_pair
    assert callable(calls[0]["_quiescent_check"])
    with pytest.raises(ValueError):
        m.install_first_phase_scopes(
            proof=proof,
            configurations={"Product": product},
            evidence_path=None,
            expected_images={"Product": proof["image"]},
            activation_evidence={"phase_folders": ["formal-first-phase"]},
        )


def test_off_runtime_repair_requires_verified_previous_image_and_schema_compatibility(monkeypatch):
    from scripts.f7_engineering import activation

    m = module()
    _, product, _, writer = configurations()
    configs = {
        "Product": product | {"f7_packages_enabled": False},
        "Writer": writer | {"f7_packages_enabled": False},
    }
    previous = {"image": "sha256:" + "a" * 64, "source_sha256": "c" * 64}
    candidate = {"image": "sha256:" + "b" * 64, "source_sha256": "d" * 64}
    gates, calls = [], []
    monkeypatch.setattr(
        m.retained, "retained_helper", lambda: SimpleNamespace(PRODUCT="Product", WRITER="Writer")
    )
    monkeypatch.setattr(
        m.release, "require_snapshot", lambda value: gates.append(("snapshot", value))
    )
    monkeypatch.setattr(
        activation, "require_migrated_runtime_compatibility",
        lambda old, new: gates.append(("compatible", old, new)),
    )
    monkeypatch.setattr(
        m, "_require_current_interim", lambda **kw: gates.append(("current_authority", kw))
    )
    monkeypatch.setattr(
        m.retained, "update_services", lambda **kw: calls.append(kw) or {"status": "prepared"}
    )
    args = dict(
        proof=candidate, previous_proof=previous, configurations=configs, evidence_path=None,
        expected_images={name: previous["image"] for name in configs}, activation_evidence={},
    )
    assert m.install_first_phase_scopes(**args) == {"status": "prepared"}
    assert gates[0] == ("snapshot", previous)
    assert gates[1] == ("compatible", previous, candidate)
    assert gates[2][0] == "current_authority"
    assert calls[0]["expected_images"] == args["expected_images"]
    with pytest.raises(ValueError):
        m.install_first_phase_scopes(**(args | {
            "expected_images": {name: candidate["image"] for name in configs},
        }))
    assert len(calls) == 1


def test_recovered_activation_cannot_restart_budget_or_change_model_even_if_digest_supplied():
    m = module()
    _, final, _, _ = configurations()
    off = deepcopy(final)
    off["f7_packages_enabled"] = False
    for key, value in (
        ("stage_budget", final["model"]["stage_budget"] | {"stage_id": "new-stage"}),
        ("state_directory", "new-ledger"),
        ("reasoning_effort", "low"),
    ):
        changed = deepcopy(final)
        changed["model"][key] = value
        with pytest.raises(ValueError):
            m.require_recovered_activation_config_delta(
                off,
                changed,
                product=True,
                approved_coordinates={"model." + key: m.coordinate_digest(value)},
            )


def test_recovered_activation_reuses_final_gate_and_requires_same_image(monkeypatch):
    m = module()
    _, product, _, writer = configurations()
    configurations_by_name = {"Product": product, "Writer": writer}
    helper = SimpleNamespace(PRODUCT="Product", WRITER="Writer")
    image = "sha256:" + "a" * 64
    calls, gates, rechecks = [], [], []
    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "update_services", lambda **kw: calls.append(kw))
    monkeypatch.setattr(m, "_require_start_authority", lambda **kw: gates.append(kw) or {})
    monkeypatch.setattr(m, "_require_start_unchanged", lambda **kw: rechecks.append(kw))
    monkeypatch.setattr(m, "require_quiescent", lambda *args: None)
    args = dict(
        proof={"image": image},
        configurations=configurations_by_name,
        expected_images={"Product": image, "Writer": image},
        evidence_path=None,
        recovery_evidence="final-pair.json",
        activation_evidence={"fixed": "receipt-paths"},
    )
    m.enable_package_intake(**args)
    assert len(gates) == 1 and gates[0]["activation"] == "f7_on"
    assert calls[0]["_config_check"] is m.require_recovered_activation_config_delta
    calls[0]["_quiescent_check"](helper, m.CONTRACT)
    assert rechecks[0]["activation"] == "f7_on"
    with pytest.raises(ValueError):
        m.enable_package_intake(**(args | {"expected_images": {"Product": image, "Writer": "old"}}))
    assert len(calls) == 1

    def reject(**kwargs):
        raise ValueError("Current grant expired")

    monkeypatch.setattr(m, "_require_start_authority", reject)
    with pytest.raises(ValueError, match="expired"):
        m.enable_package_intake(**args)
    assert len(calls) == 1


def test_recovered_activation_failure_closes_already_verified_owned_writer(monkeypatch, tmp_path):
    m = module()
    _, product, _, writer = configurations()
    image = "sha256:" + "a" * 64
    names = ("Product", "Writer")
    live = {
        name: {"Id": name + "-new", "Image": image, "State": {"Running": name == "Writer"}}
        for name in names
    }
    commands = []

    def safe_run(argv, **kw):
        commands.append(argv)
        assert argv[:2] == ["docker", "stop"]
        next(row for row in live.values() if row["Id"] == argv[-1])["State"]["Running"] = False

    helper = SimpleNamespace(
        PRODUCT="Product",
        WRITER="Writer",
        DOCKER=["docker"],
        inspect=lambda name: live[name],
        safe_run=safe_run,
    )
    evidence = tmp_path / "reactivation.json"

    def partial_failure(**kw):
        evidence.write_text(
            json.dumps(
                {
                    "image": image,
                    "status": "stopped",
                    "services": [
                        {"name": "Writer", "new_id": "Writer-new", "phase": "verified"},
                        {
                            "name": "Product",
                            "new_id": "Product-new",
                            "phase": "unverified_candidate_stopped",
                        },
                    ],
                }
            )
        )
        raise RuntimeError("candidate failed")

    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(m.retained, "update_services", partial_failure)
    monkeypatch.setattr(m, "_require_start_authority", lambda **kw: {})
    with pytest.raises(RuntimeError):
        m.enable_package_intake(
            proof={"image": image},
            configurations={"Product": product, "Writer": writer},
            expected_images=dict.fromkeys(names, image),
            evidence_path=evidence,
            recovery_evidence="final.json",
            activation_evidence={},
            execute=True,
        )
    assert not any(row["State"]["Running"] for row in live.values())
    assert len(commands) == 1 and commands[0][-1] == "Writer-new"
    assert json.loads(evidence.read_text())["status"] == "stopped"


def test_reactivation_cleanup_does_not_stop_replaced_or_foreign_owner(tmp_path):
    m = module()
    image = "sha256:" + "a" * 64
    evidence = tmp_path / "outcome.json"
    evidence.write_text(
        json.dumps(
            {
                "image": image,
                "services": [
                    {"name": "Writer", "new_id": "owned-id", "phase": "verified"},
                ],
            }
        )
    )
    stopped = []
    helper = SimpleNamespace(
        PRODUCT="Product",
        WRITER="Writer",
        DOCKER=["docker"],
        inspect=lambda name: {"Id": "replacement-id", "Image": image, "State": {"Running": True}},
        safe_run=lambda *args, **kwargs: stopped.append(args),
    )
    with pytest.raises(RuntimeError, match="reconciliation"):
        m._close_owned_activation_pair(helper, proof={"image": image}, evidence_path=evidence)
    assert not stopped
    assert json.loads(evidence.read_text())["status"] == "closure_unknown"


@pytest.fixture
def paired_backout(monkeypatch, tmp_path):
    """Drive the real retained updater against a bounded synthetic Docker adapter."""
    m = module()
    _, product, _, writer = configurations()
    product["model"]["stage_budget"]["cap_microyuan"] = 4_000_000
    configs = {"Product": product, "Writer": writer}
    for name, config in configs.items():
        config["reader"]["schema_contract"] = m.REPAIRED_CONTRACT
        config["runtime" if name == "Product" else "writer"][
            "postgres_schema_contract" if name == "Product" else "schema_contract"
        ] = m.REPAIRED_CONTRACT
    proofs = {
        name: {"image": "sha256:" + token * 64, "source_sha256": token * 64,
               "schema_contract": m.REPAIRED_CONTRACT}
        for name, token in (("Product", "a"), ("Writer", "b"))
    }
    live = {
        name: {
            "Id": name + "-original", "Image": proofs[name]["image"],
            "State": {"Running": True}, "Mounts": [],
            "Config": {"Env": [], "Labels": {"project": "owned"}},
            "HostConfig": {},
            "NetworkSettings": {"Networks": {"campus-school-facts-f1-model-egress": {}}},
        } for name in configs
    }
    originals = deepcopy(live)
    private = deepcopy(configs)
    final = {name: value | {"f7_packages_enabled": False} for name, value in configs.items()}
    calls = []
    faults = {}

    def name_for(path):
        return "Product" if path.name == "product-config.json" else "Writer"

    def safe_run(argv, **kwargs):
        args = argv[1:]
        calls.append(tuple(args))
        if args[0] == "ps":
            return b""
        if args[0] == "stop":
            target = args[-1]
            if faults.pop("stop", None) == target:
                raise RuntimeError("synthetic stop failure")
            row = live[target] if target in live else next(
                r for r in live.values() if r["Id"] == target
            )
            row["State"]["Running"] = False
        elif args[0] == "rename":
            live[args[2]] = live.pop(args[1])
        elif args[0] == "create":
            name, image = args[1:]
            if faults.get("create") == name:
                raise RuntimeError("synthetic creation failure")
            live[name] = deepcopy(originals[name])
            live[name].update(Id=name + "-new", Image=image, State={"Running": False})
            return live[name]["Id"].encode()
        elif args[0] == "start":
            live[args[1]]["State"]["Running"] = True
        return b""

    def manifest(name, proof):
        assert live[name]["Image"] == proof["image"]
        calls.append(("manifest", name, proof["image"]))

    helper = SimpleNamespace(
        PRODUCT="Product", WRITER="Writer", DOCKER=["docker"],
        inspect=lambda name: deepcopy(live[name] if name in live else next(
            row for row in live.values() if row["Id"] == name)), safe_run=safe_run,
        read_json=lambda path: deepcopy(private[name_for(path)]),
        atomic_private_config=lambda path, value: private.__setitem__(
            name_for(path), deepcopy(value)
        ),
        require_candidate_image=lambda image, proof: calls.append(
            ("candidate", image, proof["image"])
        ),
        validate_runtime_config=lambda image, config, **kw: calls.append(
            ("config", image, kw["product"])
        ),
        recreate_command=lambda name, old, image: ["docker", "create", name, image],
        verify_manifest=manifest, verify_service_surface=lambda *a, **kw: None,
    )
    monkeypatch.setattr(m.retained, "retained_helper", lambda: helper)
    monkeypatch.setattr(
        m.release, "require_snapshot", lambda proof: calls.append(("snapshot", proof["image"]))
    )
    from scripts.f7_engineering import activation
    monkeypatch.setattr(activation, "require_migrated_runtime_compatibility",
                        lambda old, new: calls.append(("compatible", old["image"], new["image"])))
    monkeypatch.setattr(m, "require_quiescent", lambda *a: None)
    monkeypatch.setattr(
        m, "verify_f7_surface", lambda h, name, **kw: calls.append(("surface", name, kw))
    )
    args = dict(proof=proofs["Product"], service_proofs=proofs, configurations=final,
                expected_images={name: proof["image"] for name, proof in proofs.items()},
                evidence_path=tmp_path / "pair.json")
    return SimpleNamespace(m=m, args=args, helper=helper, live=live, originals=originals,
                           private=private, calls=calls, faults=faults)


def test_distinct_backout_preserves_each_verified_image_and_four_yuan_budget(paired_backout):
    p = paired_backout
    result = p.m.disable_package_intake(**p.args, execute=True)
    assert result["status"] == "verified"
    assert result["service_artifacts"] == {
        name: {"image": proof["image"], "source_sha256": proof["source_sha256"]}
        for name, proof in p.args["service_proofs"].items()
    }
    for name, proof in p.args["service_proofs"].items():
        assert p.live[name]["Image"] == proof["image"]
        assert ("manifest", name, proof["image"]) in p.calls
        assert p.private[name]["f7_packages_enabled"] is False
    assert p.private["Product"]["model"]["stage_budget"]["cap_microyuan"] == 4_000_000


@pytest.mark.parametrize("fault", ["missing", "swapped", "schema", "foreign", "budget"])
def test_distinct_backout_rejects_wrong_pair_before_mutation(paired_backout, fault):
    p = paired_backout
    if fault == "missing":
        del p.args["service_proofs"]["Writer"]
    elif fault == "swapped":
        p.args["service_proofs"]["Writer"] = deepcopy(p.args["proof"])
    elif fault == "schema":
        p.args["service_proofs"]["Writer"]["schema_contract"] = p.m.CONTRACT
    elif fault == "foreign":
        for field in ("configurations", "service_proofs", "expected_images"):
            p.args[field]["ForeignWriter"] = p.args[field].pop("Writer")
    else:
        p.args["configurations"]["Product"]["model"] = deepcopy(p.private["Product"]["model"])
        p.args["configurations"]["Product"]["model"]["stage_budget"]["cap_microyuan"] = 3_000_000
    with pytest.raises(ValueError):
        p.m.disable_package_intake(**p.args, execute=True)
    assert not any(c[0] in {"stop", "rename", "create", "start"} for c in p.calls)


def test_distinct_backout_failure_closes_writer_and_original_product(paired_backout):
    p = paired_backout
    p.faults["create"] = "Product"
    with pytest.raises(RuntimeError):
        p.m.disable_package_intake(**p.args, execute=True)
    assert all(not row["State"]["Running"] for row in p.live.values())
    result = json.loads(p.args["evidence_path"].read_bytes())
    assert result["status"] == "stopped"
    assert result["original_product_phase"] == "preserved_stopped"


def test_distinct_backout_mount_change_closes_all_owned_candidates(paired_backout, monkeypatch):
    p = paired_backout
    def changed_mount(h, name, **kwargs):
        if name == "Product":
            p.live[name]["Mounts"] = [{"Type": "volume", "Name": "changed",
                                        "Source": "changed", "Destination": "/ledger", "RW": True}]
    monkeypatch.setattr(p.m, "verify_f7_surface", changed_mount)
    with pytest.raises(RuntimeError):
        p.m.disable_package_intake(**p.args, execute=True)
    assert all(not row["State"]["Running"] for row in p.live.values())
    assert json.loads(p.args["evidence_path"].read_bytes())["status"] == "stopped"


def test_distinct_backout_stop_failure_closes_both_owned_originals(paired_backout):
    p = paired_backout
    p.faults["stop"] = "Writer"
    with pytest.raises(RuntimeError):
        p.m.disable_package_intake(**p.args, execute=True)
    assert all(not row["State"]["Running"] for row in p.live.values())


def test_distinct_backout_does_not_close_foreign_replacement(paired_backout, monkeypatch):
    p = paired_backout
    def replace_writer(h, name, **kwargs):
        p.live["Writer"]["Id"] = "foreign-id"
        p.live["Writer"]["Config"]["Labels"] = {"project": "foreign"}
        raise RuntimeError("synthetic replaced owner")
    monkeypatch.setattr(p.m, "verify_f7_surface", replace_writer)
    with pytest.raises(RuntimeError, match="reconciliation"):
        p.m.disable_package_intake(**p.args, execute=True)
    assert p.live["Writer"]["State"]["Running"] is True
    assert not any(c[0] == "stop" and c[-1] == "foreign-id" for c in p.calls)
    assert json.loads(p.args["evidence_path"].read_bytes())["status"] == "closure_unknown"


def test_distinct_backout_mount_drift_before_stop_is_rejected(paired_backout, monkeypatch):
    p = paired_backout
    checks = []
    def drift(*args):
        checks.append(True)
        if len(checks) == 2:
            p.live["Writer"]["Mounts"] = [{"Type": "bind", "Source": "foreign",
                "Destination": "/config", "RW": False}]
    monkeypatch.setattr(p.m, "require_quiescent", drift)
    with pytest.raises(RuntimeError):
        p.m.disable_package_intake(**p.args, execute=True)
    assert not any(c[0] in {"rename", "create", "start"} for c in p.calls)
