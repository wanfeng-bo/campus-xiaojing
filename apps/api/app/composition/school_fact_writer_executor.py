# pyright: strict

from __future__ import annotations

import hmac
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast, runtime_checkable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_family_correction_executor import (
    FamilyCorrectionExecutorKernel,
    create_family_correction_executor_app,
)
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_evidence import validation_batch
from app.composition.school_fact_writer_settings import (
    SchoolFactWriterSettings,
    build_school_fact_writer_engine,
    verify_school_fact_writer_database,
)
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.domains.students.family_rules import CurrentFamilyFactActorValidator
from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.executor_context_codec import (
    CommandContextTransportCodec,
    CommandContextTransportRejected,
)
from app.platform.identity.tuple_validator import SqlAlchemyIdentityTupleValidator
from app.platform.school_facts.application import (
    SCHOOL_FACT_CONFIRMATION_WINDOW,
    SchoolFactChangeRejected,
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.executor_contracts import (
    EXECUTOR_AUTH_HEADER,
    SchoolFactExecutorApplyResponse,
    SchoolFactExecutorConfirmRequest,
    SchoolFactExecutorDigestResultRequest,
    SchoolFactExecutorOwnResult,
    SchoolFactExecutorOwnResultListRequest,
    SchoolFactExecutorOwnResultLocatorRequest,
    SchoolFactExecutorOwnResultLookupResponse,
    SchoolFactExecutorOwnResultPage,
    SchoolFactExecutorPreflightRequest,
    SchoolFactExecutorPrepareRequest,
    SchoolFactExecutorResultRequest,
    SchoolFactExecutorResultResponse,
)
from app.platform.school_facts.f7_receipt_contracts import (
    SchoolFactExecutorOwnTargetPage,
    SchoolFactExecutorOwnTargetPageRequest,
    SchoolFactExecutorOwnTargetPageResponse,
)
from app.platform.school_facts.ports import (
    OwnOriginalReadOptions,
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactOwnOriginalResult,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnResultScope,
    SchoolFactOwnTargetPage,
    SchoolFactResultLookup,
    validate_own_original_selection,
)
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable


class SchoolFactWriterExecutorKernel(Protocol):
    """固定四操作适配口；不暴露 SQL、表、条件或通用命令。"""

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1: ...

    def prepare(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None,
    ) -> ConfirmationReceiptV1: ...

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult: ...

    def result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactResultLookup: ...

    def result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup: ...


@runtime_checkable
class SchoolFactOwnResultExecutorKernel(Protocol):
    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int = 20,
    ) -> SchoolFactOwnResultPage: ...

    def check_own_result_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        original_fields: dict[str, tuple[str, ...]] | None = None,
        target_offset: int = 0,
        target_limit: int = 3,
    ) -> SchoolFactOwnResult | None: ...


class OwnResultReadAuthorization(Protocol):
    @property
    def allowed_targets(self) -> SchoolFactOwnResultScope: ...
    def revalidate(self) -> None: ...


class OwnTerminalReadAuthorization(Protocol):
    """Independent current own-receipt duty; never inferred from fact read scopes."""

    def revalidate(self) -> None: ...


