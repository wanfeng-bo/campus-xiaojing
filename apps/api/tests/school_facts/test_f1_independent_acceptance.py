"""Independent F1 acceptance assets; no prompts, model calls or secrets live here.

The PG fixture uses a disposable schema22 clone and the sole canonical Writer.
Its exact-plan bootstrap guard is TEST ONLY, never an extension of F1 production
maintenance permission. It is not evidence of independently hosted processes.
Import ``independent_school`` into a live-model test module to reuse its fixture.
Full dialogue and thinking must remain in that caller's memory; persist only the
allowlisted structured recorder output and fixed rubric verdicts.
"""

from __future__ import annotations

import json
import runpy
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_initialization import SchoolFactInitializer
from app.composition.school_fact_product_contacts import contact_query_view
from app.composition.school_fact_product_identity import (
    ENGINEERING_ACCOUNTS,
    build_school_fact_product_identity_service,
)
from app.composition.school_fact_product_services import create_product_thin_service
from app.composition.school_fact_product_settings import ExplicitReaderSettings
from app.composition.school_fact_product_writer import ExplicitWriterSettings
from app.composition.school_fact_query_repository import OfficialContactFinalAuthorizationGuard
from app.composition.school_fact_reader import (
    build_school_fact_reader_engine,
    verify_school_fact_reader_database,
)
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.composition.school_fact_writer_settings import (
    build_school_fact_writer_engine,
    verify_school_fact_writer_database,
)
from app.db.connection import (
    build_engine,
    build_session_factory,
    verify_runtime_context_signing_key,
)
from app.platform.authorization.thin_contracts import ThinAuthorizationBasis
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_command_context
from app.platform.identity.service import find_or_create_namespaced_test_user
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, canonical_plan_sha256
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_f1_product_postgres import (
    PROJECT_ROOT,
    _migration_engine,
    _query,
    _request,
    _role_settings,
    _root,
    _runtime_settings,
)

QUERY_TOOL = "school_facts.official_contacts.query"
RUBRIC = (
    "current_intent_resolved",
    "references_and_corrections_resolved",
    "facts_match_current_authorized_evidence",
    "scope_time_and_coverage_not_overstated",
    "necessary_clarification_only",
    "unsupported_requests_explained_honestly",
    "identity_and_conversation_isolated",
)
# Seven orthogonal groups, independent of any implementation's diagnostic words.
GROUPS = (
    "H1_ellipsis_source",
    "H2_constraint_correction",
    "H3_order_count_scope",
    "H4_current_time_boundary",
    "H5_topic_switch",
    "H6_real_ambiguity_unsupported",
    "H7_identity_isolation",
)


@dataclass(frozen=True)
class Dataset:
    variant: str
    campus_names: tuple[str, ...]
    duplicate_name: str
    primary_values: tuple[str, ...]
    secondary_values: tuple[str, ...]
    excluded_values: tuple[str, ...]


def dataset(variant: str) -> Dataset:
    if variant not in {"cedar", "quartz"}:
        raise ValueError("unknown independent fixture variant")
    names = (
        ("合成溪桥校区", "合成杉岸校区", "合成荷汀校区")
        if variant == "cedar"
        else ("合成砚山校区", "合成鹭洲校区", "合成松渚校区", "合成泉台校区")
    )
    return Dataset(
        variant,
        names,
        "合成同名校区" if variant == "cedar" else "合成重名校区",
        (f"{variant}-desk@example.invalid", "000-00000001"),
        tuple(f"{variant}-channel-{index}@example.invalid" for index in range(1, 4)),
        tuple(
            f"{variant}-{kind}@example.invalid"
            for kind in ("expired", "future", "restricted", "department")
        ),
    )


