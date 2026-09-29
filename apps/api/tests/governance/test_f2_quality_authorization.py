from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import ReadAuthorization
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f2_read_authorization import F2ReadAuthorizationAdapter
from tests.test_secure_reader_projection import _actor


def test_quality_requires_and_revalidates_underlying_target_scope_before_read():
    actor = _actor()
    now = datetime.now(UTC)
    quality = F2FactScope(actor.school_id, "fact_quality", ("q-known",))
    person = F2FactScope(actor.school_id, "person", ("one-person",))
    scopes = {"fact_quality": quality, "person": person}
    live = [True]

    class Base:
        def decide(self, *, operation, actor, as_of):
            kind = operation.code.split(".")[-2]
            if kind not in scopes:
                return ReadAuthorization(False, None, None, None, None)
            return ReadAuthorization(
                True,
                kind,
                ScopePlan(uuid4(), "school_facts.f2", scopes[kind].reference, 1),
                NamedProjection(operation.projection_name, 1),
                kind,
            )

        def revalidate(self, authorization, *, actor, as_of):
            return live[0]

    adapter = F2ReadAuthorizationAdapter(
        thin_service=object(), scope_id_resolver=lambda *args: uuid4()
    )
    adapter._base = cast(ThinAuthorizationReadAdapter, Base())
    op = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    auth = adapter.decide(operation=op, actor=actor, as_of=now)
    assert auth.allowed
    assert auth.scope_plan is not None
    assert person.reference.replace('"', '\\"') in auth.scope_plan.reference
    assert adapter.revalidate(auth, actor=actor, as_of=now)
    live[0] = False
    assert not adapter.revalidate(auth, actor=actor, as_of=now)
    scopes.pop("person")
    assert not adapter.decide(operation=op, actor=actor, as_of=now).allowed


def test_bound_quality_checks_target_and_all_owner_families_but_no_unrelated_family():
    import json

    from app.platform.school_facts.f2_policy import F2_FACT_KINDS

    actor = _actor()
    seen = []
    live = set(F2_FACT_KINDS)

    class Base:
        def decide(self, *, operation, actor, as_of):
            kind = operation.code.split(".")[-2]
            seen.append(kind)
            if kind not in live:
                return ReadAuthorization(False, None, None, None, None)
            scope = F2FactScope(actor.school_id, kind, ("one",))
            return ReadAuthorization(
                True,
                kind,
                ScopePlan(uuid4(), "school_facts.f2", scope.reference, 1),
                NamedProjection(operation.projection_name, 1),
                kind,
            )

        def revalidate(self, authorization, *, actor, as_of):
            return authorization.basis in live

    adapter = F2ReadAuthorizationAdapter(
        thin_service=object(),
        scope_id_resolver=lambda *args: uuid4(),
        quality_target_kind="organization_profile",
    )
    adapter._base = cast(ThinAuthorizationReadAdapter, Base())
    op = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    auth = adapter.decide(operation=op, actor=actor, as_of=datetime.now(UTC))
    assert auth.allowed
    assert set(seen) == {
        "fact_quality",
        "organization_profile",
        "school",
        "person",
        "organization",
        "location",
    }
    assert auth.scope_plan is not None
    assert json.loads(auth.scope_plan.reference)["target_fact_kind"] == "organization_profile"
    live.remove("policy_version")
    assert adapter.revalidate(auth, actor=actor, as_of=datetime.now(UTC))
    live.remove("organization")
    assert not adapter.revalidate(auth, actor=actor, as_of=datetime.now(UTC))
    live.remove("organization_profile")
    assert not adapter.decide(operation=op, actor=actor, as_of=datetime.now(UTC)).allowed


