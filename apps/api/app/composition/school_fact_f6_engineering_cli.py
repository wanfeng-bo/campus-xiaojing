"""F6-only deployment manifest over the retained confirmed Governance Writer."""

import hmac
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import Field

from app.composition import school_fact_f5_engineering_cli as previous
from app.composition.school_fact_f3_engineering_cli import GrantBinding, RevokeInput
from app.composition.school_fact_f6_engineering_governance import (
    F6_ALLOWED_DUTIES,
    F6EngineeringApproval,
    F6EngineeringCommandAuthorizer,
    F6EngineeringGovernanceService,
    GrantReferenceProof,
    dependency_maintenance_allowed,
    f6_governance_command_digest,
    f6_governance_command_payload,
    parse_scope,
)


class ScopeMatrixRow(previous.ScopeMatrixRow):
    family: Literal["f2", "f3", "f4", "f5", "f6"]
    quality_owners: tuple[tuple[str, str], ...] = ()
    reserved_read_keys: tuple[str, ...] = ()

    def finite(self):
        if self.reserved_read_keys and (self.family, self.action) != ("f6", "read"):
            raise ValueError("Reserved reads require an explicit F6 read grant")
        if self.quality_owners and self.family != "f6":
            raise ValueError("Quality owner bindings belong only to F6 scope")
        if self.action == "read" and self.creation_keys:
            raise ValueError("Read does not authorize creation")
        if self.prior_scope_sha256 and self.approved_dependency_sources:
            raise ValueError("Choose one exact reviewed dependency binding")
        if self.approved_dependency_sources and (
            len(set(self.approved_dependency_sources)) != len(self.approved_dependency_sources)
            or set(self.approved_dependency_sources) != set(self.object_keys)
        ):
            raise ValueError("Reviewed dependency objects differ")
        if self.family != "f6":
            if self.action == "maintain" and not dependency_maintenance_allowed(
                self.family,
                self.fact_kind,
                self.object_keys,
                self.creation_keys,
                verified_scope_sha256=self.prior_scope_sha256,
                reviewed_object_keys=self.approved_dependency_sources,
            ):
                raise ValueError("F6 cannot maintain unrelated dependency facts")
            if self.object_keys and not (
                self.prior_scope_sha256 or self.approved_dependency_sources
            ):
                raise ValueError("Existing dependency needs an exact reviewed source binding")
        if not self.field_names and (self.family, self.fact_kind) != ("f2", "policy"):
            raise ValueError("Explicit owned fields required")
        if self.duty_code not in F6_ALLOWED_DUTIES:
            raise ValueError("Duty outside F6 package")
        return self

    def scope(self, school_id: UUID):
        document = {
            "school_id": str(school_id),
            "fact_kind": self.fact_kind,
            "object_keys": list(self.object_keys),
            "creation_keys": list(self.creation_keys),
        }
        if self.family != "f2":
            document["field_names"] = list(self.field_names)
        if self.reserved_read_keys:
            document["reserved_read_keys"] = list(self.reserved_read_keys)
        if self.quality_owners:
            document["quality_owners"] = [list(pair) for pair in self.quality_owners]
        scope = parse_scope(f"school_facts.{self.family}", document)
        if self.prior_scope_sha256 is not None and self.prior_scope_sha256 != scope.document_sha256:
            raise ValueError("Reviewed dependency fingerprint differs")
        return scope


class ScopeInput(ScopeMatrixRow, previous.ScopeInput):
    pass


class GrantInput(previous.GrantInput):
    scope_owner: Literal[
        "school_facts.f2",
        "school_facts.f3",
        "school_facts.f4",
        "school_facts.f5",
        "school_facts.f6",
    ]


DutyInput = previous.DutyInput
ExistingDutyReference = previous.ExistingDutyReference
Command = Annotated[
    DutyInput | ScopeInput | GrantInput | RevokeInput, Field(discriminator="command")
]


