from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from threading import Event
from typing import Literal
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pydantic import ValidationError

from app.composition.school_facts import (
    InMemoryPrivateSchoolFactSourceStore,
    InMemorySchoolFactPlanStore,
    SchoolFactEphemeralStoreJanitor,
    school_fact_candidate_lifespan,
)
from app.core.request_id import assign_request_id
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.interfaces.http.school_fact_schemas import (
    SchoolFactChangeSummaryResponse,
    SchoolFactFieldDiffResponse,
    SchoolFactIssueResponse,
    SchoolFactPlanResponse,
    SchoolFactStatusDecisionResponse,
)
from app.interfaces.http.school_facts import SchoolFactRequestIdentity, create_school_fact_router
from app.platform import contracts as platform_contracts
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.ports import (
    PrivateImportSourceCapacityExceeded,
    PrivateSourceOwner,
)
from app.platform.master_data_imports.school_facts_excel import (
    SCHOOL_FACT_SHEET_SPECS,
    SCHOOL_FACT_TEMPLATE_VERSION,
)
from app.platform.master_data_imports.source_mapping import (
    ResolvedSourceMappingProfile,
    SourceColumnMapping,
    SourceMappingProfile,
    SourceSheetMapping,
    source_mapping_profile_sha256,
)
from app.platform.master_data_imports.status_mappings import (
    DEFAULT_STATUS_MAPPING_PROFILE,
    StatusMappingEntry,
    StatusMappingProfile,
)
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    PreflightIssueV1,
    PreflightReportV1,
)
from app.platform.school_facts.ports import (
    FactVersionProjection,
    PreparedConfirmation,
    PreparedConfirmationResult,
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactResultLookup,
    SchoolFactRowProvenance,
    SchoolFactSchoolReactivationProjection,
    SchoolFactSourceArtifact,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)
SCHOOL = SchoolId(UUID("00000000-0000-0000-0000-000000000001"))
SCHOOL_UUID = UUID(str(SCHOOL))


@dataclass
class _Reader:
    def current_school_fact_version(self, school_id: SchoolId) -> int:
        del school_id
        return 1

    def find_fact_version(
        self, school_id: SchoolId, fact_kind: str, source_key: str
    ) -> FactVersionProjection | None:
        del school_id, fact_kind, source_key
        return None

    def unresolved_reference_codes(
        self, school_id: SchoolId, change: CanonicalFactChangeV1
    ) -> tuple[str, ...]:
        del school_id, change
        return ()

    def find_applied_plan(self, school_id: SchoolId, source_sha256: str, plan_sha256: str) -> None:
        del school_id, source_sha256, plan_sha256


@dataclass
class _Writer:
    prepared: list[PreparedConfirmation] = field(default_factory=list)

    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        self.prepared.append(prepared)
        return PreparedConfirmationResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000010"),
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
        del credential_sha256, change_set, context, session_sha256, now
        return SchoolFactApplyResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000010"),
            target_version=2,
            changed_count=1,
            replayed=False,
        )


def _change_set() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": "a" * 64,
            "base_version": 1,
            "changes": (
                {
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:demo",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "schools", "row_number": 2},
                    "content": {
                        "business_code": "XJ-001",
                        "display_name": "候选学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )


def _context() -> CommandExecutionContext:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=9,
        school_id=SCHOOL,
        person_id=platform_contracts.PersonId(UUID("00000000-0000-0000-0000-000000000002")),
        assignment_id=platform_contracts.AssignmentId(UUID("00000000-0000-0000-0000-000000000003")),
        assignment_version=2,
        authorization_version=5,
    )
    return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=UUID("00000000-0000-0000-0000-000000000004"),
        idempotency_key="sf3-confirm-click",
    )


def _excel_provenance() -> SchoolFactChangeProvenance:
    return SchoolFactChangeProvenance(
        channel="excel",
        raw_source_sha256="b" * 64,
        template_version=SCHOOL_FACT_TEMPLATE_VERSION,
        mapping_version="school_statuses_v1",
        mapping_config_sha256="c" * 64,
        source_size_bytes=4096,
        worksheet_names=("00_导入说明", "01_学校"),
        source_manifest=(
            SchoolFactSourceArtifact(
                logical_source_code="formal_workbook",
                source_sha256="b" * 64,
                size_bytes=4096,
            ),
        ),
    )


def test_sf3_provenance_flows_through_the_only_sf2_prepare_port() -> None:
    writer = _Writer()
    service = SchoolFactChangeService(reader=_Reader(), writer=writer, clock=lambda: NOW)
    provenance = _excel_provenance()

    service.prepare_confirmation(
        change_set=_change_set(),
        context=_context(),
        session_binding="server-session",
        provenance=provenance,
    )

    assert writer.prepared[0].provenance == provenance


def test_retired_plan_counts_toward_quota_then_expires_without_full_payload() -> None:
    current = [NOW]
    store = InMemorySchoolFactPlanStore(
        clock=lambda: current[0],
        max_records=1,
        max_owner_records=1,
    )
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )
    change_set = _change_set()
    preflight = PreflightReportV1(
        plan_sha256="e" * 64,
        is_clean=True,
        issues=(),
    )
    first = store.put(
        owner=owner,
        change_set=change_set,
        provenance=_excel_provenance(),
        preflight=preflight,
    )

    store.retire(first.plan_ref, owner=owner)

    with pytest.raises(KeyError):
        store.get(first.plan_ref, owner=owner)
    assert store.locate(first.plan_ref, owner=owner).plan_sha256 == "e" * 64
    with pytest.raises(PrivateImportSourceCapacityExceeded):
        store.put(
            owner=owner,
            change_set=change_set,
            provenance=_excel_provenance(),
            preflight=preflight,
        )

    current[0] = NOW + timedelta(hours=24)
    assert store.purge_expired() == 1
    replacement = store.put(
        owner=owner,
        change_set=change_set,
        provenance=_excel_provenance(),
        preflight=preflight,
    )
    assert replacement.plan_ref != first.plan_ref


def test_plan_quota_counts_preflight_provenance_and_source_refs_not_only_changes() -> None:
    change_set = _change_set()
    change_bytes = len(change_set.model_dump_json(exclude_none=True).encode("utf-8"))
    store = InMemorySchoolFactPlanStore(
        clock=lambda: NOW,
        max_total_bytes=change_bytes + 1,
        max_owner_bytes=change_bytes + 1,
    )
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )

    with pytest.raises(PrivateImportSourceCapacityExceeded):
        store.put(
            owner=owner,
            change_set=change_set,
            provenance=_excel_provenance(),
            preflight=PreflightReportV1(
                plan_sha256="e" * 64,
                is_clean=True,
                issues=(),
            ),
            source_refs=("sf3src_private_bearer_reference",),
        )


