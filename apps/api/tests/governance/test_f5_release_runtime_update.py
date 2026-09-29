import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


def module(name):
    path = Path(__file__).resolve().parents[4] / ("scripts/f5_engineering/" + name + ".py")
    spec = importlib.util.spec_from_file_location("f5_" + name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def config():
    old = {
        "purpose": "school-facts-f4-synthetic",
        "reader": {"schema_contract": "school-facts-f4-v31"},
        "runtime": {"postgres_schema_contract": "school-facts-f4-v31", "postgres_host": "fixed"},
        "family": {"scope_bindings": ["old"]},
        "directory": {"scope_bindings": ["old"]},
        "education": {"scope_bindings": ["old"]},
        "model": {
            "state_directory": "fixed-ledger",
            "model": "deepseek-flash",
            "budget": {"limit": 50},
            "stage_budget": {"stage_id": "school-facts-f4", "cap_microyuan": 3000000},
        },
    }
    new = deepcopy(old)
    new.update(
        purpose="school-facts-f5-synthetic",
        family_leadership={"version": 1},
        f5_features_enabled=True,
    )
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f5-v41"
    )
    new["model"]["stage_budget"]["stage_id"] = "school-facts-f5"
    return old, new


def test_f5_config_update_and_same_schema_feature_fallback_preserve_ledger():
    old, new = config()
    m = module("runtime_update")
    m.require_config_delta(old, new, product=True)
    fallback = deepcopy(new)
    fallback["f5_features_enabled"] = False
    m.require_config_delta(new, fallback, product=True)
    fallback["purpose"] = "school-facts-f4-synthetic"
    with pytest.raises(ValueError):
        m.require_config_delta(new, fallback, product=True)


def test_runtime_update_accepts_exact_stage_extension_and_retains_it_on_redeploy():
    _, old = config()
    old["model"]["stage_budget"]["approval_reference"] = "original-f5-budget"
    new = deepcopy(old)
    new["model"]["stage_budget"].update(
        cap_microyuan=6000000,
        approval_reference="approved-f5-extension",
        previous_cap_microyuan=3000000,
        previous_approval_reference="original-f5-budget",
    )
    m = module("runtime_update")
    m.require_config_delta(old, new, product=True)
    m.require_config_delta(new, deepcopy(new), product=True)
    fallback = deepcopy(new)
    fallback["f5_features_enabled"] = False
    m.require_config_delta(new, fallback, product=True)
    with pytest.raises(ValueError):
        m.require_config_delta(new, old, product=True)


@pytest.mark.parametrize(
    "change",
    [
        {"cap_microyuan": 6000001},
        {"previous_cap_microyuan": 2000000},
        {"previous_approval_reference": "different"},
        {"previous_approval_reference": None},
        {"approval_reference": "original-f5-budget"},
    ],
)
def test_runtime_update_rejects_stage_extensions_without_exact_old_approval(change):
    _, old = config()
    old["model"]["stage_budget"]["approval_reference"] = "original-f5-budget"
    new = deepcopy(old)
    new["model"]["stage_budget"].update(
        cap_microyuan=6000000,
        approval_reference="approved-f5-extension",
        previous_cap_microyuan=3000000,
        previous_approval_reference="original-f5-budget",
    )
    new["model"]["stage_budget"].update(change)
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old, new, product=True)


def test_runtime_update_cannot_replace_same_stage_approval_or_extend_f4_as_f5():
    old_f4, old_f5 = config()
    old_f5["model"]["stage_budget"]["approval_reference"] = "original"
    changed = deepcopy(old_f5)
    changed["model"]["stage_budget"]["approval_reference"] = "replacement"
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old_f5, changed, product=True)
    changed["model"]["stage_budget"].update(
        cap_microyuan=6000000,
        previous_cap_microyuan=3000000,
        previous_approval_reference="original",
    )
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old_f4, changed, product=True)


