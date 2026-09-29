"""The original F7 pair cannot activate from stale or merely asserted grants."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


@pytest.fixture(autouse=True)
def repository_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def module():
    return import_module("scripts.f7_engineering.activation")


@pytest.mark.parametrize(
    "mutation", [None, "migration", "added_migration", "schema", "dependency", "wrong_old_image"]
)
def test_runtime_patch_preserves_exact_migrated_schema(monkeypatch, mutation):
    m = module()
    from scripts.f7_engineering import release

    files = {
        "apps/api/alembic/versions/20260924_55_f7_family_profile_quality.py": "a" * 64,
        "apps/api/app/db/schema_contract.py": "b" * 64,
        "apps/api/pyproject.toml": "c" * 64,
        "apps/api/app/composition/school_fact_writer_client.py": "d" * 64,
    }
    old = {
        "image": "sha256:" + "1" * 64,
        "source_sha256": "2" * 64,
        "schema_contract": release.CONTRACT,
        "image_manifest_verified": True,
        "files": files,
    }
    new = deepcopy(old) | {"image": "sha256:" + "3" * 64, "source_sha256": "4" * 64}
    new["files"]["apps/api/app/composition/school_fact_writer_client.py"] = "e" * 64
    migrated = {"image": old["image"], "source_sha256": old["source_sha256"]}
    if mutation == "migration":
        new["files"][next(iter(files))] = "f" * 64
    if mutation == "added_migration":
        new["files"]["apps/api/alembic/versions/new.py"] = "f" * 64
    if mutation == "schema":
        new["files"]["apps/api/app/db/schema_contract.py"] = "f" * 64
    if mutation == "dependency":
        new["files"]["apps/api/pyproject.toml"] = "f" * 64
    if mutation == "wrong_old_image":
        old["image"] = new["image"]
    checks = []
    monkeypatch.setattr(m, "_json", lambda path: (old, "proof-digest"))
    monkeypatch.setattr(
        release, "require_snapshot", lambda proof: checks.append(proof["source_sha256"])
    )
    if mutation:
        with pytest.raises(ValueError):
            m.require_migrated_runtime_compatibility(migrated, new)
    else:
        m.require_migrated_runtime_compatibility(migrated, new)
        assert checks == [old["source_sha256"], new["source_sha256"]]


def test_new_scope_bindings_preserve_the_entire_old_map():
    m = module()
    old = {
        "directory": {
            "scope_bindings": [
                {
                    "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
                    "operation_code": "school_facts.f2.person.read",
                    "scope_id": "6d0ab3b7-aa8e-4d1d-b317-1a761b932d9b",
                }
            ]
        },
        "family": {"scope_bindings": []},
        "education": {"scope_bindings": []},
        "residence": {"scope_bindings": []},
    }
    new = deepcopy(old)
    added = {
        "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
        "operation_code": "school_facts.f4.class_teacher_assignment.read",
        "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
    }
    new["education"]["scope_bindings"].append(added)
    assert m.new_scope_bindings(old, new) == (added,)

    changed = deepcopy(new)
    changed["directory"]["scope_bindings"][0]["scope_id"] = added["scope_id"]
    # A grant expires without making its old binding safe to keep. The current
    # grant may replace the same operation, but only through formal proof below.
    assert m.new_scope_bindings(old, changed) == (changed["directory"]["scope_bindings"][0], added)
    removed = deepcopy(new)
    removed["directory"]["scope_bindings"] = []
    with pytest.raises(ValueError, match="retained"):
        m.new_scope_bindings(old, removed)
    with pytest.raises(ValueError, match="duplicate"):
        m.new_scope_bindings(old, new | {"education": {"scope_bindings": [added, added]}})


@pytest.mark.parametrize("schema", ["20260924_55", "20260924_56"])
def test_retained_student_read_requires_original_eight_object_binding_and_live_grant(schema):
    m = module()
    binding = {
        "assignment_id": "d69b992d-d0e5-411e-91bd-a9acc586c659",
        "operation_code": "school_facts.f3.student.read",
        "scope_id": "07332c7b-89e9-4b1a-b799-394035a241b7",
    }
    configurations = tuple({"family": {"scope_bindings": [deepcopy(binding)]}} for _ in range(4))
    calls = []
    reply = {"schema": schema, "valid": 1}
    helper = SimpleNamespace(
        DOCKER=["docker"],
        safe_run=lambda args, **kw: (
            calls.append((args, kw["data"])) or (__import__("json").dumps(reply) + "\n").encode()
        ),
    )
    m.require_retained_student_read(
        helper, m.ORIGINAL_SCHOOL, *configurations, expected_schema=schema
    )
    sql = calls[0][1]
    assert b"REPEATABLE READ READ ONLY" in sql
    assert b"2026-09-28T10:47:18.775146+00:00" in sql
    assert b"3b0d8b7d-45fc-4aed-a2f6-f35ab5cdd5e4" in sql
    narrowed = list(deepcopy(configurations))
    narrowed[-1]["family"]["scope_bindings"][0]["scope_id"] = str(UUID(int=7))
    with pytest.raises(ValueError, match="retained student read"):
        m.require_retained_student_read(
            helper, m.ORIGINAL_SCHOOL, *narrowed, expected_schema=schema
        )
    reply["valid"] = 0
    with pytest.raises(ValueError, match="retained student read"):
        m.require_retained_student_read(
            helper, m.ORIGINAL_SCHOOL, *configurations, expected_schema=schema
        )


@pytest.mark.parametrize("nested", [False, True])
def test_new_scope_bindings_reject_additional_student_read_with_retained_binding(nested):
    m = module()
    retained = {
        "assignment_id": "d69b992d-d0e5-411e-91bd-a9acc586c659",
        "operation_code": "school_facts.f3.student.read",
        "scope_id": "07332c7b-89e9-4b1a-b799-394035a241b7",
    }
    before = {
        family: {"scope_bindings": []}
        for family in ("directory", "family", "education", "residence")
    }
    before["family"]["scope_bindings"] = [retained]
    after = deepcopy(before)
    added = {
        "assignment_id": (
            retained["assignment_id"] if nested else "0f732f6c-4860-4b55-bafe-43a12af6aa31"
        ),
        "operation_code": "school_facts.f3.student.read",
        "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
    }
    owner = (
        after["education"].setdefault("dependency_family", {"scope_bindings": []})
        if nested
        else after["family"]
    )
    owner["scope_bindings"].append(added)

    with pytest.raises(ValueError, match="new F3 student read"):
        m.new_scope_bindings(before, after)


def test_new_scope_bindings_preserve_family_read_dependency_bindings():
    m = module()
    before = {
        family: {"scope_bindings": []}
        for family in ("directory", "family", "education", "residence")
    }
    before["family"]["read_dependency_bindings"] = [
        {
            "assignment_id": "d69b992d-d0e5-411e-91bd-a9acc586c659",
            "operation_code": "school_facts.f3.student.read",
            "scope_id": "07332c7b-89e9-4b1a-b799-394035a241b7",
            "object_keys": ["f3:student:a"],
        }
    ]
    after = deepcopy(before)
    after["family"]["read_dependency_bindings"].append(
        {
            "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
            "operation_code": "school_facts.f3.student.read",
            "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
            "object_keys": ["f3:student:b"],
        }
    )

    assert m.new_scope_bindings(before, before) == ()
    with pytest.raises(ValueError, match="read dependency bindings"):
        m.new_scope_bindings(before, after)


def test_new_scope_bindings_preserve_nested_read_dependency_bindings():
    m = module()
    before = {
        family: {"scope_bindings": []}
        for family in ("directory", "family", "education", "residence")
    }
    before["education"]["dependency_family"] = {
        "scope_bindings": [],
        "read_dependency_bindings": [
            {
                "assignment_id": "d69b992d-d0e5-411e-91bd-a9acc586c659",
                "operation_code": "school_facts.f3.person.read",
                "scope_id": "07332c7b-89e9-4b1a-b799-394035a241b7",
                "object_keys": ["f3:person:a"],
            }
        ],
    }
    after = deepcopy(before)
    after["education"]["dependency_family"]["read_dependency_bindings"].append(
        {
            "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
            "operation_code": "school_facts.f3.student.read",
            "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
            "object_keys": ["f3:student:b"],
        }
    )

    assert m.new_scope_bindings(before, before) == ()
    with pytest.raises(ValueError, match="read dependency bindings"):
        m.new_scope_bindings(before, after)


def test_new_scope_bindings_reject_non_list_nested_read_dependencies():
    m = module()
    before = {
        family: {"scope_bindings": []}
        for family in ("directory", "family", "education", "residence")
    }
    before["education"]["dependency_family"] = {"read_dependency_bindings": "invalid"}

    with pytest.raises(ValueError, match="read dependency binding list"):
        m.new_scope_bindings(before, deepcopy(before))


def test_new_scope_bindings_include_nested_domain_dependencies():
    m = module()
    old_scope = "6d0ab3b7-aa8e-4d1d-b317-1a761b932d9b"
    new_scope = "8c3ded01-9165-49d7-923e-ce766d8388a9"
    actor = "0f732f6c-4860-4b55-bafe-43a12af6aa31"
    binding = {
        "assignment_id": actor,
        "operation_code": "school_facts.f2.fact_quality.read",
        "scope_id": old_scope,
    }
    before = {
        "directory": {"scope_bindings": [binding]},
        "family": {"scope_bindings": []},
        "education": {
            "scope_bindings": [],
            "dependency_directory": {"scope_bindings": [binding]},
        },
        "residence": {
            "scope_bindings": [],
            "dependency_education": {"dependency_directory": {"scope_bindings": [binding]}},
        },
    }
    after = deepcopy(before)
    revised = {**binding, "scope_id": new_scope}
    after["education"]["dependency_directory"]["scope_bindings"] = [revised]
    # A nested owner can keep an old scoped grant while another owner uses a
    # new one. Every changed nested coordinate still needs its formal receipt.
    assert m.new_scope_bindings(before, after) == (revised,)
    del after["residence"]["dependency_education"]["dependency_directory"]
    with pytest.raises(ValueError, match="retained"):
        m.new_scope_bindings(before, after)


def test_new_scope_bindings_preserve_existing_guardian_lifecycle_operations():
    m = module()
    actor = "0f732f6c-4860-4b55-bafe-43a12af6aa31"
    old = {
        "directory": {"scope_bindings": []},
        "family": {
            "scope_bindings": [
                {
                    "assignment_id": actor,
                    "operation_code": f"school_facts.f3.guardian_relationship.{action}",
                    "scope_id": str(UUID(int=index)),
                }
                for index, action in enumerate(("request", "end", "revoke"), start=1)
            ]
        },
        "education": {"scope_bindings": []},
        "residence": {"scope_bindings": []},
    }
    new = deepcopy(old)
    added = {
        "assignment_id": actor,
        "operation_code": "school_facts.f3.student.maintain",
        "scope_id": str(UUID(int=4)),
    }
    new["family"]["scope_bindings"].append(added)
    assert m.new_scope_bindings(old, new) == (added,)

    changed = deepcopy(new)
    changed["family"]["scope_bindings"][0]["scope_id"] = str(UUID(int=5))
    with pytest.raises(ValueError, match="retained guardian"):
        m.new_scope_bindings(old, changed)

    invented = deepcopy(old)
    invented["family"]["scope_bindings"].append(
        {**old["family"]["scope_bindings"][0], "assignment_id": str(UUID(int=6))}
    )
    with pytest.raises(ValueError, match="retained guardian"):
        m.new_scope_bindings(old, invented)

    wrong_owner = deepcopy(old)
    wrong_owner["education"]["scope_bindings"].append(old["family"]["scope_bindings"][0])
    with pytest.raises(ValueError, match="operation code"):
        m.new_scope_bindings(old, wrong_owner)


@pytest.mark.parametrize(
    ("manifest_format", "approval_format"),
    [("offset", "offset"), ("z", "offset"), ("offset", "z"), ("z", "east")],
)
def test_closed_formal_phase_requires_every_receipt_and_fixed_deadline(
    tmp_path, manifest_format, approval_format
):
    m = module()
    school = UUID("2ad71788-afcc-4378-a432-2493fc26aaf0")
    actor = UUID("0f732f6c-4860-4b55-bafe-43a12af6aa31")
    scope = UUID("8c3ded01-9165-49d7-923e-ce766d8388a9")
    grant = UUID("c5cce4b4-47d3-4b9f-9341-b3c2e53c3371")
    epoch = datetime(2026, 9, 24, 8, tzinfo=UTC)
    start = epoch + timedelta(seconds=1)
    end = start + timedelta(minutes=10)

    def encoded_time(value, representation):
        if representation == "z":
            return value.isoformat().replace("+00:00", "Z")
        if representation == "east":
            return value.astimezone(timezone(timedelta(hours=8))).isoformat()
        return value.isoformat()

    manifest = {
        "manifest_version": "f7_governance_v1",
        "school_id": str(school),
        "grant_epoch": epoch.isoformat(),
        "grant_valid_to": (epoch + timedelta(days=7)).isoformat(),
        "approval_reference": "user-approved-f7-fixed-test",
        "valid_from": encoded_time(start, manifest_format),
        "valid_to": encoded_time(end, manifest_format),
        "commands": [
            {
                "command": "grant",
                "key": "teacher_read",
                "assignment_id": str(actor),
                "duty_code": "f4_class_teacher_assignment_reader",
                "scope_id": str(scope),
                "scope_version": 1,
                "scope_owner": "school_facts.f4",
                "scope_sha256": "a" * 64,
                "duty_version": 1,
                "valid_from": start.isoformat(),
                "effective_from": start.isoformat(),
                "planned_valid_to": (epoch + timedelta(days=7)).isoformat(),
            }
        ],
    }
    import hashlib
    import json

    raw = json.dumps(manifest).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (tmp_path / "manifest.json").write_bytes(raw)
    approval = {
        "manifest_sha256": digest,
        "issuer_reference": m.ISSUER_REFERENCE,
        "actor_reference": m.ACTOR_REFERENCE,
        "valid_from": encoded_time(start, approval_format),
        "valid_to": encoded_time(end, approval_format),
        "fixed_deadline": (epoch + timedelta(days=7)).isoformat(),
        "authorization_reference": "approved-f7-fixed-review",
        "state": "closed",
        "closed_at": end.isoformat(),
    }
    receipts = {
        "manifest_sha256": digest,
        "status": "succeeded",
        "commands": {
            "f7:teacher_read": {
                "state": "succeeded",
                "digest": "b" * 64,
                "receipt": {"target_id": str(grant), "target_version": 1},
            }
        },
    }
    (tmp_path / "execution-approval.json").write_text(json.dumps(approval))
    (tmp_path / "receipts.json").write_text(json.dumps(receipts))
    entries = m.closed_phase_grants(tmp_path, school_id=school, now=end + timedelta(seconds=1))
    assert len(entries) == 1 and entries[0]["grant_id"] == str(grant)

    # Equivalent encodings do not authorize a different instant or a naive time.
    for field, instant in (("valid_from", start), ("valid_to", end)):
        original = approval[field]
        for altered in (
            (instant + timedelta(seconds=1)).isoformat(),
            instant.replace(tzinfo=None).isoformat(),
        ):
            approval[field] = altered
            (tmp_path / "execution-approval.json").write_text(json.dumps(approval))
            with pytest.raises(ValueError, match="closed|aware formal times"):
                m.closed_phase_grants(tmp_path, school_id=school, now=end + timedelta(seconds=1))
        approval[field] = original
    (tmp_path / "execution-approval.json").write_text(json.dumps(approval))

    receipts["commands"]["f7:teacher_read"]["state"] = "failed"
    (tmp_path / "receipts.json").write_text(json.dumps(receipts))
    with pytest.raises(ValueError, match="receipt"):
        m.closed_phase_grants(tmp_path, school_id=school, now=end + timedelta(seconds=1))
    receipts["commands"]["f7:teacher_read"]["state"] = "succeeded"
    (tmp_path / "receipts.json").write_text(json.dumps(receipts))
    approval["state"] = "open"
    (tmp_path / "execution-approval.json").write_text(json.dumps(approval))
    with pytest.raises(ValueError, match="closed"):
        m.closed_phase_grants(tmp_path, school_id=school, now=end + timedelta(seconds=1))


def test_live_authority_probe_is_read_only_and_rejects_stale_results():
    m = module()
    entries = [
        {
            "assignment_id": "0f732f6c-4860-4b55-bafe-43a12af6aa31",
            "scope_id": "8c3ded01-9165-49d7-923e-ce766d8388a9",
            "grant_id": "c5cce4b4-47d3-4b9f-9341-b3c2e53c3371",
            "grant_version": 1,
            "scope_version": 1,
            "duty_version": 1,
            "scope_sha256": "a" * 64,
            "duty_code": "f4_class_teacher_assignment_reader",
            "scope_owner": "school_facts.f4",
            "basis_reference": "user-approved-f7-fixed-test",
            "valid_from": "2026-09-24T08:00:01+00:00",
            "effective_from": "2026-09-24T08:00:01+00:00",
            "planned_valid_to": "2026-10-01T08:00:00+00:00",
        }
    ]
    calls = []
    reply = {
        "schema": "20260924_55",
        "school_matches": 1,
        "governance_nologin": True,
        "governance_sessions": 0,
        "requested": 1,
        "valid": 1,
    }
    helper = SimpleNamespace(
        DOCKER=["docker"],
        safe_run=lambda args, **kw: (
            calls.append((args, kw["data"])) or (json.dumps(reply) + "\n").encode()
        ),
    )
    import json

    m.probe_current_grants(helper, UUID("2ad71788-afcc-4378-a432-2493fc26aaf0"), entries)
    assert b"BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in calls[0][1]
    assert "campus-school-facts-f1-postgres-1" in calls[0][0]
    reply["valid"] = 0
    with pytest.raises(ValueError, match="current"):
        m.probe_current_grants(helper, UUID("2ad71788-afcc-4378-a432-2493fc26aaf0"), entries)
    reply["valid"] = 1
    reply["governance_nologin"] = False
    with pytest.raises(ValueError, match="closed"):
        m.probe_current_grants(helper, UUID("2ad71788-afcc-4378-a432-2493fc26aaf0"), entries)


@pytest.mark.parametrize("schema", ["20260924_55", "20260924_56"])
def test_on_transition_requires_formal_grant_for_each_new_binding(monkeypatch, schema):
    m = module()
    school = str(m.ORIGINAL_SCHOOL)
    assignment = "0f732f6c-4860-4b55-bafe-43a12af6aa31"
    scope = "8c3ded01-9165-49d7-923e-ce766d8388a9"
    old = {
        family: {"scope_bindings": []}
        for family in ("directory", "family", "education", "residence")
    }
    old["family"]["scope_bindings"].append(
        {
            "assignment_id": "d69b992d-d0e5-411e-91bd-a9acc586c659",
            "operation_code": "school_facts.f3.student.read",
            "scope_id": "07332c7b-89e9-4b1a-b799-394035a241b7",
        }
    )
    new = deepcopy(old)
    new["education"]["scope_bindings"] = [
        {
            "assignment_id": assignment,
            "operation_code": "school_facts.f4.class_teacher_assignment.read",
            "scope_id": scope,
        }
    ]
    old_product = deepcopy(old) | {"reader": {"expected_school_id": school}}
    old_writer = deepcopy(old) | {"reader": {"expected_school_id": school}}
    product = deepcopy(new) | {
        "purpose": "school-facts-f7-synthetic",
        "reader": {"expected_school_id": school},
        "f7_packages_enabled": True,
    }
    writer = deepcopy(new) | {
        "writer": {"schema_contract": "school-facts-f7-v55"},
        "reader": {"expected_school_id": school},
        "f7_packages_enabled": True,
    }
    configs = {"Product": product, "Writer": writer}
    proof = {
        "image": "sha256:" + "a" * 64,
        "source_sha256": "b" * 64,
        "schema_contract": "school-facts-f7-v" + schema.rsplit("_", 1)[-1],
    }
    migrated = {
        "status": "migrated_checked",
        "from": "20260922_45",
        "to": "20260924_55",
        "exit_code": 0,
        "fallback_mode": "f7_pregrant_recovery",
        "image": proof["image"],
        "source_sha256": proof["source_sha256"],
        "fallback_evidence": "pregrant",
        "fallback_sha256": "pregrant-digest",
    }
    pregrant = {
        "mode": "f7_pregrant_recovery",
        "scope_status": "retained_f6_coordinates",
        "image": proof["image"],
        "source_sha256": proof["source_sha256"],
    }
    evidence = {
        "migration_evidence": "migration",
        "migration_sha256": "migration-digest",
        "phase_folders": ["formal-grants"],
        "school_id": school,
    }
    repaired = {"synthetic": "separate repair evidence"}
    repaired_calls = []
    if schema.endswith("56"):
        evidence |= {"schema_repair_evidence": "repair", "schema_repair_sha256": "repair-digest"}
    monkeypatch.setattr(
        m,
        "require_schema_repair_chain",
        lambda original, updated, candidate, **kw: repaired_calls.append(
            (original, updated, candidate)
        ),
    )
    monkeypatch.setattr(m, "_confined", lambda path, root: Path(path))
    from scripts.f7_engineering import private_paths

    monkeypatch.setattr(private_paths, "require_external_private_storage", lambda path: path)
    monkeypatch.setattr(
        m,
        "_json",
        lambda path, **kw: (
            (migrated, "migration-digest")
            if str(path) == "migration"
            else (repaired, "repair-digest")
            if str(path) == "repair"
            else (pregrant, "pregrant-digest")
        ),
    )
    monkeypatch.setattr(
        m.migration,
        "_private_configurations",
        lambda *args, **kw: (
            {},
            {
                "product_config": old_product,
                "writer_config": old_writer,
            },
        ),
    )
    calls = []
    monkeypatch.setattr(
        m,
        "probe_current_grants",
        lambda helper, school_id, grants, **kw: calls.append((school_id, grants, kw)),
    )
    monkeypatch.setattr(m, "verify_original_database_container", lambda *args: None)
    retained_calls = []
    monkeypatch.setattr(
        m,
        "require_retained_student_read",
        lambda *args, **kw: retained_calls.append((*args, kw)),
    )
    grant = {
        "assignment_id": assignment,
        "scope_id": scope,
        "duty_code": "f4_class_teacher_assignment_reader",
    }
    terminal = {
        "assignment_id": assignment,
        "scope_id": "e6c8ab4d-5399-4b30-8ba3-7f7b55cb4c1d",
        "duty_code": "f7_own_terminal_reader",
    }
    creation_grant = {
        "assignment_id": assignment,
        "scope_id": "a8df477a-e089-4e73-b28d-5452d24d1a6e",
        "duty_code": "f2_fact_quality_maintainer",
    }
    monkeypatch.setattr(
        m, "closed_phase_grants", lambda *args, **kw: (creation_grant, grant, terminal)
    )
    m.require_current_activation(
        proof=proof, configurations=configs, evidence=evidence, helper=object()
    )
    # The completed creation phase is still audited, but its grant may be
    # revoked once published. Only final bindings and own-terminal must remain
    # currently valid at activation.
    assert len(calls) == 1 and len(calls[0][1]) == 2
    assert len(retained_calls) == 1
    assert retained_calls[0][1] == m.ORIGINAL_SCHOOL
    assert calls[0][2] == retained_calls[0][-1] == {"expected_schema": schema}
    assert repaired_calls == ([(migrated, repaired, proof)] if schema.endswith("56") else [])
    assert retained_calls[0][2] == old_product
    assert creation_grant not in calls[0][1]

    interim = deepcopy(configs)
    for value in interim.values():
        value["f7_packages_enabled"] = False
    m.require_current_interim_scopes(
        proof=proof, configurations=interim, evidence=evidence, helper=object()
    )
    assert len(calls) == 2 and len(calls[1][1]) == 2
    with pytest.raises(ValueError, match="ON"):
        m.require_current_activation(
            proof=proof, configurations=interim, evidence=evidence, helper=object()
        )
    with pytest.raises(ValueError, match="OFF"):
        m.require_current_interim_scopes(
            proof=proof, configurations=configs, evidence=evidence, helper=object()
        )

    monkeypatch.setattr(m, "closed_phase_grants", lambda *args, **kw: (terminal,))
    with pytest.raises(ValueError, match="exact formal grant"):
        m.require_current_activation(
            proof=proof, configurations=configs, evidence=evidence, helper=object()
        )
    with pytest.raises(ValueError, match="evidence"):
        m.require_current_activation(
            proof=proof, configurations=configs, evidence={"ok": True}, helper=object()
        )
    if schema.endswith("56"):
        for omitted in ("schema_repair_evidence", "schema_repair_sha256"):
            with pytest.raises(ValueError, match="evidence"):
                m.require_current_activation(
                    proof=proof,
                    configurations=configs,
                    evidence={k: v for k, v in evidence.items() if k != omitted},
                    helper=object(),
                )
        with pytest.raises(ValueError, match="digest changed"):
            m.require_current_activation(
                proof=proof,
                configurations=configs,
                evidence=evidence | {"schema_repair_sha256": "wrong"},
                helper=object(),
            )
        with pytest.raises(ValueError, match="separate successful repair"):
            m.require_current_activation(
                proof=proof,
                configurations=configs,
                evidence={k: v for k, v in evidence.items() if not k.startswith("schema_repair")},
                helper=object(),
            )


@pytest.mark.parametrize("expected_from", ["20260922_45", "20260924_55"])
def test_activation_rejects_database_replacement_after_migration(monkeypatch, expected_from):
    m = module()
    backup = {
        "status": "backup_verified",
        "sha256": "b" * 64,
        "service_state": "stopped_product_writer",
        "snapshot": {"schema": expected_from},
        "services": [
            {
                "name": "campus-school-facts-f1-postgres-1",
                "id": "old-pg",
                "image": "sha256:" + "a" * 64,
            }
        ],
    }
    monkeypatch.setattr(m, "_confined", lambda path, root: Path(path))
    monkeypatch.setattr(m, "_json", lambda *args, **kw: (backup, "backup-digest"))
    helper = SimpleNamespace(
        inspect=lambda name: {
            "Id": "other-pg",
            "Image": "sha256:" + "a" * 64,
            "State": {"Running": True},
        }
    )
    with pytest.raises(ValueError, match="database container changed"):
        m.verify_original_database_container(
            helper,
            {"backup_evidence": "backup-evidence", "backup_sha256": "b" * 64},
            expected_from=expected_from,
        )

    helper.inspect = lambda name: {
        "Id": "old-pg",
        "Image": "sha256:" + "a" * 64,
        "State": {"Running": True},
    }
    with pytest.raises(ValueError, match="backup digest changed"):
        m.verify_original_database_container(
            helper,
            {"backup_evidence": "backup-evidence", "backup_sha256": "c" * 64},
            expected_from=expected_from,
        )
    m.verify_original_database_container(
        helper,
        {"backup_evidence": "backup-evidence", "backup_sha256": "b" * 64},
        expected_from=expected_from,
    )
    backup["snapshot"]["schema"] = "20260924_56"
    with pytest.raises(ValueError, match="backup required"):
        m.verify_original_database_container(
            helper,
            {"backup_evidence": "backup-evidence", "backup_sha256": "b" * 64},
            expected_from=expected_from,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "old_migration",
        "dependency",
        "extra",
        "missing",
        "wrong_from",
        "failed",
        "exit",
        "old_exit",
        "relabel",
        "wrong_contract",
    ],
)
def test_only_explicit_55_to_56_schema_repair_preserves_original_history(monkeypatch, mutation):
    m = module()
    from scripts.f7_engineering import release

    migration55 = "apps/api/alembic/versions/20260924_55_f7_family_profile_quality.py"
    migration56 = "apps/api/alembic/versions/20260924_56_f7_family_teaching_quality.py"
    files = {
        migration55: "a" * 64,
        "apps/api/app/db/schema_contract.py": "b" * 64,
        "apps/api/pyproject.toml": "c" * 64,
    }
    old = {
        "image": "sha256:" + "1" * 64,
        "source_sha256": "2" * 64,
        "schema_contract": "school-facts-f7-v55",
        "image_manifest_verified": True,
        "files": files,
    }
    new = deepcopy(old) | {
        "image": "sha256:" + "3" * 64,
        "source_sha256": "4" * 64,
        "schema_contract": "school-facts-f7-v56",
    }
    new["files"][migration56] = "e" * 64
    new["files"]["apps/api/app/db/schema_contract.py"] = "f" * 64
    original = {
        "status": "migrated_checked",
        "from": "20260922_45",
        "to": "20260924_55",
        "exit_code": 0,
        "fallback_mode": "f7_pregrant_recovery",
        "image": old["image"],
        "source_sha256": old["source_sha256"],
    }
    repaired = {
        "status": "migrated_checked",
        "from": "20260924_55",
        "to": "20260924_56",
        "exit_code": 0,
        "fallback_mode": "f7_packages_disabled",
        "image": new["image"],
        "source_sha256": new["source_sha256"],
        "migration_hashes": {migration56: "e" * 64},
    }
    if mutation == "old_migration":
        new["files"][migration55] = "9" * 64
    if mutation == "dependency":
        new["files"]["apps/api/pyproject.toml"] = "9" * 64
    if mutation == "extra":
        new["files"]["apps/api/alembic/versions/20260924_57_unapproved.py"] = "9" * 64
    if mutation == "missing":
        del new["files"][migration55]
    if mutation == "wrong_from":
        repaired["from"] = "20260922_45"
    if mutation == "failed":
        repaired["status"] = "failed_requires_revision_check"
    if mutation == "exit":
        repaired["exit_code"] = 1
    if mutation == "old_exit":
        original["exit_code"] = None
    if mutation == "relabel":
        original["image"] = new["image"]
    if mutation == "wrong_contract":
        new["schema_contract"] = old["schema_contract"]
    monkeypatch.setattr(
        m,
        "_json",
        lambda path: (old if old["source_sha256"][:16] in str(path) else new, "proof-digest"),
    )
    checked = []
    monkeypatch.setattr(release, "require_snapshot", lambda proof: checked.append(proof))
    databases = []
    monkeypatch.setattr(
        m,
        "verify_original_database_container",
        lambda helper, value, **kw: databases.append((value, kw)),
    )
    old_copy = deepcopy(original)
    if mutation:
        with pytest.raises(ValueError):
            m.require_schema_repair_chain(original, repaired, new, helper=object())
    else:
        m.require_schema_repair_chain(original, repaired, new, helper=object())
        assert checked == [old, new]
        assert databases == [(repaired, {"expected_from": "20260924_55"})]
    assert original == old_copy


@pytest.mark.parametrize("schema", ["20260924_55", "20260924_56"])
def test_current_grant_probe_requires_explicit_supported_actual_schema(schema):
    import json

    m = module()
    calls = []
    reply = {
        "schema": schema,
        "school_matches": 1,
        "governance_nologin": True,
        "governance_sessions": 0,
        "requested": 1,
        "valid": 1,
    }
    helper = SimpleNamespace(
        DOCKER=["docker"],
        safe_run=lambda *args, **kwargs: calls.append(True) or json.dumps(reply).encode(),
    )
    m.probe_current_grants(helper, UUID(int=1), [{}], expected_schema=schema)
    reply["schema"] = "20260924_56" if schema.endswith("55") else "20260924_55"
    with pytest.raises(ValueError):
        m.probe_current_grants(helper, UUID(int=1), [{}], expected_schema=schema)
    with pytest.raises(ValueError):
        m.probe_current_grants(helper, UUID(int=1), [{}], expected_schema="20260924_57")
    assert len(calls) == 2