def _maximum_lineage_policy_plan() -> tuple[
    CanonicalFactChangeSetV1,
    SchoolFactChangeProvenance,
]:
    changes = tuple(
        CanonicalFactChangeV1.model_validate(
            {
                "operation": "upsert",
                "fact_kind": "policy_version",
                "source_key": f"policy-version:plan-capacity:{index:05d}",
                "expected_version": 0,
                "effective_at": NOW,
                "origin": {
                    "sheet_code": f"policy_capacity_{(index - 1) // 10_000 + 1}",
                    "row_number": (index - 1) % 10_000 + 2,
                },
                "content": {
                    "policy_source_key": f"policy:plan-capacity:{index:05d}",
                    "business_code": f"PV-{index:05d}",
                    "display_name": f"制度版本{index:05d}",
                    "category_code": "general",
                    "classification": "internal",
                    "publishing_organization_source_key": "organization:school-office",
                    "published_at": NOW,
                    "effective_from": NOW,
                    "effective_to": NOW + timedelta(days=365),
                    "content_text": "候选实例制度正文",
                    "version_note": "首版",
                    "supersedes_policy_version_source_key": "policy-version:previous",
                },
            }
        )
        for index in range(1, 50_001)
    )
    change_set = CanonicalFactChangeSetV1(
        source_sha256="9" * 64,
        base_version=1,
        changes=changes,
    )
    provenance = replace(
        _excel_provenance(),
        source_size_bytes=10 * 1024 * 1024,
        row_lineage=tuple(
            SchoolFactRowProvenance(
                fact_kind="policy_version",
                fact_source_key=change.source_key,
                raw_source_sha256="b" * 64,
                source_slot="formal_workbook",
                source_sheet_code=f"policy_capacity_{(index - 1) // 10_000 + 1}",
                source_row_number=(index - 1) % 10_000 + 2,
                target_field_codes=(
                    "policy_source_key",
                    "business_code",
                    "display_name",
                    "category_code",
                    "classification",
                    "publishing_organization_source_key",
                    "published_at",
                    "effective_from",
                    "effective_to",
                    "content_text",
                    "version_note",
                    "supersedes_policy_version_source_key",
                ),
                source_field_codes=(
                    "policy_ref",
                    "business_code",
                    "display_name",
                    "category_code",
                    "classification",
                    "publisher_ref",
                    "published_at",
                    "effective_from",
                    "effective_to",
                    "content_text",
                    "version_note",
                    "supersedes_ref",
                ),
            )
            for index, change in enumerate(changes, start=1)
        ),
    )
    return change_set, provenance


def test_default_plan_quota_accepts_one_complete_50000_lineage_plan_and_bounds_second() -> None:
    store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )
    change_set, provenance = _maximum_lineage_policy_plan()
    preflight = PreflightReportV1(
        plan_sha256="e" * 64,
        is_clean=True,
        issues=(),
    )

    first = store.put(
        owner=owner,
        change_set=change_set,
        provenance=provenance,
        preflight=preflight,
    )

    assert store.get(first.plan_ref, owner=owner).change_set == change_set
    with pytest.raises(PrivateImportSourceCapacityExceeded):
        store.put(
            owner=owner,
            change_set=change_set,
            provenance=provenance,
            preflight=preflight,
        )


def test_plan_store_keeps_preflight_issues_only_in_the_immediate_response_record() -> None:
    store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )
    issue = PreflightIssueV1(
        sheet_code="schools",
        row_number=2,
        column_code="source_key",
        error_code="fact.not_found",
    )

    immediate = store.put(
        owner=owner,
        change_set=_change_set(),
        provenance=_excel_provenance(),
        preflight=PreflightReportV1(
            plan_sha256="e" * 64,
            is_clean=False,
            issues=(issue,),
        ),
    )

    assert immediate.preflight.issues == (issue,)
    retained = store.get(immediate.plan_ref, owner=owner)
    assert retained.preflight.is_clean is False
    assert retained.preflight.issues == ()


def test_plan_response_arrays_have_distinct_mechanical_capacity_limits() -> None:
    issue = SchoolFactIssueResponse(
        source_slot="formal_workbook",
        sheet_code="student_bed_residences",
        row_number=2,
        column_code="student_source_key",
        error_code="fact.reference_not_found",
    )
    change = SchoolFactChangeSummaryResponse(
        operation="upsert",
        fact_kind="policy",
        source_key="policy:capacity",
        expected_version=0,
        sheet_code="policies",
        row_number=2,
        fields=(SchoolFactFieldDiffResponse(field_code="display_name", new_value="制度"),),
    )
    decision = SchoolFactStatusDecisionResponse(
        source_slot="formal_workbook",
        sheet_code="assignments",
        row_number=2,
        field_code="status",
        original_status_value="在岗",
        standard_value="confirmed",
        mapping_version="school_statuses_v1",
    )
    base = {
        "plan_ref": "sf3plan_capacity",
        "plan_sha256": "e" * 64,
        "is_clean": False,
        "issues": (),
        "changes": (change,),
        "status_decisions": (),
    }

    assert (
        len(SchoolFactPlanResponse.model_validate({**base, "issues": (issue,) * 250_001}).issues)
        == 250_001
    )
    with pytest.raises(ValidationError):
        SchoolFactPlanResponse.model_validate({**base, "issues": (issue,) * 250_002})
    assert (
        len(SchoolFactPlanResponse.model_validate({**base, "changes": (change,) * 50_000}).changes)
        == 50_000
    )
    with pytest.raises(ValidationError):
        SchoolFactPlanResponse.model_validate({**base, "changes": (change,) * 50_001})
    assert (
        len(
            SchoolFactPlanResponse.model_validate(
                {**base, "status_decisions": (decision,) * 100_000}
            ).status_decisions
        )
        == 100_000
    )
    with pytest.raises(ValidationError):
        SchoolFactPlanResponse.model_validate({**base, "status_decisions": (decision,) * 100_001})


def test_plan_owner_and_global_record_quota_are_atomic_under_concurrency() -> None:
    store = InMemorySchoolFactPlanStore(
        clock=lambda: NOW,
        max_records=1,
        max_owner_records=1,
    )
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )

    def stage(_index: int) -> object:
        try:
            return store.put(
                owner=owner,
                change_set=_change_set(),
                provenance=_excel_provenance(),
                preflight=PreflightReportV1(
                    plan_sha256="e" * 64,
                    is_clean=True,
                    issues=(),
                ),
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(stage, (1, 2)))

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, PrivateImportSourceCapacityExceeded) for item in outcomes) == 1


