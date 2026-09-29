from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.settings import get_settings
from app.db.migration_settings import get_migration_settings
from tests.master_data.conftest import PostgresRoles

API_ROOT = Path(__file__).resolve().parents[1]


def test_released_migrations_use_exact_ruff_exceptions_and_frozen_bytes() -> None:
    """发布后迁移的静态例外必须精确，并由 canonical Git blob 冻结。"""

    revision_09 = "alembic/versions/20260808_09_master_data_imports.py"
    revision_10 = "alembic/versions/20260809_10_master_data_import_takeover_boundary.py"
    revision_11 = "alembic/versions/20260809_11_master_data_import_takeover_audit_route.py"
    revision_12 = "alembic/versions/20260809_12_master_data_import_takeover_lineage.py"
    expected_hashes = {
        revision_09: "694faebee821c5e7ba97c413d739579fc0e130dad140ace3ca039ec613b7794f",
        revision_10: "b950da6b5178d3342564e901a1a25c5febb704fbc3fa253afb5b77f70538354f",
        revision_11: "ede4f02aca79932c54f76262f7c317a65a37c815506dc337df459f4549db4d1f",
        revision_12: "d1a1b31739743f6c5709133c445e488c87431c489ae09c176f5fdee7641a6602",
    }

    project_config = tomllib.loads((API_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff_config = project_config["tool"]["ruff"]
    assert ruff_config.get("lint", {}).get("per-file-ignores") == {revision_10: ["E501"]}
    assert ruff_config.get("format", {}).get("exclude") == [revision_09, revision_10]

    repository_root = API_ROOT.parents[1]
    repository_paths = tuple(f"apps/api/{path}" for path in expected_hashes)
    canonical_blobs: dict[str, bytes] = {}
    for relative_path, repository_path in zip(
        expected_hashes,
        repository_paths,
        strict=True,
    ):
        result = subprocess.run(
            ["git", "show", f"HEAD:{repository_path}"],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        canonical_blobs[relative_path] = result.stdout

    diff_result = subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", *repository_paths],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    assert diff_result.returncode == 0, (diff_result.stdout + diff_result.stderr).decode(
        errors="replace"
    )

    actual_hashes = {
        relative_path: hashlib.sha256(blob).hexdigest()
        for relative_path, blob in canonical_blobs.items()
    }
    assert actual_hashes == expected_hashes


def test_released_migration_hashes_use_canonical_git_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows checkout 换行不得改变 Git canonical migration 冻结结论。"""

    relative_paths = (
        "alembic/versions/20260808_09_master_data_imports.py",
        "alembic/versions/20260809_10_master_data_import_takeover_boundary.py",
        "alembic/versions/20260809_11_master_data_import_takeover_audit_route.py",
        "alembic/versions/20260809_12_master_data_import_takeover_lineage.py",
    )
    repository_paths = tuple(f"apps/api/{path}" for path in relative_paths)
    repository_root = API_ROOT.parents[1]
    canonical_blobs: dict[str, bytes] = {}
    for repository_path in repository_paths:
        result = subprocess.run(
            ["git", "show", f"HEAD:{repository_path}"],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        canonical_blobs[repository_path] = result.stdout
    original_read_bytes = Path.read_bytes

    def read_checkout_bytes(path: Path) -> bytes:
        content = original_read_bytes(path)
        if path.as_posix().endswith(relative_paths):
            return content.replace(b"\n", b"\r\n")
        return content

    git_calls: list[tuple[str, ...]] = []

    def run_git(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        git_calls.append(tuple(command))
        if command[:2] == ["git", "show"]:
            repository_path = command[2].removeprefix("HEAD:")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=canonical_blobs[repository_path],
                stderr=b"",
            )
        if command == ["git", "diff", "--exit-code", "HEAD", "--", *repository_paths]:
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected Git command: {command!r}")

    monkeypatch.setattr(Path, "read_bytes", read_checkout_bytes)
    monkeypatch.setattr(subprocess, "run", run_git)

    test_released_migrations_use_exact_ruff_exceptions_and_frozen_bytes()

    assert git_calls == [
        *(("git", "show", f"HEAD:{path}") for path in repository_paths),
        ("git", "diff", "--exit-code", "HEAD", "--", *repository_paths),
    ]


def _current_alembic_head() -> str:
    """从迁移图取得当前唯一 head，不把历史 revision 写成永久 head。"""

    head = ScriptDirectory.from_config(Config(API_ROOT / "alembic.ini")).get_current_head()
    assert head is not None
    return head


def _read_current_database_revision() -> str:
    """通过受控迁移身份读取迁移版本，不放宽 runtime 的元数据权限。"""

    engine = create_engine(get_migration_settings().database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
        }
    )
    return environment


def _read_revision_with_roles(postgres_roles: PostgresRoles) -> str:
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.integration
def test_migration_baseline_records_current_revision(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260905_22"],
        cwd=API_ROOT,
        env=_migration_environment(school_facts_f1_schema),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    revision = _read_revision_with_roles(school_facts_f1_schema)

    assert revision == _current_alembic_head()


@pytest.mark.integration
def test_alembic_check_has_no_pending_operations(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    """当前 ORM metadata 必须与 fresh head 数据库约束完全一致。"""

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    upgrade_result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260905_22"],
        cwd=API_ROOT,
        env=_migration_environment(school_facts_f1_schema),
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgrade_result.returncode == 0, upgrade_result.stdout + upgrade_result.stderr

    check_result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=API_ROOT,
        env=_migration_environment(school_facts_f1_schema),
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_result.returncode == 0, check_result.stdout + check_result.stderr


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_audit_hardening_downgrade_removes_objects_and_restores_head(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """降级必须移除审计加固对象，并在测试结束前恢复数据库迁移头。"""

    del isolated_revision19_schema

    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("需要显式启用本地 PostgreSQL 集成测试")

    upgrade_result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260823_19"],
        cwd=API_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgrade_result.returncode == 0, upgrade_result.stderr

    engine = create_engine(get_settings().database_url)
    revision = _read_current_database_revision()
    current_head = "20260823_19"
    assert revision == current_head

    try:
        downgrade_result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "20260728_02"],
            cwd=API_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert downgrade_result.returncode == 0, downgrade_result.stderr

        inspector = inspect(engine)
        hardened_indexes = {
            "ix_audit_logs_created_at",
            "ix_audit_logs_actor_created_at",
            "ix_audit_logs_target_created_at",
            "ix_audit_logs_request_id",
        }
        assert hardened_indexes.isdisjoint(
            {index["name"] for index in inspector.get_indexes("audit_logs")}
        )
        audit_actor_foreign_key = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("audit_logs")
            if foreign_key["constrained_columns"] == ["actor_user_id"]
        )
        assert audit_actor_foreign_key.get("options", {}).get("ondelete") == "SET NULL"
        with engine.connect() as connection:
            trigger_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE tgrelid = 'audit_logs'::regclass
                      AND tgname = 'trg_audit_logs_reject_update_delete'
                      AND NOT tgisinternal
                    """
                )
            ).scalar_one()
            function_reference = connection.execute(
                text("SELECT to_regprocedure('prevent_audit_log_mutation()')")
            ).scalar_one()
        assert trigger_count == 0
        assert function_reference is None
    finally:
        restore_result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260823_19"],
            cwd=API_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert restore_result.returncode == 0, restore_result.stderr
    restored_revision = _read_current_database_revision()
    engine.dispose()
    assert restored_revision == current_head
