"""Establish the initial migration baseline.

Revision ID: 20260728_01
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260728_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立空数据库的初始版本标识，不创建或修改任何业务表。"""

    op.execute("SELECT 1")


def downgrade() -> None:
    """撤销该空基线时保持无业务数据副作用。"""

    op.execute("SELECT 1")