def test_candidate_lifecycle_physically_purges_expired_bytes_and_plan_without_requests() -> None:
    current = [NOW]
    source_store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: current[0])
    plan_store = InMemorySchoolFactPlanStore(clock=lambda: current[0])
    owner = PrivateSourceOwner(
        school_id=str(SCHOOL),
        actor_user_id=9,
        session_sha256="d" * 64,
    )
    source = source_store.put(
        filename_summary="private-source.xlsx",
        content=_formal_workbook(),
        owner=owner,
    )
    retained_bytes = source_store._records[source.opaque_ref].content  # pyright: ignore[reportPrivateUsage]
    plan_store.put(
        owner=owner,
        change_set=_change_set(),
        provenance=_excel_provenance(),
        preflight=PreflightReportV1(
            plan_sha256="e" * 64,
            is_clean=True,
            issues=(),
        ),
        source_refs=(source.opaque_ref,),
    )
    swept = Event()

    def observe_sweep(removed: int) -> None:
        if removed >= 2:
            swept.set()

    with SchoolFactEphemeralStoreJanitor(
        source_store,
        plan_store,
        interval_seconds=0.01,
        on_sweep=observe_sweep,
    ) as janitor:
        current[0] = NOW + timedelta(hours=24)
        assert swept.wait(timeout=1)

    assert janitor.failure_count == 0
    assert bytes(retained_bytes) == b"\x00" * len(retained_bytes)
    assert source_store._records == {}  # pyright: ignore[reportPrivateUsage]
    assert plan_store._records == {}  # pyright: ignore[reportPrivateUsage]


def test_janitor_isolates_callback_failure_and_keeps_sweeping() -> None:
    swept_twice = Event()

    class _CountingStore:
        count = 0

        def purge_expired(self, *, now: datetime | None = None) -> int:
            del now
            self.count += 1
            if self.count >= 2:
                swept_twice.set()
            return 0

    store = _CountingStore()

    def broken_observer(_removed: int) -> None:
        raise RuntimeError("observer failure must not kill cleanup")

    with SchoolFactEphemeralStoreJanitor(
        store,
        interval_seconds=0.01,
        on_sweep=broken_observer,
    ) as janitor:
        assert swept_twice.wait(timeout=1)

    assert janitor.failure_count >= 2
    assert store.count >= 2


def test_candidate_app_lifespan_starts_and_joins_janitor_without_production_mount() -> None:
    plan_store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    janitor = SchoolFactEphemeralStoreJanitor(plan_store, interval_seconds=0.01)
    app = FastAPI(lifespan=school_fact_candidate_lifespan(janitor))

    assert janitor.is_running is False
    with TestClient(app):
        assert janitor.is_running is True
    assert janitor.is_running is False


def test_school_fact_route_factory_exposes_only_the_unmounted_sf3_channel() -> None:
    writer = _Writer()
    service = SchoolFactChangeService(reader=_Reader(), writer=writer, clock=lambda: NOW)
    actor = _context().actor
    assert isinstance(actor, StaffActorContext)

    def identity() -> SchoolFactRequestIdentity:
        return SchoolFactRequestIdentity(
            actor=actor,
            session_binding="server-session",
            authorized_actions=frozenset({"school_facts.manage"}),
        )

    app = FastAPI()
    app.include_router(
        create_school_fact_router(
            change_service=service,
            source_store=__import__(
                "app.composition.school_facts", fromlist=["InMemoryPrivateSchoolFactSourceStore"]
            ).InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW),
            plan_store=InMemorySchoolFactPlanStore(clock=lambda: NOW),
            identity_dependency=identity,
        )
    )

    paths = set(app.openapi()["paths"])
    assert paths == {
        "/api/v1/school-facts/sources",
        "/api/v1/school-facts/sources/{source_ref}",
        "/api/v1/school-facts/plans/excel",
        "/api/v1/school-facts/plans/admin",
        "/api/v1/school-facts/plans/adapted",
        "/api/v1/school-facts/plans/{plan_ref}/confirmation",
        "/api/v1/school-facts/plans/{plan_ref}/confirm",
        "/api/v1/school-facts/plans/{plan_ref}/result",
    }
    assert set(app.openapi()["paths"]["/api/v1/school-facts/sources/{source_ref}"]) == {"delete"}


@dataclass
class _HttpReader(_Reader):
    applied: SchoolFactApplyResult | None = None
    locator_results: dict[tuple[str, int, str, str], SchoolFactApplyResult | None] = field(
        default_factory=dict
    )

    def find_applied_plan(
        self, school_id: SchoolId, source_sha256: str, plan_sha256: str
    ) -> SchoolFactApplyResult | None:
        del school_id, source_sha256, plan_sha256
        return self.applied

    def find_result_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        session_sha256: str,
        locator_sha256: str,
    ) -> SchoolFactResultLookup:
        key = (str(school_id), actor_user_id, session_sha256, locator_sha256)
        if key not in self.locator_results:
            return SchoolFactResultLookup(found=False, result=None)
        return SchoolFactResultLookup(found=True, result=self.locator_results[key])


@dataclass
class _HttpWriter(_Writer):
    reader: _HttpReader = field(default_factory=_HttpReader)
    confirm_calls: int = 0

    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        result = super().prepare(prepared)
        provenance = prepared.provenance
        assert provenance is not None
        assert provenance.result_locator_sha256 is not None
        self.reader.locator_results[
            (
                str(prepared.school_id),
                prepared.actor_user_id,
                prepared.session_sha256,
                provenance.result_locator_sha256,
            )
        ] = None
        return result

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult:
        del credential_sha256, change_set, context, session_sha256, now
        self.confirm_calls += 1
        result = SchoolFactApplyResult(
            batch_id=UUID("00000000-0000-0000-0000-000000000010"),
            target_version=2,
            changed_count=1,
            replayed=self.confirm_calls > 1,
        )
        self.reader.applied = result
        prepared = self.prepared[-1]
        assert prepared.provenance is not None
        assert prepared.provenance.result_locator_sha256 is not None
        self.reader.locator_results[
            (
                str(prepared.school_id),
                prepared.actor_user_id,
                prepared.session_sha256,
                prepared.provenance.result_locator_sha256,
            )
        ] = result
        return result


def _identity(
    *,
    school: UUID = SCHOOL_UUID,
    authorized: bool = True,
    account_user_id: int = 9,
    session_binding: str = "server-session-A",
    scope: Literal["normal", "school_reactivation_only"] = "normal",
) -> SchoolFactRequestIdentity:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=account_user_id,
        school_id=SchoolId(school),
        person_id=platform_contracts.PersonId(UUID("00000000-0000-0000-0000-000000000002")),
        assignment_id=platform_contracts.AssignmentId(UUID("00000000-0000-0000-0000-000000000003")),
        assignment_version=2,
        authorization_version=5,
    )
    return SchoolFactRequestIdentity(
        actor=actor,
        session_binding=session_binding,
        authorized_actions=(frozenset({"school_facts.manage"}) if authorized else frozenset()),
        scope=scope,
    )


