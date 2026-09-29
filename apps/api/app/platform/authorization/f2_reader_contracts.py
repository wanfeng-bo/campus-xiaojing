"""Server-only F2 reader coordinates and exact projection DTO."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class F2FactReadSelection:
    source_key: str | None = None
    name: str | None = None
    creation_key: str | None = None
    related_kind: str | None = None
    related_source_key: str | None = None
    # Server-selected subset of the existing fixed content projection. Never adds fields.
    content_fields: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.content_fields is not None and (
            type(self.content_fields) is not tuple or not self.content_fields
            or len(self.content_fields) > 70 or len(set(self.content_fields)) != len(self.content_fields)
            or any(type(field) is not str or not field or len(field) > 100 for field in self.content_fields)
        ):
            raise ValueError("Finite selected content fields required.")
        if (
            (self.related_kind is None) != (self.related_source_key is None)
            or self.related_kind is not None
            and (self.source_key is not None or self.creation_key is not None)
        ):
            raise ValueError("Invalid fixed related target.")
        values = (self.source_key, self.name, self.creation_key)
        if sum(v is not None for v in values) > 1:
            raise ValueError("Select exactly one fixed F2 coordinate.")
        if any(
            v is not None and (type(v) is not str or not v.strip() or len(v) > 255) for v in values
        ):
            raise ValueError("Invalid F2 coordinate.")

    @property
    def binding_key(self) -> str:
        return json.dumps(
            [
                self.source_key,
                self.name,
                self.creation_key,
                self.related_kind,
                self.related_source_key,
            ] + ([list(self.content_fields)] if self.content_fields is not None else []),
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class F2FactRow:
    row_id: UUID
    fact_kind: str
    source_key: str
    content: dict[str, object]
    expected_version: int
    school_fact_version: int
    valid_from: datetime
    valid_to: datetime | None
    source_kind: str
    publication_id: UUID
    published_at: datetime
    fact_version: int = 1


F2_ROW_SPECS = (
    ("row_id", (UUID,)),
    ("fact_kind", (str,)),
    ("source_key", (str,)),
    ("content", (dict,)),
    ("expected_version", (int,)),
    ("school_fact_version", (int,)),
    ("valid_from", (datetime,)),
    ("valid_to", (datetime, type(None))),
    ("source_kind", (str,)),
    ("publication_id", (UUID,)),
    ("published_at", (datetime,)),
    ("fact_version", (int,)),
)
F2_ROW_FIELDS = tuple(field for field, _ in F2_ROW_SPECS)
