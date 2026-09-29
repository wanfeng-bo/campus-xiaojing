from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """声明平台数据模型共享的 SQLAlchemy 映射基类。"""