@contextmanager
def _http_client(
    *,
    mapping_profile_resolver: Callable[
        [SchoolId, str, tuple[str, ...]], ResolvedSourceMappingProfile
    ]
    | None = None,
    source_store: InMemoryPrivateSchoolFactSourceStore | None = None,
    plan_store: InMemorySchoolFactPlanStore | None = None,
    writer: _HttpWriter | None = None,
    enable_package: bool = False,
    package_only: bool = False,
    result_only: bool = False,
    authorize_package=None,
    authorize_package_result=None,
    package_replacement=None,
    package_source_adapter=None,
    authorize_package_selection=None,
) -> Iterator[tuple[TestClient, dict[str, SchoolFactRequestIdentity], _HttpWriter]]:
    reader = _HttpReader() if writer is None else writer.reader
    writer = _HttpWriter(reader=reader) if writer is None else writer
    identities = {"current": _identity()}

    def resolve_identity() -> SchoolFactRequestIdentity:
        return identities["current"]

    app = FastAPI()
    app.middleware("http")(assign_request_id)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(
        create_school_fact_router(
            change_service=SchoolFactChangeService(reader=reader, writer=writer, clock=lambda: NOW),
            source_store=source_store or InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW),
            plan_store=plan_store or InMemorySchoolFactPlanStore(clock=lambda: NOW),
            identity_dependency=resolve_identity,
            mapping_profile_resolver=mapping_profile_resolver,
            enable_package=enable_package,
            package_only=package_only,
            result_only=result_only,
            authorize_package=authorize_package,
            authorize_package_result=authorize_package_result,
            package_replacement=package_replacement,
            package_source_adapter=package_source_adapter,
            authorize_package_selection=authorize_package_selection,
            clock=lambda: NOW,
        )
    )
    with TestClient(app) as client:
        yield client, identities, writer


def test_http_source_abandon_physically_wipes_and_is_idempotent_for_owner() -> None:
    source_store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    with _http_client(source_store=source_store) as (client, _identities, writer):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook()).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        retained_bytes = source_store._records[source_ref].content  # pyright: ignore[reportPrivateUsage]

        abandoned = client.delete(f"/api/v1/school-facts/sources/{source_ref}")
        repeated = client.delete(f"/api/v1/school-facts/sources/{source_ref}")

    assert abandoned.status_code == 204
    assert repeated.status_code == 204
    assert abandoned.content == b""
    assert abandoned.headers["cache-control"] == "no-store"
    assert bytes(retained_bytes) == b"\x00" * len(retained_bytes)
    assert source_store.contains(source_ref, owner=_http_owner()) is False
    assert writer.confirm_calls == 0


def test_http_source_abandon_rejects_cross_owner_and_attached_content() -> None:
    source_store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    with _http_client(source_store=source_store) as (client, identities, _writer):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook()).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]

        identities["current"] = _identity(account_user_id=10)
        cross_actor = client.delete(f"/api/v1/school-facts/sources/{source_ref}")
        identities["current"] = _identity(session_binding="server-session-B")
        cross_session = client.delete(f"/api/v1/school-facts/sources/{source_ref}")
        identities["current"] = _identity(school=UUID("00000000-0000-0000-0000-000000000099"))
        cross_school = client.delete(f"/api/v1/school-facts/sources/{source_ref}")
        identities["current"] = _identity()
        with_query = client.delete(f"/api/v1/school-facts/sources/{source_ref}?force=true")
        with_body = client.request(
            "DELETE",
            f"/api/v1/school-facts/sources/{source_ref}",
            json={"force": True},
        )

        assert source_store.contains(source_ref, owner=_http_owner()) is True
        owner_delete = client.delete(f"/api/v1/school-facts/sources/{source_ref}")

    assert [cross_actor.status_code, cross_session.status_code, cross_school.status_code] == [
        404,
        404,
        404,
    ]
    assert with_query.status_code == 422
    assert with_body.status_code == 422
    assert owner_delete.status_code == 204


def _admin_payload() -> dict[str, object]:
    return {
        "base_version": 1,
        "changes": [
            {
                "operation": "upsert",
                "fact_kind": "school",
                "source_key": "school:demo",
                "expected_version": 0,
                "effective_at": NOW.isoformat(),
                "content": {
                    "business_code": "XJ-001",
                    "display_name": "候选学校",
                    "short_name": "候选校",
                    "timezone_name": "Asia/Shanghai",
                    "lifecycle_status": "active",
                },
            }
        ],
    }


def _school_reactivation_admin_payload() -> dict[str, object]:
    payload = _admin_payload()
    change = payload["changes"][0]  # type: ignore[index]
    change["operation"] = "reactivate"  # type: ignore[index]
    change["expected_version"] = 2  # type: ignore[index]
    return payload


@dataclass
class _SchoolReactivationHttpReader(_HttpReader):
    def preflight_school_reactivation(
        self,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        plan_sha256: str,
    ) -> SchoolFactSchoolReactivationProjection:
        del school_id, change_set, plan_sha256
        return SchoolFactSchoolReactivationProjection(
            replay_result=self.applied,
            current_fact_version=1,
            current_target_version=2,
        )

    def find_fact_version(
        self,
        school_id: SchoolId,
        fact_kind: str,
        source_key: str,
    ) -> FactVersionProjection | None:
        del school_id, source_key
        if fact_kind in {"school", "person", "student", "organization", "location"}:
            return FactVersionProjection(version=2)
        return None


def test_school_reactivation_candidate_scope_is_exact_and_plan_bound() -> None:
    reader = _SchoolReactivationHttpReader()
    writer = _HttpWriter(reader=reader)
    plan_store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    with _http_client(plan_store=plan_store, writer=writer) as (client, identities, _writer):
        normal_plan = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        assert normal_plan.status_code == 200
        normal_plan_ref = normal_plan.json()["plan_ref"]

        identities["current"] = _identity(scope="school_reactivation_only")
        rejected_source = client.post(
            "/api/v1/school-facts/sources",
            json={"filename_summary": "facts.xlsx", "content_base64": "YQ=="},
        )
        rejected_excel = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": "sf3src_not_owned", "base_version": 1},
        )
        rejected_adapted = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [{"source_slot": "school", "source_ref": "sf3src_not_owned"}],
                "mapping_version": "school_v1",
                "base_version": 1,
            },
        )
        rejected_admin = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        rejected_existing_prepare = client.post(
            f"/api/v1/school-facts/plans/{normal_plan_ref}/confirmation",
            headers={"Idempotency-Key": "candidate-normal-plan"},
        )
        rejected_existing_result = client.get(
            f"/api/v1/school-facts/plans/{normal_plan_ref}/result"
        )

        accepted = client.post(
            "/api/v1/school-facts/plans/admin",
            json=_school_reactivation_admin_payload(),
        )

    assert accepted.status_code == 200
    for response in (
        rejected_source,
        rejected_excel,
        rejected_adapted,
        rejected_admin,
        rejected_existing_prepare,
        rejected_existing_result,
    ):
        assert response.status_code == 403
        assert response.json()["code"] == "school_facts.forbidden"
    assert writer.prepared == []


