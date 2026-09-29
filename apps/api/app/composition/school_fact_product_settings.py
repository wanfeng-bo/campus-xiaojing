"""Explicit F1 configuration; the provider key is loaded only from its named backend file."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Literal, Protocol, Self
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
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.composition.deepseek_assistant_model import CurrentModelApproval
from app.composition.school_fact_current_model_budget import ApprovedModelStageBudget
from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_education_settings import EducationConfiguration
from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_model_budget import ApprovedModelBudget
from app.composition.school_fact_reader import SchoolFactReaderSettings
from app.composition.school_fact_residence_settings import ResidenceConfiguration
from app.core.settings import Settings
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f5_policy import F5_FACT_KINDS, F5_OPERATIONS
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy


class ProductConfigurationError(RuntimeError):
    """Safe startup failure, without configuration content or secret paths."""


class _InitOnlySettings:
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class ExplicitRuntimeSettings(_InitOnlySettings, Settings):
    model_config = SettingsConfigDict(
        env_file=None, extra="forbid", frozen=True, hide_input_in_errors=True
    )
    postgres_password: str = Field(repr=False, min_length=32)
    redis_password: str = Field(repr=False, min_length=32)

    @model_validator(mode="after")
    def product_runtime_only(self) -> Self:
        if (
            self.app_env not in {"test", "development"}
            or self.identity_provider != "local_test"
            or self.postgres_user != "campus_ai_runtime"
            or self.postgres_schema_contract
            not in {
                "school-facts-f1-v22",
                "school-facts-f2-v25",
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
            or self.postgres_expected_anchor_id is None
            or self.postgres_expected_school_id is None
            or self.postgres_expected_system_identifier is None
            or self.repair_draft_model_enabled
            or self.repair_draft_model_api_key is not None
            or self.dingtalk_app_secret is not None
        ):
            raise ValueError("F1 requires the explicit local engineering runtime and schema22.")
        return self


class ExplicitReaderSettings(_InitOnlySettings, SchoolFactReaderSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="forbid", frozen=True, hide_input_in_errors=True
    )


class ExplicitExecutorSettings(_InitOnlySettings, SchoolFactExecutorClientSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="forbid", frozen=True, hide_input_in_errors=True
    )


def require_matching_school_and_credentials(
    *,
    runtime: Settings,
    reader: SchoolFactReaderSettings,
    executor: SchoolFactExecutorClientSettings,
    control_signing_key: SecretStr,
    extra_secrets: tuple[str, ...] = (),
) -> None:
    """Shared exact-school and independent-purpose check for engineering and live."""

    if (
        reader.schema_contract != runtime.postgres_schema_contract
        or (
            runtime.postgres_expected_anchor_id,
            runtime.postgres_expected_school_id,
            runtime.postgres_expected_system_identifier,
        )
        != (
            reader.expected_anchor_id,
            reader.expected_school_id,
            reader.expected_system_identifier,
        )
        or (runtime.postgres_host, runtime.postgres_port, runtime.postgres_db)
        != (reader.database_host, reader.database_port, reader.database_name)
    ):
        raise ValueError("Runtime and Reader must target one exact school and schema.")
    keys = (
        runtime.postgres_password,
        runtime.redis_password,
        runtime.rls_context_signing_key.get_secret_value(),
        reader.database_password.get_secret_value(),
        reader.cursor_hmac_key.get_secret_value(),
        executor.caller_auth_key.get_secret_value(),
        executor.context_transport_hmac_key.get_secret_value(),
        control_signing_key.get_secret_value(),
    ) + extra_secrets
    if any(len(key.encode("utf-8")) < 32 for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Credential purposes require independent keys of at least 32 bytes.")


class ApprovedProductModel(BaseModel):
    """No service/version/spend/retention candidate becomes an approval by default."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    api_key_file: Path = Field(repr=False)
    model: Literal["deepseek-v4-flash", "deepseek-flash"]
    current_approval: CurrentModelApproval | None = None
    stage_budget: ApprovedModelStageBudget | None = None
    thinking: Literal["enabled"]
    reasoning_effort: Literal["high"]
    approval_reference: str = Field(min_length=1, max_length=200)
    budget: ApprovedModelBudget
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

    @model_validator(mode="after")
    def approved_f1_model_policy(self) -> Self:
        if self.model == "deepseek-flash":
            if (
                self.current_approval is None
                or self.current_approval.model != self.model
                or self.current_approval.approval_reference != self.approval_reference
            ):
                raise ValueError("An explicit matching current model approval is required.")
        elif self.current_approval is not None:
            raise ValueError("Current model approval cannot authorize the legacy model.")
        # These original rates identify the shared ledger; current pricing belongs
        # to the separately bound current-model permit and never rewrites history.
        if (
            self.budget.cap_fen != 5000
            or self.budget.input_cny_per_million != Decimal("3")
            or self.budget.output_cny_per_million != Decimal("9")
        ):
            raise ValueError("F1 requires the approved cumulative cap and peak estimate rates.")
        return self

    @field_validator("state_directory")
    @classmethod
    def explicit_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir():
            raise ValueError(
                "Model metadata requires an explicitly provisioned absolute directory."
            )
        return value


