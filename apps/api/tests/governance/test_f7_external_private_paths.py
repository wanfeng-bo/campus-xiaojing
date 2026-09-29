"""Synthetic-only checks for F7 secret material outside every source checkout."""

import base64
import importlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def source_path(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))


def module():
    assert importlib.util.find_spec("scripts.f7_engineering.private_paths") is not None
    return importlib.import_module("scripts.f7_engineering.private_paths")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    m = module()
    repository = tmp_path / "repository"
    private = tmp_path / "external-private" / "school-facts-f1"
    repository.mkdir()
    private.mkdir(parents=True)
    monkeypatch.setattr(m, "PRIVATE_ROOT", private)
    monkeypatch.setattr(m, "REPOSITORY_ROOTS", (repository,))
    return m, repository, private


def test_pregrant_rejects_repository_copy_before_writing(tmp_path, monkeypatch):
    m = importlib.import_module("scripts.f7_engineering.pregrant")
    repository = tmp_path / "repository"
    folder = repository / "runtime/f7-execution/private/candidate"
    folder.mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", repository)
    monkeypatch.setattr(m, "_acl", lambda *args: None)
    with pytest.raises(ValueError):
        m._write_private_copy(folder, "product", {"synthetic_only": "fixture"})
    assert not (folder / "product-config.json").exists()


def test_only_fixed_external_root_accepted(paths):
    m, repository, private = paths
    candidate = private / "f7" / "candidate.json"
    candidate.parent.mkdir()
    candidate.write_text("synthetic")
    assert m.require_external_private_path(candidate) == candidate
    for path in (repository, private.parent, private / ".." / "outside"):
        with pytest.raises(ValueError):
            m.require_external_private_path(path, file=False)


def test_external_root_cannot_be_changed_into_a_checkout(paths, monkeypatch):
    m, repository, private = paths
    (private / ".git").write_text("synthetic metadata, never opened")
    with pytest.raises(ValueError):
        m.require_external_private_path(private, file=False)
    monkeypatch.setattr(m, "PRIVATE_ROOT", repository)
    with pytest.raises(ValueError):
        m.require_external_private_path(repository, file=False)


def test_reparse_or_hardlink_rejected_before_private_content(paths, monkeypatch):
    m, _, private = paths
    candidate = private / "candidate.json"
    candidate.write_text("synthetic")
    original = Path.lstat

    def reparse(path):
        result = original(path)
        if path == private.parent:
            return SimpleNamespace(st_mode=result.st_mode, st_nlink=1,
                                   st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError):
        m.require_external_private_path(candidate)
    monkeypatch.setattr(Path, "lstat", original)
    hardlink = private / "second.json"
    os.link(candidate, hardlink)
    with pytest.raises(ValueError):
        m.require_external_private_path(candidate)


def test_acl_failure_never_exposes_child_output(paths, monkeypatch):
    m, _, private = paths
    observed = []

    def failed(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"synthetic-private-value",
                               stderr=b"synthetic-private-value")

    monkeypatch.setattr(m.subprocess, "run", failed)
    with pytest.raises(ValueError) as error:
        m.require_external_private_acl((private,))
    assert "synthetic-private-value" not in str(error.value)
    assert observed and observed[0][1]["capture_output"] is True


def test_acl_accepts_only_fixed_success_metadata(paths, monkeypatch):
    m, _, private = paths
    monkeypatch.setattr(m.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=b'{"private_acl_verified":true,"paths_checked":1}'))
    m.require_external_private_acl((private,))


def test_acl_uses_available_powershell_and_its_own_module_path(paths, monkeypatch):
    m, _, private = paths
    observed = []
    monkeypatch.setenv("PSModulePath", "synthetic-other-powershell-version/modules")
    monkeypatch.setattr(m.shutil, "which", lambda name: "synthetic-pwsh.exe")

    def verified(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"private_acl_verified":true,"paths_checked":1}',
        )

    monkeypatch.setattr(m.subprocess, "run", verified)
    m.require_external_private_acl((private,))
    command, options = observed[0]
    assert command[0] == "synthetic-pwsh.exe"
    assert "PSModulePath" not in options["env"]
    script = base64.b64decode(command[-1]).decode("utf-16le")
    assert "Get-Acl -LiteralPath $path" in script
    assert "[IO.Directory]::GetAccessControl" not in script
    assert "[IO.File]::GetAccessControl" not in script
    assert "ReadAllText" not in script and "Get-Content" not in script


def test_migration_rejects_legacy_candidate_location_before_read(tmp_path, monkeypatch):
    m = importlib.import_module("scripts.f7_engineering.migration")
    private = tmp_path / "repo/runtime/f7-execution/private"
    private.mkdir(parents=True)
    candidate = private / "writer-config.json"
    candidate.write_text("synthetic")
    monkeypatch.setattr(m, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(m, "_require_private_acl", lambda _: None)
    with pytest.raises(ValueError, match="External"):
        m._private_configurations({"writer_config": str(candidate)}, names=("writer_config",))


def test_storage_checks_all_private_ancestors(paths, monkeypatch):
    m, _, private = paths
    candidate = private / "f7" / "candidate.json"
    candidate.parent.mkdir()
    candidate.write_text("synthetic")
    observed = []
    monkeypatch.setattr(m, "require_external_private_acl", lambda paths: observed.append(paths))
    assert m.require_external_private_storage(candidate) == candidate
    assert observed == [(candidate, candidate.parent, private)]


def test_private_cli_error_is_static_when_implementation_raises(monkeypatch, capsys):
    # This verifies the callable CLI boundary, not just the top-level script guard.
    m = importlib.import_module("scripts.f7_engineering.pregrant")

    def fail(**kwargs):
        raise ValueError("synthetic-private-value")

    monkeypatch.setattr(m, "prepare", fail)
    monkeypatch.setattr(
        "sys.argv", ["pregrant", "--proof", "public-proof", "--actual-off-probe", "public-probe"]
    )
    assert m.main() == 1
    captured = capsys.readouterr()
    assert "synthetic-private-value" not in captured.out + captured.err
    assert '"status": "stopped"' in captured.out