def test_school_reactivation_candidate_can_prepare_confirm_and_recover_only_its_plan() -> None:
    reader = _SchoolReactivationHttpReader()
    writer = _HttpWriter(reader=reader)
    with _http_client(writer=writer) as (client, identities, _writer):
        identities["current"] = _identity(scope="school_reactivation_only")
        preflight = client.post(
            "/api/v1/school-facts/plans/admin",
            json=_school_reactivation_admin_payload(),
        )
        assert preflight.status_code == 200
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "candidate-reactivate-prepare"},
        )
        applied = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "candidate-reactivate-confirm"},
            json={"credential": prepared.json()["credential"]},
        )
        recovered = client.get(f"/api/v1/school-facts/plans/{plan_ref}/result")
        missing = client.get("/api/v1/school-facts/plans/sf3plan_missing_result/result")

    assert prepared.status_code == 200
    assert applied.status_code == 200
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "applied"
    assert missing.status_code == 404
    assert writer.confirm_calls == 1


def test_normal_school_fact_scope_remains_the_default() -> None:
    identity = _identity()
    assert identity.scope == "normal"
    with _http_client() as (client, _identities, _writer):
        response = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("fact_kind", "source_key", "content"),
    (
        (
            "person",
            "person:http-reactivate",
            {
                "person_kind": "staff",
                "display_name": "HTTP 人员",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            "student:http-reactivate",
            {"person_source_key": "person:http-reactivate", "status": "active"},
        ),
        (
            "organization",
            "organization:http-reactivate",
            {
                "kind": "department",
                "business_code": "HTTP-ORG",
                "display_name": "HTTP 组织",
                "display_path": "/HTTP 组织",
                "status": "active",
            },
        ),
        (
            "location",
            "location:http-reactivate",
            {
                "kind": "campus",
                "business_code": "HTTP-CAMPUS",
                "display_name": "HTTP 校区",
                "display_path": "/HTTP 校区",
                "status": "active",
            },
        ),
    ),
)
def test_normal_http_scope_can_submit_the_other_four_root_reactivations(
    fact_kind: str,
    source_key: str,
    content: dict[str, object],
) -> None:
    writer = _HttpWriter(reader=_SchoolReactivationHttpReader())
    payload = {
        "base_version": 1,
        "changes": [
            {
                "operation": "reactivate",
                "fact_kind": fact_kind,
                "source_key": source_key,
                "expected_version": 2,
                "effective_at": NOW.isoformat(),
                "content": content,
            }
        ],
    }
    with _http_client(writer=writer) as (client, _identities, _writer):
        response = client.post("/api/v1/school-facts/plans/admin", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["changes"][0]["operation"] == "reactivate"


def _formal_workbook(*, school_status: str = "正常") -> bytes:
    workbook = Workbook()
    active = workbook.active
    assert active is not None
    workbook.remove(active)
    for sheet_name, spec in SCHOOL_FACT_SHEET_SPECS.items():
        worksheet = workbook.create_sheet(sheet_name)
        worksheet.append(spec.headers)
        if sheet_name == "00_导入说明":
            worksheet.append([SCHOOL_FACT_TEMPLATE_VERSION, "formal"])
        elif sheet_name == "01_学校":
            row = {
                "操作": "upsert",
                "稳定标识": "school:demo",
                "预期版本": 0,
                "生效时间": NOW.isoformat(),
                "学校编号": "XJ-001",
                "学校名称": "候选学校",
                "学校简称": "候选校",
                "时区": "Asia/Shanghai",
                "状态": school_status,
            }
            worksheet.append([row.get(header) for header in spec.headers])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _legacy_school_workbook(*, school_status: str) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "学校旧表"
    worksheet.append(["唯一号", "版本", "时间", "编码", "名称", "使用情况"])
    worksheet.append(["school:demo", 0, NOW.isoformat(), "XJ-001", "候选学校", school_status])
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _legacy_person_workbook(
    *,
    person_status: str = "在岗",
    expected_version: int = 0,
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "员工旧表"
    worksheet.append(["唯一号", "版本", "时间", "工号", "姓", "名", "任职情况"])
    worksheet.append(
        [
            "person:teacher-1",
            expected_version,
            NOW.isoformat(),
            "T-001",
            "张",
            "静",
            person_status,
        ]
    )
    target = BytesIO()
    workbook.save(target)
    workbook.close()
    return target.getvalue()


def _legacy_school_profile(
    *,
    source_ref: str,
    version: str,
    status_profile: StatusMappingProfile = DEFAULT_STATUS_MAPPING_PROFILE,
) -> SourceMappingProfile:
    return SourceMappingProfile(
        version=version,
        status_profile=status_profile,
        sheets=(
            SourceSheetMapping(
                source_ref=source_ref,
                source_sheet="学校旧表",
                target_sheet="01_学校",
                source_slot="school_file",
                source_sheet_code="legacy_school",
                columns=(
                    SourceColumnMapping("operation", "操作", constant="upsert"),
                    SourceColumnMapping(
                        "唯一号",
                        "稳定标识",
                        source_field_code="school_key",
                    ),
                    SourceColumnMapping(
                        "版本",
                        "预期版本",
                        source_field_code="school_version",
                    ),
                    SourceColumnMapping("时间", "生效时间"),
                    SourceColumnMapping("编码", "学校编号"),
                    SourceColumnMapping("名称", "学校名称"),
                    SourceColumnMapping("short", "学校简称", constant="候选校"),
                    SourceColumnMapping("timezone", "时区", constant="Asia/Shanghai"),
                    SourceColumnMapping("使用情况", "状态"),
                ),
            ),
        ),
    )


def _legacy_multi_file_profile(
    *,
    source_refs: tuple[str, ...],
    version: str,
) -> SourceMappingProfile:
    school_ref, person_ref = source_refs
    school_sheet = _legacy_school_profile(
        source_ref=school_ref,
        version=version,
    ).sheets[0]
    return SourceMappingProfile(
        version=version,
        sheets=(
            school_sheet,
            SourceSheetMapping(
                source_ref=person_ref,
                source_sheet="员工旧表",
                target_sheet="02_人员",
                source_slot="staff_file",
                source_sheet_code="legacy_staff",
                columns=(
                    SourceColumnMapping("operation", "操作", constant="upsert"),
                    SourceColumnMapping(
                        "唯一号",
                        "稳定标识",
                        source_field_code="staff_key",
                    ),
                    SourceColumnMapping(
                        "版本",
                        "预期版本",
                        source_field_code="staff_version",
                    ),
                    SourceColumnMapping("时间", "生效时间"),
                    SourceColumnMapping("工号", "人员编号"),
                    SourceColumnMapping("姓", "姓名", additional_source_headers=("名",)),
                    SourceColumnMapping("kind", "人员类型", constant="staff"),
                    SourceColumnMapping(
                        "任职情况",
                        "状态",
                        source_field_code="employment_state",
                    ),
                ),
            ),
        ),
    )


def test_http_excel_and_admin_inputs_independently_produce_same_plan_hash() -> None:
    with _http_client() as (client, _identities, _writer):
        content = _formal_workbook()
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "facts.xlsx",
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        assert upload.status_code == 201
        source_ref = upload.json()["source_ref"]
        excel = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": source_ref, "base_version": 1},
        )
        admin = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())

    assert excel.status_code == 200
    assert admin.status_code == 200
    assert excel.json()["plan_sha256"] == admin.json()["plan_sha256"]
    assert excel.json()["changes"] == admin.json()["changes"]
    assert excel.json()["changes"][0]["fields"] == [
        {"field_code": "business_code", "new_value": "XJ-001"},
        {"field_code": "display_name", "new_value": "候选学校"},
        {"field_code": "lifecycle_status", "new_value": "active"},
        {"field_code": "short_name", "new_value": "候选校"},
        {"field_code": "timezone_name", "new_value": "Asia/Shanghai"},
    ]
    assert excel.headers["cache-control"] == "no-store"