class EngineeringManifest(previous.EngineeringManifest):
    manifest_version: Literal["f6_governance_v1"]
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)
    allowed_duties: ClassVar = F6_ALLOWED_DUTIES
    actor_kind: ClassVar = "f6_engineering"
    actor_reason: ClassVar = "Approved finite F6 engineering"
    key_prefix: ClassVar = "f6"
    basis_authority: ClassVar = "f6_engineering_policy"
    approval_type: ClassVar = F6EngineeringApproval
    grant_input_type: ClassVar = GrantInput
    scope_schemas: ClassVar = F6EngineeringCommandAuthorizer.scope_schemas


class EngineeringPrivateConfiguration(previous.EngineeringPrivateConfiguration):
    expected_schema_contract: Literal["school-facts-f6-v44", "school-facts-f6-v45"]


def load_manifest(path: Path, expected_digest: str):
    if path.stat().st_size > 1_048_576:
        raise ValueError("Manifest too large")
    raw = path.read_bytes()
    if not hmac.compare_digest(sha256(raw).hexdigest(), expected_digest):
        raise ValueError("Approved F6 manifest changed")
    return EngineeringManifest.model_validate_json(raw)


def command_marker(digest, command):
    return f"CONFIRM-F6:{digest}:{f6_governance_command_digest(command)}"


def review(manifest, digest):
    return {
        "manifest_sha256": digest,
        "manifest": manifest.model_dump(mode="json"),
        "f2_field_scope_note": (
            "F2 scope is object-level; listed fields do not create field authorization"
        ),
        "commands": [
            {
                "index": i,
                "payload": json.loads(f6_governance_command_payload(c)),
                "confirm_marker": command_marker(digest, c),
            }
            for i, c in enumerate(manifest.materialize())
        ],
    }


def build_duties_and_scopes(**kwargs):
    return previous.build_duties_and_scopes(
        **kwargs, _manifest_type=EngineeringManifest, _scope_type=ScopeInput
    )


def build_grants(manifest, bindings, **kwargs):
    return previous.build_grants(manifest, bindings, **kwargs)


def grant_reference_matches(
    proof: GrantReferenceProof,
    *,
    school_id,
    actual_duty_code,
    actual_owner,
    actual_document,
    actual_sha256,
):
    try:
        scope = parse_scope(actual_owner, actual_document)
        family = actual_owner.rsplit(".", 1)[1]
        allowed = {f"{family}_{scope.fact_kind}_reader"} if not scope.creation_keys else set()
        if family == "f6" or dependency_maintenance_allowed(
            family,
            scope.fact_kind,
            scope.object_keys,
            scope.creation_keys,
            verified_scope_sha256=proof.scope_sha256,
        ):
            allowed.add(f"{family}_{scope.fact_kind}_maintainer")
        return (
            actual_duty_code == proof.duty_code
            and actual_duty_code in allowed & F6_ALLOWED_DUTIES
            and actual_owner == proof.scope_owner
            and scope.school_id == school_id
            and actual_sha256 == scope.document_sha256 == proof.scope_sha256
        )
    except (ValueError, TypeError, KeyError):
        return False


def compose(config, manifest, *, approval_is_current, receipt_only=False):
    from app.composition.school_fact_residence_authority import ResidenceScopeOwnerLookup
    from app.platform.school_facts.f6_policy import F6ScopeResolver

    return previous.compose(
        config,
        manifest,
        approval_is_current=approval_is_current,
        receipt_only=receipt_only,
        _authorizer_type=F6EngineeringCommandAuthorizer,
        _service_type=F6EngineeringGovernanceService,
        _reference_matches=grant_reference_matches,
        _scope_resolvers=lambda sessions: {
            "school_facts.f6": F6ScopeResolver(
                lookup_scope=ResidenceScopeOwnerLookup(sessions).scope_exists
            )
        },
    )


__all__ = [
    "EngineeringManifest",
    "EngineeringPrivateConfiguration",
    "ScopeMatrixRow",
    "ScopeInput",
    "GrantInput",
    "DutyInput",
    "ExistingDutyReference",
    "GrantBinding",
    "load_manifest",
    "command_marker",
    "review",
    "build_duties_and_scopes",
    "build_grants",
    "grant_reference_matches",
    "compose",
    "F6EngineeringCommandAuthorizer",
]