@pytest.mark.parametrize("field", ["model", "ledger", "family", "database", "schema"])
def test_f5_update_rejects_unrelated_changes(field):
    old, new = config()
    if field == "model":
        new["model"]["model"] = "different"
    if field == "ledger":
        new["model"]["state_directory"] = "fresh-ledger"
    if field == "family":
        new["family"]["scope_bindings"] = ["broader"]
    if field == "database":
        new["runtime"]["postgres_host"] = "other"
    if field == "schema":
        new["reader"]["schema_contract"] = "school-facts-f5-v34"
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old, new, product=True)


def test_update_requires_exact40_readonly_database_probe():
    calls = []
    h = SimpleNamespace(
        DOCKER=[],
        safe_run=lambda *a, **kw: (
            calls.append(kw["data"].decode()) or b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"
        ),
    )
    module("runtime_update").require_quiescent(h, "school-facts-f5-v40")
    assert "='20260914_40'" in calls[0]
    assert "BEGIN READ ONLY" in calls[0]
    assert "20260913_31" not in calls[0]


def test_disabled35_fallback_is_retained_but_enabled35_is_rejected():
    old, new = config()
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f5-v35"
    )
    m = module("runtime_update")
    with pytest.raises(ValueError):
        m.require_config_delta(old, new, product=True)
    new["f5_features_enabled"] = False
    m.require_config_delta(old, new, product=True)


def test_new_release_allows_only_exact_added_migration40():
    m = module("release")
    assert m.allowed_addition(
        "apps/api/alembic/versions/20260914_40_f5_current_school_relationship.py"
    )
    assert not m.allowed_addition("apps/api/alembic/versions/20260914_41_unreviewed.py")


def test_current_release_adds_exact41_and_requires_exact_readonly_schema41_probe():
    release = module("release")
    assert release.allowed_addition(
        "apps/api/alembic/versions/20260914_41_f5_cancellation_intervals.py"
    )
    calls = []
    helper = SimpleNamespace(
        DOCKER=[],
        safe_run=lambda *a, **kw: (
            calls.append(kw["data"].decode()) or b"c1137be2-0734-4b69-8937-37dbd741f141\nready\n"
        ),
    )
    module("runtime_update").require_quiescent(helper)
    assert "='20260914_41'" in calls[0]
    assert "BEGIN READ ONLY" in calls[0]


def test_enabled40_is_retained_only_as_explicit_disabled_compatibility():
    old, new = config()
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f5-v40"
    )
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old, new, product=True)
    new["f5_features_enabled"] = False
    module("runtime_update").require_config_delta(old, new, product=True)


def test_explicit_followup_migration_keeps_original35_precondition():
    m = module("migration")
    _, code = m.retained_runner(expected_from="20260914_35", target="20260914_36")
    assert "20260914_35" in code and "20260914_36" in code
    assert "20260913_31" not in code
    with pytest.raises(ValueError):
        m.retained_runner(expected_from="20260913_31", target="20260914_36")


def test_retained_migration_template_matches_original_f4_bytes_and_only_prepares_argv(tmp_path):
    import hashlib

    migration = module("migration")
    template = migration._retained_template()

    assert hashlib.sha256(template.derive_f4_runner().encode()).hexdigest() == (
        "a81710cf9036814443ba78cf141f20b37519b6427d5cfb2fc044afd31dcbb242"
    )
    helper, code = migration.retained_runner()
    assert "20260913_31" in code and "20260914_35" in code
    helper.RUNNER = code
    argv = helper.migration_command({"image": "synthetic-image", "path": tmp_path / "proof.json"})
    assert argv[-3:] == ["python", "-c", code]
    assert argv[argv.index("--network") + 1] == "campus-school-facts-f1-private"
    assert "synthetic-image" in argv
    assert all("runtime/f3-execution" not in part for part in argv)