def test_adapted_http_merges_two_private_sources_and_binds_safe_provenance() -> None:
    def resolve(
        school_id: SchoolId,
        version: str,
        source_slots: tuple[str, ...],
    ) -> ResolvedSourceMappingProfile:
        profile = _legacy_multi_file_profile(source_refs=source_slots, version=version)
        return ResolvedSourceMappingProfile(
            school_id=str(school_id),
            source_slots=source_slots,
            profile=profile,
            config_sha256=source_mapping_profile_sha256(profile),
        )

    with _http_client(mapping_profile_resolver=resolve) as (
        client,
        _identities,
        writer,
    ):
        uploads = []
        for filename, content in (
            ("legacy-school-private-name.xlsx", _legacy_school_workbook(school_status="在用")),
            ("legacy-staff-private-name.xlsx", _legacy_person_workbook()),
        ):
            uploaded = client.post(
                "/api/v1/school-facts/sources",
                json={
                    "filename_summary": filename,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
            )
            assert uploaded.status_code == 201
            uploads.append(uploaded.json())
        preflight = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [
                    {"source_slot": source_slot, "source_ref": item["source_ref"]}
                    for source_slot, item in zip(
                        ("school_file", "staff_file"),
                        uploads,
                        strict=True,
                    )
                ],
                "mapping_version": "legacy_multi_v1",
                "base_version": 1,
            },
        )
        assert preflight.status_code == 200
        assert [item["fact_kind"] for item in preflight.json()["changes"]] == [
            "school",
            "person",
        ]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{preflight.json()['plan_ref']}/confirmation",
            headers={"Idempotency-Key": "adapted-multi-prepare"},
        )

    assert prepared.status_code == 200
    provenance = writer.prepared[-1].provenance
    assert provenance is not None
    assert provenance.mapping_version == "legacy_multi_v1"
    assert provenance.mapping_config_sha256 == source_mapping_profile_sha256(
        _legacy_multi_file_profile(
            source_refs=tuple(item["source_ref"] for item in uploads),
            version="legacy_multi_v1",
        )
    )
    assert provenance.source_size_bytes is not None
    assert provenance.source_size_bytes <= 10 * 1024 * 1024
    assert [
        (item.logical_source_code, item.source_sha256) for item in provenance.source_manifest
    ] == [
        ("school_file", uploads[0]["source_sha256"]),
        ("staff_file", uploads[1]["source_sha256"]),
    ]
    assert [
        (item.source_sheet_code, item.source_row_number, item.fact_kind)
        for item in provenance.row_lineage
    ] == [
        ("legacy_school", 2, "school"),
        ("legacy_staff", 2, "person"),
    ]
    serialized = str(asdict(provenance))
    for forbidden in (
        "legacy-school-private-name.xlsx",
        "legacy-staff-private-name.xlsx",
        "学校旧表",
        "员工旧表",
        "候选学校",
        "张静",
    ):
        assert forbidden not in serialized


def test_adapted_http_rejects_ninth_source_in_the_strict_request_contract() -> None:
    with _http_client(mapping_profile_resolver=lambda *_args: pytest.fail("resolver called")) as (
        client,
        _identities,
        _writer,
    ):
        response = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [
                    {
                        "source_slot": f"source_{index}",
                        "source_ref": f"sf3source_{index:02d}_1234567890",
                    }
                    for index in range(9)
                ],
                "mapping_version": "bounded_sources_v1",
                "base_version": 1,
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "http.request_invalid"


def test_adapted_http_rejects_raw_aggregate_above_32_mib_before_open_or_mapping() -> None:
    class _OversizedMetadataStore(InMemoryPrivateSchoolFactSourceStore):
        def describe(
            self,
            opaque_ref: str,
            *,
            owner: PrivateSourceOwner,
            now: datetime | None = None,
        ) -> ProtectedImportSourceRef:
            del owner, now
            return ProtectedImportSourceRef(
                opaque_ref=opaque_ref,
                source_sha256="e" * 64,
                filename_summary="bounded-source.xlsx",
                size_bytes=9 * 1024 * 1024,
                worksheet_names=("source_sheet",),
            )

        def open(  # pyright: ignore[reportIncompatibleMethodOverride]
            self,
            opaque_ref: str,
            *,
            owner: PrivateSourceOwner,
            now: datetime | None = None,
        ) -> BytesIO:
            del opaque_ref, owner, now
            raise AssertionError("capacity must fail before raw bytes are opened")

    raw_refs = tuple(f"sf3source_{index:02d}_1234567890" for index in range(4))
    with _http_client(
        source_store=_OversizedMetadataStore(clock=lambda: NOW),
        mapping_profile_resolver=lambda *_args: pytest.fail("resolver called"),
    ) as (client, _identities, _writer):
        response = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [
                    {"source_slot": f"source_{index}", "source_ref": source_ref}
                    for index, source_ref in enumerate(raw_refs)
                ],
                "mapping_version": "bounded_sources_v1",
                "base_version": 1,
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "school_facts.source_set_capacity_exceeded"
    assert all(source_ref not in response.text for source_ref in raw_refs)


