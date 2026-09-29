"""F2-owned, server-installed school leadership vocabulary; never model input."""

from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.platform.contracts import FrozenContractModel


class FamilyLeadershipPolicy(FrozenContractModel):
    school_id: UUID
    publication_reference: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)
    school_role_codes: tuple[str, ...] = Field(max_length=16)
    campus_role_codes: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def finite_vocabulary(self):
        codes = self.school_role_codes + self.campus_role_codes
        if (
            not codes
            or len(set(codes)) != len(codes)
            or any(not c or len(c) > 80 or c.strip() != c for c in codes)
        ):
            raise ValueError("Explicit non-overlapping formal role codes required")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    def codes(self, role_kind: Literal["all", "school_leader", "campus_leader"]):
        return (
            self.school_role_codes if role_kind != "campus_leader" else (),
            self.campus_role_codes if role_kind != "school_leader" else (),
        )