def _seed_plan(data: Dataset, *, base_version: int, now: datetime) -> CanonicalFactChangeSetV1:
    changes: list[dict[str, Any]] = []

    def add(kind: str, key: str, content: dict[str, Any], *, effective=None):
        changes.append(
            dict(
                operation="upsert",
                fact_kind=kind,
                source_key=f"heldout:{data.variant}:{key}",
                expected_version=0,
                effective_at=effective or now - timedelta(minutes=5),
                origin=dict(sheet_code="independent_f1_fixture", row_number=len(changes) + 1),
                content=content,
            )
        )

    def contact(key, owner, value, *, visibility="public", effective=None):
        content = dict(
            owner_kind="location",
            location_source_key=f"heldout:{data.variant}:{owner}",
            contact_kind="email" if "@" in value else "phone",
            contact_value=value,
            service_hours=None if key.endswith("0") else "周二至周五 10:20—15:40",
            lifecycle_status="active",
            visibility_scope=visibility,
        )
        if visibility == "restricted":
            content["audience_code"] = "test_only_restricted"
        add("official_contact", key, content, effective=effective)

    # Insertion order differs from candidate order and from the second layout.
    indices = list(range(len(data.campus_names)))
    if data.variant == "cedar":
        indices.reverse()
    else:
        indices = indices[2:] + indices[:2]
    for index in indices:
        name = data.campus_names[index]
        add(
            "location",
            f"campus-{index}",
            dict(kind="campus", display_name=name, display_path=name, status="active"),
        )
    for index in (1, 0):
        add(
            "location",
            f"duplicate-{index}",
            dict(
                kind="campus",
                display_name=data.duplicate_name,
                display_path=f"独立测试/{index}/{data.duplicate_name}",
                status="active",
            ),
        )
        contact(
            f"duplicate-contact-{index}",
            f"duplicate-{index}",
            f"{data.variant}-duplicate-{index}@example.invalid",
        )
    for index, value in enumerate(data.primary_values):
        contact(f"primary-{index}", "campus-0", value)
    for index, value in enumerate(data.secondary_values):
        contact(f"secondary-{index}", "campus-1", value, visibility="school_authenticated")
    contact("expired", "campus-0", data.excluded_values[0])
    contact("future", "campus-0", data.excluded_values[1], effective=now + timedelta(days=2))
    contact("restricted", "campus-0", data.excluded_values[2], visibility="restricted")
    add(
        "official_contact",
        "department",
        dict(
            owner_kind="organization",
            organization_source_key="org:f1-office",
            contact_kind="email",
            contact_value=data.excluded_values[3],
            lifecycle_status="active",
            visibility_scope="public",
        ),
    )
    # A second *real* identity, with no Governance grant. No forged ActorContext.
    add(
        "person",
        "denied-person",
        dict(display_name="合成独立验收无权限人员", person_kind="staff", lifecycle_status="active"),
    )
    add(
        "login_mapping",
        "denied-login",
        dict(
            person_source_key=f"heldout:{data.variant}:denied-person",
            provider_code="local_test",
            provider_tenant_key="development",
            external_subject="f1-unprivileged",
            status="confirmed",
        ),
    )
    add(
        "assignment",
        "denied-assignment",
        dict(
            person_source_key=f"heldout:{data.variant}:denied-person",
            organization_source_key="org:f1-office",
            position_title="合成独立验收岗位",
            semantic_role_code="heldout_no_grant",
            assignment_kind="substantive",
            appointment_source="approved_manifest",
            approval_reference="pytest:heldout:exact-plan",
            status="confirmed",
        ),
    )
    return CanonicalFactChangeSetV1.model_validate(
        dict(
            source_sha256=sha256(f"independent-f1:{data.variant}".encode()).hexdigest(),
            base_version=base_version,
            changes=tuple(changes),
        )
    )


def _terminal_plan(variant: str, *, base_version: int, now: datetime) -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        dict(
            source_sha256=sha256(f"independent-f1:{variant}:end".encode()).hexdigest(),
            base_version=base_version,
            changes=(
                dict(
                    operation="end",
                    content=None,
                    fact_kind="official_contact",
                    source_key=f"heldout:{variant}:expired",
                    expected_version=1,
                    effective_at=now - timedelta(minutes=1),
                    origin=dict(sheet_code="independent_f1_fixture", row_number=1),
                ),
            ),
        )
    )


@dataclass
class IndependentSchool:
    data: Dataset
    query: Any = field(repr=False)
    identity: Any = field(repr=False)
    tokens: dict[str, str] = field(repr=False)
    guard: Any = field(repr=False)
    writer_engine: Any = field(repr=False)
    writer_settings: Any = field(repr=False)
    metadata: dict[str, Any]

    def current(self, account="maintainer"):
        return self.identity.resolve_request(self.tokens[account])

    def view(self, account="maintainer", **selection):
        result = self.query.read_official_contacts(actor=self.current(account).actor, **selection)
        return contact_query_view(result)


