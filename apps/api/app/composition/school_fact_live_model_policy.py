"""First real-school model projection. Authorized display cards stay with the owner.

This is an allowlisted *model* view over owner results, not an authorization
decision. Unknown fields have no model release until classified by that owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from app.composition.school_fact_directory_adapter import FIELD_CONTEXT_LABELS, FIELD_LABELS
from app.composition.school_fact_live_settings import ApprovedLiveModelDataPolicy
from app.platform.assistant.runtime import (
    AssistantServiceError,
    ContinuationEvidence,
    ResultContinuation,
)
from app.platform.assistant.tool_loop import ToolExecutionResult, ToolGateway


class LiveModelDataPolicyError(AssistantServiceError):
    def __init__(self) -> None:
        super().__init__(
            "Real-school content cannot be sent under the approved data policy.",
            reason="data_policy",
        )


_SAFE_VALUES = frozenset(
    {
        "status",
        "code",
        "kind",
        "fact_kind",
        "name",
        "display_name",
        "school_name",
        "campus_name",
        "class_name",
        "class_label",
        "grade_name",
        "subject",
        "subject_name",
        "course_name",
        "teacher_name",
        "staff_name",
        "student_name",
        "role_name",
        "position_name",
        "office_name",
        "official_contact",
        "official_channel",
        "service_name",
        "location_name",
        "building_name",
        "relationship",
        "relationship_type",
        "scope",
        "query_scope",
        "as_of",
        "factual_at",
        "queried_at",
        "valid_from",
        "valid_to",
        "effective_at",
        "published_at",
        "verified_at",
        "review_due_at",
        "fact_timezone_name",
        "source_name",
        "source_type",
        "source_status",
        "evidence_status",
        "query_completion",
        "coverage",
        "truncated",
        "limit",
        "offset",
        "count",
        "total",
        "has_more",
        "version",
        "action_number",
        "reference",
        "target_ref",
        "related_ref",
        "creation_ref",
        "proposal_ref",
        "receipt_ref",
        "cursor",
        "result_state",
        "state",
        "reason",
        "limitations",
        "warnings",
        "available_fields",
        "allowed_fields",
        "fact_family",
        "field_name",
        "field_label",
        "value_label",
        "time_labels",
        "time_label",
        "status_label",
        "school_year",
        "term",
        "academic_year",
        "classroom_name",
        "teaching_group_name",
        "official_contact_name",
        "official_contact_type",
        "is_current",
        "is_active",
        "contact_kind",
        "service_hours",
        "label",
        "path",
        "description",
        "entity",
        "teacher_role",
        "teaching_role",
        "class_teacher_role",
        "role",
        "subjects",
        "relationship_kind",
        "enrollment_status",
        "residence_mode",
        "course",
        "offering",
        "offering_status",
        "teacher_status",
        "dispute_status",
        "effective_from",
        "effective_to",
        "future_basis",
        "outcome",
        "fact_type",
        "confirmation_requirement",
        "expires_at",
        "changed_count",
        "reading",
        "events",
        "field_values",
        "recovery",
        "reason_code",
        "record_version",
        "total_count",
        "target_count",
        "coverage_status",
        "negative_code",
        "quality_kind",
        "source_availability",
        "reading_limit",
        "applies_to_query",
        "applies_to_records",
        "owner_kind",
        "target_fact_kind",
        "item_index",
        "conditions",
        "source_statement",
        "child_ref",
        "district_name",
        "city_name",
        "province_name",
        "campus",
        "position_title",
        "assignment_kind",
        "role_kind",
        "organization",
        "owner_name",
        "channel_meaning",
        "owner",
        "subject_ref",
        "target_type",
        "relation",
        "target_name",
        "matched_count",
        "returned_count",
        "evidence_truncated",
        "query_text",
    }
)
_SAFE_CONTAINERS = frozenset(
    {
        "records",
        "facts",
        "results",
        "items",
        "context",
        "summary",
        "quality",
        "query",
        "query_scope",
        "resolved_subject",
        "resolution_checks",
        "field_states",
        "supporting_evidence",
        "answer_constraints",
        "tool_context",
        "issues",
        "original_results",
        "target",
        "subject",
        "source",
        "time_labels",
        "constraints",
        "coverage",
        "labels",
        "names",
        "classes",
        "courses",
        "teachers",
        "students",
        "official_channels",
        "members",
        "relationships",
        "candidates",
        "resolved_campuses",
        "listing",
        "fields",
        "field_labels",
        "profile",
        "notices",
        "reality_coverage",
        "related_context",
        "fact_reading",
        "authorized_result_set",
        "changes",
        "receipt",
        "receipts",
        "current_results",
        "evidence_scope",
        "read_detail",
        "current_read",
        "targets",
        "target_page",
        "read_next",
        "arguments",
        "established_absences",
        "issues_affecting_answer",
        "evidence",
        "details",
        "risks",
        "statements",
        "original",
        "current",
        "interval",
        "children",
        "region",
        "sources",
        "leaders",
        "campuses",
        "official_contacts",
        "relationship_facts",
        "basis",
        "channels",
        "audiences",
        "timeline",
        "query_window",
    }
)
_DIRECTORY_FIELD_RELEASE: dict[str, frozenset[str]] = {
    "school": frozenset({"display_name", "short_name", "timezone_name"}),
    "person": frozenset({"display_name", "person_kind"}),
    "organization": frozenset({"display_name", "short_name", "lifecycle_status"}),
    "assignment": frozenset(
        {"position_title", "assignment_kind", "appointment_source", "valid_from", "valid_to"}
    ),
    "organization_campus_relationship": frozenset({"valid_from", "valid_to"}),
    "location": frozenset({"display_name", "short_name", "display_path", "purpose"}),
    "location_alias": frozenset({"alias", "alias_kind", "valid_from", "valid_to"}),
    "person_contact": frozenset({"contact_kind", "visibility_scope", "valid_from", "valid_to"}),
    "official_contact": frozenset(
        {
            "contact_kind",
            "contact_value",
            "service_hours",
            "visibility_scope",
            "valid_from",
            "valid_to",
            "is_primary",
        }
    ),
    "policy": frozenset({"display_name", "classification"}),
    "policy_version": frozenset(
        {
            "classification",
            "published_at",
            "effective_from",
            "effective_to",
            "content_text",
            "version_note",
        }
    ),
    "policy_scope": frozenset({"audience_description", "valid_from", "valid_to"}),
    "organization_profile": frozenset({"display_name", "organization_type", "function_summary"}),
    "organization_membership": frozenset({"membership_kind", "valid_from", "valid_to"}),
    "organization_location": frozenset({"purpose", "service_hours", "valid_from", "valid_to"}),
    "person_workplace": frozenset({"purpose", "service_hours", "valid_from", "valid_to"}),
    "school_service": frozenset(
        {"display_name", "service_summary", "audience_description", "service_hours"}
    ),
    "fact_quality": frozenset(
        {
            "quality_kind",
            "coverage_status",
            "negative_code",
            "dispute_status",
            "source_status",
            "verified_at",
            "review_due_at",
            "responsibility",
            "last_success_at",
        }
    ),
}
_TRUSTED_VIEW_ONLY = frozenset(
    {
        "private_phone",
        "personal_phone",
        "mobile",
        "phone_number",
        "id_number",
        "identity_number",
        "certificate_number",
        "detailed_address",
        "home_address",
        "proof",
        "proof_document",
        "care_details",
        "family_sensitive_details",
        "bed_name",
        "bed_number",
        "room_name",
        "room_number",
        "bed_ref",
        "room_ref",
        "exact_bed",
        "exact_room",
        "contact_phone",
    }
)
_FORBIDDEN_KEYS = _TRUSTED_VIEW_ONLY | frozenset(
    {
        "password",
        "secret",
        "secret_key",
        "api_key",
        "access_token",
        "refresh_token",
        "confirmation_token",
        "signature",
        "dsn",
        "database_url",
        "private_key",
        "source_reference",
        "source_trace_reference",
    }
)
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PRIVATE_PATTERN = re.compile(
    r"(?<!\d)\d{17}[\dXx](?!\d)|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|数据库密码)\s*[:=]\s*[^\s,;]{4,}"
)


def project_live_material(material: dict[str, Any]) -> dict[str, Any]:
    """Release approved task facts, source/time/scope and opaque references only.

    A new structured field becomes an explicit unavailable state rather than
    being copied optimistically. Values with apparent personal identifiers are
    held even when a normally safe owner field happens to contain them.
    """
    if not isinstance(material, dict):
        raise LiveModelDataPolicyError()

    def visit(value: Any, *, official_item: bool = False, inherited_kind: str | None = None) -> Any:
        if isinstance(value, list):
            return [visit(item, inherited_kind=inherited_kind) for item in value]
        if not isinstance(value, dict):
            if isinstance(value, str) and (
                _PRIVATE_PATTERN.search(value)
                or _PHONE_PATTERN.search(value)
                or _SECRET_ASSIGNMENT.search(value)
            ):
                return "data_policy_unavailable"
            return value
        output: dict[str, Any] = {}
        held: dict[str, str] = {}
        unknown = 0
        context = value.get("tool_context")
        fact_kind = (
            value.get("fact_kind")
            or (context.get("fact_kind") if isinstance(context, dict) else None)
            or (value.get("kind") if value.get("kind") == "family_teaching" else None)
            or inherited_kind
        )
        exact_residence = fact_kind in {"dorm_bed", "dorm_room", "student_residence"}
        official_listing = (
            isinstance(value.get("query"), dict)
            and value["query"].get("subject") == "official_contacts"
            and isinstance(value.get("listing"), dict)
            and value["listing"].get("entity") == "contacts"
        )

        def directory_fields(fields: dict[str, Any], kind: str) -> dict[str, Any]:
            approved = _DIRECTORY_FIELD_RELEASE[kind]
            result: dict[str, Any] = {}
            held = 0
            for field, content in fields.items():
                if field not in approved or not isinstance(
                    content, (str, int, float, bool, type(None))
                ):
                    held += 1
                    continue
                if field == "contact_value" and fields.get("visibility_scope") != "public":
                    held += 1
                    continue
                if field == "content_text" and fields.get("classification") != "public":
                    held += 1
                    continue
                if isinstance(content, str) and (
                    _PRIVATE_PATTERN.search(content)
                    or _SECRET_ASSIGNMENT.search(content)
                    or (_PHONE_PATTERN.search(content) and field != "contact_value")
                ):
                    held += 1
                    continue
                result[field] = content
            if held:
                result["unclassified_fields"] = "data_policy_unavailable"
            return result

        def directory_readable_facts(
            fields: dict[str, Any], kind: str, *, missing: bool = False
        ) -> dict[str, Any]:
            approved = _DIRECTORY_FIELD_RELEASE[kind]
            allowed_labels = {
                FIELD_CONTEXT_LABELS.get((kind, field), FIELD_LABELS.get(field)): field
                for field in approved
            }
            public_scope = fields.get(FIELD_LABELS["visibility_scope"]) in {
                "public",
                "按正式发布范围公开",
            }
            public_policy = fields.get(FIELD_LABELS["classification"]) in {
                "public",
                "按正式发布范围公开",
            }
            result: dict[str, Any] = {}
            omitted = 0
            for label, content in fields.items():
                field = allowed_labels.get(label)
                if field is None or not isinstance(content, (str, int, float, bool, type(None))):
                    omitted += 1
                    continue
                if field == "contact_value" and not public_scope:
                    omitted += 1
                    continue
                if field == "content_text" and not public_policy:
                    omitted += 1
                    continue
                if missing and content != "记录未提供":
                    omitted += 1
                    continue
                if isinstance(content, str) and (
                    _PRIVATE_PATTERN.search(content)
                    or _SECRET_ASSIGNMENT.search(content)
                    or (_PHONE_PATTERN.search(content) and field != "contact_value")
                ):
                    omitted += 1
                    continue
                result[label] = content
            if omitted:
                result["unclassified_fields"] = "data_policy_unavailable"
            return result

        for key, item in value.items():
            if not isinstance(key, str):
                unknown += 1
                continue
            if key in _TRUSTED_VIEW_ONLY or (
                exact_residence and key in {"name", "display_name", "location_name"}
            ):
                held[key] = "trusted_view_only"
                continue
            if key == "contact_value" and official_item and isinstance(item, str):
                publication = value.get("source")
                published_channel = isinstance(publication, dict) and (
                    publication.get("kind") in {"formal_publication", "official"}
                    or publication.get("label") == "学校正式发布的组织或办公地点公开渠道"
                )
                if (
                    published_channel
                    and not _PRIVATE_PATTERN.search(item)
                    and not _SECRET_ASSIGNMENT.search(item)
                ):
                    output[key] = item
                else:
                    held[key] = "trusted_view_only"
                continue
            if (
                key == "fields"
                and isinstance(item, dict)
                and isinstance(fact_kind, str)
                and fact_kind in _DIRECTORY_FIELD_RELEASE
            ):
                output[key] = directory_fields(item, fact_kind)
                continue
            if (
                key in {"readable_facts", "missing_facts"}
                and isinstance(item, dict)
                and isinstance(fact_kind, str)
                and fact_kind in _DIRECTORY_FIELD_RELEASE
            ):
                output[key] = directory_readable_facts(
                    item, fact_kind, missing=key == "missing_facts"
                )
                continue
            if key == "changes" and isinstance(item, list) and value.get("kind") == "fact_proposal":
                output[key] = [
                    {
                        label: row[label]
                        for label in ("field", "meaning")
                        if isinstance(row.get(label), str)
                    }
                    for row in item
                    if isinstance(row, dict)
                ]
                continue
            if key in _SAFE_CONTAINERS or key in _SAFE_VALUES:
                if isinstance(item, (dict, list)):
                    output[key] = (
                        [
                            visit(entry, official_item=True, inherited_kind=fact_kind)
                            for entry in item
                        ]
                        if key == "items" and official_listing and isinstance(item, list)
                        else [
                            visit(entry, official_item=True, inherited_kind=fact_kind)
                            for entry in item
                        ]
                        if key == "official_contacts"
                        and fact_kind in {"family_school_leadership_leader", "family_teaching"}
                        and isinstance(item, list)
                        else [
                            visit(entry, inherited_kind="family_school_leadership_leader")
                            for entry in item
                        ]
                        if key == "leaders"
                        and value.get("kind") == "family_school_leadership"
                        and isinstance(item, list)
                        else visit(item, inherited_kind=fact_kind)
                    )
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    if isinstance(item, str) and (
                        _PRIVATE_PATTERN.search(item) or _PHONE_PATTERN.search(item)
                    ):
                        held[key] = "trusted_view_only"
                    else:
                        output[key] = item
                else:
                    unknown += 1
            else:
                unknown += 1
        if held:
            states = output.get("field_states")
            if not isinstance(states, dict):
                states = {}
            states.update(held)
            output["field_states"] = states
        if unknown:
            output["unclassified_fields"] = "data_policy_unavailable"
        return output

    return visit(material)


class _LiveGateway:
    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    @property
    def descriptors(self):
        return self._gateway.descriptors

    def revalidate(self, actor_context_ref: str) -> None:
        self._gateway.revalidate(actor_context_ref)

    def execute(self, call, context) -> ToolExecutionResult:
        result = self._gateway.execute(call, context)
        continuation = result.continuation
        if continuation is not None:
            original_load = continuation.load

            def projected_load() -> ContinuationEvidence:
                evidence = original_load()
                return replace(
                    evidence, model_content=project_live_material(evidence.model_content)
                )

            continuation = ResultContinuation(reference=continuation.reference, load=projected_load)
        return replace(
            result,
            model_content=project_live_material(result.model_content),
            history_content=project_live_material(result.history_content)
            if result.history_content is not None
            else None,
            continuation=continuation,
        )


def wrap_live_gateway(gateway: ToolGateway, policy: ApprovedLiveModelDataPolicy) -> ToolGateway:
    if (
        type(policy) is not ApprovedLiveModelDataPolicy
        or policy.projection_version != "school-facts-live-v1"
    ):
        raise LiveModelDataPolicyError()
    return _LiveGateway(gateway)


def check_live_user_text(text: str, *, known_credentials: tuple[str, ...] = ()) -> None:
    if (
        not isinstance(text, str)
        or _PRIVATE_PATTERN.search(text)
        or _PHONE_PATTERN.search(text)
        or _SECRET_ASSIGNMENT.search(text)
    ):
        raise LiveModelDataPolicyError()
    if any(secret and secret in text for secret in known_credentials):
        raise LiveModelDataPolicyError()


def guard_live_wire(encoded: bytes, *, known_credentials: tuple[str, ...] = ()) -> None:
    """Inspect the actual SDK envelope before budget reservation and HTTP send.

    Tool schemas are not user data. Every message role, including thinking and
    tool arguments, is checked. Parsing failures fail closed without logging it.
    """
    try:
        body = json.loads(encoded)
        messages = body["messages"]
        if not isinstance(messages, list):
            raise ValueError()

        def check_payload_values(value: Any) -> None:
            """Reject credential values anywhere in the request, including tool schemas."""
            if isinstance(value, str):
                if (
                    _PRIVATE_PATTERN.search(value)
                    or _SECRET_ASSIGNMENT.search(value)
                    or any(secret and secret in value for secret in known_credentials)
                ):
                    raise LiveModelDataPolicyError()
                if value.startswith(("{", "[")):
                    try:
                        nested = json.loads(value)
                    except (TypeError, ValueError):
                        return
                    check_payload_values(nested)
            elif isinstance(value, list):
                for item in value:
                    check_payload_values(item)
            elif isinstance(value, dict):
                for item in value.values():
                    check_payload_values(item)

        check_payload_values(body)

        def check(value: Any, *, role: str | None = None, parent_key: str | None = None) -> None:
            if isinstance(value, str):
                if _PRIVATE_PATTERN.search(value) or _SECRET_ASSIGNMENT.search(value):
                    raise LiveModelDataPolicyError()
                if any(secret and secret in value for secret in known_credentials):
                    raise LiveModelDataPolicyError()
                if role == "user" and _PHONE_PATTERN.search(value):
                    raise LiveModelDataPolicyError()
                if value.startswith("{") or value.startswith("["):
                    try:
                        parsed = json.loads(value)
                    except (TypeError, ValueError):
                        return
                    check(parsed, role=role, parent_key=parent_key)
            elif isinstance(value, list):
                for item in value:
                    check(item, role=role, parent_key=parent_key)
            elif isinstance(value, dict):
                role = value.get("role", role)
                for key, item in value.items():
                    key_name = str(key).lower()
                    if key_name in _FORBIDDEN_KEYS:
                        if parent_key == "field_states" and item == "trusted_view_only":
                            continue
                        raise LiveModelDataPolicyError()
                    check(item, role=role, parent_key=key_name)

        for message in messages:
            check(message)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise LiveModelDataPolicyError() from None
