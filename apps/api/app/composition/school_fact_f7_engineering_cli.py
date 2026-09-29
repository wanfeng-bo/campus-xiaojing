"""Reviewed F2–F6 scopes and existing-school own-terminal grants for F7 only."""

import hmac
import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from app.composition import school_fact_f5_engineering_cli as base
from app.composition import school_fact_f6_engineering_cli as previous
from app.composition.school_fact_f3_engineering_cli import GrantBinding as GrantBinding
from app.composition.school_fact_f3_engineering_cli import RevokeInput
from app.composition.school_fact_f5_engineering_governance import GrantReferenceProof
from app.composition.school_fact_f6_engineering_governance import (
    f6_governance_command_digest as command_digest,
)
from app.composition.school_fact_f6_engineering_governance import (
    f6_governance_command_payload as command_payload,
)
from app.composition.school_fact_f6_engineering_governance import (
    parse_scope,
)
from app.composition.school_fact_f7_engineering_governance import (
    F7_ALLOWED_DUTIES,
    F7EngineeringApproval,
    F7EngineeringCommandAuthorizer,
    F7EngineeringGovernanceService,
    finite_maintenance_allowed,
)
from app.platform.school_facts.f7_own_terminal_authorization import F7_OWN_TERMINAL_DUTY
from app.platform.school_facts.read_policy import SCHOOL_SCOPE_OWNER, school_scope_document_hash


class ScopeMatrixRow(previous.ScopeMatrixRow):
    action: Literal["read", "maintain", "verify"]

    @property
    def duty_code(self):
        if self.action == "verify":
            return "f3_guardian_relationship_verifier"
        return super().duty_code

    def finite(self):
        if self.reserved_read_keys or (self.quality_owners and self.family != "f6"):
            raise ValueError("Quality owners belong to F6; reserved read scopes are not issued")
        if self.action == "verify" and (
            (self.family, self.fact_kind) != ("f3", "guardian_relationship")
            or self.creation_keys
            or not self.object_keys
        ):
            raise ValueError("Independent verification covers existing guardian relationships only")
        if self.prior_scope_sha256 and self.approved_dependency_sources:
            raise ValueError("Choose one exact reviewed source binding")
        if self.approved_dependency_sources and (
            len(set(self.approved_dependency_sources)) != len(self.approved_dependency_sources)
            or set(self.approved_dependency_sources) != set(self.object_keys)
        ):
            raise ValueError("Reviewed object set differs")
        if self.object_keys and not (self.prior_scope_sha256 or self.approved_dependency_sources):
            raise ValueError("Existing objects require exact independent review")
        if self.action == "read" and self.creation_keys:
            raise ValueError("Read cannot reserve creation authority")
        if self.creation_keys and not finite_maintenance_allowed(
            self.family,
            self.fact_kind,
            self.object_keys,
            self.creation_keys,
        ):
            raise ValueError("Only explicitly approved F7 creation sources are permitted")
        if self.duty_code not in F7_ALLOWED_DUTIES or (
            not self.field_names and (self.family, self.fact_kind) != ("f2", "policy")
        ):
            raise ValueError("Exact F7 duty and owned fields required")
        return self


class ScopeInput(ScopeMatrixRow, base.ScopeInput):
    pass


class GrantInput(base.GrantInput):
    scope_owner: Literal[
        "school_facts.f2",
        "school_facts.f3",
        "school_facts.f4",
        "school_facts.f5",
        "school_facts.f6",
        "school_facts.school",
    ]


DutyInput = base.DutyInput
ExistingDutyReference = base.ExistingDutyReference
Command = Annotated[
    DutyInput | ScopeInput | GrantInput | RevokeInput, Field(discriminator="command")
]


class EngineeringManifest(base.EngineeringManifest):
    manifest_version: Literal["f7_governance_v1"]
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)
    allowed_duties: ClassVar = F7_ALLOWED_DUTIES
    actor_kind: ClassVar = "f7_engineering"
    actor_reason: ClassVar = "Explicitly approved finite F7 engineering governance"
    key_prefix: ClassVar = "f7"
    basis_authority: ClassVar = "f7_engineering_policy"
    approval_type: ClassVar = F7EngineeringApproval
    grant_input_type: ClassVar = GrantInput
    scope_schemas: ClassVar = F7EngineeringCommandAuthorizer.scope_schemas

    def finite(self):
        # Retain the established finite-manifest checks; only the independently
        # approved own-terminal duty may use the pre-existing school owner.
        prior = {d.duty_code for d in self.existing_duties}
        if (
            len(prior) != len(self.existing_duties)
            or not prior <= self.approved_duty_codes
            or any(isinstance(c, DutyInput) and c.duty_code in prior for c in self.commands)
            or not timedelta(0) < self.valid_to - self.valid_from <= timedelta(minutes=15)
            or self.grant_valid_to != self.grant_epoch + timedelta(days=7)
            or not self.grant_epoch <= self.valid_from < self.valid_to <= self.grant_valid_to
            or not self.approved_duty_codes <= self.allowed_duties
            or len({c.key for c in self.commands}) != len(self.commands)
            or self.actor_reference in {str(a) for a in self.approved_assignment_ids}
        ):
            raise ValueError("Exact independent approval and original seven-day epoch required")
        for c in self.commands:
            if not self.valid_from <= c.effective_from < self.valid_to:
                raise ValueError("Command is outside the short execution window")
            if isinstance(c, (DutyInput, ScopeInput, GrantInput)):
                if c.duty_code not in self.approved_duty_codes:
                    raise ValueError("Command duty is not explicitly approved")
            if isinstance(c, ScopeInput):
                c.scope(self.school_id)
            if isinstance(c, GrantInput):
                if (
                    c.assignment_id not in self.approved_assignment_ids
                    or not self.valid_from <= c.valid_from < self.valid_to
                    or c.planned_valid_to != self.grant_valid_to
                ):
                    raise ValueError("Exact assignment, start and fixed deadline required")
                if c.duty_code == F7_OWN_TERMINAL_DUTY:
                    if (
                        c.scope_owner != SCHOOL_SCOPE_OWNER
                        or c.scope_sha256 != school_scope_document_hash(self.school_id)
                    ):
                        raise ValueError("Own terminal requires the existing exact school scope")
                elif c.scope_owner == SCHOOL_SCOPE_OWNER or not c.duty_code.startswith(
                    c.scope_owner.rsplit(".", 1)[1] + "_"
                ):
                    raise ValueError("Duty and scope owner differ")
        return self


