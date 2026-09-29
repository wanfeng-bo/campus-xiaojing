from __future__ import annotations

import base64
import hmac
import json
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from threading import Event, RLock, Thread
from typing import Protocol

from app.platform.master_data_imports.contracts import ProtectedImportSourceRef
from app.platform.master_data_imports.excel import RestrictedExcelInspector
from app.platform.master_data_imports.ports import (
    PrivateImportSourceAccessDenied,
    PrivateImportSourceCapacityExceeded,
    PrivateImportSourceExpired,
    PrivateSourceOwner,
    SchoolFactPlanLocator,
    StoredSchoolFactPlan,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, PreflightReportV1
from app.platform.school_facts.ports import (
    ConfirmationCredentialBinding,
    SchoolFactChangeProvenance,
)

_SOURCE_TTL = timedelta(hours=24)
_PLAN_STORE_DEFAULT_TOTAL_BYTES = 192 * 1024 * 1024
_PLAN_STORE_DEFAULT_OWNER_BYTES = 96 * 1024 * 1024


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("私有导入源时钟必须携带时区。")
    return value


def _is_single_school_reactivation(change_set: CanonicalFactChangeSetV1) -> bool:
    return (
        len(change_set.changes) == 1
        and change_set.changes[0].fact_kind == "school"
        and change_set.changes[0].operation == "reactivate"
    )


class HmacSchoolFactConfirmationCredentialIssuer:
    """以系统秘密值签发用途分离、可重放且不落明文的确认凭据。"""

    def __init__(self, signing_key: bytes) -> None:
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("学校事实确认签名密钥至少需要 32 字节。")
        self._signing_key = signing_key

    def issue(self, binding: ConfirmationCredentialBinding) -> str:
        material = json.dumps(
            {
                "batch_id": str(binding.batch_id),
                "school_id": str(binding.school_id),
                "actor_user_id": binding.actor_user_id,
                "session_sha256": binding.session_sha256,
                "idempotency_key_sha256": binding.idempotency_key_sha256,
                "plan_sha256": binding.plan_sha256,
                "source_sha256": binding.source_sha256,
                "base_version": binding.base_version,
                "result_locator_sha256": binding.result_locator_sha256,
                "expires_at": binding.expires_at.astimezone(UTC).isoformat(),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        digest = hmac.new(
            self._signing_key,
            b"school-facts-confirmation:v1\x00" + material,
            sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _ExpiringSchoolFactStore(Protocol):
    def purge_expired(self, *, now: datetime | None = None) -> int: ...


class SchoolFactEphemeralStoreJanitor:
    """候选实例生命周期内的常驻清理器；不依赖后续请求触发过期删除。"""

    def __init__(
        self,
        *stores: _ExpiringSchoolFactStore,
        interval_seconds: float = 60.0,
        on_sweep: Callable[[int], None] | None = None,
    ) -> None:
        if not stores:
            raise ValueError("学校事实临时存储清理器至少需要一个目标。")
        if not 0.01 <= interval_seconds <= 3600:
            raise ValueError("学校事实临时存储清理周期无效。")
        self._stores = stores
        self._interval_seconds = interval_seconds
        self._on_sweep = on_sweep
        self._stop = Event()
        self._thread: Thread | None = None
        self._failure_count = 0
        self._lock = RLock()

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("学校事实临时存储清理器已经启动。")
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="school-fact-ephemeral-store-janitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        thread.join(timeout=max(1.0, self._interval_seconds * 2))
        if thread.is_alive():
            raise RuntimeError("学校事实临时存储清理器未能停止。")
        with self._lock:
            self._thread = None

    def __enter__(self) -> SchoolFactEphemeralStoreJanitor:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            removed = 0
            for store in self._stores:
                try:
                    removed += store.purge_expired()
                except Exception:
                    with self._lock:
                        self._failure_count += 1
            if self._on_sweep is not None:
                try:
                    self._on_sweep(removed)
                except Exception:
                    with self._lock:
                        self._failure_count += 1


def school_fact_candidate_lifespan(
    janitor: SchoolFactEphemeralStoreJanitor,
) -> Callable[[object], AbstractAsyncContextManager[None]]:
    """只供隔离候选应用组合；主应用不调用此装配口。"""

    @asynccontextmanager
    async def lifespan(_app: object) -> AsyncGenerator[None]:
        janitor.start()
        try:
            yield
        finally:
            janitor.stop()

    return lifespan


@dataclass(slots=True)
class _PrivateSourceRecord:
    source: ProtectedImportSourceRef
    owner: PrivateSourceOwner
    content: bytearray
    expires_at: datetime


class InMemorySchoolFactPlanStore:
    """只供 SF3 隔离路由组合使用的短时计划适配器，不是业务事实源。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        max_records: int = 128,
        max_owner_records: int = 32,
        max_total_bytes: int = _PLAN_STORE_DEFAULT_TOTAL_BYTES,
        max_owner_bytes: int = _PLAN_STORE_DEFAULT_OWNER_BYTES,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: dict[str, StoredSchoolFactPlan] = {}
        self._locators: dict[str, SchoolFactPlanLocator] = {}
        self._sizes: dict[str, int] = {}
        self._locator_sizes: dict[str, int] = {}
        self._max_records = max_records
        self._max_owner_records = max_owner_records
        self._max_total_bytes = max_total_bytes
        self._max_owner_bytes = max_owner_bytes
        self._lock = RLock()

    def put(
        self,
        *,
        owner: PrivateSourceOwner,
        change_set: CanonicalFactChangeSetV1,
        provenance: SchoolFactChangeProvenance,
        preflight: PreflightReportV1,
        source_refs: tuple[str, ...] = (),
        package_request_json: str | None = None,
    ) -> StoredSchoolFactPlan:
        now = _require_aware(self._clock())
        plan_ref = f"sf3plan_{secrets.token_urlsafe(24)}"
        response_record = StoredSchoolFactPlan(
            plan_ref=plan_ref,
            owner=owner,
            change_set=change_set,
            provenance=replace(
                provenance,
                result_locator_sha256=sha256(plan_ref.encode("ascii")).hexdigest(),
            ),
            preflight=preflight,
            source_refs=source_refs,
            expires_at=now + timedelta(minutes=30),
            package_request_json=package_request_json,
        )
        retained_record = replace(
            response_record,
            preflight=PreflightReportV1(
                plan_sha256=response_record.preflight.plan_sha256,
                is_clean=response_record.preflight.is_clean,
                issues=(),
            ),
        )
        encoded_size = self._encoded_record_size(retained_record)
        with self._lock:
            self._purge_expired_locked(now)
            owner_record_refs = [
                ref for ref, candidate in self._records.items() if candidate.owner == owner
            ]
            owner_locator_refs = [
                ref for ref, candidate in self._locators.items() if candidate.owner == owner
            ]
            owner_bytes = sum(self._sizes[ref] for ref in owner_record_refs) + sum(
                self._locator_sizes[ref] for ref in owner_locator_refs
            )
            if (
                len(self._records) + len(self._locators) >= self._max_records
                or len(owner_record_refs) + len(owner_locator_refs) >= self._max_owner_records
                or sum(self._sizes.values()) + sum(self._locator_sizes.values()) + encoded_size
                > self._max_total_bytes
                or owner_bytes + encoded_size > self._max_owner_bytes
            ):
                raise PrivateImportSourceCapacityExceeded("PLAN_CAPACITY_EXCEEDED")
            self._records[plan_ref] = retained_record
            self._sizes[plan_ref] = encoded_size
        return response_record

    @staticmethod
    def _encoded_record_size(record: StoredSchoolFactPlan) -> int:
        """按完整短时记录估算配额，不让问题/血缘绕过容量边界。"""

        payload = {
            "plan_ref": record.plan_ref,
            "owner": asdict(record.owner),
            "change_set": record.change_set.model_dump(mode="json", exclude_none=True),
            "provenance": {
                **asdict(record.provenance),
                "replacement_targets": [t.model_dump(mode="json")
                                        for t in record.provenance.replacement_targets],
            },
            "preflight": record.preflight.model_dump(mode="json", exclude_none=True),
            "source_refs": record.source_refs,
            "package_request_json": record.package_request_json,
            "expires_at": record.expires_at.isoformat(),
        }
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def get(self, plan_ref: str, *, owner: PrivateSourceOwner) -> StoredSchoolFactPlan:
        now = _require_aware(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(plan_ref)
            if record is None:
                raise KeyError("PLAN_NOT_FOUND")
            if record.owner != owner:
                raise PrivateImportSourceAccessDenied("PLAN_ACCESS_DENIED")
            return record

    def locate(self, plan_ref: str, *, owner: PrivateSourceOwner) -> SchoolFactPlanLocator:
        now = _require_aware(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(plan_ref)
            locator = self._locators.get(plan_ref)
            candidate_owner = (
                record.owner if record is not None else locator.owner if locator else None
            )
            if candidate_owner is None:
                raise KeyError("PLAN_NOT_FOUND")
            if candidate_owner != owner:
                raise PrivateImportSourceAccessDenied("PLAN_ACCESS_DENIED")
            if locator is not None:
                return locator
            assert record is not None
            return SchoolFactPlanLocator(
                plan_ref=record.plan_ref,
                owner=record.owner,
                source_sha256=record.change_set.source_sha256,
                plan_sha256=record.preflight.plan_sha256,
                expires_at=record.expires_at,
                is_single_school_reactivation=_is_single_school_reactivation(record.change_set),
            )

    def retire(self, plan_ref: str, *, owner: PrivateSourceOwner) -> None:
        now = _require_aware(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(plan_ref)
            if record is None:
                locator = self._locators.get(plan_ref)
                if locator is not None and locator.owner != owner:
                    raise PrivateImportSourceAccessDenied("PLAN_ACCESS_DENIED")
                return
            if record.owner != owner:
                raise PrivateImportSourceAccessDenied("PLAN_ACCESS_DENIED")
            self._records.pop(plan_ref, None)
            self._sizes.pop(plan_ref, None)
            locator = SchoolFactPlanLocator(
                plan_ref=record.plan_ref,
                owner=record.owner,
                source_sha256=record.change_set.source_sha256,
                plan_sha256=record.preflight.plan_sha256,
                expires_at=now + timedelta(hours=24),
                is_single_school_reactivation=_is_single_school_reactivation(record.change_set),
            )
            self._locators[plan_ref] = locator
            self._locator_sizes[plan_ref] = len(
                (
                    locator.plan_ref
                    + locator.owner.school_id
                    + locator.owner.session_sha256
                    + locator.source_sha256
                    + locator.plan_sha256
                    + str(locator.is_single_school_reactivation)
                ).encode("utf-8")
            )

    def delete(self, plan_ref: str, *, owner: PrivateSourceOwner) -> None:
        with self._lock:
            record = self._records.get(plan_ref)
            locator = self._locators.get(plan_ref)
            candidate_owner = (
                record.owner if record is not None else locator.owner if locator else None
            )
            if candidate_owner is None:
                return
            if candidate_owner != owner:
                raise PrivateImportSourceAccessDenied("PLAN_ACCESS_DENIED")
            self._records.pop(plan_ref, None)
            self._sizes.pop(plan_ref, None)
            self._locators.pop(plan_ref, None)
            self._locator_sizes.pop(plan_ref, None)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        checked_at = _require_aware(now or self._clock())
        with self._lock:
            return self._purge_expired_locked(checked_at)

    def _purge_expired_locked(self, now: datetime) -> int:
        expired_records = [ref for ref, record in self._records.items() if now >= record.expires_at]
        expired_locators = [
            ref for ref, locator in self._locators.items() if now >= locator.expires_at
        ]
        for ref in expired_records:
            self._records.pop(ref, None)
            self._sizes.pop(ref, None)
        for ref in expired_locators:
            self._locators.pop(ref, None)
            self._locator_sizes.pop(ref, None)
        return len(expired_records) + len(expired_locators)


class InMemoryPrivateSchoolFactSourceStore:
    """候选阶段的私有临时源适配器；字节最长保留 24 小时。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        inspector: RestrictedExcelInspector | None = None,
        max_records: int = 32,
        max_owner_records: int = 32,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_owner_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._inspector = inspector or RestrictedExcelInspector(max_rows=10_001)
        self._records: dict[str, _PrivateSourceRecord] = {}
        self._max_records = max_records
        self._max_owner_records = max_owner_records
        self._max_total_bytes = max_total_bytes
        self._max_owner_bytes = max_owner_bytes
        self._lock = RLock()

    def put(
        self,
        *,
        filename_summary: str,
        content: bytes,
        owner: PrivateSourceOwner,
    ) -> ProtectedImportSourceRef:
        now = _require_aware(self._clock())
        if type(content) is not bytes or not content:
            raise ValueError("私有导入源必须是非空字节。")
        opaque_ref = f"sf3src_{secrets.token_urlsafe(24)}"
        if filename_summary.lower().endswith(".xlsx"):
            source = self._inspector.register(
                BytesIO(content),
                opaque_ref=opaque_ref,
                filename_summary=filename_summary,
            )
        elif filename_summary.lower().endswith((".json", ".txt")):
            # A private F7 candidate is opaque until its versioned package
            # adapter verifies the exact bytes, mapping and declared use.
            if len(content) > 10 * 1024 * 1024 or b"\x00" in content:
                raise ValueError("学校事实候选来源字节无效。")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("学校事实候选来源须为 UTF-8 文本。") from None
            source = ProtectedImportSourceRef(
                opaque_ref=opaque_ref,
                source_sha256=sha256(content).hexdigest(),
                filename_summary=filename_summary,
                size_bytes=len(content),
                worksheet_names=("f7_source",),
            )
        else:
            raise ValueError("学校事实候选来源类型未登记。")
        with self._lock:
            self._purge_expired_locked(now)
            self._ensure_capacity_locked(owner=owner, added_bytes=len(content))
            self._records[opaque_ref] = _PrivateSourceRecord(
                source=source,
                owner=owner,
                content=bytearray(content),
                expires_at=now + _SOURCE_TTL,
            )
        return source

    def open(
        self,
        opaque_ref: str,
        *,
        owner: PrivateSourceOwner,
        now: datetime | None = None,
    ) -> BytesIO:
        checked_at = _require_aware(now or self._clock())
        with self._lock:
            record = self._records.get(opaque_ref)
            if record is None:
                self._purge_expired_locked(checked_at)
                raise KeyError("SOURCE_NOT_FOUND")
            self._require_owner(record, owner)
            if checked_at >= record.expires_at:
                self._wipe_and_remove(opaque_ref, record)
                self._purge_expired_locked(checked_at)
                raise PrivateImportSourceExpired("SOURCE_EXPIRED")
            self._purge_expired_locked(checked_at)
            return BytesIO(bytes(record.content))

    def describe(
        self,
        opaque_ref: str,
        *,
        owner: PrivateSourceOwner,
        now: datetime | None = None,
    ) -> ProtectedImportSourceRef:
        checked_at = _require_aware(now or self._clock())
        with self._lock:
            record = self._records.get(opaque_ref)
            if record is None:
                self._purge_expired_locked(checked_at)
                raise KeyError("SOURCE_NOT_FOUND")
            self._require_owner(record, owner)
            if checked_at >= record.expires_at:
                self._wipe_and_remove(opaque_ref, record)
                self._purge_expired_locked(checked_at)
                raise PrivateImportSourceExpired("SOURCE_EXPIRED")
            self._purge_expired_locked(checked_at)
            return record.source

    def delete(self, opaque_ref: str, *, owner: PrivateSourceOwner) -> None:
        now = _require_aware(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(opaque_ref)
            if record is None:
                return
            self._require_owner(record, owner)
            self._wipe_and_remove(opaque_ref, record)

    def contains(self, opaque_ref: str, *, owner: PrivateSourceOwner) -> bool:
        now = _require_aware(self._clock())
        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(opaque_ref)
            if record is None:
                return False
            self._require_owner(record, owner)
            return True

    def purge_expired(self, *, now: datetime | None = None) -> int:
        checked_at = _require_aware(now or self._clock())
        with self._lock:
            return self._purge_expired_locked(checked_at)

    def _purge_expired_locked(self, now: datetime) -> int:
        expired = [
            (opaque_ref, record)
            for opaque_ref, record in self._records.items()
            if now >= record.expires_at
        ]
        for opaque_ref, record in expired:
            self._wipe_and_remove(opaque_ref, record)
        return len(expired)

    def _wipe_and_remove(self, opaque_ref: str, record: _PrivateSourceRecord) -> None:
        record.content[:] = b"\x00" * len(record.content)
        self._records.pop(opaque_ref, None)

    def _ensure_capacity_locked(
        self,
        *,
        owner: PrivateSourceOwner,
        added_bytes: int,
    ) -> None:
        owner_records = [record for record in self._records.values() if record.owner == owner]
        if (
            len(self._records) >= self._max_records
            or len(owner_records) >= self._max_owner_records
            or sum(len(record.content) for record in self._records.values()) + added_bytes
            > self._max_total_bytes
            or sum(len(record.content) for record in owner_records) + added_bytes
            > self._max_owner_bytes
        ):
            raise PrivateImportSourceCapacityExceeded("SOURCE_CAPACITY_EXCEEDED")

    @staticmethod
    def _require_owner(record: _PrivateSourceRecord, owner: PrivateSourceOwner) -> None:
        if record.owner != owner:
            raise PrivateImportSourceAccessDenied("SOURCE_ACCESS_DENIED")


__all__ = [
    "HmacSchoolFactConfirmationCredentialIssuer",
    "InMemoryPrivateSchoolFactSourceStore",
    "InMemorySchoolFactPlanStore",
    "SchoolFactEphemeralStoreJanitor",
    "school_fact_candidate_lifespan",
]