@contextmanager
def build_independent_school(roles: PostgresRoles, variant="cedar") -> Iterator[IndependentSchool]:
    """No model invocation; caller must supply the inherited disposable PG16 fixture."""
    database_name = roles.runtime_connection_parameters["dbname"]
    if not str(database_name).endswith("_test"):
        raise ValueError("independent fixture requires a disposable test database")
    data = dataset(variant)
    school_id = uuid4()
    anchor_id = _root(roles, school_id)
    request = _request(roles, school_id, anchor_id)
    settings = _runtime_settings(roles, school_id, anchor_id)
    with ExitStack() as resources:
        runtime = build_engine(settings)
        resources.callback(runtime.dispose)
        verify_runtime_context_signing_key(runtime, settings)
        sessions = build_session_factory(runtime)
        for account in ENGINEERING_ACCOUNTS:
            with sessions() as session:
                find_or_create_namespaced_test_user(
                    session, account.external_subject, account.label
                )
        migration = _migration_engine(roles)
        resources.callback(migration.dispose)
        migration_sessions = sessionmaker(bind=migration, expire_on_commit=False)
        initializer = SchoolFactInitializer(session_factory=migration_sessions)
        prepared = initializer.prepare(request)
        assert prepared.credential is not None
        initialized = initializer.confirm(request, credential=prepared.credential)
        cli = runpy.run_path(str(PROJECT_ROOT / "scripts/school_facts_f1_init.py"))
        deployment_sessions = cli["migration_session_factory"](migration)
        governance = cli["configure_governance"](deployment_sessions, request, initializer)
        scope_id = governance["scope_id"]
        reader_settings = _role_settings(roles, school_id, anchor_id)
        assert isinstance(reader_settings, ExplicitReaderSettings)
        reader_engine = build_school_fact_reader_engine(reader_settings)
        resources.callback(reader_engine.dispose)
        verify_school_fact_reader_database(reader_engine, settings=reader_settings)
        reader_sessions = sessionmaker(bind=reader_engine, expire_on_commit=False)
        thin = create_product_thin_service(reader_sessions)
        guard = OfficialContactFinalAuthorizationGuard(thin_service=thin, scope_id=scope_id)
        query = _query(reader_sessions, thin, scope_id)
        redis = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        resources.callback(redis.close)
        assert redis.ping()

        def can_maintain(actor):
            try:
                return guard.require_current(actor).allowed
            except SchoolFactChangeRejected:
                return False

        identity = build_school_fact_product_identity_service(
            app_env="test",
            session_factory=sessions,
            redis=redis,
            proof_signer=ContextProofSigner.from_settings(settings),
            can_maintain=can_maintain,
        )
        tokens: dict[str, str] = {}

        def login(account):
            token = identity.login(account)
            resources.callback(identity.logout, token)
            contexts = identity.session_view(token).contexts
            assert len(contexts) == 1
            identity.select_context(token, contexts[0].ref)
            tokens[account] = token

        login("maintainer")
        writer_settings = _role_settings(roles, school_id, anchor_id, writer=True)
        assert isinstance(writer_settings, ExplicitWriterSettings)
        writer_engine = build_school_fact_writer_engine(writer_settings)
        resources.callback(writer_engine.dispose)
        verify_school_fact_writer_database(writer_engine, settings=writer_settings)
        initial_actor = identity.resolve_request(tokens["maintainer"]).actor
        receipts = []

        def apply(plan):
            approved_digest = canonical_plan_sha256(plan)

            def exact_plan_guard(session, context, candidate):
                observed = session.execute(text("SELECT current_database(),session_user")).one()
                if (
                    tuple(observed) != (database_name, "campus_ai_school_fact_writer")
                    or context.actor.school_id != school_id
                    or context.actor.account_user_id != initial_actor.account_user_id
                    or canonical_plan_sha256(candidate) != approved_digest
                ):
                    raise SchoolFactChangeRejected("test-only exact fixture plan rejected")
                # Retain the real grant/identity chain and the same epoch lock boundary.
                epoch = session.scalar(
                    text("SELECT app.lock_school_fact_governance_epoch(:s,:a)"),
                    {"s": school_id, "a": UUID(str(context.actor.assignment_id))},
                )
                authorization = guard.require_current(context.actor)
                if (
                    not isinstance(authorization.basis, ThinAuthorizationBasis)
                    or authorization.basis.governance.assignment_epoch != epoch
                ):
                    raise SchoolFactChangeRejected("test-only fixture authorization changed")

            kernel = SqlAlchemySchoolFactWriterExecutorKernel(
                engine=writer_engine,
                settings=writer_settings,
                final_authorization_guard=exact_plan_guard,
            )
            current = identity.resolve_request(tokens["maintainer"])
            context = compose_command_context(current.actor, uuid4(), f"heldout:{approved_digest}")
            preflight = kernel.preflight(
                school_id=current.actor.school_id,
                change_set=plan,
                context=context,
                session_binding=current.session_binding,
            )
            assert preflight.is_clean, tuple(issue.error_code for issue in preflight.issues)
            confirmation = kernel.prepare(
                change_set=plan,
                context=context,
                session_binding=current.session_binding,
                provenance=SchoolFactChangeProvenance(
                    channel="admin",
                    raw_source_sha256=plan.source_sha256,
                    template_version="independent_f1_test_v1",
                    mapping_version="independent_f1_test_v1",
                    mapping_config_sha256="d" * 64,
                    source_size_bytes=None,
                    worksheet_names=(),
                ),
            )
            result = kernel.confirm(
                credential=confirmation.credential,
                change_set=plan,
                context=context,
                session_binding=current.session_binding,
            )
            receipts.append(
                dict(
                    batch_id=str(result.batch_id),
                    changed_count=result.changed_count,
                    target_version=result.target_version,
                    plan_sha256=approved_digest,
                )
            )
            return result

        now = datetime.now(UTC)
        seed = _seed_plan(data, base_version=cast(int, initialized["target_version"]), now=now)
        result = apply(seed)
        terminal = _terminal_plan(variant, base_version=result.target_version, now=now)
        result = apply(terminal)
        login("unprivileged")
        yield IndependentSchool(
            data,
            query,
            identity,
            tokens,
            guard,
            writer_engine,
            writer_settings,
            dict(
                schema="20260905_22",
                database_name=database_name,
                cluster_system_identifier=roles.disposable_cluster_system_identifier,
                school_id=str(school_id),
                anchor_id=str(anchor_id),
                variant=variant,
                target_version=result.target_version,
                receipts=receipts,
                fixture_only=True,
                production_initialization_closure_claimed=False,
            ),
        )


