"""Externally reviewed live deployment batch; no bootstrap or ongoing governance power."""

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from app.composition.school_fact_f5_engineering_governance import (
    F5EngineeringApproval,
    F5EngineeringGovernanceService,
)
from app.composition.school_fact_f6_engineering_governance import F6EngineeringCommandAuthorizer
from app.composition.school_fact_f7_engineering_governance import F7_ALLOWED_DUTIES

LIVE_ALLOWED_DUTIES = F7_ALLOWED_DUTIES | frozenset({"f3_guardian_relationship_recorder"})


def parse_live_scope(owner, document):
    from app.platform.school_facts.f2_policy import F2FactScope
    from app.platform.school_facts.f3_policy import F3FactScope
    from app.platform.school_facts.f4_policy import F4FactScope
    from app.platform.school_facts.f5_policy import F5FactScope
    from app.platform.school_facts.f6_policy import F6FactScope

    classes = {"school_facts.f2": F2FactScope, "school_facts.f3": F3FactScope,
               "school_facts.f4": F4FactScope, "school_facts.f5": F5FactScope,
               "school_facts.f6": F6FactScope}
    if owner not in classes:
        raise ValueError("Unapproved live scope owner")
    return classes[owner].from_document(document)


@dataclass(frozen=True, slots=True)
class LiveGovernanceApproval(F5EngineeringApproval):
    allowed_duties: ClassVar = LIVE_ALLOWED_DUTIES

    def __post_init__(self):
        if (
            any(
                t.utcoffset() is None
                for t in (self.valid_from, self.valid_to, self.grant_epoch, self.grant_valid_to)
            )
            or not timedelta(0) < self.valid_to - self.valid_from <= timedelta(minutes=15)
            or self.grant_valid_to != self.grant_epoch + timedelta(days=90)
            or not self.grant_epoch <= self.valid_from < self.valid_to <= self.grant_valid_to
            or not self.school_id.int
            or not self.actor_reference.strip()
            or not self.approval_reference.strip()
            or not self.assignment_ids
            or self.actor_reference in {str(a) for a in self.assignment_ids}
            or not self.duty_codes
            or not self.duty_codes <= self.allowed_duties
            or not 1 <= len(self.command_digests) <= 300
            or any(
                len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
                for d in self.command_digests
            )
        ):
            raise ValueError(
                "Exact live approval, independent actor and fixed 90-day epoch required"
            )


class LiveGovernanceCommandAuthorizer(F6EngineeringCommandAuthorizer):
    actor_kind = "live_deployment"
    basis_authority = "reviewed_live_deployment"
    own_scope_owner = None
    parse_scope = staticmethod(parse_live_scope)

    @staticmethod
    def dependency_maintenance_allowed(family, kind, object_keys, creation_keys):
        # The live manifest pin, not an old synthetic creation prefix, approves each exact key.
        return (
            bool(object_keys or creation_keys)
            and f"{family}_{kind}_maintainer" in LIVE_ALLOWED_DUTIES
        )

    def authorize(self, request):
        try:
            bound = json.loads(request.command_payload_json)
            payload = bound["payload"]
            actor = payload["actor"]
            if (
                payload["school_id"] != str(request.school_id)
                or actor["school_id"] != str(request.actor.school_id)
                or actor["actor_kind"] != request.actor.actor_kind
                or actor["actor_reference"] != request.actor.actor_reference
                or actor["actor_assignment_id"] is not None
                or actor["actor_assignment_version"] is not None
                or actor["reason"] != request.actor.reason
                or request.target_kind != request.command_name.split(".")[1]
                or request.target_id is not None
                or request.command_name
                not in {
                    "governance.duty.create",
                    "governance.scope.create",
                    "governance.grant.create",
                }
            ):
                return False
            return super().authorize(request)
        except (KeyError, TypeError, ValueError, IndexError):
            return False


class LiveGovernanceService(F5EngineeringGovernanceService):
    command_authorizer_type = LiveGovernanceCommandAuthorizer
