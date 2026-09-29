from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from app.composition import school_fact_persistence as persistence_module
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.db.rls_context import DatabaseExecutionContextEnvelope
from app.platform import contracts as platform_contracts
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.school_facts.application import (
    SchoolFactChangeRejected,
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.ports import (
    ConfirmationCredentialBinding,
    FactVersionProjection,
    PreparedConfirmation,
    PreparedConfirmationResult,
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactSchoolReactivationProjection,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
SCHOOL = UUID("00000000-0000-0000-0000-000000000001")


def _context(*, school: UUID = SCHOOL, user: int = 9) -> CommandExecutionContext:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=user,
        school_id=SchoolId(school),
        person_id=platform_contracts.PersonId(UUID("00000000-0000-0000-0000-000000000002")),
        assignment_id=platform_contracts.AssignmentId(UUID("00000000-0000-0000-0000-000000000003")),
        assignment_version=2,
        authorization_version=5,
    )
    return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=UUID("00000000-0000-0000-0000-000000000004"),
        idempotency_key="sf2-confirm-click",
    )


def _change_set() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "schema_version": 1,
            "source_sha256": "c" * 64,
            "base_version": 4,
            "changes": (
                {
                    "schema_version": 1,
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:demo",
                    "expected_version": 4,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "schools", "row_number": 2},
                    "content": {
                        "business_code": "XJ-002",
                        "display_name": "小静学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )


def _school_reactivation_change_set() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "schema_version": 1,
            "source_sha256": "d" * 64,
            "base_version": 4,
            "changes": (
                {
                    "schema_version": 1,
                    "operation": "reactivate",
                    "fact_kind": "school",
                    "source_key": "school:demo",
                    "expected_version": 4,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "schools", "row_number": 7},
                    "content": {
                        "business_code": "XJ-002",
                        "display_name": "小静学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )


@dataclass
class _Reader:
    def current_school_fact_version(self, school_id: SchoolId) -> int:
        del school_id
        return 4

    def find_fact_version(
        self, school_id: SchoolId, fact_kind: str, source_key: str
    ) -> FactVersionProjection | None:
        del school_id, fact_kind, source_key
        return FactVersionProjection(version=4)

    def unresolved_reference_codes(
        self, school_id: SchoolId, change: CanonicalFactChangeV1
    ) -> tuple[str, ...]:
        del school_id, change
        return ()

    def find_applied_plan(self, school_id: SchoolId, source_sha256: str, plan_sha256: str) -> None:
        del school_id, source_sha256, plan_sha256
        return None


@dataclass
class _Writer:
    prepared: list[PreparedConfirmation] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        self.prepared.append(prepared)
        return PreparedConfirmationResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000005"),
            credential="a" * 43,
            expires_at=prepared.expires_at,
        )

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult:
        del change_set, context, session_sha256, now
        self.applied.append(credential_sha256)
        return SchoolFactApplyResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000005"),
            target_version=5,
            changed_count=1,
            replayed=len(self.applied) > 1,
        )


@dataclass
class _SchoolReactivationReader:
    projection: SchoolFactSchoolReactivationProjection
    bound_contexts: list[CommandExecutionContext] = field(default_factory=list)
    narrow_reads: int = 0

    def for_request(self, context: CommandExecutionContext) -> _SchoolReactivationReader:
        self.bound_contexts.append(context)
        return self

    def preflight_school_reactivation(
        self,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        plan_sha256: str,
    ) -> SchoolFactSchoolReactivationProjection:
        assert school_id == SchoolId(SCHOOL)
        assert change_set == _school_reactivation_change_set()
        assert len(plan_sha256) == 64
        self.narrow_reads += 1
        return self.projection

    def find_applied_plan(
        self,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
    ) -> None:
        del school_id, source_sha256, plan_sha256
        raise AssertionError("停用学校预检不得进入普通结果查询")

    def current_school_fact_version(self, school_id: SchoolId) -> int:
        del school_id
        raise AssertionError("停用学校预检不得进入普通学校版本查询")

    def find_fact_version(
        self,
        school_id: SchoolId,
        fact_kind: str,
        source_key: str,
    ) -> FactVersionProjection | None:
        del school_id, fact_kind, source_key
        raise AssertionError("停用学校预检不得进入普通事实版本查询")

    def unresolved_reference_codes(
        self,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
    ) -> tuple[str, ...]:
        del school_id, change
        raise AssertionError("停用学校预检不得进入普通引用查询")


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


@dataclass
class _NarrowPrepareSession:
    closed: bool = False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("停用学校 prepare 不得执行普通 ORM/SQL 路径")

    def add(self, *_args: object) -> None:
        raise AssertionError("停用学校 prepare 不得由普通 ORM 插入")

    def flush(self) -> None:
        raise AssertionError("停用学校 prepare 不得由普通 ORM flush")

    def close(self) -> None:
        self.closed = True


class _DeterministicCredentialIssuer:
    credential = "z" * 43

    def issue(self, binding: ConfirmationCredentialBinding) -> str:
        del binding
        return self.credential


def _command_envelope() -> DatabaseExecutionContextEnvelope:
    context = _context()
    actor = cast(StaffActorContext, context.actor)
    return DatabaseExecutionContextEnvelope(
        school_id=UUID(str(actor.school_id)),
        account_user_id=actor.account_user_id,
        person_id=UUID(str(actor.person_id)),
        context_kind="assignment",
        assignment_id=UUID(str(actor.assignment_id)),
        assignment_version=actor.assignment_version,
        guardian_relationship_id=None,
        guardian_relationship_version=None,
        authorization_version=actor.authorization_version,
        request_id=context.request_id,
        context_purpose="command",
        idempotency_key_hash=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
        session_nonce_hash="e" * 64,
        proof_expires_at=NOW + timedelta(hours=1),
        server_proof="f" * 64,
    )


def _prepared_school_reactivation() -> PreparedConfirmation:
    context = _context()
    actor = cast(StaffActorContext, context.actor)
    change_set = _school_reactivation_change_set()
    return PreparedConfirmation(
        school_id=actor.school_id,
        actor_user_id=actor.account_user_id,
        actor_person_id=UUID(str(actor.person_id)),
        actor_assignment_id=UUID(str(actor.assignment_id)),
        actor_assignment_version=actor.assignment_version,
        authorization_version=actor.authorization_version,
        request_id=context.request_id,
        idempotency_key_sha256=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
        plan_sha256=canonical_plan_sha256(change_set),
        source_sha256=change_set.source_sha256,
        base_version=change_set.base_version,
        session_sha256=sha256(b"server-session-A").hexdigest(),
        expires_at=NOW + timedelta(minutes=30),
        change_set=change_set,
        provenance=SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=change_set.source_sha256,
            template_version="canonical_v1",
            mapping_version="manual_v1",
            mapping_config_sha256=sha256(b"school_facts:manual_v1:canonical-contract").hexdigest(),
            source_size_bytes=None,
            worksheet_names=(),
        ),
    )


def test_prepare_school_reactivation_uses_only_the_narrow_database_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _NarrowPrepareSession()
    envelope = _command_envelope()
    calls: list[dict[str, object]] = []

    def narrow_prepare(
        called_session: object,
        called_envelope: DatabaseExecutionContextEnvelope,
        **parameters: object,
    ) -> SimpleNamespace:
        assert called_session is session
        assert called_envelope == envelope
        calls.append(parameters)
        return SimpleNamespace(
            batch_id=parameters["proposed_batch_id"],
            confirmation_token_sha256=parameters["confirmation_token_sha256"],
            confirmation_expires_at=parameters["confirmation_expires_at"],
            reused=False,
        )

    monkeypatch.setattr(
        persistence_module,
        "school_fact_school_reactivation_prepare",
        narrow_prepare,
        raising=False,
    )
    persistence = SqlAlchemySchoolFactPersistence(  # type: ignore[arg-type]
        session_factory=cast(Any, lambda: session),
        envelope_factory=lambda _context: envelope,
        confirmation_credential_issuer=_DeterministicCredentialIssuer(),
    ).for_request(_context())

    result = persistence.prepare(_prepared_school_reactivation())

    assert result.credential == _DeterministicCredentialIssuer.credential
    assert result.expires_at == NOW + timedelta(minutes=30)
    assert session.closed is True
    assert len(calls) == 1
    call = calls[0]
    assert call["fact_source_key"] == "school:demo"
    assert call["expected_version"] == 4
    assert call["base_fact_version"] == 4
    assert call["source_sha256"] == "d" * 64
    assert call["plan_sha256"] == canonical_plan_sha256(_school_reactivation_change_set())
    assert call["template_version"] == "canonical_v1"
    assert call["mapping_version"] == "manual_v1"
    assert call["origin_sheet_code"] == "schools"
    assert call["origin_row_number"] == 7
    assert (
        call["confirmation_token_sha256"]
        == sha256(_DeterministicCredentialIssuer.credential.encode("ascii")).hexdigest()
    )


def test_prepare_binds_server_context_session_and_exact_thirty_minute_expiry() -> None:
    writer = _Writer()
    service = SchoolFactChangeService(reader=_Reader(), writer=writer, clock=lambda: NOW)

    receipt = service.prepare_confirmation(
        change_set=_change_set(),
        context=_context(),
        session_binding="server-session-A",
    )

    assert receipt.expires_at == NOW + timedelta(minutes=30)
    assert receipt.credential
    assert len(writer.prepared) == 1
    staged = writer.prepared[0]
    assert staged.school_id == SchoolId(SCHOOL)
    assert staged.actor_user_id == 9
    assert staged.plan_sha256 == receipt.plan_sha256
    assert staged.source_sha256 == "c" * 64
    assert staged.base_version == 4
    assert staged.session_sha256 != "server-session-A"
    assert receipt.credential == "a" * 43


def test_school_reactivation_preflight_uses_only_the_narrow_projection() -> None:
    reader = _SchoolReactivationReader(
        projection=SchoolFactSchoolReactivationProjection(
            replay_result=None,
            current_fact_version=4,
            current_target_version=4,
        )
    )
    writer = _Writer()
    service = SchoolFactChangeService(reader=reader, writer=writer, clock=lambda: NOW)

    receipt = service.prepare_confirmation(
        change_set=_school_reactivation_change_set(),
        context=_context(),
        session_binding="server-session-A",
    )

    assert receipt.credential == "a" * 43
    assert reader.bound_contexts == [_context()]
    assert reader.narrow_reads == 1
    assert len(writer.prepared) == 1


@pytest.mark.parametrize(
    ("projection", "expected_issue"),
    (
        (
            SchoolFactSchoolReactivationProjection(
                replay_result=None,
                current_fact_version=3,
                current_target_version=4,
            ),
            ("base_version", "school.version_conflict"),
        ),
        (
            SchoolFactSchoolReactivationProjection(
                replay_result=None,
                current_fact_version=4,
                current_target_version=3,
            ),
            ("expected_version", "fact.version_conflict"),
        ),
        (
            SchoolFactSchoolReactivationProjection(
                replay_result=None,
                current_fact_version=4,
                current_target_version=None,
            ),
            ("source_key", "fact.not_found"),
        ),
    ),
)
def test_school_reactivation_preflight_preserves_existing_version_issue_codes(
    projection: SchoolFactSchoolReactivationProjection,
    expected_issue: tuple[str, str],
) -> None:
    service = SchoolFactChangeService(
        reader=_SchoolReactivationReader(projection=projection),
        writer=_Writer(),
        clock=lambda: NOW,
    )

    report = service.preflight(
        school_id=SchoolId(SCHOOL),
        change_set=_school_reactivation_change_set(),
        context=_context(),
        session_binding="server-session-A",
    )

    assert report.is_clean is False
    assert [(issue.column_code, issue.error_code) for issue in report.issues] == [expected_issue]


@pytest.mark.parametrize("session_binding", ["", " ", "A" * 257])
def test_prepare_rejects_invalid_server_session_binding(session_binding: str) -> None:
    service = SchoolFactChangeService(reader=_Reader(), writer=_Writer(), clock=lambda: NOW)
    with pytest.raises(SchoolFactChangeRejected):
        service.prepare_confirmation(
            change_set=_change_set(),
            context=_context(),
            session_binding=session_binding,
        )


def test_confirm_passes_only_credential_digest_and_returns_writer_result() -> None:
    writer = _Writer()
    service = SchoolFactChangeService(reader=_Reader(), writer=writer, clock=lambda: NOW)
    receipt = service.prepare_confirmation(
        change_set=_change_set(), context=_context(), session_binding="session-A"
    )

    result = service.confirm(
        credential=receipt.credential,
        change_set=_change_set(),
        context=_context(),
        session_binding="session-A",
    )

    assert result.target_version == 5
    assert writer.applied == [sha256(receipt.credential.encode("ascii")).hexdigest()]


def test_forged_credential_is_not_mapped_to_a_safe_default() -> None:
    writer = _Writer()
    service = SchoolFactChangeService(reader=_Reader(), writer=writer, clock=lambda: NOW)
    with pytest.raises(SchoolFactChangeRejected):
        service.confirm(
            credential="forged token with spaces",
            change_set=_change_set(),
            context=_context(),
            session_binding="session-A",
        )
    assert not writer.applied
