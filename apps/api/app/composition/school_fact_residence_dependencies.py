"""F6 uses existing family/education owners under its separate, exact dependencies."""

from typing import Any

from app.composition.school_fact_directory_product import FactChangeInput
from app.composition.school_fact_education_maintenance import resolve_family_with_education
from app.composition.school_fact_education_product import EducationChangeInput
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

ELIGIBILITY_KIND = "student_school_relationship"


class ResidenceEligibilityAdapter:
    def __init__(self, *, education: Any, lifecycle: Any) -> None:
        self.education, self.lifecycle = education, lifecycle

    def describe(self, *, actor: Any, fact_kind: str) -> Any:
        self._kind(fact_kind)
        value = self.education.family.query.describe(actor=actor, fact_kind=fact_kind)
        return {
            **value,
            "creation_choices": [],
            "label": "在校与寄宿方式",
            "notice": "这里调整已登记的在校与寄宿方式。离校或转为走读时，须一并核对相关安排。",
        }

    @staticmethod
    def _kind(kind: str) -> None:
        if kind != ELIGIBILITY_KIND:
            raise ValueError("此依赖入口仅处理已登记的在校与寄宿方式。")

    def query(self, *, actor: Any, fact_kind: str = ELIGIBILITY_KIND, **selection: Any) -> Any:
        self._kind(fact_kind)
        if selection.pop("cursor", None):
            raise ValueError("请重新选择已登记的学生在校关系。")
        selection = {key: value for key, value in selection.items() if value is not None}
        if set(selection) - {
            "reference",
            "name",
            "query_text",
            "student_number",
            "related_field",
            "related_ref",
            "as_of",
            "limit",
        }:
            raise ValueError("在校关系查询包含不支持的条件。")
        limit = selection.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("请核对在校关系查询数量。")
        # Preserve the original F3 bounded result and its incomplete/truncated marker.
        selection["limit"] = min(limit, 20)
        if "name" in selection and "query_text" in selection:
            raise ValueError("请只提供一个学生姓名条件。")
        name = selection.pop("name", selection.pop("query_text", None))
        number = selection.get("student_number")
        related = selection.get("related_ref")
        field = selection.get("related_field")
        if (
            related
            and not field
            and (
                related.startswith("f3:student:")
                or school_scope_reference_coordinates(related) == ("f3", "student", False)
            )
        ):
            selection["related_field"] = field = "student_ref"
        if (
            bool(related) != bool(field)
            or sum(bool(v) for v in (name or number, related, selection.get("reference"))) > 1
        ):
            raise ValueError("请选择唯一学生、在校关系或完整关联条件。")
        if name is not None:
            # Names belong to person. Reuse F3's finite authorized person→student
            # selection, including its ambiguity and field-level permission checks.
            selected = self.education.family.query.student_eligibility_profile(
                actor=actor,
                name=name,
                student_number=selection.pop("student_number", None),
                fields=("name",),
                as_of=selection.get("as_of"),
            )
            if selected["status"] != "known":
                return {
                    **selected,
                    "kind": "directory_facts",
                    "fact_kind": fact_kind,
                    "records": [],
                    "truncated": selected["status"] == "incomplete",
                }
            selection.update(
                related_field="student_ref", related_ref=selected["tool_context"]["student_ref"]
            )
        return self.education.family.query.query_eligibility(
            actor=actor, fact_kind=fact_kind, **selection
        )

    def reference_choices(self, **selection: Any) -> Any:
        self._kind(selection["fact_kind"])
        return self.education.family.query.reference_choices(**selection)

    def resolve_change(
        self, *, actor: Any, payload: Any, request_id: Any, effective_at: Any
    ) -> Any:
        self._kind(payload.fact_kind)
        if payload.creation_ref or getattr(payload, "additional_changes", ()):
            raise ValueError("请选择一项已有在校关系，系统会核对完整关联影响。")
        selected = EducationChangeInput.model_validate_json(
            payload.model_dump_json(include=set(FactChangeInput.model_fields))
        )
        result = resolve_family_with_education(
            maintenance=self.education.maintenance,
            fallback=self.education.family.adapter.resolve_change,
            actor=actor,
            payload=selected,
            request_id=request_id,
            effective_at=effective_at,
        )
        # Contract version separates the precisely approved dependency execution;
        # it never constitutes authority. Each owner is still finally checked.
        material = result.change_set.model_dump(mode="json", exclude_unset=True)
        material["schema_version"] = 4
        for change in material["changes"]:
            change["schema_version"] = 4
        import json
        from dataclasses import replace

        result = replace(
            result, change_set=CanonicalFactChangeSetV4.model_validate_json(json.dumps(material))
        )
        return self.lifecycle.expand(actor=actor, resolved=result)