def test_retained_runner_loads_from_adjacent_file_under_api_ci_working_directory():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[4]
    api = root / "apps/api"
    script = root / "scripts/f5_engineering/migration.py"
    code = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('f5_ci',sys.argv[1]); "
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
        "_,runner=m.retained_runner(expected_from='20260914_35',target='20260914_36'); "
        "assert '20260914_36' in runner; print('ok')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(api)
    result = subprocess.run(
        [sys.executable, "-c", code, str(script)],
        cwd=api,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "ok"


def test_release_pins_baseline_image_and_excludes_unrelated_frontend():
    m = module("release")
    assert m.IMPLEMENTATION_HEAD == "98bda14704508ab8cb2c079ad33e3f66ec660613"
    assert m.BASELINE == "677a82fcb5d207a3d4e9919e0a8f49a318897577"
    assert m.BASE == "sha256:360bc7ae0ca58f85e7e90397d30d3e246788acfe98521998c221f1fece327a20"
    assert len(m.FRONTEND_EXCLUSIONS) == 3
    assert m.allowed_addition("apps/api/app/composition/school_fact_family_teaching_query.py")
    assert not m.allowed_addition(
        "apps/api/build/lib/app/composition/school_fact_family_teaching_query.py"
    )
    assert not m.allowed_addition("apps/web/src/app/Unrelated.vue")


def test_retained_scope_delta_requires_exact_approved_coordinate_hash():
    old, new = config()
    m = module("runtime_update")
    new["directory"]["scope_bindings"] = ["reviewed-union"]
    digest = m.coordinate_digest(new["directory"]["scope_bindings"])
    m.require_config_delta(
        old, new, product=True, approved_coordinates={"directory.scope_bindings": digest}
    )
    new["directory"]["scope_bindings"] = ["changed-after-review"]
    with pytest.raises(ValueError):
        m.require_config_delta(
            old, new, product=True, approved_coordinates={"directory.scope_bindings": digest}
        )


def test_release_snapshot_tamper_and_escape_are_rejected(tmp_path, monkeypatch):
    m = module("release")
    monkeypatch.setattr(m, "OUT", tmp_path)
    folder = tmp_path / "snapshot"
    folder.mkdir()
    (folder / "source.py").write_bytes(b"original")
    files = {"source.py": m.digest(b"original")}
    import json

    proof = dict(
        baseline=m.BASELINE,
        base_image=m.BASE,
        schema_contract="school-facts-f5-v41",
        implementation_head=m.IMPLEMENTATION_HEAD,
        snapshot_path=str(folder),
        files=files,
        image_files=files,
        source_sha256=m.digest(json.dumps(files, sort_keys=True).encode()),
    )
    m.require_snapshot(proof)
    with pytest.raises(ValueError):
        m.require_snapshot(dict(proof, implementation_head="unapproved-head"))
    historical = dict(proof, schema_contract="school-facts-f5-v35")
    with pytest.raises(ValueError):
        m.require_snapshot(historical)
    m.require_snapshot(historical, expected_contract="school-facts-f5-v35")
    (folder / "source.py").write_bytes(b"changed")
    with pytest.raises(ValueError):
        m.require_snapshot(proof)
    proof["snapshot_path"] = str(tmp_path.parent)
    with pytest.raises(ValueError):
        m.require_snapshot(proof)


def test_mount_or_network_change_is_rejected():
    m = module("runtime_update")
    old = {
        "Config": {"Env": ["A=retained"]},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "ledger",
                "Source": "fixed",
                "Destination": "/ledger",
                "RW": True,
            }
        ],
        "HostConfig": {},
        "NetworkSettings": {"Networks": {"private": {}}},
    }
    new = deepcopy(old)
    new["Mounts"][0]["Name"] = "fresh-ledger"
    with pytest.raises(ValueError):
        m.require_preserved_shape(old, new)
    new = deepcopy(old)
    new["NetworkSettings"]["Networks"]["public"] = {}
    with pytest.raises(ValueError):
        m.require_preserved_shape(old, new)


def test_product_writer_feature_flags_must_match():
    m = module("runtime_update")
    m.require_feature_pair(
        {"Product": {"f5_features_enabled": False}, "Writer": {"f5_features_enabled": False}}
    )
    with pytest.raises(ValueError):
        m.require_feature_pair({"Product": {"f5_features_enabled": False}, "Writer": {}})
    with pytest.raises(ValueError):
        m.require_feature_pair(
            {"Product": {"f5_features_enabled": 0}, "Writer": {"f5_features_enabled": 0}}
        )


