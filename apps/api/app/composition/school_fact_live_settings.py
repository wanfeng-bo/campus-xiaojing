"""Fail-closed, explicit formal school-facts runtime and model approval."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic_settings import SettingsConfigDict

from app.composition.deepseek_assistant_model import CurrentModelApproval, ModelCallUsage
from app.composition.school_fact_current_model_budget import (
    SqliteCurrentModelCallPermit,
    _CurrentReservation,
)
from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_education_settings import EducationConfiguration
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_model_budget import ApprovedModelBudget, SqliteModelCallPermit
from app.composition.school_fact_product_settings import (
    ExplicitExecutorSettings,
    ExplicitReaderSettings,
    ProductConfigurationError,
    _InitOnlySettings,
    _invalid_number,
    _unique_object,
    require_matching_school_and_credentials,
)
from app.composition.school_fact_residence_settings import ResidenceConfiguration
from app.core.settings import Settings
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy


class ExplicitLiveRuntimeSettings(_InitOnlySettings, Settings):
    """Formal settings come only from the reviewed document, never ambient env/.env."""

    model_config = SettingsConfigDict(
        env_file=None, extra="forbid", frozen=True, hide_input_in_errors=True
    )
    postgres_password: str = Field(repr=False, min_length=32)
    redis_password: str = Field(repr=False, min_length=32)

    @model_validator(mode="after")
    def formal_runtime_only(self) -> Self:
        if (
            self.app_env != "production"
            or self.identity_provider != "dingtalk"
            or self.postgres_user != "campus_ai_runtime"
            or self.postgres_schema_contract not in {"school-facts-f7-v56", "school-facts-live-v57"}
            or not self.postgres_expected_anchor_id
            or not self.postgres_expected_school_id
            or not self.postgres_expected_system_identifier
            or not self.dingtalk_corp_id
            or self.identity_session_cookie_name != "school_fact_live_session"
            or self.repair_draft_model_enabled
            or self.repair_draft_model_api_key is not None
        ):
            raise ValueError(
                "Formal school facts requires explicit matching production schema and identity."
            )
        return self


class ApprovedLiveModelDataPolicy(BaseModel):
    """Reviewed decision references for the one supported external-model projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    approval_reference: str = Field(min_length=1, max_length=200)
    operator_notice_reference: str = Field(min_length=1, max_length=200)
    supplier_policy_reference: str = Field(min_length=1, max_length=200)
    projection_version: Literal["school-facts-live-v1"]
    allow_normal_names: Literal[True]
    chat_retention_hours: int = Field(strict=True, gt=0, le=24)

    @model_validator(mode="after")
    def references_are_explicit(self) -> Self:
        if any(
            value != value.strip()
            for value in (
                self.approval_reference,
                self.operator_notice_reference,
                self.supplier_policy_reference,
            )
        ):
            raise ValueError("Formal model data policy needs exact reviewed references.")
        return self


