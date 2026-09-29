"""Finite F7 engineering policy over the existing confirmed Governance Writer."""

from typing import ClassVar

from app.composition.school_fact_f5_engineering_governance import (
    F5EngineeringApproval,
    F5EngineeringGovernanceService,
)
from app.composition.school_fact_f6_engineering_governance import (
    F6EngineeringCommandAuthorizer,
    parse_scope,
)
from app.platform.school_facts.f2_policy import F2_FACT_KINDS
from app.platform.school_facts.f3_policy import F3_FACT_KINDS
from app.platform.school_facts.f4_policy import F4_FACT_KINDS
from app.platform.school_facts.f5_policy import F5_FACT_KINDS
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.f7_own_terminal_authorization import F7_OWN_TERMINAL_DUTY

F7_FACT_KINDS = {
    "f2": F2_FACT_KINDS,
    "f3": F3_FACT_KINDS,
    "f4": F4_FACT_KINDS,
    "f5": F5_FACT_KINDS,
    "f6": F6_FACT_KINDS,
}
F7_ALLOWED_DUTIES = frozenset(
    {F7_OWN_TERMINAL_DUTY, "f3_guardian_relationship_verifier"}
) | frozenset(
    f"{family}_{kind}_{action}"
    for family, kinds in F7_FACT_KINDS.items()
    for kind in kinds
    for action in ("reader", "maintainer")
)


def finite_maintenance_allowed(family, kind, object_keys, creation_keys):
    return (
        kind in F7_FACT_KINDS.get(family, ())
        and bool(object_keys or creation_keys)
        and all(key.startswith(f"f7:{kind}:") for key in creation_keys)
    )


class F7EngineeringApproval(F5EngineeringApproval):
    allowed_duties: ClassVar[frozenset[str]] = F7_ALLOWED_DUTIES


class F7EngineeringCommandAuthorizer(F6EngineeringCommandAuthorizer):
    actor_kind = "f7_engineering"
    basis_authority = "f7_engineering_policy"
    # No whole owner is exempted from the explicit creation-source restriction.
    own_scope_owner = "school_facts.f7"
    parse_scope = staticmethod(parse_scope)
    dependency_maintenance_allowed = staticmethod(finite_maintenance_allowed)


class F7EngineeringGovernanceService(F5EngineeringGovernanceService):
    command_authorizer_type = F7EngineeringCommandAuthorizer
