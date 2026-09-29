from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypedDict, runtime_checkable
from uuid import UUID

from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    require_neutral_lineage_code,
)
from app.platform.school_facts.replacement_contracts import (
    MAX_REPLACEMENT_AUDIT_ARTIFACTS,
    MAX_REPLACEMENT_AUDIT_BYTES,
    REPLACEMENT_AUDIT_CHUNK,
    REPLACEMENT_AUDIT_PREFIX,
    PackageReplacementTarget,
)


@dataclass(frozen=True, slots=True)
class FactVersionProjection:
    version: int


@dataclass(frozen=True, slots=True)
class ResidenceFactProjection:
    student_source_key: str
    bed_source_key: str


@dataclass(frozen=True, slots=True)
class SchoolFactSourceArtifact:
    """长期保存的安全来源清单：只含逻辑槽、摘要和大小。"""

    logical_source_code: str
    source_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        require_neutral_lineage_code(self.logical_source_code)
        if len(self.source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_sha256
        ):
            raise ValueError("学校事实来源文件摘要无效。")
        if not 1 <= self.size_bytes <= 10 * 1024 * 1024:
            raise ValueError("学校事实来源文件大小无效。")


@dataclass(frozen=True, slots=True)
class SchoolFactRowProvenance:
    """适配后事实对应的安全原行血缘，不保存原表名、路径或单元格原文。"""

    fact_kind: str
    fact_source_key: str
    raw_source_sha256: str
    source_slot: str
    source_sheet_code: str
    source_row_number: int
    target_field_codes: tuple[str, ...]
    source_field_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        require_neutral_lineage_code(self.fact_kind)
        if not self.fact_source_key:
            raise ValueError("学校事实原行目标无效。")
        if len(self.raw_source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.raw_source_sha256
        ):
            raise ValueError("学校事实原行来源摘要无效。")
        require_neutral_lineage_code(self.source_slot)
        require_neutral_lineage_code(self.source_sheet_code)
        if not 2 <= self.source_row_number <= 10_001:
            raise ValueError("学校事实原行行号无效。")
        if not self.target_field_codes or len(set(self.target_field_codes)) != len(
            self.target_field_codes
        ):
            raise ValueError("学校事实原行目标字段无效。")
        for field_code in self.target_field_codes:
            require_neutral_lineage_code(field_code)
        if len(self.source_field_codes) != len(self.target_field_codes):
            raise ValueError("学校事实原行字段血缘不完整。")
        for field_code in self.source_field_codes:
            require_neutral_lineage_code(field_code)


@dataclass(frozen=True, slots=True)
class SchoolFactPolicyTextAttachment:
    """A separately hashed input whose exact text is already in one canonical policy change."""

    source_slot: str
    source_sha256: str
    fact_source_key: str
    field_code: Literal["content_text"] = "content_text"

    def __post_init__(self) -> None:
        require_neutral_lineage_code(self.source_slot)
        if len(self.source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_sha256
        ):
            raise ValueError("学校制度正文附件摘要无效。")
        if not self.fact_source_key or len(self.fact_source_key) > 255:
            raise ValueError("学校制度正文附件目标无效。")