def test_explicit_timezone_followup_requires_original36():
    m = module("migration")
    _, code = m.retained_runner(expected_from="20260914_36", target="20260914_37")
    assert "20260914_36" in code and "20260914_37" in code
    assert "20260913_31" not in code
    with pytest.raises(ValueError):
        m.retained_runner(expected_from="20260914_35", target="20260914_37")


def test_disabled36_compatibility_preserves_f5_stage():
    old, new = config()
    new["reader"]["schema_contract"] = new["runtime"]["postgres_schema_contract"] = (
        "school-facts-f5-v36"
    )
    with pytest.raises(ValueError):
        module("runtime_update").require_config_delta(old, new, product=True)
    new["f5_features_enabled"] = False
    module("runtime_update").require_config_delta(old, new, product=True)


def test_explicit_combined38_requires_both_migration_hashes(tmp_path):
    import hashlib
    import json

    from tests.governance.test_f5_migration_guards import inputs

    m, backup, proof, _ = inputs.__wrapped__(tmp_path)
    hashes = {}
    for revision in (37, 38):
        name = f"apps/api/alembic/versions/20260914_{revision}_test.py"
        path = tmp_path / name
        path.write_bytes(str(revision).encode())
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    backup["snapshot"]["schema"] = "20260914_36"
    proof.update(
        schema_contract="school-facts-f5-v38",
        files=hashes.copy(),
        image_files=hashes.copy(),
        source_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    )
    kwargs = dict(backup=backup, proof=proof, expected_from="20260914_36", target="20260914_38")
    assert m.validate(**kwargs, tested_hashes=hashes) == hashes
    for key in hashes:
        with pytest.raises(ValueError):
            m.validate(**kwargs, tested_hashes={k: v for k, v in hashes.items() if k != key})
    _, code = module("migration").retained_runner(expected_from="20260914_36", target="20260914_38")
    assert "20260914_36" in code and "20260914_38" in code
    with pytest.raises(ValueError):
        module("migration").retained_runner(expected_from="20260914_35", target="20260914_38")


def test_explicit39_requires_only_exact40_hash_and_original38(tmp_path):
    import hashlib
    import json

    from tests.governance.test_f5_migration_guards import inputs

    m, backup, proof, _ = inputs.__wrapped__(tmp_path)
    name = "apps/api/alembic/versions/20260914_39_f5_cancellation_proof.py"
    path = tmp_path / name
    path.write_bytes(b"39")
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()}
    backup["snapshot"]["schema"] = "20260914_38"
    proof.update(
        schema_contract="school-facts-f5-v39",
        files=hashes.copy(),
        image_files=hashes.copy(),
        source_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    )
    kwargs = dict(backup=backup, proof=proof, expected_from="20260914_38", target="20260914_39")
    assert m.validate(**kwargs, tested_hashes=hashes) == hashes
    for wrong in ({}, {name: "bad"}, hashes | {"unreviewed": "extra"}):
        with pytest.raises(ValueError):
            m.validate(**kwargs, tested_hashes=wrong)
    _, code = module("migration").retained_runner(expected_from="20260914_38", target="20260914_39")
    assert "20260914_38" in code and "20260914_39" in code
    with pytest.raises(ValueError):
        module("migration").retained_runner(expected_from="20260914_36", target="20260914_39")


