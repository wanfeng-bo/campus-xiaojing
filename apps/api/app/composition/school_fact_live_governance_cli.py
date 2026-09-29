"""Strict production deployment adapter over retained Writer/receipt mechanics.

The OS-protected reviewer configuration and its independently pinned evidence are
an external deployment trust boundary, not evidence created by this program.
"""

import hmac
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.composition import school_fact_f5_engineering_cli as base
from app.composition import school_fact_f6_engineering_cli as scopes
from app.composition.school_fact_engineering_governance import governance_command_digest
from app.composition.school_fact_f3_engineering_cli import GrantBinding as GrantBinding
from app.composition.school_fact_f3_engineering_cli import StrictInput
from app.composition.school_fact_live_governance import (
    LIVE_ALLOWED_DUTIES,
    LiveGovernanceApproval,
    LiveGovernanceCommandAuthorizer,
    LiveGovernanceService,
    parse_live_scope,
)
from app.composition.school_fact_live_initialization_contracts import (
    LiveDatabase,
    ReviewedDeployment,
)
from app.platform.school_facts.f7_own_terminal_authorization import F7_OWN_TERMINAL_DUTY
from app.platform.school_facts.read_policy import SCHOOL_SCOPE_OWNER, school_scope_document_hash


class ScopeInput(scopes.ScopeInput):
    f2_object_level_acknowledged: bool = False
    coverage: Literal["exact", "school"] = "exact"

    def finite(self):
        if self.coverage == "school" and (
            self.object_keys or self.creation_keys or self.prior_scope_sha256
            or self.approved_dependency_sources
        ):
            raise ValueError("School coverage cannot mix exact reviewed coordinates")
        if self.reserved_read_keys or self.quality_owners:
            raise ValueError("Only exact ordinary object and creation scopes are approved")
        if self.duty_code not in LIVE_ALLOWED_DUTIES:
            raise ValueError("Unknown duty")
        if self.family == "f2":
            if not self.f2_object_level_acknowledged or self.field_names:
                raise ValueError("F2 grants whole objects; field isolation cannot be requested")
        elif self.f2_object_level_acknowledged or not self.field_names:
            raise ValueError("Exact owned fields required")
        if self.action == "read" and self.creation_keys:
            raise ValueError("Read cannot create")
        if len(self.approved_dependency_sources) != len(set(self.approved_dependency_sources)):
            raise ValueError("Duplicate reviewed objects")
        if self.approved_dependency_sources and set(self.approved_dependency_sources) != set(
            self.object_keys
        ):
            raise ValueError("Reviewed object set differs")
        self.scope(UUID(int=1)) if self.prior_scope_sha256 is None else None
        return self

    def scope(self, school_id: UUID):
        document = {
            "school_id": str(school_id), "fact_kind": self.fact_kind,
            "object_keys": list(self.object_keys), "creation_keys": list(self.creation_keys),
        }
        if self.family != "f2":
            document["field_names"] = list(self.field_names)
        if self.coverage == "school":
            document["coverage"] = "school"
        scope = parse_live_scope(f"school_facts.{self.family}", document)
        if self.prior_scope_sha256 and self.prior_scope_sha256 != scope.document_sha256:
            raise ValueError("Reviewed scope fingerprint differs")
        return scope


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
Command = Annotated[DutyInput | ScopeInput | GrantInput, Field(discriminator="command")]