@dataclass(frozen=True, slots=True)
class SchoolFactChangeProvenance:
    """通过唯一 SF2 写核持久的机械来源血缘，不携带原文或对象键。"""

    channel: Literal["admin", "excel", "package"]
    raw_source_sha256: str
    template_version: str
    mapping_version: str
    mapping_config_sha256: str
    source_size_bytes: int | None
    worksheet_names: tuple[str, ...]
    source_manifest: tuple[SchoolFactSourceArtifact, ...] = ()
    row_lineage: tuple[SchoolFactRowProvenance, ...] = ()
    policy_attachments: tuple[SchoolFactPolicyTextAttachment, ...] = ()
    result_locator_sha256: str | None = None
    replacement_targets: tuple[PackageReplacementTarget, ...] = ()

    def __post_init__(self) -> None:
        if len(self.raw_source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.raw_source_sha256
        ):
            raise ValueError("学校事实来源摘要无效。")
        if not self.template_version or not self.mapping_version:
            raise ValueError("学校事实来源版本无效。")
        if len(self.mapping_config_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.mapping_config_sha256
        ):
            raise ValueError("学校事实映射配置摘要无效。")
        if (
            len(self.source_manifest) > (32 + MAX_REPLACEMENT_AUDIT_ARTIFACTS
                                         if self.channel == "package" else 32)
            or len(self.row_lineage) > 50_000
            or len(self.policy_attachments) > 32
        ):
            raise ValueError("学校事实来源血缘超过边界。")
        if self.result_locator_sha256 is not None and (
            len(self.result_locator_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.result_locator_sha256)
        ):
            raise ValueError("学校事实结果定位摘要无效。")
        manifest_codes = tuple(item.logical_source_code for item in self.source_manifest)
        if self.replacement_targets and (
            self.channel != "package" or len(self.replacement_targets) > 50_000
            or len({(t.fact_kind, t.source_key) for t in self.replacement_targets})
            != len(self.replacement_targets)
            or any(type(t) is not PackageReplacementTarget for t in self.replacement_targets)
        ):
            raise ValueError("完整替换的选定对象证据无效。")
        if len(set(manifest_codes)) != len(manifest_codes):
            raise ValueError("学校事实逻辑来源编码重复。")
        if self.channel == "excel":
            if self.policy_attachments:
                raise ValueError("Excel 来源不能混入独立正文附件。")
            if self.source_size_bytes is None or not (
                1 <= self.source_size_bytes <= 10 * 1024 * 1024
            ):
                raise ValueError("Excel 来源字节数无效。")
            if not self.worksheet_names:
                raise ValueError("Excel 来源工作表集合不能为空。")
            if not self.source_manifest:
                raise ValueError("Excel 来源安全清单不能为空。")
        elif self.channel == "package":
            if self.template_version != "school_facts_v5" or self.worksheet_names:
                raise ValueError("全范围来源模板或工作表元数据无效。")
            if not self.source_manifest or not self.row_lineage:
                raise ValueError("全范围来源缺少安全清单或原行血缘。")
            total_bytes = sum(item.size_bytes for item in self.source_manifest)
            user_sources = tuple(s for s in self.source_manifest
                                 if not s.logical_source_code.startswith(REPLACEMENT_AUDIT_PREFIX))
            derived = tuple(s for s in self.source_manifest if s not in user_sources)
            if (len(user_sources) > 32
                    or sum(s.size_bytes for s in user_sources) > 10 * 1024 * 1024
                    or self.source_size_bytes != total_bytes):
                raise ValueError("全范围来源总字节数无效。")
            if derived:
                if (not self.replacement_targets
                        or len(derived) > MAX_REPLACEMENT_AUDIT_ARTIFACTS
                        or tuple(s.logical_source_code for s in derived) != tuple(
                            f"{REPLACEMENT_AUDIT_PREFIX}{i + 1}" for i in range(len(derived)))
                        or any(s.size_bytes > MAX_REPLACEMENT_AUDIT_BYTES for s in derived)):
                    raise ValueError("完整替换内部审计来源超过独立边界。")
                for source in derived:
                    rows = tuple(r for r in self.row_lineage
                                 if r.source_slot == source.logical_source_code)
                    if (not 1 <= len(rows) <= REPLACEMENT_AUDIT_CHUNK
                            or tuple(r.source_row_number for r in rows) != tuple(
                                range(2, len(rows) + 2))):
                        raise ValueError("完整替换内部审计来源没有有界派生变更。")
            manifest_by_slot = {item.logical_source_code: item for item in self.source_manifest}
            if any(
                (source := manifest_by_slot.get(item.source_slot)) is None
                or source.source_sha256 != item.raw_source_sha256
                for item in self.row_lineage
            ):
                raise ValueError("全范围原行血缘与来源清单不一致。")
            if len({item.source_slot for item in self.policy_attachments}) != len(
                self.policy_attachments
            ) or any(
                (source := manifest_by_slot.get(item.source_slot)) is None
                or source.source_sha256 != item.source_sha256
                for item in self.policy_attachments
            ):
                raise ValueError("制度正文附件与来源清单不一致。")
        elif (
            self.source_size_bytes is not None
            or self.worksheet_names
            or self.source_manifest
            or self.row_lineage
            or self.policy_attachments
        ):
            raise ValueError("管理台来源不能伪装 Excel 元数据。")