@pytest.mark.parametrize("from_revision,revisions", [(38, (39, 40)), (39, (40,))])
def test_exact40_transition_rejects_missing_extra_hash_or_unapproved_span(
    tmp_path, from_revision, revisions
):
    import hashlib
    import json

    from tests.governance.test_f5_migration_guards import inputs

    m, backup, proof, _ = inputs.__wrapped__(tmp_path)
    hashes = {}
    for revision in revisions:
        name = f"apps/api/alembic/versions/20260914_{revision}_test.py"
        path = tmp_path / name
        path.write_bytes(str(revision).encode())
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    backup["snapshot"]["schema"] = f"20260914_{from_revision}"
    proof.update(
        schema_contract="school-facts-f5-v40",
        files=hashes.copy(),
        image_files=hashes.copy(),
        source_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    )
    kwargs = dict(
        backup=backup, proof=proof, expected_from=f"20260914_{from_revision}", target="20260914_40"
    )
    assert m.validate(**kwargs, tested_hashes=hashes) == hashes
    for key in hashes:
        with pytest.raises(ValueError):
            m.validate(**kwargs, tested_hashes={k: v for k, v in hashes.items() if k != key})
    with pytest.raises(ValueError):
        m.validate(**kwargs, tested_hashes=hashes | {"unapproved": "extra"})
    _, code = module("migration").retained_runner(
        expected_from=f"20260914_{from_revision}", target="20260914_40"
    )
    assert f"20260914_{from_revision}" in code and "20260914_40" in code
    with pytest.raises(ValueError):
        module("migration").retained_runner(expected_from="20260914_37", target="20260914_40")


def test_family_report_read_gateway_is_explicit_release_addition():
    release = module("release")
    assert release.allowed_addition(
        "apps/api/app/composition/school_fact_family_correction_gateway.py"
    )
    assert not release.allowed_addition("apps/api/app/composition/arbitrary_correction_gateway.py")


def test_exact40_to41_transition_requires_original40_and_only_41_hash(tmp_path):
    import hashlib
    import json

    from tests.governance.test_f5_migration_guards import inputs

    migration, backup, proof, _ = inputs.__wrapped__(tmp_path)
    name = "apps/api/alembic/versions/20260914_41_f5_cancellation_intervals.py"
    path = tmp_path / name
    path.write_bytes(b"41")
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()}
    backup["snapshot"]["schema"] = "20260914_40"
    proof.update(
        schema_contract="school-facts-f5-v41",
        implementation_head="c2962e4b762aa8f619503b7742915e8fe4219c7e",
        files=hashes.copy(),
        image_files=hashes.copy(),
        source_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    )
    kwargs = dict(backup=backup, proof=proof, expected_from="20260914_40", target="20260914_41")
    assert migration.validate(**kwargs, tested_hashes=hashes) == hashes
    with pytest.raises(ValueError):
        migration.validate(
            **(kwargs | {"proof": dict(proof, implementation_head="other")}), tested_hashes=hashes
        )
    for wrong in ({}, {name: "bad"}, hashes | {"unreviewed": "extra"}):
        with pytest.raises(ValueError):
            migration.validate(**kwargs, tested_hashes=wrong)
    _, code = module("migration").retained_runner(expected_from="20260914_40", target="20260914_41")
    assert "20260914_40" in code and "20260914_41" in code
    for start in ("20260914_38", "20260914_39", "20260914_41"):
        with pytest.raises(ValueError):
            module("migration").retained_runner(expected_from=start, target="20260914_41")


def test_resumed_release_binds_current_head_and_preserves_original_frontend(tmp_path, monkeypatch):
    import json

    release = module("release")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    inherited = tmp_path / "runtime/f5-execution/baseline.json"
    inherited.parent.mkdir(parents=True)
    inherited.write_text(
        json.dumps(
            {
                "head": release.BASELINE,
                "inherited_files_sha256": {str(i): "hash" for i in range(236)},
            }
        )
    )
    production = "apps/api/app/composition/school_fact_product.py"
    path = tmp_path / production
    path.parent.mkdir(parents=True)
    path.write_bytes(b"resumed-code")
    current = release.IMPLEMENTATION_HEAD
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[1] == "rev-parse":
            return current.encode()
        if args[1] == "ls-tree":
            assert args[4] == current
            return "\n".join([production, *sorted(release.FRONTEND_EXCLUSIONS)]).encode()
        if args[1] == "ls-files":
            return b""
        assert args[1] == "show" and args[2].startswith(release.BASELINE + ":")
        return b"preserved-original-frontend"

    monkeypatch.setattr(release, "command", command)
    result = release.sources()
    assert result[production] == b"resumed-code"
    assert all(
        result[name] == b"preserved-original-frontend" for name in release.FRONTEND_EXCLUSIONS
    )
    current = "unapproved-head"
    with pytest.raises(RuntimeError, match="baseline_changed"):
        release.sources()