def test_adapted_http_translates_second_source_issues_to_safe_raw_coordinates() -> None:
    def resolve(
        school_id: SchoolId,
        version: str,
        source_slots: tuple[str, ...],
    ) -> ResolvedSourceMappingProfile:
        profile = _legacy_multi_file_profile(source_refs=source_slots, version=version)
        return ResolvedSourceMappingProfile(
            school_id=str(school_id),
            source_slots=source_slots,
            profile=profile,
            config_sha256=source_mapping_profile_sha256(profile),
        )

    with _http_client(mapping_profile_resolver=resolve) as (
        client,
        _identities,
        _writer,
    ):
        source_refs = []
        for filename, content in (
            ("raw-school-secret.xlsx", _legacy_school_workbook(school_status="在用")),
            (
                "raw-staff-secret.xlsx",
                _legacy_person_workbook(expected_version=1),
            ),
        ):
            upload = client.post(
                "/api/v1/school-facts/sources",
                json={
                    "filename_summary": filename,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
            )
            source_refs.append(upload.json()["source_ref"])
        response = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [
                    {"source_slot": source_slot, "source_ref": source_ref}
                    for source_slot, source_ref in zip(
                        ("school_file", "staff_file"),
                        source_refs,
                        strict=True,
                    )
                ],
                "mapping_version": "legacy_multi_issue_v1",
                "base_version": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["issues"] == [
        {
            "source_slot": "staff_file",
            "sheet_code": "legacy_staff",
            "row_number": 2,
            "column_code": "staff_key",
            "error_code": "fact.not_found",
        }
    ]
    person_status = next(
        item for item in response.json()["status_decisions"] if item["sheet_code"] == "legacy_staff"
    )
    assert person_status == {
        "source_slot": "staff_file",
        "sheet_code": "legacy_staff",
        "row_number": 2,
        "field_code": "employment_state",
        "original_status_value": "在岗",
        "standard_value": "active",
        "mapping_version": "school_statuses_v1",
    }
    for forbidden in (
        "raw-school-secret.xlsx",
        "raw-staff-secret.xlsx",
        "员工旧表",
    ):
        assert forbidden not in response.text


def test_http_requires_server_authority_and_rejects_cross_school_source() -> None:
    with _http_client() as (client, identities, _writer):
        content = _formal_workbook()
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "facts.xlsx",
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        identities["current"] = _identity(authorized=False)
        forbidden = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        identities["current"] = _identity(school=UUID("00000000-0000-0000-0000-000000000099"))
        cross_school = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": source_ref, "base_version": 1},
        )

    assert forbidden.status_code == 403
    assert cross_school.status_code == 404


def test_adapted_mapping_is_resolved_only_after_owner_check_and_is_school_bound() -> None:
    resolver_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def resolve(
        school_id: SchoolId,
        version: str,
        source_slots: tuple[str, ...],
    ) -> ResolvedSourceMappingProfile:
        resolver_calls.append((str(school_id), version, source_slots))
        profile = _legacy_school_profile(
            source_ref="school_file",
            version=version,
        )
        return ResolvedSourceMappingProfile(
            school_id=str(school_id),
            source_slots=source_slots,
            profile=profile,
            config_sha256=source_mapping_profile_sha256(profile),
        )

    with _http_client(mapping_profile_resolver=resolve) as (
        client,
        identities,
        _writer,
    ):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-source.xlsx",
                "content_base64": base64.b64encode(
                    _legacy_school_workbook(school_status="在用")
                ).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        identities["current"] = _identity(school=UUID("00000000-0000-0000-0000-000000000099"))
        cross_school = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [{"source_slot": "school_file", "source_ref": source_ref}],
                "mapping_version": "school_a_legacy_v1",
                "base_version": 1,
            },
        )
        assert resolver_calls == []
        identities["current"] = _identity()
        accepted = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [{"source_slot": "school_file", "source_ref": source_ref}],
                "mapping_version": "school_a_legacy_v1",
                "base_version": 1,
            },
        )

    assert cross_school.status_code == 404
    assert accepted.status_code == 200
    assert resolver_calls == [(str(SCHOOL), "school_a_legacy_v1", ("school_file",))]


def test_http_only_explicit_click_confirms_and_get_result_never_replays_confirm() -> None:
    with _http_client() as (client, _identities, writer):
        preflight = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "prepare-1"},
        )
        credential = prepared.json()["credential"]
        natural_language = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "confirm-1"},
            json={"credential": credential, "confirmation": "确认"},
        )
        assert natural_language.status_code == 422
        assert writer.confirm_calls == 0
        applied = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "confirm-1"},
            json={"credential": credential},
        )
        checked = client.get(f"/api/v1/school-facts/plans/{plan_ref}/result")

    assert applied.status_code == 200
    assert checked.status_code == 200
    assert checked.json()["state"] == "applied"
    assert writer.confirm_calls == 1


def test_missing_idempotency_key_and_invalid_plan_ref_are_fixed_safe_422() -> None:
    with _http_client() as (client, _identities, _writer):
        preflight = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        missing_key = client.post(
            f"/api/v1/school-facts/plans/{preflight.json()['plan_ref']}/confirmation"
        )
        invalid_ref = client.get("/api/v1/school-facts/plans/sf3plan_%E4%B8%AD%E6%96%87/result")

    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "school_facts.request_invalid"
    assert invalid_ref.status_code == 422
    assert invalid_ref.json()["code"] == "http.request_invalid"
    assert "中文" not in invalid_ref.text


def _http_owner() -> PrivateSourceOwner:
    identity = _identity()
    return PrivateSourceOwner(
        school_id=str(identity.actor.school_id),
        actor_user_id=identity.actor.account_user_id,
        session_sha256=sha256(identity.session_binding.encode("utf-8")).hexdigest(),
    )


def test_successful_confirm_deletes_raw_source_and_retires_full_plan() -> None:
    source_store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    plan_store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    with _http_client(source_store=source_store, plan_store=plan_store) as (
        client,
        _identities,
        _writer,
    ):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook()).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        preflight = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": source_ref, "base_version": 1},
        )
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "cleanup-prepare"},
        )
        applied = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "cleanup-confirm"},
            json={"credential": prepared.json()["credential"]},
        )

    assert applied.status_code == 200
    assert applied.json()["cleanup_pending"] is False
    assert source_store.contains(source_ref, owner=_http_owner()) is False
    with pytest.raises(KeyError):
        plan_store.get(plan_ref, owner=_http_owner())
    assert plan_store.locate(plan_ref, owner=_http_owner()).plan_ref == plan_ref


def test_cleanup_failure_is_reported_and_raw_bytes_still_expire() -> None:
    current = [NOW]

    class _DeleteFailingStore(InMemoryPrivateSchoolFactSourceStore):
        def delete(self, opaque_ref: str, *, owner: PrivateSourceOwner) -> None:
            del opaque_ref, owner
            raise RuntimeError("synthetic cleanup failure")

    source_store = _DeleteFailingStore(clock=lambda: current[0])
    with _http_client(source_store=source_store) as (client, _identities, _writer):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook()).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        preflight = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": source_ref, "base_version": 1},
        )
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "cleanup-fail-prepare"},
        )
        applied = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "cleanup-fail-confirm"},
            json={"credential": prepared.json()["credential"]},
        )

    assert applied.status_code == 200
    assert applied.json()["cleanup_pending"] is True
    assert source_store.contains(source_ref, owner=_http_owner()) is True
    current[0] = NOW + timedelta(hours=24)
    assert source_store.purge_expired() == 1
    assert source_store.contains(source_ref, owner=_http_owner()) is False


