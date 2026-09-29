"""Intersect quality projections with the underlying family's current authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import ReadAuthorization
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import ScopePlan
from app.platform.contracts import StaffActorContext

from .f2_policy import F2_FACT_KINDS, F2FactScope


@dataclass(frozen=True, slots=True)
class _QualityAuthorization(ReadAuthorization):
    dependencies: tuple[ReadAuthorization, ...]


class F2ReadAuthorizationAdapter:
    def __init__(
        self,
        *,
        thin_service: object,
        scope_id_resolver: Callable[[ReadOperation, StaffActorContext], UUID],
        quality_target_kind: str | None = None,
        authorization_round: Callable[[StaffActorContext, datetime], AbstractContextManager[None]]
        | None = None,
        serial_reads: Callable[[], bool] | None = None,
    ):
        if quality_target_kind is not None and quality_target_kind not in F2_FACT_KINDS - {
            "fact_quality"
        }:
            raise ValueError("Invalid quality target family.")
        self._quality_target_kind = quality_target_kind
        self._authorization_round = authorization_round or (lambda actor, at: nullcontext())
        self._serial_reads = serial_reads or (lambda: False)
        self._base = ThinAuthorizationReadAdapter(
            thin_service=thin_service, scope_id_resolver=scope_id_resolver
        )

    def decide(
        self, *, operation: ReadOperation, actor: StaffActorContext, as_of: datetime
    ) -> ReadAuthorization:
        if operation.code not in {
            "school_facts.f2.fact_quality.read",
            "school_facts.f2.fact_quality.maintain",
        }:
            return self._base.decide(operation=operation, actor=actor, as_of=as_of)
        # This phase finishes before SecureReader can compile or fetch a row.
        with self._authorization_round(actor, as_of):
            return self._decide_quality(operation=operation, actor=actor, as_of=as_of)

    def _decide_quality(
        self, *, operation: ReadOperation, actor: StaffActorContext, as_of: datetime
    ) -> ReadAuthorization:
        original = self._base.decide(operation=operation, actor=actor, as_of=as_of)
        if not original.allowed or operation.code not in {
            "school_facts.f2.fact_quality.read",
            "school_facts.f2.fact_quality.maintain",
        }:
            return original
        scope = original.scope_plan
        if scope is None:
            return ReadAuthorization(False, None, None, None, None)
        purpose = operation.code.rsplit(".", 1)[1]
        dependencies = [original]
        target_scopes = []
        kinds = F2_FACT_KINDS - {"fact_quality"}
        if self._quality_target_kind is not None:
            kinds = frozenset(
                {self._quality_target_kind, "school", "organization", "person", "location"}
            )
        authorized_kinds: set[str] = set()
        ordered_kinds = sorted(kinds)

        def read_dependency(kind: str) -> ReadAuthorization:
            code = f"school_facts.f2.{kind}.{purpose}"
            return self._base.decide(
                operation=ReadOperation(code, f"f2_{kind}_{purpose}", code, "school_facts.f2"),
                actor=actor,
                as_of=as_of,
            )

        # Independent owner authorities retain their own sessions and all checks.
        # Only overlap IO within this call; no authority/result survives its boundary.
        # Two workers leave room for the surrounding Reader transaction in its pool.
        if getattr(self._base, "supports_batch", False):
            operations = tuple(
                ReadOperation(
                    f"school_facts.f2.{kind}.{purpose}",
                    f"f2_{kind}_{purpose}",
                    f"school_facts.f2.{kind}.{purpose}",
                    "school_facts.f2",
                )
                for kind in ordered_kinds
            )
            decisions = self._base.decide_many(operations=operations, actor=actor, as_of=as_of)
        elif self._serial_reads():
            decisions = [read_dependency(kind) for kind in ordered_kinds]
        else:
            with ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="fact-quality-read"
            ) as workers:
                futures = [
                    workers.submit(copy_context().run, read_dependency, kind)
                    for kind in ordered_kinds
                ]
                decisions = [future.result() for future in futures]
        for kind, dependency in zip(ordered_kinds, decisions, strict=True):
            if not dependency.allowed or dependency.scope_plan is None:
                continue
            try:
                target = F2FactScope.from_reference(dependency.scope_plan.reference)
            except (ValueError, TypeError):
                continue
            if target.fact_kind != kind or target.school_id != actor.school_id:
                continue
            authorized_kinds.add(kind)
            dependencies.append(dependency)
            target_scopes.append(target.reference)
        if (
            not target_scopes
            or self._quality_target_kind is not None
            and self._quality_target_kind not in authorized_kinds
        ):
            return ReadAuthorization(False, None, None, None, None)
        envelope: dict[str, object] = {
            "quality_scope": scope.reference,
            "target_scopes": target_scopes,
        }
        if self._quality_target_kind is not None:
            envelope["target_fact_kind"] = self._quality_target_kind
        reference = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(
            json.dumps([d.basis_fingerprint for d in dependencies], separators=(",", ":")).encode()
        ).hexdigest()
        return _QualityAuthorization(
            True,
            original.basis,
            ScopePlan(scope.scope_id, scope.owner, reference, scope.version),
            original.projection,
            fingerprint,
            tuple(dependencies),
        )

    def revalidate(
        self, authorization: object, *, actor: StaffActorContext, as_of: datetime
    ) -> bool:
        if type(authorization) is _QualityAuthorization:
            if getattr(self._base, "supports_batch", False) or self._serial_reads():
                with self._authorization_round(actor, as_of):
                    if getattr(self._base, "supports_batch", False):
                        for value in self._base.revalidate_many(
                            authorization.dependencies, actor=actor, as_of=as_of
                        ):
                            if isinstance(value, Exception):
                                raise value
                            if not value:
                                return False
                        return True
                    return all(
                        self._base.revalidate(dependency, actor=actor, as_of=as_of)
                        for dependency in authorization.dependencies
                    )
            # Each pre-read/post-read check owns a fresh round; none crosses IO.
            with (
                self._authorization_round(actor, as_of),
                ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="fact-quality-read"
                ) as workers,
            ):
                futures = [
                    workers.submit(
                        copy_context().run,
                        self._base.revalidate,
                        dependency,
                        actor=actor,
                        as_of=as_of,
                    )
                    for dependency in authorization.dependencies
                ]
                # Preserve ordered false/error precedence, and wait for every submitted
                # read to finish before leaving. No background read or cached approval.
                return all(future.result() for future in futures)
        return self._base.revalidate(authorization, actor=actor, as_of=as_of)