def test_read_dependency_config_requires_reviewed_exact_digest():
    old, new = config()
    m = module("runtime_update")
    new["family"]["read_dependency_bindings"] = ["approved-existing-scope"]
    with pytest.raises(ValueError):
        m.require_config_delta(old, new, product=True)
    approved = {
        "family.read_dependency_bindings": m.coordinate_digest(
            new["family"]["read_dependency_bindings"]
        )
    }
    m.require_config_delta(old, new, product=True, approved_coordinates=approved)
    new["family"]["read_dependency_bindings"] = ["another-scope"]
    with pytest.raises(ValueError):
        m.require_config_delta(old, new, product=True, approved_coordinates=approved)


def test_product_only_update_is_explicit_and_keeps_writer_as_checked_dependency():
    m = module("runtime_update")
    h = SimpleNamespace(PRODUCT="our-product", WRITER="our-writer")
    images = {h.PRODUCT: "old-product-image", h.WRITER: "old-writer-image"}
    with pytest.raises(ValueError):
        m.update_targets(h, {h.PRODUCT: {}}, images, product_only=False)
    assert m.update_targets(h, {h.PRODUCT: {}}, images, product_only=True) == (h.PRODUCT,)
    assert m.update_targets(h, {h.PRODUCT: {}, h.WRITER: {}}, images, product_only=False) == (
        h.WRITER,
        h.PRODUCT,
    )
    for bad in ({h.WRITER: {}}, {h.PRODUCT: {}, "other-project": {}}):
        with pytest.raises(ValueError):
            m.update_targets(h, bad, images, product_only=True)
    with pytest.raises(ValueError):
        m.update_targets(h, {h.PRODUCT: {}}, {h.PRODUCT: "image"}, product_only=True)


def product_only_update_simulation(monkeypatch, tmp_path, *, writer_drift=None):
    """Exercise orchestration with all Docker/config I/O replaced by in-memory state."""
    m = module("runtime_update")
    _, product_configuration = config()
    writer_configuration = {
        "reader": {"schema_contract": "school-facts-f5-v41"},
        "writer": {"schema_contract": "school-facts-f5-v41"},
        "f5_features_enabled": True,
    }
    original = {
        "Id": "original-product-id",
        "Image": "sha256:original-product",
        "State": {"Running": True},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "original-ledger",
                "Source": "retained-volume",
                "Destination": "/ledger",
                "RW": True,
            }
        ],
        "Config": {"Env": ["RETAINED_VALUE=test-only"]},
        "HostConfig": {},
        "NetworkSettings": {
            "Networks": {"campus-school-facts-f1-model-egress": {"Aliases": []}},
        },
    }
    state = {
        "product": deepcopy(original),
        "writer": {
            "Id": "original-writer-id",
            "Image": "sha256:original-writer",
            "State": {"Running": True},
        },
    }
    commands, configuration_writes = [], []

    def resolve(name):
        if name in state:
            return state[name]
        return next(record for record in state.values() if record["Id"] == name)

    def safe_run(args, **kwargs):
        assert args[0] == "fake-docker"
        command = args[1:]
        commands.append(command)
        operation = command[0]
        if operation == "stop":
            resolve(command[-1])["State"]["Running"] = False
        elif operation == "rename":
            state[command[-1]] = state.pop(command[-2])
        elif operation == "create":
            state["product"] = deepcopy(original)
            state["product"].update(Id="candidate-product-id", Image="sha256:candidate")
            state["product"]["State"]["Running"] = False
            return b"candidate-product-id"
        elif operation == "start":
            resolve(command[-1])["State"]["Running"] = True
        elif operation not in {"ps", "network"}:
            pytest.fail(f"Unexpected simulated Docker operation: {operation}")
        return b""

    def verify_surface(name, **kwargs):
        if name != "product":
            return
        if writer_drift == "id":
            state["writer"]["Id"] = "replacement-writer-id"
        elif writer_drift == "stopped":
            state["writer"]["State"]["Running"] = False
        elif writer_drift == "image":
            state["writer"]["Image"] = "sha256:unexpected-writer"
        elif writer_drift == "configuration":
            writer_configuration["f5_features_enabled"] = False

    helper = SimpleNamespace(
        PRODUCT="product",
        WRITER="writer",
        DOCKER=["fake-docker"],
        inspect=lambda name: deepcopy(resolve(name)),
        read_json=lambda path: deepcopy(
            product_configuration if path.name == "product-config.json" else writer_configuration
        ),
        require_candidate_image=lambda *args: None,
        validate_runtime_config=lambda *args, **kwargs: None,
        verify_manifest=lambda *args: None,
        verify_service_surface=verify_surface,
        recreate_command=lambda name, *args: ["fake-docker", "create", name],
        atomic_private_config=lambda path, value: configuration_writes.append(path.name),
        safe_run=safe_run,
    )
    monkeypatch.setattr(m, "retained_helper", lambda: helper)
    monkeypatch.setattr(m, "require_quiescent", lambda *args: None)
    # Immutable artifact validation has separate tests; this case owns service transitions only.
    monkeypatch.setattr(
        m.importlib.util,
        "spec_from_file_location",
        lambda *args: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None)),
    )
    monkeypatch.setattr(
        m.importlib.util,
        "module_from_spec",
        lambda spec: SimpleNamespace(require_snapshot=lambda proof: None),
    )
    kwargs = {
        "proof": {"image": "sha256:candidate", "source_sha256": "reviewed-source"},
        "configurations": {"product": product_configuration},
        "evidence_path": tmp_path / "product-only-update.json",
        "expected_images": {
            "product": "sha256:original-product",
            "writer": "sha256:original-writer",
        },
        "execute": True,
        "product_only": True,
    }
    return m, kwargs, state, commands, configuration_writes, original


