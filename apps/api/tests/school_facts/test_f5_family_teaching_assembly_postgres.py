"""Real identity resolver + Reader repository + Python teaching projection in the owned DB."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_family_teaching_query import (
    FamilyTeachingQueryService,
    FamilyTeachingRepository,
)
from app.platform.contracts import GuardianActorContext
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ActorContextResolver
from app.platform.identity.session import IdentitySessionStore
from tests.school_facts.test_f3_family_postgres import KEY, existing_fixture_writer
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f5_family_teaching_postgres import owned_family  # noqa: F401
from tests.test_f1_product_identity import MemoryRedis


@pytest.fixture
def assembled(owned_family):  # noqa: F811 - pytest resolves the imported owned fixture
    engines, guardian = owned_family
    with engines["migration"].connect() as connection:
        row = connection.execute(
            text(
                "SELECT u.id,u.external_subject,u.display_name,u.security_version FROM users u "
                "JOIN account_person_bindings b ON b.account_user_id=u.id WHERE "
                "b.school_id=:school_id AND b.person_id=:adult"
            ),
            guardian,
        ).one()
    user = CurrentUser(*row)
    redis = MemoryRedis()
    sessions = IdentitySessionStore(redis=redis, ttl_seconds=900)
    token = sessions.create(user)
    identity = sessions.read(token)
    assert identity is not None
    resolver = ActorContextResolver(
        session_factory=sessionmaker(engines["runtime"]),
        redis=redis,
        proof_signer=ContextProofSigner(KEY),
        current_user=user,
        identity_session=identity,
    )
    options = [
        (option, resolver.resolve(option.opaque_ref))
        for option in resolver.list_options()
        if option.kind == "guardian_relationship"
    ]
    option, actor = next(
        (option, actor)
        for option, actor in options
        if type(actor) is GuardianActorContext
        and actor.guardian_relationship_id == guardian["relation"]
    )
    state = SimpleNamespace(active=0, permitted=True, clock=datetime.now(UTC))

    def checked(candidate):
        return (
            state.permitted
            and sessions.read(token) is not None
            and resolver.resolve(option.opaque_ref) == candidate
        )

    def checkout(*_):
        state.active += 1

    def checkin(*_):
        state.active -= 1

    event.listen(engines["reader"], "checkout", checkout)
    event.listen(engines["reader"], "checkin", checkin)
    repository = FamilyTeachingRepository(sessionmaker(engines["reader"]))
    service = FamilyTeachingQueryService(
        repository=repository, current_actor=checked, clock=lambda: state.clock
    )
    try:
        yield service, actor, state, engines
        assert state.active == 0, "Reader connection leaked across release/model wait"
    finally:
        event.remove(engines["reader"], "checkout", checkout)
        event.remove(engines["reader"], "checkin", checkin)


def test_current_real_reader_python_projection_and_revalidation(assembled):
    service, actor, state, _ = assembled
    result = service.query(actor=actor, subject_name="数学")
    public = result.public()
    assert public["status"] == "available", public
    assert public["items"]
    assert result.evidence is not None and result.evidence.valid(actor)
    assert state.active == 0
    material = education_model_material(public)
    rendered = str(material)
    assert "陈老师" in rendered
    assert "student:f3-a" not in rendered and "private_mobile" not in rendered
    state.permitted = False
    assert not result.evidence.valid(actor)
    assert service.query(actor=actor).public()["status"] == "denied"


def test_real_term_contains_slices_groups_quality_and_authorized_limit(assembled):
    service, actor, state, _ = assembled
    result = service.query(actor=actor, period="current_term", course_name="Own independent course")
    public = result.public()
    assert public["status"] == "available", public
    assert public["period_limited_to_authorized"] is True
    assert len(public["timeline"]) >= 2
    assert any(part["items"] for part in public["timeline"])
    assert result.evidence is not None and result.evidence.valid(actor)
    assert state.active == 0
    raw = service.repository.teaching_read(
        actor,
        factual_at=None,
        period="current",
        subject_name=None,
        course_name="Own independent course",
        contacts=False,
    )
    assert raw["related_groups"] and raw["quality"]
    boundary = datetime.fromisoformat(raw["next_boundary"])
    state.clock = boundary
    assert not result.evidence.valid(actor)


def test_real_school_fact_change_invalidates_python_evidence(assembled):
    service, actor, state, engines = assembled
    result = service.query(actor=actor, course_name="Unrecorded controlled course")
    assert result.public()["status"] == "no_current_records"
    assert result.evidence is not None and result.evidence.valid(actor)
    staff, identity, context, writer = existing_fixture_writer(engines)
    apply(
        (engines, staff, identity, context, writer),
        [
            {
                "fact_kind": "teaching_course",
                "source_key": "f5-assembly-stamp:" + uuid4().hex,
                "operation": "upsert",
                "expected_version": 0,
                "effective_at": datetime.now(UTC),
                "content": {"display_name": "Owned stamp invalidation"},
            }
        ],
    )
    assert not result.evidence.valid(actor)
    assert state.active == 0


def test_real_projection_rejects_accidental_private_teacher_field(assembled, monkeypatch):
    service, actor, state, _ = assembled
    original = service.repository.teaching_read

    def overbroad(*args, **kwargs):
        raw = original(*args, **kwargs)
        assert raw["related_teachers"]
        raw["related_teachers"][0]["private_mobile"] = "not-authorized"
        return raw

    monkeypatch.setattr(service.repository, "teaching_read", overbroad)
    result = service.query(actor=actor, subject_name="数学")
    assert result.public()["status"] == "unavailable"
    assert result.evidence is None and state.active == 0


def test_runtime_identity_role_cannot_replace_formal_reader(assembled):
    service, actor, _, engines = assembled
    wrong = FamilyTeachingQueryService(
        repository=FamilyTeachingRepository(sessionmaker(engines["runtime"])),
        current_actor=lambda candidate: candidate == actor,
    )
    result = wrong.query(actor=actor)
    assert result.public()["status"] in {"denied", "unavailable"}
    assert result.evidence is None