class ApprovedLiveProductModel(BaseModel):
    """Independent 100-yuan first-30-day ledger; current Flash rates stay explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    ledger_purpose: Literal["school-facts-live"]
    api_key_file: Path = Field(repr=False)
    model: Literal["deepseek-flash"]
    current_approval: CurrentModelApproval
    thinking: Literal["enabled"]
    reasoning_effort: Literal["high"]
    approval_reference: str = Field(min_length=1, max_length=200)
    budget: ApprovedModelBudget
    budget_window_start: datetime
    budget_window_end: datetime
    retention_approval_reference: str = Field(min_length=1, max_length=200)
    chat_retention_hours: int = Field(strict=True, gt=0, le=24)
    state_directory: Path
    max_input_tokens: int = Field(default=32768, strict=True, ge=1024, le=131072)
    max_output_tokens: int = Field(default=16384, strict=True, ge=1, le=32768)

    @field_validator("api_key_file")
    @classmethod
    def explicit_key_file(cls, value: Path) -> Path:
        if not value.is_absolute() or len(str(value)) > 4096:
            raise ValueError("An absolute backend model key file is required.")
        return value

    @field_validator("state_directory")
    @classmethod
    def explicit_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir():
            raise ValueError("Formal model metadata needs a provisioned absolute directory.")
        return value

    @model_validator(mode="after")
    def approved_live_model_policy(self) -> Self:
        if (
            self.current_approval.model != self.model
            or self.current_approval.approval_reference != self.approval_reference
            or self.budget.cap_fen != 10000
            or self.budget.input_cny_per_million != Decimal("3")
            or self.budget.output_cny_per_million != Decimal("9")
            or self.budget_window_start.utcoffset() is None
            or self.budget_window_end.utcoffset() is None
            or self.budget_window_end.astimezone(UTC)
            != self.budget_window_start.astimezone(UTC) + timedelta(days=30)
        ):
            raise ValueError("Formal model needs matching approval and fixed 30-day budget.")
        return self

    def require_current_window(self, now: datetime | None = None) -> None:
        """Check each proposed paid request against the fixed, nonrenewing window."""

        checked = datetime.now(UTC) if now is None else now
        if checked.utcoffset() is None:
            raise RuntimeError("Formal model budget requires an aware clock.")
        if not self.budget_window_start <= checked < self.budget_window_end:
            raise RuntimeError("Formal model budget window is closed.")


class LiveSchoolFactProductConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    purpose: Literal["school-facts-live"]
    runtime: ExplicitLiveRuntimeSettings
    reader: ExplicitReaderSettings
    executor: ExplicitExecutorSettings
    scope_id: UUID
    target_school_id: UUID
    target_corp_id: str = Field(min_length=1, max_length=200)
    allowed_origin: str
    trusted_proxy_cidrs: tuple[str, ...]
    control_signing_key: SecretStr
    assistant_enabled: StrictBool = True
    model: ApprovedLiveProductModel | None = None
    model_data_policy: ApprovedLiveModelDataPolicy | None = None
    directory: DirectoryConfiguration
    family: FamilyConfiguration
    education: EducationConfiguration
    family_leadership: FamilyLeadershipPolicy
    residence: ResidenceConfiguration
    f5_features_enabled: Literal[True] = True
    f6_features_enabled: Literal[True] = True
    f7_packages_enabled: StrictBool = True

    @field_validator("allowed_origin")
    @classmethod
    def exact_https_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
            address = ip_address(parsed.hostname or "")
        except ValueError:
            address = None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname == "localhost"
            or (address is not None and not address.is_global)
            or not parsed.hostname.isascii()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or value != value.strip()
        ):
            raise ValueError("Formal browser origin must be one exact public HTTPS origin.")
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def narrow_private_proxy_peers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            network = ip_network(entry, strict=True)
            if not network.is_private or network.num_addresses > 256:
                raise ValueError("Formal proxy peers must be narrow private networks.")
        return value

    @model_validator(mode="after")
    def complete_formal_dependencies(self) -> Self:
        runtime, reader = self.runtime, self.reader
        require_matching_school_and_credentials(
            runtime=runtime,
            reader=reader,
            executor=self.executor,
            control_signing_key=self.control_signing_key,
            extra_secrets=(runtime.dingtalk_app_secret or "",),
        )
        if (
            self.target_school_id != reader.expected_school_id
            or self.target_corp_id != runtime.dingtalk_corp_id
            or self.target_corp_id != self.target_corp_id.strip()
            or self.family_leadership.school_id != self.target_school_id
            or self.residence.dependency_education is None
        ):
            raise ValueError("Formal school, corp, dependency and model policy must agree.")
        if self.assistant_enabled:
            if (
                self.model is None
                or self.model_data_policy is None
                or self.model.chat_retention_hours != self.model_data_policy.chat_retention_hours
            ):
                raise ValueError("Enabled formal assistant requires matching model approvals.")
        elif self.model is not None or self.model_data_policy is not None:
            raise ValueError("Disabled formal assistant cannot retain model configuration.")
        return self


def load_school_fact_live_configuration() -> LiveSchoolFactProductConfiguration:
    """Load only a bounded reviewed document at an explicit absolute path."""

    try:
        raw_path = os.environ.get("SCHOOL_FACT_LIVE_CONFIG")
        if not raw_path:
            raise ValueError("missing configuration path")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("configuration path must be absolute")
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("configuration too large")
        json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
        return LiveSchoolFactProductConfiguration.model_validate_json(raw)
    except (OSError, ValueError, TypeError):
        raise ProductConfigurationError("Formal product configuration is unavailable.") from None


class _WindowedLiveModelCallPermit:
    def __init__(
        self,
        *,
        model: ApprovedLiveProductModel,
        delegate: SqliteCurrentModelCallPermit,
        clock: Callable[[], datetime],
    ) -> None:
        self._model, self._delegate, self._clock = model, delegate, clock

    @property
    def current_approval(self) -> CurrentModelApproval:
        return self._delegate.current_approval

    def reserve(self, max_input_tokens: int, max_output_tokens: int):
        self._model.require_current_window(self._clock())
        reservation = self._delegate.reserve(max_input_tokens, max_output_tokens)
        return _WindowedLiveReservation(self._model, reservation, self._clock)

    def summary(self) -> dict[str, int]:
        return self._delegate.summary()


class _WindowedLiveReservation:
    def __init__(
        self,
        model: ApprovedLiveProductModel,
        reservation: _CurrentReservation,
        clock: Callable[[], datetime],
    ):
        self._model, self._reservation, self._clock = model, reservation, clock

    def mark_transmission_started(self) -> None:
        try:
            self._model.require_current_window(self._clock())
        except RuntimeError:
            self._reservation.unknown()
            raise
        self._reservation.mark_transmission_started()

    def settle(self, usage: ModelCallUsage) -> None:
        self._reservation.settle(usage)

    def unknown(self) -> None:
        self._reservation.unknown()


def build_live_model_call_permit(
    model: ApprovedLiveProductModel,
    *,
    clock: Callable[[], datetime] | None = None,
) -> _WindowedLiveModelCallPermit:
    """Bind an independent ledger and recheck the approved window at each reserve."""

    checked_clock = clock or (lambda: datetime.now(UTC))
    ledger_path = model.state_directory / "school-facts-live-model-budget.sqlite3"
    SqliteModelCallPermit(path=ledger_path, policy=model.budget)
    delegate = SqliteCurrentModelCallPermit(
        path=ledger_path,
        ledger_policy=model.budget,
        current_approval=model.current_approval,
        ledger_purpose="school-facts-live",
        budget_window_start=model.budget_window_start,
        budget_window_end=model.budget_window_end,
        clock=checked_clock,
    )
    return _WindowedLiveModelCallPermit(model=model, delegate=delegate, clock=checked_clock)