@pytest.mark.parametrize("writer_drift", ["id", "stopped", "image", "configuration"])
def test_product_only_final_writer_drift_stops_owned_candidate_without_touching_writer(
    monkeypatch,
    tmp_path,
    writer_drift,
):
    import json

    m, kwargs, state, commands, writes, _ = product_only_update_simulation(
        monkeypatch,
        tmp_path,
        writer_drift=writer_drift,
    )
    with pytest.raises(RuntimeError):
        m.update_services(**kwargs)
    report = json.loads(kwargs["evidence_path"].read_text(encoding="utf-8"))
    assert report["status"] == "stopped"
    assert report["services"][0]["phase"] == "unverified_candidate_stopped"
    assert state["product"]["Id"] == "candidate-product-id"
    assert state["product"]["State"]["Running"] is False
    assert [command[-1] for command in commands if command[0] == "stop"] == [
        "product",
        "candidate-product-id",
    ]
    assert all("writer" not in str(command) for command in commands)
    assert writes == ["product-config.json"]
    assert "f5_features_enabled=false" not in report["recovery"]


def test_product_only_success_keeps_original_writer_and_persistent_shape(monkeypatch, tmp_path):
    m, kwargs, state, commands, writes, original = product_only_update_simulation(
        monkeypatch,
        tmp_path,
    )
    writer_before = deepcopy(state["writer"])
    report = m.update_services(**kwargs)
    assert report["status"] == "verified"
    assert report["services"][0]["phase"] == "verified"
    assert report["services"][0]["name"] == "product"
    assert len(report["services"]) == 1
    assert state["writer"] == writer_before
    assert all("writer" not in str(command) for command in commands)
    assert writes == ["product-config.json"]
    assert state["product"]["Mounts"] == original["Mounts"]
    assert state["product"]["Config"] == original["Config"]
    assert state["product"]["State"]["Running"] is True
    assert report["ledger_restored"] is False and report["database_mutations"] == 0