@pytest.fixture(params=("cedar", "quartz"))
def independent_school(school_facts_f1_schema, request):
    with build_independent_school(school_facts_f1_schema, request.param) as school:
        yield school


@pytest.mark.integration
def test_independent_fixture_real_writer_reader_truth(independent_school):
    school = independent_school
    data = school.data
    primary = school.view(campus_name=data.campus_names[0])
    secondary = school.view(campus_name=data.campus_names[1])
    assert primary.status == secondary.status == "known"
    assert {item.contact_value for item in primary.items} == set(data.primary_values)
    assert {item.contact_value for item in secondary.items} == set(data.secondary_values)
    assert all(item.valid_from <= primary.queried_at for item in primary.items)
    assert all(item.source.reference.startswith("publication:") for item in primary.items)
    assert all(value not in primary.model_dump_json() for value in data.excluded_values)
    missing = school.view(campus_name=data.campus_names[2])
    assert missing.status == "no_current_records" and not missing.items
    assert missing.coverage.status == "unknown"
    all_candidates = school.view()
    assert all_candidates.status == "selection_required" and not all_candidates.items
    assert len(all_candidates.candidates) == len(data.campus_names) + 2
    campus_inventory = school.view(subject="campuses", scope="all_authorized")
    assert campus_inventory.status == "known" and not campus_inventory.items
    assert len(campus_inventory.resolved_campuses) == len(data.campus_names) + 2
    assert campus_inventory.listing.matched_count == len(data.campus_names) + 2
    assert not campus_inventory.listing.truncated
    assert campus_inventory.coverage.status == "unknown"
    contact_inventory = school.view(scope="all_authorized")
    assert contact_inventory.status == "known"
    assert len(contact_inventory.items) == 7
    assert all(value not in contact_inventory.model_dump_json() for value in data.excluded_values)
    assert school.view(campus_name="合成并无匹配校区").status == "unmatched"
    duplicate = school.view(campus_name=data.duplicate_name)
    assert duplicate.status == "ambiguous" and len(duplicate.candidates) == 2
    assert len({item.reference for item in duplicate.candidates}) == 2
    assert len({item.path for item in duplicate.candidates}) == 2
    for candidate in duplicate.candidates:
        expected_index = next(
            index for index in (0, 1) if candidate.path == f"独立测试/{index}/{data.duplicate_name}"
        )
        selected = school.view(campus_ref=candidate.reference)
        assert selected.status == "known"
        assert {item.contact_value for item in selected.items} == {
            f"{data.variant}-duplicate-{expected_index}@example.invalid"
        }

    denied = school.view("unprivileged", campus_name=data.campus_names[0])
    assert denied.status == "denied" and not denied.items and not denied.candidates
    assert school.current().actor.person_id != school.current("unprivileged").actor.person_id