class _ModelKeyFile(Protocol):
    @property
    def api_key_file(self) -> Path: ...


def load_product_model_api_key(configuration: _ModelKeyFile) -> SecretStr:
    """Read one bounded backend key file; never retain its path or content in an error."""
    try:
        with configuration.api_key_file.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized key")
        value = raw.decode("utf-8").strip()
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("invalid key")
        return SecretStr(value)
    except (OSError, ValueError, UnicodeError):
        pass
    # Raising after the handler drops the original decoding/I/O exception and its raw bytes.
    raise ProductConfigurationError("F1 model configuration is unavailable.")


class SchoolFactProductConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    purpose: Literal[
        "school-facts-f1-synthetic",
        "school-facts-f2-synthetic",
        "school-facts-f3-synthetic",
        "school-facts-f4-synthetic",
        "school-facts-f5-synthetic",
        "school-facts-f6-synthetic",
        "school-facts-f7-synthetic",
        "school-facts-live-integration-synthetic",
    ]
    runtime: ExplicitRuntimeSettings
    reader: ExplicitReaderSettings
    executor: ExplicitExecutorSettings
    scope_id: UUID
    allowed_origin: str
    trusted_proxy_cidrs: tuple[str, ...] = ()
    control_signing_key: SecretStr
    model: ApprovedProductModel | None = None
    directory: DirectoryConfiguration | None = None
    family: FamilyConfiguration | None = None
    education: EducationConfiguration | None = None
    family_leadership: FamilyLeadershipPolicy | None = None
    f5_features_enabled: StrictBool = True
    residence: ResidenceConfiguration | None = None
    f6_features_enabled: StrictBool = True
    f7_packages_enabled: StrictBool = True

    @field_validator("allowed_origin")
    @classmethod
    def loopback_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            loopback = (
                parsed.hostname == "localhost" or ip_address(parsed.hostname or "").is_loopback
            )
            _ = parsed.port
        except ValueError:
            loopback = False
        if (
            not loopback
            or parsed.scheme not in {"http", "https"}
            or parsed.path
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("F1 needs one exact local browser origin.")
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def narrow_proxies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            network = ip_network(entry, strict=True)
            if not network.is_private or network.num_addresses > 256:
                raise ValueError(
                    "Engineering proxy peers must be explicit narrow private networks."
                )
        return value

    @model_validator(mode="after")
    def one_school_and_separate_credentials(self) -> Self:
        runtime, reader = self.runtime, self.reader
        require_matching_school_and_credentials(
            runtime=runtime,
            reader=reader,
            executor=self.executor,
            control_signing_key=self.control_signing_key,
        )
        expected = {
            "school-facts-f1-synthetic": "school-facts-f1-v22",
            "school-facts-f2-synthetic": "school-facts-f2-v25",
            "school-facts-f3-synthetic": "school-facts-f3-v27",
            "school-facts-f4-synthetic": (
                "school-facts-f4-v29",
                "school-facts-f4-v30",
                "school-facts-f4-v31",
            ),
            "school-facts-f5-synthetic": ("school-facts-f5-v41",)
            if self.f5_features_enabled
            else (
                "school-facts-f5-v35",
                "school-facts-f5-v36",
                "school-facts-f5-v37",
                "school-facts-f5-v38",
                "school-facts-f5-v39",
                "school-facts-f5-v40",
                "school-facts-f5-v41",
            ),
            "school-facts-f6-synthetic": ("school-facts-f6-v44", "school-facts-f6-v45"),
            "school-facts-f7-synthetic": (
                "school-facts-f7-v47",
                "school-facts-f7-v48",
                "school-facts-f7-v49",
                "school-facts-f7-v50",
                "school-facts-f7-v51",
                "school-facts-f7-v52",
                "school-facts-f7-v53",
                "school-facts-f7-v54",
                "school-facts-f7-v55",
                "school-facts-f7-v56",
            ),
            "school-facts-live-integration-synthetic": (
                "school-facts-f7-v56", "school-facts-live-v57"
            ),
        }[self.purpose]
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if runtime.postgres_schema_contract not in allowed:
            raise ValueError("Product purpose requires its exact schema contract.")
        is_f3 = self.purpose == "school-facts-f3-synthetic"
        is_f4 = self.purpose == "school-facts-f4-synthetic"
        is_f5 = self.purpose == "school-facts-f5-synthetic"
        is_f6 = self.purpose == "school-facts-f6-synthetic"
        is_live_integration = self.purpose == "school-facts-live-integration-synthetic"
        is_f7 = self.purpose == "school-facts-f7-synthetic" or is_live_integration
        if not is_f7 and not self.f7_packages_enabled:
            raise ValueError("F7 package shutoff requires the F7 product purpose.")
        has_f6 = is_f6 or is_f7
        if not (is_f5 or has_f6) and not self.f5_features_enabled:
            raise ValueError("F5 compatibility mode requires the retained F5 purpose and budget.")
        if (self.residence is not None) != has_f6 or (not has_f6 and not self.f6_features_enabled):
            raise ValueError("Residence configuration and F6 engineering purpose must agree.")
        if has_f6 and self.residence is not None and self.residence.dependency_education is None:
            raise ValueError("F6 requires its independently approved dependency scopes.")
        if self.family_leadership is not None and (
            not (is_f5 or has_f6) or self.family_leadership.school_id != reader.expected_school_id
        ):
            raise ValueError("Leadership policy must belong to the configured F5 school.")
        if (self.directory is not None) != (self.purpose != "school-facts-f1-synthetic"):
            raise ValueError("Engineering purpose and directory approval must agree.")
        if (self.family is not None) != (is_f3 or is_f4 or is_f5 or has_f6):
            raise ValueError("Engineering purpose and family approval must agree.")
        if (self.education is not None) != (is_f4 or is_f5 or has_f6):
            raise ValueError("Engineering purpose and education approval must agree.")
        if (
            not (is_f5 or has_f6)
            and self.education is not None
            and (
                any(
                    binding.operation_code in F5_OPERATIONS
                    for binding in self.education.scope_bindings
                )
                or set(self.education.creation_catalog) & F5_FACT_KINDS
            )
        ):
            raise ValueError("Teaching coordinates require the complete approved F5 product.")
        stage = self.model.stage_budget if self.model is not None else None
        if is_f3 or is_f4 or is_f5 or has_f6:
            if (
                self.model is None
                or self.model.model != "deepseek-flash"
                or self.model.current_approval is None
                or stage is None
                or stage.stage_id
                != (
                    "school-facts-live-integration"
                    if is_live_integration
                    else "school-facts-f7"
                    if is_f7
                    else "school-facts-f6"
                    if is_f6
                    else "school-facts-f5"
                    if is_f5
                    else "school-facts-f4"
                    if is_f4
                    else "school-facts-f3"
                )
                or not (
                    stage.cap_microyuan <= 3_000_000
                    and stage.previous_cap_microyuan is None
                    or is_f5
                    and stage.cap_microyuan == 6_000_000
                    and stage.previous_cap_microyuan == 3_000_000
                    or is_f7
                    and not is_live_integration
                    and stage.cap_microyuan == 4_000_000
                    and stage.previous_cap_microyuan == 3_000_000
                    or is_live_integration
                    and stage.cap_microyuan <= 6_000_000
                    and stage.previous_cap_microyuan is None
                )
            ):
                raise ValueError(
                    "The product stage requires its approved current model and cumulative ceiling."
                )
        elif stage is not None:
            raise ValueError("A stage budget must match an explicitly approved product purpose.")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _invalid_number(_value: str) -> object:
    raise ValueError("invalid configuration number")


def load_school_fact_product_configuration() -> SchoolFactProductConfiguration:
    try:
        raw_path = os.environ.get("SCHOOL_FACT_PRODUCT_CONFIG")
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
        return SchoolFactProductConfiguration.model_validate_json(raw)
    except (OSError, ValueError, TypeError):
        raise ProductConfigurationError(
            "F1 explicit configuration is missing or invalid."
        ) from None


class _ProductFactFeatureFlags(Protocol):
    @property
    def f5_features_enabled(self) -> bool: ...

    @property
    def f6_features_enabled(self) -> bool: ...


def require_product_fact_features(
    configuration: _ProductFactFeatureFlags, change_set: CanonicalFactChangeSetV1
) -> None:
    """Disable new F5 proposals without changing installed Writer/receipt authorization."""
    f5_enabled = configuration.f5_features_enabled
    f6_enabled = configuration.f6_features_enabled
    if f5_enabled and f6_enabled:
        return
    for change in change_set.changes:
        if not f5_enabled and change.fact_kind in F5_FACT_KINDS:
            raise PermissionError("教学功能已停用，不能发起或确认新的教学变更。")
        if not f6_enabled and change.fact_kind in F6_FACT_KINDS:
            raise PermissionError("住宿维护已停用，不能发起或确认新的住宿变更。")
        contents = (change.content,) + tuple(
            segment.content for segment in (getattr(change, "replacement_segments", None) or ())
        )
        for content in contents:
            if (
                not f5_enabled
                and content is not None
                and any(
                    getattr(content, field, None) in F5_FACT_KINDS
                    for field in ("target_fact_kind", "education_target_kind")
                )
            ):
                raise PermissionError("教学功能已停用，不能修改新的教学质量或适用范围。")
            if (
                not f6_enabled
                and content is not None
                and (getattr(content, "target_fact_kind", None) in F6_FACT_KINDS)
            ):
                raise PermissionError("住宿维护已停用，不能修改住宿资料质量声明。")
