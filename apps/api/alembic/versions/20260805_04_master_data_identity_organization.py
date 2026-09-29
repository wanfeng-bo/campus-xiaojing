"""Add schools, people, account bindings, organizations, and assignments.

Revision ID: 20260805_04
Revises: 20260729_03
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260805_04"
down_revision: str | None = "20260729_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIRMATION_STATUSES = "status IN ('draft', 'confirmed', 'rejected')"
_VALID_INTERVAL = "valid_to IS NULL OR valid_to > valid_from"


def _uuid_column(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _version_column() -> sa.Column[object]:
    return sa.Column(
        "version",
        sa.BigInteger(),
        server_default=sa.text("1"),
        nullable=False,
    )


def _created_at_column() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _updated_at_column() -> sa.Column[object]:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


def _effective_range() -> sa.TextClause:
    return sa.text("tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)')")


def upgrade() -> None:
    """创建 Task 3 主数据表和数据库级跨校、有效期约束。"""

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")
    op.add_column(
        "users",
        sa.Column(
            "security_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_users_security_version_positive",
        "users",
        "security_version >= 1",
    )

    op.create_table(
        "schools",
        _uuid_column("id"),
        sa.Column("school_code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_schools"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_schools_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_schools_version_positive"),
    )

    op.create_table(
        "school_people",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_school_people"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_school_people_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_school_people_school",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'merged')",
            name="ck_school_people_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_people_version_positive",
        ),
    )

    op.create_table(
        "school_person_names",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("person_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_school_person_names"),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_school_person_names_school_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_school_person_names_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_school_person_names_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_school_person_names_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_school_person_names_confirmed_period",
        ),
    )
    op.create_index(
        "ix_school_person_names_as_of",
        "school_person_names",
        ["school_id", "person_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "account_person_bindings",
        _uuid_column("id"),
        _uuid_column("school_id"),
        sa.Column("account_user_id", sa.Integer(), nullable=False),
        _uuid_column("person_id"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_account_person_bindings"),
        sa.ForeignKeyConstraint(
            ["account_user_id"],
            ["users.id"],
            name="fk_account_person_bindings_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_account_person_bindings_school_person",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_account_person_bindings_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_account_person_bindings_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_account_person_bindings_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("account_user_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_account_person_bindings_account_period",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_account_person_bindings_person_period",
        ),
    )
    op.create_index(
        "ix_account_person_bindings_account_as_of",
        "account_person_bindings",
        ["school_id", "account_user_id", "status", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_account_person_bindings_person_as_of",
        "account_person_bindings",
        ["school_id", "person_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "organization_units",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("parent_organization_unit_id", nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_organization_units"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_organization_units_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name="fk_organization_units_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "parent_organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_units_school_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_organization_units_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_organization_units_version_positive",
        ),
    )

    op.create_table(
        "organization_unit_versions",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("organization_unit_id"),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("short_name", sa.String(length=120), nullable=True),
        sa.Column("display_path", sa.String(length=1000), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_organization_unit_versions"),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_organization_unit_versions_school_unit",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_organization_unit_versions_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_organization_unit_versions_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_organization_unit_versions_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("organization_unit_id", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_organization_unit_versions_confirmed_period",
        ),
    )
    op.create_index(
        "ix_organization_unit_versions_as_of",
        "organization_unit_versions",
        ["school_id", "organization_unit_id", "status", "valid_from", "valid_to"],
    )

    op.create_table(
        "staff_assignments",
        _uuid_column("id"),
        _uuid_column("school_id"),
        _uuid_column("person_id"),
        _uuid_column("organization_unit_id"),
        sa.Column("position_title", sa.String(length=200), nullable=False),
        sa.Column("semantic_role_code", sa.String(length=80), nullable=False),
        sa.Column("appointment_source", sa.String(length=40), nullable=False),
        sa.Column("approval_reference", sa.String(length=255), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        _version_column(),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id", name="pk_staff_assignments"),
        sa.UniqueConstraint(
            "school_id",
            "id",
            name="uq_staff_assignments_school_id_id",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_staff_assignments_school_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "organization_unit_id"],
            ["organization_units.school_id", "organization_units.id"],
            name="fk_staff_assignments_school_unit",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _CONFIRMATION_STATUSES,
            name="ck_staff_assignments_status",
        ),
        sa.CheckConstraint(
            _VALID_INTERVAL,
            name="ck_staff_assignments_valid_interval",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_staff_assignments_version_positive",
        ),
        postgresql.ExcludeConstraint(
            ("school_id", "="),
            ("person_id", "="),
            ("organization_unit_id", "="),
            ("semantic_role_code", "="),
            (_effective_range(), "&&"),
            where=sa.text("status = 'confirmed'"),
            using="gist",
            name="excl_staff_assignments_confirmed_slot_period",
        ),
    )
    op.create_index(
        "ix_staff_assignments_current_person",
        "staff_assignments",
        ["school_id", "person_id", "status", "valid_from", "valid_to"],
    )
    op.create_index(
        "ix_staff_assignments_current_unit_role",
        "staff_assignments",
        [
            "school_id",
            "organization_unit_id",
            "semantic_role_code",
            "status",
            "valid_from",
            "valid_to",
        ],
    )


def downgrade() -> None:
    """逆序移除 Task 3 表和账号安全版本列。"""

    op.drop_table("staff_assignments")
    op.drop_table("organization_unit_versions")
    op.drop_table("organization_units")
    op.drop_table("account_person_bindings")
    op.drop_table("school_person_names")
    op.drop_table("school_people")
    op.drop_table("schools")
    op.drop_constraint("ck_users_security_version_positive", "users", type_="check")
    op.drop_column("users", "security_version")