class SqlAlchemySchoolFactWriterExecutorKernel:
    """只组合既有 SchoolFactChangeService 与唯一 SQLAlchemy 写核。"""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: SchoolFactWriterSettings,
        final_authorization_guard: Callable[
            [Session, CommandExecutionContext, CanonicalFactChangeSetV1], None
        ]
        | None = None,
        family_actor_validator: CurrentFamilyFactActorValidator | None = None,
        own_result_authorizer: Callable[[CommandExecutionContext], OwnResultReadAuthorization]
        | None = None,
        own_terminal_authorizer: Callable[[CommandExecutionContext], OwnTerminalReadAuthorization]
        | None = None,
        package_final_authorization_guard: Callable[
            [Session, CommandExecutionContext, CanonicalFactChangeSetV1], None
        ]
        | None = None,
    ) -> None:
        if (
            settings.schema_contract
            in {
                "school-facts-f1-v21",
                "school-facts-f1-v22",
                "school-facts-f2-v23",
                "school-facts-f2-v24",
                "school-facts-f2-v25",
                "school-facts-f3-v26",
                "school-facts-f3-v27",
                "school-facts-f4-v29",
                "school-facts-f4-v30",
                "school-facts-f4-v31",
                "school-facts-f5-v32",
                "school-facts-f5-v33",
                "school-facts-f5-v34",
                "school-facts-f5-v35",
                "school-facts-f5-v36",
                "school-facts-f5-v37",
                "school-facts-f5-v38",
                "school-facts-f5-v39",
                "school-facts-f5-v40",
                "school-facts-f5-v41",
                "school-facts-f6-v44",
                "school-facts-f6-v45",
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
            and final_authorization_guard is None
        ):
            raise ValueError("Product Writer requires its final authorization guard.")
        if (
            settings.schema_contract
            in {
                "school-facts-f3-v26",
                "school-facts-f3-v27",
                "school-facts-f4-v29",
                "school-facts-f4-v30",
                "school-facts-f4-v31",
                "school-facts-f5-v32",
                "school-facts-f5-v33",
                "school-facts-f5-v34",
                "school-facts-f5-v35",
                "school-facts-f5-v36",
                "school-facts-f5-v37",
                "school-facts-f5-v38",
                "school-facts-f5-v39",
                "school-facts-f5-v40",
                "school-facts-f5-v41",
                "school-facts-f6-v44",
                "school-facts-f6-v45",
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
            and family_actor_validator is None
        ):
            raise ValueError("F3 Writer requires current family actor validation.")
        self._family_actor_validator = family_actor_validator
        if (
            settings.schema_contract
            in {
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
            and package_final_authorization_guard is None
        ):
            raise ValueError("F7 Writer requires its exact package final authorization guard.")
        self._final_authorization_guard = final_authorization_guard
        self._package_final_authorization_guard = package_final_authorization_guard
        self._own_result_authorizer = own_result_authorizer
        self._own_terminal_authorizer = own_terminal_authorizer
        self._identity_tuple_validator = SqlAlchemyIdentityTupleValidator(
            school_fact_command_wrapper=settings.schema_contract
            in {
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56", "school-facts-live-v57",
            }
        )
        self._engine = engine
        self._expected_school_id = SchoolId(settings.expected_school_id)
        self._session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self._context_signer = ContextProofSigner(settings.rls_context_signing_key)
        self._credential_issuer = HmacSchoolFactConfirmationCredentialIssuer(
            settings.confirmation_hmac_key.get_secret_value().encode("utf-8")
        )

    @contextmanager
    def correction_transaction(
        self, context: CommandExecutionContext, session_binding: str
    ) -> Generator[Session]:
        self._require_expected_school(context.actor.school_id)
        envelope = self._context_signer.sign_execution_context(
            context,
            session_nonce_hash=sha256(session_binding.encode("utf-8")).hexdigest(),
            proof_expires_at=datetime.now(UTC) + SCHOOL_FACT_CONFIRMATION_WINDOW,
        )
        with self._session_factory() as session:
            with session.begin():
                from app.db.rls_context import set_local_execution_context

                set_local_execution_context(session, envelope)
                yield session

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1:
        self._require_expected_school(school_id)
        return self._service(context=context, session_binding=session_binding).preflight(
            school_id=school_id,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )

    def prepare(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None,
    ) -> ConfirmationReceiptV1:
        self._require_expected_school(context.actor.school_id)
        return self._service(
            context=context,
            session_binding=session_binding,
        ).prepare_confirmation(
            change_set=change_set,
            context=context,
            session_binding=session_binding,
            provenance=provenance,
        )

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult:
        self._require_expected_school(context.actor.school_id)
        return self._service(
            context=context,
            session_binding=session_binding,
        ).confirm(
            credential=credential,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )

    def result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactResultLookup:
        self._require_expected_school(school_id)
        result = self._service(
            context=context,
            session_binding=session_binding,
        ).check_result_by_digest(
            school_id=school_id,
            source_sha256=source_sha256,
            plan_sha256=plan_sha256,
            context=context,
            session_binding=session_binding,
        )
        return SchoolFactResultLookup(found=result is not None, result=result)

    def result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup:
        self._require_expected_school(school_id)
        return self._service(
            context=context,
            session_binding=session_binding,
        ).check_result_by_locator(
            school_id=school_id,
            actor_user_id=actor_user_id,
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            context=context,
        )

    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int = 20,
    ) -> SchoolFactOwnResultPage:
        self._require_expected_school(context.actor.school_id)
        if self._own_result_authorizer is None:
            raise SchoolFactChangeRejected("Own receipt reading is not enabled.")
        # Finish Writer pool queuing before current Reader-owner authorization.
        # The two roles use independent engines; no transaction spans the owner call.
        with self._engine.connect() as connection:
            bound = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
            authorization = self._own_result_authorizer(context)
            result = self._service(
                context=context,
                session_binding=session_binding,
                session_factory=bound,
            ).list_own_results(
                context=context,
                allowed_targets=authorization.allowed_targets,
                limit=limit,
                **(
                    {"creation_only_targets": authorization.creation_only_targets}
                    if getattr(authorization, "creation_only_targets", ())
                    else {}
                ),
            )
            authorization.revalidate()
            return result

    def check_own_result_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        original_fields: dict[str, tuple[str, ...]] | None = None,
        target_offset: int = 0,
        target_limit: int = 3,
    ) -> SchoolFactOwnResult | None:
        validate_own_original_selection(original_fields, target_offset, target_limit)
        self._require_expected_school(context.actor.school_id)
        if self._own_result_authorizer is None:
            raise SchoolFactChangeRejected("Own receipt reading is not enabled.")
        with self._engine.connect() as connection:
            bound = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
            service = self._service(
                context=context,
                session_binding=session_binding,
                session_factory=bound,
            )
            targets = service.locate_own_result_targets(
                context=context, locator_sha256=locator_sha256
            )
            if targets is None:
                return None
            authorize = getattr(self._own_result_authorizer, "for_targets", None)
            if not targets or not callable(authorize):
                raise SchoolFactChangeRejected("Exact own receipt authorization is unavailable.")
            authorization = cast(OwnResultReadAuthorization, authorize(context, targets))
            if not targets <= authorization.allowed_targets:
                raise SchoolFactChangeRejected("The complete receipt target scope is unavailable.")
            result = service.check_own_result_by_locator(
                context=context,
                allowed_targets=targets,
                locator_sha256=locator_sha256,
                **(
                    {"creation_only_targets": authorization.creation_only_targets}
                    if getattr(authorization, "creation_only_targets", ())
                    else {}
                ),
            )
            original_proofs: list[OwnResultReadAuthorization] = []
            if original_fields is not None and result is not None:
                revised = list(result.targets)
                for index in range(target_offset, min(len(revised), target_offset + target_limit)):
                    target = revised[index]
                    fields = original_fields.get(target.fact_kind, ())
                    original = SchoolFactOwnOriginalResult(
                        "unsupported",
                        target.target_version,
                        target.operation,
                        fields,
                    )
                    if fields and target.fact_kind == "teaching_assignment":
                        authorize_original = getattr(
                            self._own_result_authorizer, "authorize_original", None
                        )
                        if target.creation_receipt or not callable(authorize_original):
                            original = replace(original, status="denied")
                        else:
                            try:
                                proof = cast(
                                    OwnResultReadAuthorization,
                                    authorize_original(context, target, fields),
                                )
                            except (PermissionError, SchoolFactChangeRejected, SecureReadDenied):
                                original = replace(original, status="denied")
                            else:
                                try:
                                    original = service.read_own_original_target(
                                        context=context,
                                        result=result,
                                        target=target,
                                        fields=fields,
                                    )
                                except Exception:
                                    original = replace(original, status="unavailable")
                                proof.revalidate()
                                original_proofs.append(proof)
                    revised[index] = replace(target, original_result=original)
                result = replace(result, targets=tuple(revised))
            try:
                with validation_batch() if original_fields is not None else nullcontext():
                    authorization.revalidate()
                    for proof in original_proofs:
                        proof.revalidate()
            except AssistantRuntimeError:
                raise SecureReadDenied(
                    "Original receipt authority changed before release."
                ) from None
            return result

    def read_own_target_page(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        offset: int,
        limit: int,
    ) -> SchoolFactOwnTargetPage | None:
        """Narrow owner terminal plus currently readable targets, never raw lineage."""

        self._require_expected_school(context.actor.school_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SchoolFactChangeRejected("Invalid own receipt page.")
        if self._own_result_authorizer is None or self._own_terminal_authorizer is None:
            raise SchoolFactChangeRejected("Own receipt reading is not enabled.")
        with self._engine.connect() as connection:
            bound = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
            # This established independent owner-receipt read route is checked
            # before even locating an original terminal. The raw service has no
            # product/HTTP route and does not decide disclosure.
            duty = self._own_terminal_authorizer(context)
            service = self._service(
                context=context, session_binding=session_binding, session_factory=bound
            )
            page = service.read_own_target_page(
                context=context, locator_sha256=locator_sha256, offset=offset, limit=limit
            )
            if page is None:
                duty.revalidate()
                return None
            if (
                page.terminal.locator_sha256 != locator_sha256
                or page.offset != offset
                or len(page.items) > limit
                or not offset <= page.next_offset <= offset + limit
                or not page.terminal.result.replayed
                or page.terminal.result.changed_count != page.terminal.target_count
                or page.terminal.target_count > 50_000
                or page.has_more != (page.next_offset < page.terminal.target_count)
                or len({(t.fact_kind, t.source_key) for t in page.items}) != len(page.items)
            ):
                raise SchoolFactChangeRejected("Own receipt page is inconsistent.")
            authorize = getattr(self._own_result_authorizer, "for_targets", None)
            if not callable(authorize):
                raise SchoolFactChangeRejected("Exact own receipt authorization is unavailable.")
            proofs: list[OwnResultReadAuthorization] = []
            permitted: list = []
            coordinates = frozenset((item.fact_kind, item.source_key) for item in page.items)
            if coordinates:
                try:
                    bulk = cast(OwnResultReadAuthorization, authorize(context, coordinates))
                except (PermissionError, SchoolFactChangeRejected):
                    bulk = None
                if bulk is not None and coordinates <= bulk.allowed_targets:
                    creation_only = frozenset(getattr(bulk, "creation_only_targets", ()))
                    permitted.extend(
                        target
                        for target in page.items
                        if (target.fact_kind, target.source_key) not in creation_only
                    )
                    proofs.append(bulk)
                else:
                    for target in page.items:
                        exact = frozenset({(target.fact_kind, target.source_key)})
                        try:
                            proof = cast(OwnResultReadAuthorization, authorize(context, exact))
                        except (PermissionError, SchoolFactChangeRejected):
                            continue
                        if exact <= proof.allowed_targets and not exact & frozenset(
                            getattr(proof, "creation_only_targets", ())
                        ):
                            permitted.append(target)
                            proofs.append(proof)
            try:
                duty.revalidate()
                for proof in proofs:
                    proof.revalidate()
            except AssistantRuntimeError:
                raise SecureReadDenied("Own receipt authority changed before release.") from None
            return replace(page, items=tuple(permitted))

    def _require_expected_school(self, school_id: SchoolId) -> None:
        if school_id != self._expected_school_id:
            raise SchoolFactChangeRejected("Writer Executor 学校锚点不匹配。")

    def _service(
        self,
        *,
        context: CommandExecutionContext | None = None,
        session_binding: str | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> SchoolFactChangeService:
        def envelope_factory(context: CommandExecutionContext):  # pyright: ignore[reportMissingReturnType]
            if session_binding is None:
                raise RuntimeError("当前固定操作不得建立数据库命令上下文。")
            return self._context_signer.sign_execution_context(
                context,
                session_nonce_hash=sha256(session_binding.encode("utf-8")).hexdigest(),
                proof_expires_at=datetime.now(UTC) + SCHOOL_FACT_CONFIRMATION_WINDOW,
            )

        persistence = SqlAlchemySchoolFactPersistence(
            self._session_factory if session_factory is None else session_factory,
            envelope_factory,
            self._credential_issuer,
            request_context=context,
            final_authorization_guard=self._final_authorization_guard,
            package_final_authorization_guard=self._package_final_authorization_guard,
            family_actor_validator=self._family_actor_validator,
            identity_tuple_validator=self._identity_tuple_validator,
        )
        return SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            family_actor_validator=self._family_actor_validator,
        )


def _apply_response(result: SchoolFactApplyResult) -> SchoolFactExecutorApplyResponse:
    return SchoolFactExecutorApplyResponse(
        batch_id=result.batch_id,
        target_version=result.target_version,
        changed_count=result.changed_count,
        replayed=result.replayed,
    )


def _result_response(lookup: SchoolFactResultLookup) -> SchoolFactExecutorResultResponse:
    return SchoolFactExecutorResultResponse(
        found=lookup.found,
        result=_apply_response(lookup.result) if lookup.result is not None else None,
    )


def _caller_key_bytes(value: bytes | str) -> bytes:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif type(value) is bytes:
        raw = value
    else:
        raise TypeError("Executor 调用者密钥类型无效。")
    if len(raw) < 32:
        raise ValueError("Executor 调用者密钥至少需要 32 字节。")
    return raw


def create_school_fact_writer_executor_app(
    *,
    kernel: SchoolFactWriterExecutorKernel,
    context_codec: CommandContextTransportCodec,
    caller_auth_key: bytes | str,
    lifespan: Callable[[FastAPI], object] | None = None,
    enable_own_results: bool = False,
    correction_kernel: FamilyCorrectionExecutorKernel | None = None,
) -> FastAPI:
    """建立固定内部写入端口，按显式装配开放本人回执和家长报错。"""

    expected_key = _caller_key_bytes(caller_auth_key)
    if enable_own_results and not isinstance(kernel, SchoolFactOwnResultExecutorKernel):
        raise ValueError("Own receipt routes require the independent read kernel.")
    app = FastAPI(
        title="School Fact Writer Executor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,  # type: ignore[arg-type]
    )

    def require_caller(
        supplied: str | None = Header(default=None, alias=EXECUTOR_AUTH_HEADER),
    ) -> None:
        supplied_bytes = supplied.encode("utf-8") if supplied is not None else b""
        if not hmac.compare_digest(supplied_bytes, expected_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Writer Executor caller authentication failed.",
            )

    def decode_context(token: str) -> CommandExecutionContext:
        try:
            return context_codec.decode(token)
        except CommandContextTransportRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Trusted command context is invalid.",
            ) from exc

    def require_plan_digest(change_set: CanonicalFactChangeSetV1, supplied: str) -> None:
        if not hmac.compare_digest(canonical_plan_sha256(change_set), supplied):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Canonical plan digest mismatch.",
            )

    @app.post(
        "/internal/v1/school-facts/preflight",
        response_model=PreflightReportV1,
        dependencies=[Depends(require_caller)],
    )
    def preflight(request: SchoolFactExecutorPreflightRequest) -> PreflightReportV1:  # pyright: ignore[reportUnusedFunction]
        return kernel.preflight(
            school_id=SchoolId(request.school_id),
            change_set=request.change_set,
            context=decode_context(request.context_token),
            session_binding=request.session_binding,
        )

    @app.post(
        "/internal/v1/school-facts/prepare",
        response_model=ConfirmationReceiptV1,
        dependencies=[Depends(require_caller)],
    )
    def prepare(request: SchoolFactExecutorPrepareRequest) -> ConfirmationReceiptV1:  # pyright: ignore[reportUnusedFunction]
        require_plan_digest(request.change_set, request.plan_sha256)
        return kernel.prepare(
            change_set=request.change_set,
            context=decode_context(request.context_token),
            session_binding=request.session_binding,
            provenance=(request.provenance.to_domain() if request.provenance is not None else None),
        )

    @app.post(
        "/internal/v1/school-facts/confirm",
        response_model=SchoolFactExecutorApplyResponse,
        dependencies=[Depends(require_caller)],
    )
    def confirm(request: SchoolFactExecutorConfirmRequest) -> SchoolFactExecutorApplyResponse:  # pyright: ignore[reportUnusedFunction]
        require_plan_digest(request.change_set, request.plan_sha256)
        return _apply_response(
            kernel.confirm(
                credential=request.credential,
                change_set=request.change_set,
                context=decode_context(request.context_token),
                session_binding=request.session_binding,
            )
        )

    @app.post(
        "/internal/v1/school-facts/result",
        response_model=SchoolFactExecutorResultResponse,
        dependencies=[Depends(require_caller)],
    )
    def result(  # pyright: ignore[reportUnusedFunction]
        request: SchoolFactExecutorResultRequest,
    ) -> SchoolFactExecutorResultResponse:
        if isinstance(request, SchoolFactExecutorDigestResultRequest):
            lookup = kernel.result_by_digest(
                school_id=SchoolId(request.school_id),
                source_sha256=request.source_sha256,
                plan_sha256=request.plan_sha256,
                context=decode_context(request.context_token),
                session_binding=request.session_binding,
            )
        else:
            lookup = kernel.result_by_locator(
                school_id=SchoolId(request.school_id),
                actor_user_id=request.actor_user_id,
                session_binding=request.session_binding,
                locator_sha256=request.locator_sha256,
                context=decode_context(request.context_token),
            )
        return _result_response(lookup)

    if enable_own_results and isinstance(kernel, SchoolFactOwnResultExecutorKernel):
        own_kernel = kernel

        if callable(getattr(own_kernel, "read_own_target_page", None)):

            @app.post(
                "/internal/v1/school-facts/own-results/target-page",
                response_model=SchoolFactExecutorOwnTargetPageResponse,
                dependencies=[Depends(require_caller)],
            )
            def own_target_page(  # pyright: ignore[reportUnusedFunction]
                request: SchoolFactExecutorOwnTargetPageRequest,
            ) -> SchoolFactExecutorOwnTargetPageResponse:
                try:
                    exact = cast(SqlAlchemySchoolFactWriterExecutorKernel, own_kernel)
                    page = exact.read_own_target_page(
                        context=decode_context(request.context_token),
                        session_binding=request.session_binding,
                        locator_sha256=request.locator_sha256,
                        offset=request.offset,
                        limit=request.limit,
                    )
                except SecureReadDenied as exc:
                    raise HTTPException(
                        status_code=409, detail="Current receipt authority changed."
                    ) from exc
                except SchoolFactAuthorizationUnavailable as exc:
                    raise HTTPException(
                        status_code=503, detail="Current receipt authority unavailable."
                    ) from exc
                except (SchoolFactChangeRejected, PermissionError) as exc:
                    raise HTTPException(
                        status_code=403, detail="Own receipt inaccessible."
                    ) from exc
                return SchoolFactExecutorOwnTargetPageResponse(
                    page=SchoolFactExecutorOwnTargetPage.from_domain(page) if page else None
                )

        @app.post(
            "/internal/v1/school-facts/own-results/list",
            response_model=SchoolFactExecutorOwnResultPage,
            dependencies=[Depends(require_caller)],
        )
        def own_results(  # pyright: ignore[reportUnusedFunction]
            request: SchoolFactExecutorOwnResultListRequest,
        ) -> SchoolFactExecutorOwnResultPage:
            try:
                page = own_kernel.list_own_results(
                    context=decode_context(request.context_token),
                    session_binding=request.session_binding,
                    limit=request.limit,
                )
            except SecureReadDenied as exc:
                raise HTTPException(
                    status_code=409, detail="Current receipt authority changed."
                ) from exc
            except SchoolFactAuthorizationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Current receipt authority unavailable."
                ) from exc
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403, detail="Own receipt inaccessible."
                ) from exc
            return SchoolFactExecutorOwnResultPage(
                items=tuple(SchoolFactExecutorOwnResult.from_domain(item) for item in page.items),
                has_more=page.has_more,
            )

        @app.post(
            "/internal/v1/school-facts/own-results/lookup",
            response_model=SchoolFactExecutorOwnResultLookupResponse,
            dependencies=[Depends(require_caller)],
        )
        def own_result(  # pyright: ignore[reportUnusedFunction]
            request: SchoolFactExecutorOwnResultLocatorRequest,
        ) -> SchoolFactExecutorOwnResultLookupResponse:
            options: OwnOriginalReadOptions = (
                {
                    "original_fields": request.original_fields,
                    "target_offset": request.target_offset,
                    "target_limit": request.target_limit,
                }
                if request.original_fields is not None
                else {}
            )
            try:
                item = own_kernel.check_own_result_by_locator(
                    context=decode_context(request.context_token),
                    session_binding=request.session_binding,
                    locator_sha256=request.locator_sha256,
                    **options,
                )
            except SecureReadDenied as exc:
                raise HTTPException(
                    status_code=409, detail="Current receipt authority changed."
                ) from exc
            except SchoolFactAuthorizationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Current receipt authority unavailable."
                ) from exc
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403, detail="Own receipt inaccessible."
                ) from exc
            return SchoolFactExecutorOwnResultLookupResponse(
                found=item is not None,
                item=SchoolFactExecutorOwnResult.from_domain(item) if item is not None else None,
            )

    if correction_kernel is not None:
        correction_app = create_family_correction_executor_app(
            kernel=correction_kernel,
            context_codec=context_codec,
            caller_auth_key=expected_key,
        )
        app.include_router(correction_app.router)

    @app.exception_handler(SchoolFactChangeRejected)
    async def rejected_change(  # pyright: ignore[reportUnusedFunction]
        _request: object, _exc: SchoolFactChangeRejected
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "School Fact change rejected."},
        )

    return app


def production_school_fact_writer_executor_app() -> FastAPI:
    """Uvicorn ``--factory`` 入口；错误 role/head/anchor 时在监听前失败关闭。"""

    settings = SchoolFactWriterSettings()  # pyright: ignore[reportCallIssue]
    engine = build_school_fact_writer_engine(settings)
    try:
        verify_school_fact_writer_database(engine, settings=settings)
    except Exception:
        engine.dispose()
        raise
    kernel = SqlAlchemySchoolFactWriterExecutorKernel(engine=engine, settings=settings)
    codec = CommandContextTransportCodec(settings.context_transport_hmac_key)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            engine.dispose()

    return create_school_fact_writer_executor_app(
        kernel=kernel,
        context_codec=codec,
        caller_auth_key=settings.caller_auth_key.get_secret_value(),
        lifespan=lifespan,
    )
