"""Deterministic approved synthetic F2 admin_v3 input; generates files, never publishes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.platform.master_data_imports.school_facts_json import (
    SchoolFactJsonInputV3,
    SchoolFactJsonPlanBuilder,
)

NOW = "2026-09-06T00:00:00Z"
END_MARKER = "\n【F2合成制度原文结束：不可用于真实学校】"


class ExistingF1Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    history_from: AwareDatetime
    history_to: AwareDatetime
    current_at: AwareDatetime = datetime(2026, 9, 6, tzinfo=UTC)
    base_version: int = Field(strict=True, ge=1)
    school_version: int = Field(strict=True, ge=1)
    school_content: dict[str, object]
    school_key: str = "school:f1-engineering"
    person_key: str = "person:f1-maintainer"
    organization_key: str = "org:f1-office"
    campus_key: str = "location:f1-main-campus"
    assignment_key: str = "assignment:f1-maintainer"

    @model_validator(mode="after")
    def chronological(self):
        if not self.history_from < self.history_to < self.current_at:
            raise ValueError("Verified school-effective historical interval required")
        return self


def synthetic_policy_text(length: int) -> str:
    if not 32768 <= length <= 100000:
        raise ValueError("F2 long-text fixture length must be 32768..100000 characters")
    unit = "【合成工程政策】此条仅用于完整原文、权限和版本验证；不定义真实学校制度。\n"
    size = length - len(END_MARKER)
    return (unit * (size // len(unit) + 1))[:size] + END_MARKER


def build_sample(inputs: ExistingF1Inputs) -> SchoolFactJsonInputV3:
    current_at = inputs.current_at.isoformat()
    rows = []

    def add(kind, key, content, *, at=current_at, expected=0):
        rows.append(
            dict(
                operation="upsert",
                fact_kind=kind,
                source_key=key,
                expected_version=expected,
                effective_at=at,
                content=content,
            )
        )

    # Exact current root content must come from the Reader inventory, never an invented replacement.
    add(
        "school",
        inputs.school_key,
        inputs.school_content,
        expected=inputs.school_version,
    )
    for suffix in ("a", "b", "missing"):
        add(
            "person",
            f"person:f2-{suffix}",
            dict(
                display_name="合成林同名" if suffix != "missing" else "合成缺录人员",
                person_kind="staff",
                lifecycle_status="active",
            ),
        )
    for suffix in ("a", "b"):
        add(
            "organization",
            f"org:f2-{suffix}",
            dict(
                parent_source_key=inputs.organization_key,
                kind="department",
                display_name="合成综合组",
                display_path=f"合成学校/合成{suffix}校区/综合组",
                status="active",
            ),
        )
    add(
        "location",
        "location:f2-campus",
        dict(
            kind="campus",
            display_name="合成第二校区",
            display_path="合成学校/合成第二校区",
            status="active",
        ),
    )
    for suffix, campus in (("a", inputs.campus_key), ("b", "location:f2-campus")):
        add(
            "location",
            f"location:f2-office-{suffix}",
            dict(
                parent_source_key=campus,
                kind="room",
                display_name="合成接待室",
                display_path=f"合成学校/{suffix}/合成接待室",
                status="active",
            ),
        )
        add(
            "organization_campus_relationship",
            f"org-campus:f2-{suffix}",
            dict(organization_source_key=f"org:f2-{suffix}", campus_source_key=campus),
        )
        add(
            "location_alias",
            f"location-alias:f2-{suffix}",
            dict(
                location_source_key=f"location:f2-office-{suffix}",
                alias="合成服务室",
                alias_kind="common",
            ),
        )
        add(
            "organization_profile",
            f"org-profile:f2-{suffix}",
            dict(
                organization_source_key=f"org:f2-{suffix}",
                organization_type="department",
                function_summary="合成资料登记与问询；成员关系不授予权限。",
                basis_reference="synthetic:f2-approved",
            ),
        )
        add(
            "organization_membership",
            f"org-member:f2-{suffix}",
            dict(
                person_source_key=inputs.person_key,
                organization_source_key=f"org:f2-{suffix}",
                membership_kind="participant",
                basis_reference="synthetic:f2-approved",
            ),
        )
        add(
            "organization_location",
            f"org-location:f2-{suffix}",
            dict(
                organization_source_key="org:f2-a",
                location_source_key=f"location:f2-office-{suffix}",
                purpose="office",
                is_primary=suffix == "a",
                basis_reference="synthetic:f2-approved",
            ),
        )
        add(
            "person_workplace",
            f"person-workplace:f2-{suffix}",
            dict(
                assignment_source_key=inputs.assignment_key,
                location_source_key=f"location:f2-office-{suffix}",
                purpose="reception",
                is_primary=suffix == "a",
                basis_reference="synthetic:f2-approved",
            ),
        )
    for suffix, at, until in (
        ("history", inputs.history_from.isoformat(), inputs.history_to.isoformat()),
        ("current", current_at, "2027-01-01T00:00:00Z"),
        ("future", "2027-01-01T00:00:00Z", None),
    ):
        content = dict(
            person_source_key="person:f2-a",
            organization_source_key="org:f2-a",
            position_title="合成资料联络岗",
            semantic_role_code="synthetic_liaison",
            assignment_kind="part_time",
            appointment_source="synthetic_engineering",
            approval_reference="synthetic:f2-approved",
            status="confirmed",
        )
        if until:
            content["valid_to"] = until
        # Historical relationship roots must exist at the same historical instant.
        add("assignment", f"assignment:f2-{suffix}", content, at=at)
    for key in ("person:f2-a", inputs.person_key):
        add(
            "person_contact",
            "person-contact:f2-" + key.split(":")[-1],
            dict(
                person_source_key=key,
                contact_kind="email",
                contact_value="synthetic-private@example.invalid",
                is_primary=True,
                visibility_scope="private",
            ),
        )
    for label, visibility in (
        ("public", "public"),
        ("internal", "school_authenticated"),
    ):
        add(
            "official_contact",
            f"official-contact:f2-{label}",
            dict(
                owner_kind="organization",
                organization_source_key="org:f2-a",
                contact_kind="email",
                contact_value=f"synthetic-{label}@example.invalid",
                lifecycle_status="active",
                visibility_scope=visibility,
            ),
        )
    for label, length in (
        ("public", 32768),
        ("internal", 50000),
        ("restricted", 100000),
    ):
        add("policy", f"policy:f2-{label}", {})
        add(
            "policy_version",
            f"policy-version:f2-{label}",
            dict(
                policy_source_key=f"policy:f2-{label}",
                business_code=f"SYNTHETIC-F2-{label}",
                display_name=f"合成{label}资料规则",
                category_code="general",
                classification=label,
                published_at=current_at,
                effective_from=current_at,
                content_text=synthetic_policy_text(length),
                publishing_organization_source_key="org:f2-a",
                version_note="用户批准的合成工程资料",
            ),
        )
        add(
            "policy_scope",
            f"policy-scope:f2-{label}",
            dict(
                policy_source_key=f"policy:f2-{label}",
                organization_source_key="org:f2-a",
            ),
        )
    add(
        "policy_scope",
        "policy-scope:f2-campus",
        dict(
            policy_source_key="policy:f2-public", campus_source_key="location:f2-campus"
        ),
    )
    add(
        "school_service",
        "service:f2-current",
        dict(
            display_name="合成资料问询",
            organization_source_key="org:f2-a",
            audience_description="获权校内身份",
            service_summary="说明服务窗口，不受理或自动完成业务。",
            basis_reference="synthetic:f2-approved",
            official_contact_source_key="official-contact:f2-internal",
            policy_source_key="policy:f2-public",
        ),
    )
    add(
        "school_service",
        "service:f2-missing-channel",
        dict(
            display_name="合成未登记渠道服务",
            organization_source_key="org:f2-b",
            audience_description="获权校内身份",
            service_summary="渠道尚未登记，不能推断无服务。",
            basis_reference="synthetic:f2-approved",
        ),
    )
    for kind, extra in (
        ("source", dict(source_status="unavailable")),
        ("coverage", dict(coverage_status="partial")),
        ("negative", dict(negative_code="no_service")),
        (
            "dispute",
            dict(
                dispute_status="open", evidence_reference="synthetic:evidence-dispute"
            ),
        ),
        ("review", dict(review_due_at="2026-09-07T00:00:00Z")),
        (
            "correction",
            dict(
                target_source_key="service:f2-current",
                replacement_source_key="service:f2-missing-channel",
                evidence_reference="synthetic:correction-evidence",
            ),
        ),
    ):
        if kind == "correction":
            continue  # Published only after the original service has formally ended.
        add(
            "fact_quality",
            f"quality:f2-{kind}",
            dict(
                quality_kind=kind,
                target_fact_kind="school_service",
                owner_kind="organization",
                organization_source_key="org:f2-b"
                if kind == "negative"
                else "org:f2-a",
                basis_reference="synthetic:f2-approved",
                responsibility="合成资料维护责任岗",
                verified_at=current_at,
                **extra,
            ),
        )
    # Historical interval is supplied from the verified existing school lifetime.
    for row in rows:
        if row["source_key"] in {"person:f2-a", "org:f2-a"}:
            row["effective_at"] = inputs.history_from.isoformat()
        if row["source_key"] == "org:f2-a":
            row["content"].pop("parent_source_key", None)
        if row["source_key"] == "service:f2-missing-channel":
            row["content"]["organization_source_key"] = "org:f2-a"
    request = SchoolFactJsonInputV3.model_validate_json(
        json.dumps(
            dict(
                input_version="admin_v3", base_version=inputs.base_version, changes=rows
            )
        )
    )
    SchoolFactJsonPlanBuilder().build(request)
    return request


def build_service_end(*, base_version: int, target_version: int, effective_at: str):
    """Separate confirmed batch; current version must be supplied by the Reader."""
    request = SchoolFactJsonInputV3.model_validate_json(
        json.dumps(
            dict(
                input_version="admin_v3",
                base_version=base_version,
                changes=[
                    dict(
                        operation="end",
                        fact_kind="school_service",
                        source_key="service:f2-current",
                        expected_version=target_version,
                        effective_at=effective_at,
                        content=None,
                    )
                ],
            )
        )
    )
    SchoolFactJsonPlanBuilder().build(request)
    return request


def build_correction(*, base_version: int, effective_at: str):
    """Only after build_service_end has a committed receipt, verified by formal preflight."""
    content = dict(
        quality_kind="correction",
        target_fact_kind="school_service",
        owner_kind="organization",
        organization_source_key="org:f2-a",
        target_source_key="service:f2-current",
        replacement_source_key="service:f2-missing-channel",
        evidence_reference="synthetic:correction-evidence",
        basis_reference="synthetic:f2-approved",
        responsibility="合成资料维护责任岗",
        verified_at=effective_at,
    )
    request = SchoolFactJsonInputV3.model_validate_json(
        json.dumps(
            dict(
                input_version="admin_v3",
                base_version=base_version,
                changes=[
                    dict(
                        operation="upsert",
                        fact_kind="fact_quality",
                        source_key="quality:f2-correction",
                        expected_version=0,
                        effective_at=effective_at,
                        content=content,
                    )
                ],
            )
        )
    )
    SchoolFactJsonPlanBuilder().build(request)
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = build_sample(
        ExistingF1Inputs.model_validate_json(args.existing.read_bytes())
    )
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(request.model_dump_json(indent=2, exclude_none=True) + "\n")


if __name__ == "__main__":
    main()
