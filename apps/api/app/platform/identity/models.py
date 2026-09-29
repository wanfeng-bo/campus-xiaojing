from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """保存外部身份主体与平台本地用户之间的稳定映射。"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "provider_code",
            "provider_tenant_key",
            "external_subject",
            name="uq_users_provider_tenant_subject",
        ),
        CheckConstraint("security_version >= 1", name="ck_users_security_version_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False, server_default="legacy")
    provider_tenant_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="legacy"
    )
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        info={"school_facts_authority": "legacy_login_label"},
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    security_version: Mapped[int] = mapped_column(
        BigInteger,
        default=1,
        nullable=False,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
