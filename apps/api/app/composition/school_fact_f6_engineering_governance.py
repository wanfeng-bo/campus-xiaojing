"""F6 deployment-only approval; the platform Writer and old approvals stay unchanged."""

from typing import ClassVar

from app.composition.school_fact_engineering_governance import (
    governance_command_digest as f6_governance_command_digest,
)
from app.composition.school_fact_engineering_governance import (
    governance_command_payload as f6_governance_command_payload,
)
from app.composition.school_fact_f5_engineering_governance import (
    F5_ALLOWED_DUTIES,
    F5EngineeringApproval,
    F5EngineeringCommandAuthorizer,
    F5EngineeringGovernanceService,
    GrantReferenceProof,
)
from app.composition.school_fact_f5_engineering_governance import (
    parse_scope as parse_previous_scope,
)
from app.platform.school_facts.f6_policy import F6_FACT_KINDS, F6_SCOPE_SCHEMA, F6FactScope

DEPENDENCY_MAINTAIN = frozenset(
    {
        ("f3", "student_school_relationship"),
        ("f2", "fact_quality"),
        ("f2", "location"),
        ("f4", "student_class_membership"),
        ("f4", "student_class_position"),
        ("f5", "teaching_group_member"),
    }
)
F6_ALLOWED_DUTIES = (
    frozenset(
        f"f6_{kind}_{purpose}" for kind in F6_FACT_KINDS for purpose in ("reader", "maintainer")
    )
    | frozenset(d for d in F5_ALLOWED_DUTIES if d.endswith("_reader"))
    | frozenset(f"{family}_{kind}_maintainer" for family, kind in DEPENDENCY_MAINTAIN)
)


def dependency_maintenance_allowed(
    family, kind, object_keys, creation_keys, *, verified_scope_sha256=None, reviewed_object_keys=()
):
    if (family, kind) not in DEPENDENCY_MAINTAIN:
        return False
    if creation_keys:
        return (
            family == "f2"
            and kind in {"fact_quality", "location"}
            and all(k.startswith(f"f6:{kind}:") for k in object_keys + creation_keys)
        )
    return bool(object_keys) and (
        verified_scope_sha256 is not None or set(reviewed_object_keys) == set(object_keys)
    )


def parse_scope(owner, document):
    if document.get("coverage") is not None:
        raise ValueError("Engineering approval cannot grant live school coverage")
    return (
        F6FactScope.from_document(document)
        if owner == "school_facts.f6"
        else parse_previous_scope(owner, document)
    )


class F6EngineeringApproval(F5EngineeringApproval):
    allowed_duties: ClassVar[frozenset[str]] = F6_ALLOWED_DUTIES


class F6EngineeringCommandAuthorizer(F5EngineeringCommandAuthorizer):
    actor_kind = "f6_engineering"
    basis_authority = "f6_engineering_policy"
    own_scope_owner = "school_facts.f6"
    parse_scope = staticmethod(parse_scope)
    dependency_maintenance_allowed = staticmethod(dependency_maintenance_allowed)
    scope_schemas = {
        **F5EngineeringCommandAuthorizer.scope_schemas,
        "school_facts.f6": F6_SCOPE_SCHEMA,
    }


class F6EngineeringGovernanceService(F5EngineeringGovernanceService):
    command_authorizer_type = F6EngineeringCommandAuthorizer


__all__ = [
    "F6_ALLOWED_DUTIES",
    "F6EngineeringApproval",
    "F6EngineeringCommandAuthorizer",
    "F6EngineeringGovernanceService",
    "GrantReferenceProof",
    "parse_scope",
    "dependency_maintenance_allowed",
    "f6_governance_command_digest",
    "f6_governance_command_payload",
]
