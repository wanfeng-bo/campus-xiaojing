"""Shared finite schema22 root boundary; revision20 owns anchor creation."""

from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.platform.school_facts.initialization_contracts import require_initialization_authority


class RootManifest(Protocol):
    @property
    def db(self) -> str: ...
    @property
    def system_identifier(self) -> str: ...
    @property
    def school_id(self) -> UUID: ...
    @property
    def source_key(self) -> str: ...
    @property
    def school_code(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    @property
    def timezone_name(self) -> str: ...


def controlled_school_root(
    engine: Engine, manifest: RootManifest, *, expected_database: str, inspect_only: bool = False
) -> dict[str, object]:
    with engine.begin() as connection:
        actual_user = connection.scalar(text("SELECT session_user"))
        if actual_user != "campus_ai_migration":
            raise ValueError("Deployment requires actual migration identity")
        connection.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
        require_initialization_authority(
            session_user=actual_user,
            alembic_heads=tuple(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            ),
        )
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('sf5b.school-root-anchor:v1', 0))")
        )
        if (
            manifest.db != expected_database
            or connection.scalar(text("SELECT current_database()")) != expected_database
        ):
            raise ValueError("root database rejected")
        actual_system = str(
            connection.scalar(
                text("SELECT system_identifier::text FROM pg_catalog.pg_control_system()")
            )
        )
        if actual_system != manifest.system_identifier:
            raise ValueError("root cluster rejected")
        roots = (
            connection.execute(
                text(
                    "SELECT id,source_key,school_code,display_name,timezone_name,status,version "
                    "FROM public.schools"
                )
            )
            .mappings()
            .all()
        )
        if not roots and not inspect_only:
            if connection.scalar(text("SELECT count(*) FROM public.school_database_anchor")):
                raise ValueError("root/anchor inconsistency")
            # Infrastructure root only: revision20 trigger performs controlled anchor creation.
            connection.execute(
                text(
                    "INSERT INTO public.schools "
                    "(id,source_key,school_code,display_name,timezone_name,status,version) "
                    "VALUES (:school_id,:source_key,:school_code,:display_name,"
                    ":timezone_name,'active',1)"
                ),
                {
                    "school_id": manifest.school_id,
                    "source_key": manifest.source_key,
                    "school_code": manifest.school_code,
                    "display_name": manifest.display_name,
                    "timezone_name": manifest.timezone_name,
                },
            )
        elif len(roots) != 1 or roots[0]["id"] != manifest.school_id:
            raise ValueError("root already exists or cannot be recovered")
        elif any(
            roots[0][name] != getattr(manifest, name)
            for name in ("source_key", "school_code", "display_name", "timezone_name")
        ):
            raise ValueError("root manifest changed")
        anchor = (
            connection.execute(
                text(
                    "SELECT anchor_id,school_id,cluster_system_identifier,"
                    "database_name,database_oid FROM public.school_database_anchor"
                )
            )
            .mappings()
            .one()
        )
        actual_oid = connection.scalar(
            text("SELECT oid::bigint FROM pg_catalog.pg_database WHERE datname=current_database()")
        )
        if (
            anchor["school_id"] != manifest.school_id
            or anchor["cluster_system_identifier"] != actual_system
            or anchor["database_name"] != expected_database
            or anchor["database_oid"] != actual_oid
        ):
            raise ValueError("root anchor rejected")
        return dict(anchor)


def migration_session_factory(engine: Engine) -> sessionmaker[Session]:
    class DeploymentSession(Session):
        pass

    @event.listens_for(DeploymentSession, "after_begin")
    def bind_deployment_role(
        _session: Session, _transaction: SessionTransaction, connection: Connection
    ) -> None:
        actual_user = connection.scalar(text("SELECT session_user"))
        if actual_user != "campus_ai_migration":
            raise ValueError("Deployment requires actual migration identity")
        connection.execute(text("SET LOCAL ROLE campus_ai_db_owner"))
        require_initialization_authority(
            session_user=actual_user,
            alembic_heads=tuple(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            ),
        )
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))

    return cast(
        sessionmaker[Session],
        sessionmaker(bind=engine, class_=DeploymentSession, expire_on_commit=False),
    )
