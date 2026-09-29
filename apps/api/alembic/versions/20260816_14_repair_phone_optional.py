"""Allow repair submissions without a reporter phone snapshot.

Revision ID: 20260816_14
Revises: 20260815_13
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260816_14"
down_revision = "20260815_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "repair_tickets",
        "reporter_phone_snapshot",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.drop_constraint(
        "ck_repair_tickets_required_text",
        "repair_tickets",
        type_="check",
    )
    op.create_check_constraint(
        "ck_repair_tickets_required_text",
        "repair_tickets",
        "btrim(reporter_name_snapshot) <> '' "
        "AND btrim(location_text) <> '' "
        "AND btrim(problem_description) <> ''",
    )
    op.create_check_constraint(
        "ck_repair_tickets_phone_nonblank",
        "repair_tickets",
        "reporter_phone_snapshot IS NULL OR btrim(reporter_phone_snapshot) <> ''",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $downgrade$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM public.repair_tickets
                    WHERE reporter_phone_snapshot IS NULL
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23502',
                        MESSAGE = 'repair reporter phone contains NULL; downgrade is unsafe';
                END IF;
            END
            $downgrade$;
            """
        )
    )
    op.drop_constraint(
        "ck_repair_tickets_phone_nonblank",
        "repair_tickets",
        type_="check",
    )
    op.drop_constraint(
        "ck_repair_tickets_required_text",
        "repair_tickets",
        type_="check",
    )
    op.create_check_constraint(
        "ck_repair_tickets_required_text",
        "repair_tickets",
        "btrim(reporter_name_snapshot) <> '' "
        "AND btrim(reporter_phone_snapshot) <> '' "
        "AND btrim(location_text) <> '' "
        "AND btrim(problem_description) <> ''",
    )
    op.alter_column(
        "repair_tickets",
        "reporter_phone_snapshot",
        existing_type=sa.String(length=32),
        nullable=False,
    )