@dataclass(frozen=True, slots=True)
class PreparedConfirmation:
    school_id: SchoolId
    actor_user_id: int
    actor_person_id: UUID
    actor_assignment_id: UUID
    actor_assignment_version: int
    authorization_version: int
    request_id: UUID
    idempotency_key_sha256: str
    plan_sha256: str
    source_sha256: str
    base_version: int
    session_sha256: str
    expires_at: datetime
    change_set: CanonicalFactChangeSetV1
    provenance: SchoolFactChangeProvenance | None = None


@dataclass(frozen=True, slots=True)
class PreparedConfirmationResult:
    batch_id: UUID
    credential: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmationCredentialBinding:
    batch_id: UUID
    school_id: SchoolId
    actor_user_id: int
    session_sha256: str
    idempotency_key_sha256: str
    plan_sha256: str
    source_sha256: str
    base_version: int
    result_locator_sha256: str | None
    expires_at: datetime


class SchoolFactConfirmationCredentialIssuer(Protocol):
    """用系统持有的秘密值机械签发可重放确认凭据。"""

    def issue(self, binding: ConfirmationCredentialBinding) -> str: ...


@dataclass(frozen=True, slots=True)
class SchoolFactApplyResult:
    batch_id: UUID
    target_version: int
    changed_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class SchoolFactSchoolReactivationProjection:
    """停用学校预检的最小数据库投影；不开放普通事实读取。"""

    replay_result: SchoolFactApplyResult | None
    current_fact_version: int
    current_target_version: int | None


@dataclass(frozen=True, slots=True)
class SchoolFactResultLookup:
    found: bool
    result: SchoolFactApplyResult | None

    def __post_init__(self) -> None:
        if not self.found and self.result is not None:
            raise ValueError("不存在的学校事实计划不能携带结果。")


SchoolFactOwnResultScope = frozenset[tuple[str, str]]


OWN_ORIGINAL_FIELDS = frozenset(
    {
        "assignment_source_key",
        "offering_source_key",
        "teaching_role",
    }
)


class OwnOriginalReadOptions(TypedDict, total=False):
    original_fields: dict[str, tuple[str, ...]]
    target_offset: int
    target_limit: int


def validate_own_original_selection(
    fields: dict[str, tuple[str, ...]] | None, offset: int, limit: int
) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Original receipt page must contain at most three targets.")
    if fields is not None and (
        type(fields) is not dict
        or set(fields) != {"teaching_assignment"}
        or not fields["teaching_assignment"]
        or len(set(fields["teaching_assignment"])) != len(fields["teaching_assignment"])
        or not set(fields["teaching_assignment"]) <= OWN_ORIGINAL_FIELDS
    ):
        raise ValueError("Original receipt fields must use the installed finite projection.")


@dataclass(frozen=True, slots=True)
class SchoolFactOwnOriginalSegment:
    effective_at: datetime
    valid_to: datetime | None
    content: dict[str, str]


@dataclass(frozen=True, slots=True)
class SchoolFactOwnOriginalResult:
    status: Literal["available", "denied", "unsupported", "unavailable"]
    target_version: int
    operation: str
    field_names: tuple[str, ...]
    segments: tuple[SchoolFactOwnOriginalSegment, ...] = ()
    before_segments: tuple[SchoolFactOwnOriginalSegment, ...] = ()
    field_changes: bool = False


@dataclass(frozen=True, slots=True)
class SchoolFactOwnResultTarget:
    fact_kind: str
    source_key: str
    operation: str
    target_version: int
    creation_receipt: bool = False
    original_result: SchoolFactOwnOriginalResult | None = None


@dataclass(frozen=True, slots=True)
class SchoolFactOwnResult:
    """Backend-only projection; no old session, credential or original plan text."""

    locator_sha256: str
    confirmed_at: datetime
    result: SchoolFactApplyResult
    targets: tuple[SchoolFactOwnResultTarget, ...]


