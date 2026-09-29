import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.composition.school_fact_family_correction_product import PublicCorrectionOptions
from app.composition.school_fact_family_correction_writer import ExactCorrectionReceiptVerifier
from app.domains.students.family_corrections import FamilyCorrectionDenied


def test_multiple_education_targets_share_field_but_not_signed_reference():
    result = PublicCorrectionOptions.model_validate_json(
        json.dumps(
            {
                "items": [
                    {
                        "field_name": "class_source_key",
                        "label": "Class",
                        "reference": f"signed-source-{i}",
                    }
                    for i in range(9)
                ]
            }
        )
    )
    assert len(result.items) == 9


@pytest.mark.parametrize(
    "operation,expected", [("correct", "maintain"), ("cancel", "maintain"), ("end", "maintain")]
)
def test_education_resolution_requires_exact_field_changed(operation, expected):
    own, differences = MagicMock(), MagicMock()
    own.check.return_value = SimpleNamespace(
        targets=(
            SimpleNamespace(
                fact_kind="student_class_membership",
                source_key="member:1",
                operation=operation,
                target_version=2,
            ),
        )
    )
    differences.changed.return_value = True
    verifier = ExactCorrectionReceiptVerifier(own_results=own, differences=differences)
    kwargs = dict(
        actor=MagicMock(),
        locator_sha256="a" * 64,
        fact_kind="student_class_membership",
        source_key="member:1",
        field_name="class_source_key",
        minimum_target_version=1,
        submitted_at=datetime.now(UTC),
    )
    assert verifier.require_applied_target(**kwargs) == expected
    differences.changed.return_value = False
    with pytest.raises(FamilyCorrectionDenied):
        verifier.require_applied_target(**kwargs)


def test_staff_education_report_list_uses_current_signed_education_resolver():
    from uuid import uuid4

    from app.composition.school_fact_education_reader import EducationRepository
    from app.composition.school_fact_family_correction_product import FamilyCorrectionProduct
    from tests.school_facts.test_f4_education_reader import Authority, actor

    current_actor = actor()
    from app.platform.school_facts.f4_policy import F4FactScope

    class ReportAuthority(Authority):
        def require_scope(self, actor, *, fact_kind, action, checked_at):
            if not self.allowed:
                raise PermissionError("revoked")
            return SimpleNamespace(
                scope=F4FactScope(
                    actor.school_id, fact_kind, ("f4:class:a",), (), ("grade_source_key",)
                )
            )

    authority = ReportAuthority()
    repo = EducationRepository(projection=None, authority=authority, reference_key=b"x" * 32)
    reference = repo._reference(current_actor, "class_placement", "f4:class:a", False)
    executor, old = MagicMock(), MagicMock(side_effect=ValueError("F3 only"))
    executor.staff_list.return_value.model_dump.return_value = {"items": []}
    product = FamilyCorrectionProduct(
        executor=executor,
        current_actor=lambda _: True,
        staff_source=old,
        education_source=repo.source_for_reference,
    )
    identity = SimpleNamespace(actor=current_actor, session_binding="current")

    def read(ref=reference):
        return product.staff_list(
            identity,
            uuid4(),
            target_reference=ref,
            field_name="grade_source_key",
            required_action="maintain",
            limit=20,
        )

    assert read().items == ()
    assert executor.staff_list.call_args.kwargs["fact_kind"] == "class_placement"
    assert executor.staff_list.call_args.kwargs["source_key"] == "f4:class:a"
    old.assert_not_called()
    authority.allowed = False
    with pytest.raises(FamilyCorrectionDenied):
        read()
    executor.staff_list.assert_called_once()
    for ref in [reference.replace("f4:", "f4create:", 1), "f4:education_class:forged"]:
        with pytest.raises(FamilyCorrectionDenied):
            read(ref)
    executor.staff_list.assert_called_once()
