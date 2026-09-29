"""Finite, server-captured coordinates retained with a package confirmation.

No source contents are persisted here. These coordinates are evidence of what
was selected, never an authorization token or a client assertion of existence.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import SchoolFactKind, Sha256Hex

# A package accepts 32 user sources / 10 MiB. These separate internal audit
# artifacts contain only hashes, at most 10,000 generated changes each. The
# existing 50,000 canonical-change bound therefore permits at most five.
REPLACEMENT_AUDIT_PREFIX = "f7_replacement_"
REPLACEMENT_AUDIT_CHUNK = 10_000
MAX_REPLACEMENT_AUDIT_ARTIFACTS = 5
MAX_REPLACEMENT_AUDIT_BYTES = 1024 * 1024
MAX_PACKAGE_PROVENANCE_BYTES = 10 * 1024 * 1024 + (
    MAX_REPLACEMENT_AUDIT_ARTIFACTS * MAX_REPLACEMENT_AUDIT_BYTES
)


class PackageReplacementTarget(FrozenContractModel):
    fact_kind: SchoolFactKind
    source_key: str = Field(min_length=1, max_length=255)
    effective_at: AwareDatetime
    expected_version: int = Field(strict=True, ge=0)
    state: Literal["active", "missing", "ended", "future", "no_current"]
    field_names: tuple[str, ...] = Field(max_length=70)
    content_sha256: Sha256Hex

    @model_validator(mode="after")
    def valid_state(self) -> Self:
        if (self.expected_version == 0) != (self.state == "missing"):
            raise ValueError("Missing selected facts alone have version zero.")
        if len(set(self.field_names)) != len(self.field_names):
            raise ValueError("Selected field names must be unique.")
        return self