class EngineeringPrivateConfiguration(base.EngineeringPrivateConfiguration):
    expected_schema_contract: Literal["school-facts-f7-v55", "school-facts-f7-v56"]


def load_manifest(path: Path, expected_digest: str) -> EngineeringManifest:
    if path.stat().st_size > 1_048_576:
        raise ValueError("Manifest too large")
    raw = path.read_bytes()
    if not hmac.compare_digest(sha256(raw).hexdigest(), expected_digest):
        raise ValueError("Approved manifest bytes changed")
    return EngineeringManifest.model_validate_json(raw)


def command_marker(digest, command):
    return f"CONFIRM-F7:{digest}:{command_digest(command)}"


def review(manifest, digest):
    return {
        "manifest_sha256": digest,
        "manifest": manifest.model_dump(mode="json"),
        "f2_field_scope_note": "F2 is object-level; listed fields do not grant field isolation",
        "own_terminal_scope_note": (
            "Existing single-school scope; own terminal status only, no fact content"
        ),
        "commands": [
            dict(
                index=i,
                payload=json.loads(command_payload(c)),
                confirm_marker=command_marker(digest, c),
            )
            for i, c in enumerate(manifest.materialize())
        ],
    }


def build_duties_and_scopes(**kwargs):
    return base.build_duties_and_scopes(
        **kwargs,
        _manifest_type=EngineeringManifest,
        _scope_type=ScopeInput,
    )


def build_grants(manifest, bindings, **kwargs):
    return base.build_grants(manifest, bindings, **kwargs)


def grant_reference_matches(
    proof: GrantReferenceProof,
    *,
    school_id,
    actual_duty_code,
    actual_owner,
    actual_document,
    actual_sha256,
):
    if actual_duty_code != proof.duty_code or actual_owner != proof.scope_owner:
        return False
    if proof.duty_code == F7_OWN_TERMINAL_DUTY:
        return (
            actual_owner == SCHOOL_SCOPE_OWNER
            and actual_document == {"school_id": str(school_id)}
            and actual_sha256 == proof.scope_sha256 == school_scope_document_hash(school_id)
        )
    try:
        scope = parse_scope(actual_owner, actual_document)
        family = actual_owner.rsplit(".", 1)[1]
        allowed = {f"{family}_{scope.fact_kind}_reader"} if not scope.creation_keys else set()
        if finite_maintenance_allowed(
            family, scope.fact_kind, scope.object_keys, scope.creation_keys
        ):
            allowed.add(f"{family}_{scope.fact_kind}_maintainer")
        if family == "f6" and scope.quality_owners and not scope.creation_keys:
            allowed.add(f"{family}_{scope.fact_kind}_maintainer")
        if (family, scope.fact_kind) == ("f3", "guardian_relationship") and not scope.creation_keys:
            allowed.add("f3_guardian_relationship_verifier")
        if family == "f6" and scope.reserved_read_keys:
            return False
        return (
            actual_duty_code in allowed & F7_ALLOWED_DUTIES
            and scope.school_id == school_id
            and actual_sha256 == scope.document_sha256 == proof.scope_sha256
        )
    except (KeyError, TypeError, ValueError):
        return False


def compose(config, manifest, *, approval_is_current, receipt_only=False):
    from app.composition.school_fact_residence_authority import ResidenceScopeOwnerLookup
    from app.platform.school_facts.f6_policy import F6ScopeResolver
    from app.platform.school_facts.read_policy import SingleSchoolScopeResolver

    return base.compose(
        config,
        manifest,
        approval_is_current=approval_is_current,
        receipt_only=receipt_only,
        _authorizer_type=F7EngineeringCommandAuthorizer,
        _service_type=F7EngineeringGovernanceService,
        _reference_matches=grant_reference_matches,
        _scope_resolvers=lambda sessions: {
            "school_facts.f6": F6ScopeResolver(
                lookup_scope=ResidenceScopeOwnerLookup(sessions).scope_exists,
            ),
            SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver(),
        },
    )
