"""One reviewed compensation for a missing school version after four first facts."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.composition.school_fact_live_initialization_contracts import (
    LiveDatabase,
    LiveInitializationManifest,
    Reference,
    ReviewedDeployment,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1


class SchoolVersionRecoveryManifest(ReviewedDeployment):
    purpose: Literal["school-facts-missing-school-version-recovery"]
    school_id: UUID
    anchor_id: UUID
    expected_system_identifier: str = Field(pattern=r"^[0-9]{10,32}$")
    database_name: LiveDatabase
    initialization_id: UUID
    recovery_request_id: UUID
    original_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corp_id: Reference
    external_subject: Reference
    school_source_key: Reference
    school_code: Reference
    school_display_name: Reference
    timezone_name: Literal["Asia/Shanghai"]

    @model_validator(mode="after")
    def non_nil(self):
        if any(
            x.int == 0
            for x in (
                self.school_id,
                self.anchor_id,
                self.initialization_id,
                self.recovery_request_id,
            )
        ):
            raise ValueError("Original and recovery identifiers must be non-nil.")
        return self

    def require_original(self, original: LiveInitializationManifest) -> None:
        if (
            self.original_manifest_sha256 != original.manifest_sha256
            or (
                self.school_id,
                self.anchor_id,
                self.expected_system_identifier,
                self.database_name,
                self.corp_id,
                self.external_subject,
            )
            != (
                original.school_id,
                original.anchor_id,
                original.expected_system_identifier,
                original.database_name,
                original.corp_id,
                original.external_subject,
            )
            or original.publication_policy != "deferred"
            or original.first_facts.base_version != 1
            or len(original.first_facts.changes) != 4
            or {c.fact_kind for c in original.first_facts.changes}
            != {"person", "organization", "login_mapping", "assignment"}
            or self.recovery_request_id == original.request_id
        ):
            raise ValueError("Recovery must bind the original four-fact deferred initialization.")

    def change_set(self, original: LiveInitializationManifest) -> CanonicalFactChangeSetV1:
        self.require_original(original)
        return CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                dict(
                    schema_version=1,
                    base_version=2,
                    source_sha256=self.manifest_sha256,
                    changes=[
                        dict(
                            schema_version=1,
                            operation="upsert",
                            fact_kind="school",
                            source_key=self.school_source_key,
                            expected_version=1,
                            effective_at=original.approved_at.isoformat(),
                            origin=dict(sheet_code="school", row_number=1),
                            content=dict(
                                business_code=self.school_code,
                                display_name=self.school_display_name,
                                timezone_name=self.timezone_name,
                                lifecycle_status="active",
                            ),
                        )
                    ],
                )
            )
        )