def capacity_configs():
    _, old = config()
    old["model"].update(
        max_input_tokens=65536,
        max_output_tokens=16384,
        reasoning_effort="high",
        max_model_calls=8,
        max_tool_calls=16,
    )
    old["model"]["stage_budget"].update(
        cap_microyuan=6000000,
        approval_reference="approved-f5-extension",
        previous_cap_microyuan=3000000,
        previous_approval_reference="original-f5-budget",
    )
    old["runtime"]["postgres_user"] = "retained-runtime-role"
    new = deepcopy(old)
    new["model"]["max_input_tokens"] = 98304
    return old, new


def test_product_96kb_capacity_requires_exact_hash_and_retains_new_bound_on_redeploy():
    old, new = capacity_configs()
    update = module("runtime_update")
    approved = {"model.max_input_tokens": update.coordinate_digest(98304)}
    with pytest.raises(ValueError):
        update.require_config_delta(old, new, product=True)
    update.require_config_delta(old, new, product=True, approved_coordinates=approved)
    update.require_config_delta(new, deepcopy(new), product=True)
    with pytest.raises(ValueError):
        update.require_config_delta(
            old,
            new,
            product=True,
            approved_coordinates={
                "model.max_input_tokens": "0" * 64,
            },
        )
    # Rollback remains the existing compatible image + preserved original config
    # recovery path; the new forward approval must not silently authorize it.
    with pytest.raises(ValueError):
        update.require_config_delta(new, old, product=True, approved_coordinates=approved)


@pytest.mark.parametrize(
    "previous,current",
    [
        (32768, 98304),
        (65536, 131072),
        (65536, 98303),
        (True, 98304),
        (65536, True),
        (65536.0, 98304),
        (65536, 98304.0),
        (98304, 98304),
    ],
)
def test_96kb_capacity_approval_is_not_an_arbitrary_numeric_change(previous, current):
    old, new = capacity_configs()
    old["model"]["max_input_tokens"] = previous
    new["model"]["max_input_tokens"] = current
    update = module("runtime_update")
    with pytest.raises(ValueError):
        update.require_config_delta(
            old,
            new,
            product=True,
            approved_coordinates={
                "model.max_input_tokens": update.coordinate_digest(current),
            },
        )


def test_writer_cannot_use_the_product_96kb_capacity_approval():
    old, new = capacity_configs()
    old["writer"] = new["writer"] = {"schema_contract": "school-facts-f5-v41"}
    update = module("runtime_update")
    with pytest.raises(ValueError):
        update.require_config_delta(
            old,
            new,
            product=False,
            approved_coordinates={
                "model.max_input_tokens": update.coordinate_digest(98304),
            },
        )


@pytest.mark.parametrize(
    "path,value",
    [
        (("model", "model"), "another-model"),
        (("model", "reasoning_effort"), "low"),
        (("model", "max_output_tokens"), 32768),
        (("model", "max_model_calls"), 9),
        (("model", "max_tool_calls"), 17),
        (("model", "budget", "limit"), 51),
        (("model", "budget", "limit"), 50.0),
        (("model", "stage_budget", "cap_microyuan"), 6000001),
        (("model", "state_directory"), "new-ledger"),
        (("runtime", "postgres_user"), "another-role"),
        (("education", "scope_bindings"), ["broader"]),
        (("f5_features_enabled",), 1),
    ],
)
def test_96kb_capacity_approval_rejects_side_changes_even_normally_allowed_ones(path, value):
    old, new = capacity_configs()
    target = new
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    update = module("runtime_update")
    with pytest.raises(ValueError):
        update.require_config_delta(
            old,
            new,
            product=True,
            approved_coordinates={
                "model.max_input_tokens": update.coordinate_digest(98304),
            },
        )


def test_96kb_capacity_batch_cannot_include_a_separate_valid_stage_extension():
    old, new = capacity_configs()
    old["model"]["stage_budget"] = {
        "stage_id": "school-facts-f5",
        "approval_reference": "original-f5-budget",
        "cap_microyuan": 3000000,
    }
    update = module("runtime_update")
    with pytest.raises(ValueError):
        update.require_config_delta(
            old,
            new,
            product=True,
            approved_coordinates={
                "model.max_input_tokens": update.coordinate_digest(98304),
            },
        )