@dataclass
class MetadataRecorder:
    """Allowlist records. Unknown arbitrary text is hashed, never copied to evidence."""

    permitted_values: set[str]
    records: list[dict[str, Any]] = field(default_factory=list)

    def _value(self, value):
        if value is None or type(value) in {bool, int}:
            return value
        if isinstance(value, str) and value in self.permitted_values | {
            "official_contacts",
            "campuses",
            "selected",
            "all_authorized",
            "email",
            "phone",
        }:
            return value
        return {"sha256": sha256(str(value).encode()).hexdigest()}

    def record(self, call, result):
        document = result.model_content
        items = document.get("items", ())
        candidates = document.get("candidates", ())
        entry = dict(
            tool=call.name
            if call.name
            in {
                QUERY_TOOL,
                "school_facts.official_contacts.prepare",
                "school_facts.official_contacts.receipt",
            }
            else "unknown_tool",
            argument_keys=sorted(
                key
                if key
                in {
                    "campus_name",
                    "campus_ref",
                    "subject",
                    "scope",
                    "contact_ref",
                    "contact_value",
                    "service_hours",
                    "reason",
                    "proposal_ref",
                }
                else "unknown_argument"
                for key in call.arguments
            ),
            arguments={
                key: self._value(value)
                for key, value in call.arguments.items()
                if key
                in {
                    "campus_name",
                    "campus_ref",
                    "subject",
                    "scope",
                    "contact_ref",
                    "contact_value",
                    "service_hours",
                }
            },
            result_status=document.get("status")
            if document.get("status")
            in {
                "known",
                "ambiguous",
                "not_recorded",
                "no_current_records",
                "selection_required",
                "unmatched",
                "denied",
                "source_unavailable",
                "clarification_required",
                "awaiting_explicit_confirmation",
                "rejected",
            }
            else "other",
            coverage_status=document.get("coverage", {}).get("status")
            if document.get("coverage", {}).get("status") in {"unknown", "partial", "complete"}
            else None,
            item_count=len(items),
            candidate_count=len(candidates),
            items=[
                {
                    key: self._value(item.get(key))
                    for key in (
                        "reference",
                        "campus_name",
                        "contact_kind",
                        "contact_value",
                        "service_hours",
                        "version",
                    )
                }
                for item in items
            ],
            candidates=[
                {key: self._value(item.get(key)) for key in ("reference", "label")}
                for item in candidates
            ],
        )
        self.records.append(entry)


def validate_trace(records, *, allowed_selections, minimum_queries=1, maximum_queries=4):
    errors = []
    if not minimum_queries <= len(records) <= maximum_queries:
        errors.append("query_count")
    for record in records:
        if record["tool"] != QUERY_TOOL:
            errors.append("unexpected_tool")
        arguments = {
            key: value
            for key, value in record["arguments"].items()
            if not (key in {"campus_name", "campus_ref"} and value is None)
        }
        normalized = {"subject": "official_contacts", "scope": "selected", **arguments}
        allowed = [
            {"subject": "official_contacts", "scope": "selected", **item}
            for item in allowed_selections
        ]
        if normalized not in allowed:
            errors.append("wrong_selection")
        if set(record["argument_keys"]) - {"campus_name", "campus_ref", "subject", "scope"}:
            errors.append("unexpected_argument")
    return sorted(set(errors))


def test_independent_data_layouts_change_names_count_order_and_time():
    now = datetime(2026, 9, 6, tzinfo=UTC)
    plans = [_seed_plan(dataset(name), base_version=2, now=now) for name in ("cedar", "quartz")]
    assert len(plans[0].changes) != len(plans[1].changes)
    for name, plan in zip(("cedar", "quartz"), plans, strict=True):
        data = dataset(name)
        locations = [change for change in plan.changes if change.fact_kind == "location"]
        assert len(locations) == len(data.campus_names) + 2
        assert locations[0].content is not None
        assert locations[0].content.display_name != data.campus_names[0]
        assert sum(change.effective_at > now for change in plan.changes) == 1
        assert (
            sum(
                change.content is not None and change.content.visibility_scope == "restricted"
                for change in plan.changes
            )
            == 1
        )
        assert len(data.primary_values) != len(data.secondary_values)
        terminal = _terminal_plan(name, base_version=3, now=now)
        assert len(terminal.changes) == 1
        assert terminal.changes[0].operation == "end"
        assert terminal.changes[0].effective_at < now