def test_unknown_confirm_outcome_keeps_source_and_full_plan_for_result_check() -> None:
    class _UnknownWriter(_HttpWriter):
        def confirm(
            self,
            *,
            credential_sha256: str,
            change_set: CanonicalFactChangeSetV1,
            context: CommandExecutionContext,
            session_sha256: str,
            now: datetime,
        ) -> SchoolFactApplyResult:
            del credential_sha256, change_set, context, session_sha256, now
            self.confirm_calls += 1
            raise RuntimeError("synthetic outcome unknown")

    source_store = InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW)
    plan_store = InMemorySchoolFactPlanStore(clock=lambda: NOW)
    reader = _HttpReader()
    writer = _UnknownWriter(reader=reader)
    with _http_client(
        source_store=source_store,
        plan_store=plan_store,
        writer=writer,
    ) as (client, _identities, _writer):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "private-school-facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook()).decode("ascii"),
            },
        )
        source_ref = upload.json()["source_ref"]
        preflight = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": source_ref, "base_version": 1},
        )
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "unknown-prepare"},
        )
        response = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "unknown-confirm"},
            json={"credential": prepared.json()["credential"]},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "school_facts.unavailable"
    assert source_store.contains(source_ref, owner=_http_owner()) is True
    assert plan_store.get(plan_ref, owner=_http_owner()).plan_ref == plan_ref
    assert writer.confirm_calls == 1


def test_result_lookup_survives_plan_store_restart_without_replaying_confirm() -> None:
    reader = _HttpReader()
    writer = _HttpWriter(reader=reader)
    identities = {"current": _identity()}

    def resolve_identity() -> SchoolFactRequestIdentity:
        return identities["current"]

    def build_app(plan_store: InMemorySchoolFactPlanStore) -> FastAPI:
        app = FastAPI()
        app.middleware("http")(assign_request_id)
        app.add_exception_handler(ApiError, api_error_handler)
        app.add_exception_handler(RequestValidationError, request_validation_error_handler)
        app.include_router(
            create_school_fact_router(
                change_service=SchoolFactChangeService(
                    reader=reader,
                    writer=writer,
                    clock=lambda: NOW,
                ),
                source_store=InMemoryPrivateSchoolFactSourceStore(clock=lambda: NOW),
                plan_store=plan_store,
                identity_dependency=resolve_identity,
                clock=lambda: NOW,
            )
        )
        return app

    with TestClient(build_app(InMemorySchoolFactPlanStore(clock=lambda: NOW))) as client:
        preflight = client.post("/api/v1/school-facts/plans/admin", json=_admin_payload())
        plan_ref = preflight.json()["plan_ref"]
        prepared = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirmation",
            headers={"Idempotency-Key": "prepare-restart"},
        )
        applied = client.post(
            f"/api/v1/school-facts/plans/{plan_ref}/confirm",
            headers={"Idempotency-Key": "confirm-restart"},
            json={"credential": prepared.json()["credential"]},
        )
        assert applied.status_code == 200

    with TestClient(build_app(InMemorySchoolFactPlanStore(clock=lambda: NOW))) as restarted:
        checked = restarted.get(f"/api/v1/school-facts/plans/{plan_ref}/result")

    assert checked.status_code == 200
    assert checked.json()["state"] == "applied"
    assert writer.confirm_calls == 1


def test_http_validation_does_not_echo_sensitive_cell_value() -> None:
    canary = "13800000000-SENSITIVE-CELL"
    payload = _admin_payload()
    payload["changes"][0]["content"]["contact_value"] = canary  # type: ignore[index]
    with _http_client() as (client, _identities, _writer):
        response = client.post("/api/v1/school-facts/plans/admin", json=payload)

    assert response.status_code == 422
    assert canary not in response.text


def test_http_unknown_status_returns_fixed_safe_coordinate_without_raw_value() -> None:
    canary = "UNKNOWN-STATUS-SENSITIVE-CANARY"
    with _http_client() as (client, _identities, _writer):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "facts.xlsx",
                "content_base64": base64.b64encode(_formal_workbook(school_status=canary)).decode(
                    "ascii"
                ),
            },
        )
        response = client.post(
            "/api/v1/school-facts/plans/excel",
            json={"source_ref": upload.json()["source_ref"], "base_version": 1},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "school_facts.status_mapping_required"
    assert "01_学校" in response.json()["message"]
    assert "2" in response.json()["message"]
    assert "lifecycle_status" in response.json()["message"]
    assert "school_statuses_v1" in response.json()["message"]
    assert canary not in response.text


def test_http_ambiguous_adapted_status_returns_fixed_code_without_raw_value() -> None:
    canary = "AMBIGUOUS-STATUS-SENSITIVE-CANARY"
    ambiguous = StatusMappingProfile(
        version="ambiguous_status_v1",
        entries=(
            *DEFAULT_STATUS_MAPPING_PROFILE.entries,
            StatusMappingEntry("01_学校", "lifecycle_status", canary, "active"),
            StatusMappingEntry("01_学校", "lifecycle_status", canary, "inactive"),
        ),
    )

    def resolve(
        school_id: SchoolId,
        version: str,
        source_slots: tuple[str, ...],
    ) -> ResolvedSourceMappingProfile:
        profile = _legacy_school_profile(
            source_ref=source_slots[0],
            version=version,
            status_profile=ambiguous,
        )
        return ResolvedSourceMappingProfile(
            school_id=str(school_id),
            source_slots=source_slots,
            profile=profile,
            config_sha256=source_mapping_profile_sha256(profile),
        )

    with _http_client(mapping_profile_resolver=resolve) as (
        client,
        _identities,
        _writer,
    ):
        upload = client.post(
            "/api/v1/school-facts/sources",
            json={
                "filename_summary": "legacy-school.xlsx",
                "content_base64": base64.b64encode(
                    _legacy_school_workbook(school_status=canary)
                ).decode("ascii"),
            },
        )
        response = client.post(
            "/api/v1/school-facts/plans/adapted",
            json={
                "sources": [
                    {
                        "source_slot": "school_file",
                        "source_ref": upload.json()["source_ref"],
                    }
                ],
                "mapping_version": "legacy_school_v1",
                "base_version": 1,
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "school_facts.status_mapping_ambiguous"
    assert "school_file/legacy_school" in response.json()["message"]
    assert "2" in response.json()["message"]
    assert "lifecycle_status" in response.json()["message"]
    assert "ambiguous_status_v1" in response.json()["message"]
    assert canary not in response.text