def test_quality_current_batches_keep_all_actions_and_fresh_revalidation():
    from uuid import NAMESPACE_URL, uuid5

    from app.composition.school_fact_product_services import ProductThinAuthorizationService
    from app.platform.governance.contracts import (
        GovernanceAuthorizationBasis,
        GovernanceDependencyVersion,
    )
    from app.platform.school_facts.f2_policy import F2FactPolicy

    who = _actor()
    events = []
    live = {"valid": True, "error_duty": None, "batch_failure": False}

    class Authority:
        def issue(self, query):
            events.append(("single", query.duty_code))
            return self.make(query)

        def make(self, query):
            kind = query.duty_code.removeprefix("f2_").removesuffix("_reader")
            scope = F2FactScope(who.school_id, kind, ("exact",))
            grant = uuid5(NAMESPACE_URL, query.duty_code)
            dependencies = (
                GovernanceDependencyVersion(
                    "school_facts.assignment", str(who.assignment_id), who.assignment_version
                ),
                GovernanceDependencyVersion("school_facts.person", str(who.person_id), 1),
                GovernanceDependencyVersion("school_facts.f2.scope-target", scope.reference, 1),
                GovernanceDependencyVersion("governance.duty", str(grant), 1),
                GovernanceDependencyVersion("governance.scope", str(query.scope_id), 1),
                GovernanceDependencyVersion("governance.grant", str(grant), 1),
                GovernanceDependencyVersion(
                    "governance.assignment_epoch", str(who.assignment_id), 1
                ),
            )
            return GovernanceAuthorizationBasis(
                who.school_id,
                who.assignment_id,
                query.duty_code,
                query.scope_id,
                grant,
                1,
                1,
                query.as_of,
                None,
                dependencies,
            )

        def issue_many(self, queries):
            from sqlalchemy.exc import SQLAlchemyError

            if live["batch_failure"]:
                raise SQLAlchemyError("batch source unavailable")
            events.append(("batch", tuple(q.duty_code for q in queries)))
            return tuple(self.make(q) for q in queries)

        def validate(self, basis, *, as_of):
            events.append(("single_validate", basis.duty_code))
            return live["valid"]

        def validate_many(self, bases, *, as_of):
            from sqlalchemy.exc import SQLAlchemyError

            events.append(("batch_validate", tuple(b.duty_code for b in bases)))
            return tuple(
                SQLAlchemyError("owner unavailable")
                if b.duty_code == live["error_duty"]
                else live["valid"]
                for b in bases
            )

    thin = ProductThinAuthorizationService(
        governance_basis_service=Authority(), module_policies={"school_facts.f2": F2FactPolicy()}
    )
    adapter = F2ReadAuthorizationAdapter(
        thin_service=thin,
        scope_id_resolver=lambda op, _: uuid5(NAMESPACE_URL, op.code),
        quality_target_kind="organization_profile",
    )
    operation = ReadOperation(
        "school_facts.f2.fact_quality.read",
        "f2_fact_quality_read",
        "school_facts.f2.fact_quality.read",
        "school_facts.f2",
    )
    auth = adapter.decide(operation=operation, actor=who, as_of=datetime.now(UTC))
    assert auth.allowed
    assert [name for name, _ in events] == ["single", "batch"]
    assert len(events[1][1]) == 5
    assert adapter.revalidate(auth, actor=who, as_of=datetime.now(UTC))
    assert events[-1][0] == "batch_validate" and len(events[-1][1]) == 6
    live["valid"] = False
    assert not adapter.revalidate(auth, actor=who, as_of=datetime.now(UTC))
    assert events[-1][0] == "batch_validate"
    # As before batching, an earlier denial wins over a later source exception.
    live["error_duty"] = "f2_location_reader"
    assert not adapter.revalidate(auth, actor=who, as_of=datetime.now(UTC))
    live["error_duty"] = "f2_fact_quality_reader"
    with pytest.raises(Exception, match="授权源暂时不可用"):
        adapter.revalidate(auth, actor=who, as_of=datetime.now(UTC))
    live["batch_failure"] = True
    with pytest.raises(Exception, match="授权源暂时不可用"):
        adapter.decide(operation=operation, actor=who, as_of=datetime.now(UTC))