class EngineeringManifest(base.EngineeringManifest):
    manifest_version: Literal["live_governance_v1"]
    commands: tuple[Command, ...] = Field(min_length=1, max_length=300)
    approved_assignment_ids: frozenset[UUID] = Field(min_length=1, max_length=100)
    allowed_duties: ClassVar = LIVE_ALLOWED_DUTIES
    actor_kind: ClassVar = "live_deployment"
    actor_reason: ClassVar = "Externally reviewed exact live deployment batch"
    key_prefix: ClassVar = "live"
    basis_authority: ClassVar = "reviewed_live_deployment"
    approval_type: ClassVar = LiveGovernanceApproval
    grant_input_type: ClassVar = GrantInput
    scope_schemas: ClassVar = LiveGovernanceCommandAuthorizer.scope_schemas

    def finite(self):
        prior = {d.duty_code for d in self.existing_duties}
        duties = [c.duty_code for c in self.commands if isinstance(c, DutyInput)]
        grants = [
            (c.assignment_id, c.duty_id, c.scope_id)
            for c in self.commands
            if isinstance(c, GrantInput)
        ]
        if (
            len(grants) != len(set(grants))
            or len(prior) != len(self.existing_duties)
            or len(set(duties)) != len(duties)
            or not prior <= self.approved_duty_codes
            or prior & set(duties)
            or len({c.key for c in self.commands}) != len(self.commands)
            or self.actor_reference in {str(a) for a in self.approved_assignment_ids}
        ):
            raise ValueError("Independent exact unique batch required")
        for c in self.commands:
            if (
                not self.valid_from <= c.effective_from < self.valid_to
                or c.duty_code not in self.approved_duty_codes
            ):
                raise ValueError("Unapproved command")
            if isinstance(c, ScopeInput):
                c.scope(self.school_id)
            if isinstance(c, GrantInput):
                if (
                    c.assignment_id not in self.approved_assignment_ids
                    or c.valid_from != self.valid_from
                    or c.planned_valid_to != self.grant_valid_to
                ):
                    raise ValueError("Fixed reviewed recipient, batch start and deadline required")
                if c.duty_code == F7_OWN_TERMINAL_DUTY:
                    if (
                        c.scope_owner != SCHOOL_SCOPE_OWNER
                        or c.scope_sha256 != school_scope_document_hash(self.school_id)
                    ):
                        raise ValueError("Own terminal requires exact existing school scope")
                elif c.scope_owner == SCHOOL_SCOPE_OWNER or not c.duty_code.startswith(
                    c.scope_owner.rsplit(".", 1)[1] + "_"
                ):
                    raise ValueError("Duty owner differs")
        self.approval()
        return self


class ClosureEvidence(StrictInput):
    school_id: UUID
    anchor_id: UUID
    system_identifier: str = Field(pattern=r"^[0-9]{10,32}$")
    database_name: LiveDatabase
    initialization_request_id: UUID
    initialization_close_receipt_id: UUID
    initialization_close_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_close_receipt_id: UUID
    governance_close_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def non_nil(self):
        if any(
            value.int == 0
            for value in (
                self.school_id,
                self.anchor_id,
                self.initialization_request_id,
                self.initialization_close_receipt_id,
                self.governance_close_receipt_id,
            )
        ):
            raise ValueError("Original non-nil closure identifiers required")
        return self


class DeploymentEvidence(ReviewedDeployment):
    purpose: Literal["school-facts-live-governance"]
    closure: ClosureEvidence
    approved_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_assignment_id: UUID | None = None
    recipient_assignment_ids: frozenset[UUID] = Field(min_length=1, max_length=100)


class EngineeringPrivateConfiguration(base.EngineeringPrivateConfiguration):
    database_name: LiveDatabase
    expected_schema_contract: Literal["school-facts-f7-v56", "school-facts-live-v57"]
    manifest_path: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)
    approved_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("Duplicate JSON key")
        result[k] = v
    return result


def read_pinned(path, digest, limit=1_048_576):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Regular reviewed file required")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or not hmac.compare_digest(sha256(raw).hexdigest(), digest):
        raise ValueError("Reviewed bytes changed")
    json.loads(raw, object_pairs_hook=unique_object)
    return raw


def load_manifest(path, expected_digest):
    return EngineeringManifest.model_validate_json(read_pinned(path, expected_digest))