def test_metadata_recorder_omits_dialogue_thinking_private_and_arbitrary_text():
    from types import SimpleNamespace

    sentinel = "PRIVATE_FULL_DIALOGUE_OR_THINKING_SENTINEL"
    recorder = MetadataRecorder({"合成测试校区", "email", "known"})
    recorder.record(
        SimpleNamespace(name=QUERY_TOOL, arguments={"campus_name": "合成测试校区"}),
        SimpleNamespace(
            model_content=dict(
                status="known",
                message=sentinel,
                reasoning_content=sentinel,
                credential=sentinel,
                items=[dict(contact_value=sentinel)],
            )
        ),
    )
    payload = json.dumps(recorder.records, ensure_ascii=False)
    assert (
        sentinel not in payload
        and "credential" not in payload
        and "reasoning_content" not in payload
    )
    assert (
        validate_trace(recorder.records, allowed_selections=[{"campus_name": "合成测试校区"}]) == []
    )
    assert validate_trace(recorder.records, allowed_selections=[{}]) == ["wrong_selection"]
    recorder.records[0]["tool"] = "school_facts.official_contacts.prepare"
    assert "unexpected_tool" in validate_trace(
        recorder.records, allowed_selections=[{"campus_name": "合成测试校区"}]
    )


class RecordingGateway:
    """Transparent test observer, preserving the exact current production gateway."""

    def __init__(self, delegate, recorder):
        self.delegate = delegate
        self.recorder = recorder

    @property
    def descriptors(self):
        return self.delegate.descriptors

    def for_turn(self, actor_context_ref):
        return RecordingGateway(self.delegate.for_turn(actor_context_ref), self.recorder)

    def revalidate(self, actor_context_ref):
        return self.delegate.revalidate(actor_context_ref)

    def execute(self, call, context):
        result = self.delegate.execute(call, context)
        self.recorder.record(call, result)
        return result


def test_recording_gateway_preserves_authorization_and_result_identity():
    from types import SimpleNamespace

    observed = []
    expected = SimpleNamespace(model_content={"status": "denied"})

    class Delegate:
        descriptors = ()

        def revalidate(self, actor_context_ref):
            observed.append(("revalidate", actor_context_ref))

        def execute(self, call, context):
            observed.append(("execute", context))
            return expected

    recorder = MetadataRecorder(set())
    wrapper = RecordingGateway(Delegate(), recorder)
    wrapper.revalidate("test-actor")
    assert (
        wrapper.execute(SimpleNamespace(name=QUERY_TOOL, arguments={}), "test-context") is expected
    )
    assert observed == [("revalidate", "test-actor"), ("execute", "test-context")]
    assert recorder.records[0]["result_status"] == "denied"


def test_metadata_oracle_distinguishes_subject_scope_and_unknown_parameters():
    from types import SimpleNamespace

    recorder = MetadataRecorder(set())
    recorder.record(
        SimpleNamespace(
            name=QUERY_TOOL, arguments={"subject": "campuses", "scope": "all_authorized"}
        ),
        SimpleNamespace(model_content={"status": "known"}),
    )
    assert recorder.records[0]["arguments"] == {"subject": "campuses", "scope": "all_authorized"}
    assert validate_trace(recorder.records, allowed_selections=[{}]) == ["wrong_selection"]
    assert (
        validate_trace(
            recorder.records,
            allowed_selections=[{"subject": "campuses", "scope": "all_authorized"}],
        )
        == []
    )
    sentinel = "UNTRUSTED_ARGUMENT_NAME_WITH_FULL_TEXT"
    recorder.record(
        SimpleNamespace(name=sentinel, arguments={sentinel: sentinel}),
        SimpleNamespace(model_content={"status": "denied"}),
    )
    assert sentinel not in json.dumps(recorder.records)
    assert {"unexpected_tool", "unexpected_argument"}.issubset(
        set(validate_trace(recorder.records, allowed_selections=[{}]))
    )