@dataclass(frozen=True, slots=True)
class SchoolFactOwnResultPage:
    items: tuple[SchoolFactOwnResult, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class SchoolFactOwnTerminal:
    """Owner-bound original outcome; intentionally contains no target coordinates."""

    locator_sha256: str
    confirmed_at: datetime
    result: SchoolFactApplyResult
    target_count: int


@dataclass(frozen=True, slots=True)
class SchoolFactOwnTargetPage:
    """Writer-internal bounded coordinates; release only after current target authorization."""

    terminal: SchoolFactOwnTerminal
    items: tuple[SchoolFactOwnResultTarget, ...]
    offset: int
    next_offset: int
    has_more: bool


@runtime_checkable
class SchoolFactOwnTerminalReader(Protocol):
    def find_own_terminal_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
    ) -> SchoolFactOwnTerminal | None: ...


@runtime_checkable
class SchoolFactOwnTargetPageReader(Protocol):
    def read_own_target_page(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
        offset: int,
        limit: int,
    ) -> SchoolFactOwnTargetPage | None: ...


@runtime_checkable
class SchoolFactOwnOriginalReader(Protocol):
    def read_own_original_target(
        self,
        actor: object,
        result: SchoolFactOwnResult,
        target: SchoolFactOwnResultTarget,
        fields: tuple[str, ...],
    ) -> SchoolFactOwnOriginalResult: ...


@runtime_checkable
class SchoolFactOwnResultMetadataReader(Protocol):
    """Writer-internal owner-bound coordinates; never receipt or fact content."""

    def find_own_result_targets(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
    ) -> SchoolFactOwnResultScope | None: ...


@runtime_checkable
class SchoolFactOwnResultReader(Protocol):
    def find_own_result_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
        allowed_targets: SchoolFactOwnResultScope,
        *,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResult | None: ...

    def list_own_results(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        allowed_targets: SchoolFactOwnResultScope,
        limit: int,
        *,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResultPage: ...


@runtime_checkable
class SchoolFactRequestContextBinder(Protocol):
    """Return an immutable request-scoped port; never mutate shared context state."""

    def for_request(self, context: CommandExecutionContext) -> object: ...


@runtime_checkable
class SchoolFactPersistentResultReader(Protocol):
    """按服务端持久摘要恢复确认结果，不依赖进程内完整计划。"""

    def find_result_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        session_sha256: str,
        locator_sha256: str,
    ) -> SchoolFactResultLookup: ...


@runtime_checkable
class SchoolFactSchoolReactivationPreflightReader(Protocol):
    """单 school/reactivate 专用预检口，避免绑定已失效的普通学校上下文。"""

    def preflight_school_reactivation(
        self,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        plan_sha256: str,
    ) -> SchoolFactSchoolReactivationProjection: ...


class SchoolFactPreflightReader(Protocol):
    def current_school_fact_version(self, school_id: SchoolId) -> int: ...

    def find_fact_version(
        self,
        school_id: SchoolId,
        fact_kind: str,
        source_key: str,
    ) -> FactVersionProjection | None: ...

    def unresolved_reference_codes(
        self,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
    ) -> tuple[str, ...]: ...

    def find_applied_plan(
        self,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
    ) -> SchoolFactApplyResult | None: ...


class OrganizationCampusFactReader(Protocol):
    """只按显式有效关系读取组织覆盖校区；零行永远表示零覆盖。"""

    def find_organization_campus_ids(
        self,
        school_id: SchoolId,
        organization_source_key: str,
        effective_at: datetime,
    ) -> tuple[UUID, ...]: ...


@runtime_checkable
class SchoolFactBulkPreflightReader(Protocol):
    """大批量预检的有界读取口；只合并查询，不改变任何预检规则。"""

    def find_fact_versions_bulk(
        self,
        school_id: SchoolId,
        targets: tuple[tuple[str, str], ...],
    ) -> dict[tuple[str, str], FactVersionProjection]: ...

    def unresolved_reference_codes_bulk(
        self,
        school_id: SchoolId,
        changes: tuple[CanonicalFactChangeV1, ...],
    ) -> dict[tuple[str, str], tuple[str, ...]]: ...

    def find_residence_student_source_keys_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, str]: ...


@runtime_checkable
class SchoolFactResidencePreflightReader(Protocol):
    """调宿语义所需的窄投影，不暴露 Residence 事实间边。"""

    def find_residence_facts_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, ResidenceFactProjection]: ...


class SchoolFactTransactionalWriter(Protocol):
    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult: ...

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult: ...