def verify_evidence(config, manifest):
    pinned = load_manifest(Path(config.manifest_path), config.approved_manifest_sha256)
    if pinned != manifest:
        raise ValueError("Supplied manifest differs from reviewed bytes")
    e = DeploymentEvidence.model_validate_json(
        read_pinned(config.evidence_path, config.approved_evidence_sha256)
    )
    if (
        (
            e.closure.school_id,
            e.closure.anchor_id,
            e.closure.system_identifier,
            e.closure.database_name,
        )
        != (
            config.expected_school_id,
            config.expected_anchor_id,
            config.expected_system_identifier,
            config.database_name,
        )
        or e.approved_manifest_sha256 != config.approved_manifest_sha256
        or e.approval_reference != manifest.approval_reference
        or e.operator_reference != manifest.actor_reference
        or e.operator_assignment_id != config.operator_assignment_id
        or e.recipient_assignment_ids != manifest.approved_assignment_ids
        or e.operator_assignment_id in e.recipient_assignment_ids
        or (e.approved_at, e.expires_at) != (manifest.valid_from, manifest.valid_to)
    ):
        raise ValueError("Independent evidence binding differs")
    return e


def command_marker(digest, command):
    return f"CONFIRM-LIVE:{digest}:{governance_command_digest(command)}"


def review(manifest, digest):
    result = base.review(manifest, digest)
    result["f2_field_scope_note"] = "F2 authorizes entire explicit objects; no field isolation."
    for row, c in zip(result["commands"], manifest.materialize(), strict=True):
        row["confirm_marker"] = command_marker(digest, c)
    return result


def build_grants(manifest, bindings, **kwargs):
    if (
        len(bindings) > 300
        or len({b.key for b in bindings}) != len(bindings)
        or len({(b.assignment_id, b.duty_key, b.scope_key) for b in bindings}) != len(bindings)
        or kwargs["grant_valid_from"] != manifest.valid_from
        or kwargs["execution_valid_to"] != manifest.valid_to
    ):
        raise ValueError("Original fixed batch window and unique bindings required")
    return base.build_grants(manifest, bindings, **kwargs)


def grant_reference_matches(
    proof, *, school_id, actual_duty_code, actual_owner, actual_document, actual_sha256
):
    if proof.duty_code != actual_duty_code or proof.scope_owner != actual_owner:
        return False
    if actual_duty_code == F7_OWN_TERMINAL_DUTY:
        return (
            actual_owner == SCHOOL_SCOPE_OWNER
            and actual_document == {"school_id": str(school_id)}
            and actual_sha256 == proof.scope_sha256 == school_scope_document_hash(school_id)
        )
    try:
        scope = parse_live_scope(actual_owner, actual_document)
        family = actual_owner.rsplit(".", 1)[1]
        allowed = {f"{family}_{scope.fact_kind}_maintainer"}
        if not scope.creation_keys:
            allowed.add(f"{family}_{scope.fact_kind}_reader")
        if (family, scope.fact_kind) == ("f3", "guardian_relationship") and not scope.creation_keys:
            allowed.update(
                {"f3_guardian_relationship_verifier", "f3_guardian_relationship_recorder"}
            )
        return (
            actual_duty_code in allowed & LIVE_ALLOWED_DUTIES
            and scope.school_id == school_id
            and actual_sha256 == scope.document_sha256 == proof.scope_sha256
        )
    except (ValueError, KeyError, TypeError):
        return False


def compose(config, manifest, *, approval_is_current, receipt_only=False):
    from app.composition.school_fact_residence_authority import ResidenceScopeOwnerLookup
    from app.platform.school_facts.f6_policy import F6ScopeResolver
    from app.platform.school_facts.read_policy import SingleSchoolScopeResolver

    verify_evidence(config, manifest)
    withdrawn = False

    def current():
        nonlocal withdrawn
        if withdrawn:
            return False
        try:
            verify_evidence(config, manifest)
            withdrawn = not approval_is_current()
        except Exception:
            withdrawn = True
        return not withdrawn

    return base.compose(
        config,
        manifest,
        approval_is_current=current,
        receipt_only=receipt_only,
        _authorizer_type=LiveGovernanceCommandAuthorizer,
        _service_type=LiveGovernanceService,
        _reference_matches=grant_reference_matches,
        _scope_resolvers=lambda sessions: {
            "school_facts.f6": F6ScopeResolver(
                lookup_scope=ResidenceScopeOwnerLookup(sessions).scope_exists
            ),
            SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver(),
        },
    )
