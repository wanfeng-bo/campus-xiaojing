"""Owned-only M16 equivalent: public batch preview plus real atomic Writer receipt."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
from app.composition.school_fact_education_product import (
    EducationBatchInput,
    EducationProductAdapter,
)
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    apply,
    read_rows,
)
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    graph as graph,
)
from tests.school_facts.test_f5_temporal_linkage_postgres import (
    owned_writer as owned_writer,
)

pytestmark = pytest.mark.skipif(not os.environ.get("F5_TEACHING_PG"), reason="explicit owned only")


def test_new_offering_source_assignment_preview_and_atomic_confirmation(graph):
    writer, k, now, at, _ = graph
    kinds = ("teaching_offering", "offering_member_source", "teaching_assignment")
    new = {kind: k("m16-" + kind) for kind in kinds}
    existing = {
        "teaching_course": (k("course"),),
        "teaching_offering": (k("offer-a"), k("offer-b")),
        "offering_member_source": (k("source-a"), k("source-b")),
        "teaching_assignment": (k("teacher-old"),),
    }

    class Repository:
        clock = staticmethod(lambda: datetime.now(UTC))

        def source_for_reference(self, *, reference, fact_kind, creation=False, **kw):
            source = reference.split(":", 2)[2]
            if creation and new.get(fact_kind) != source:
                raise PermissionError("creation outside batch")
            return source

        def reference_for(self, *, fact_kind, source_key, creation=False, **kw):
            if source_key in new.values() and not creation:
                raise PermissionError("not published read scope")
            return f"{'f4create' if creation else 'f4'}:{fact_kind}:{source_key}"

        def read_records(
            self, actor, kind, source_keys=None, fields=None, filter=None, creation=False, **kw
        ):
            keys = tuple(source_keys or existing.get(kind, ()))
            if not creation and any(s in new.values() for s in keys):
                raise PermissionError("not a read object")
            with writer[0]["reader"].connect() as conn:
                if creation:
                    raw = conn.scalar(
                        text("SELECT app.school_fact_f5_coordinates(:school,:kind,:keys)"),
                        dict(school=actor.school_id, kind=kind, keys=list(keys)),
                    )
                else:
                    raw = conn.scalar(
                        text(
                            "SELECT app.school_fact_f5_targets("
                            ":school,:kind,:keys,:fields,:at,NULL,CAST(:filter AS jsonb))"
                        ),
                        dict(
                            school=actor.school_id,
                            kind=kind,
                            keys=list(keys),
                            fields=list(fields or F5_FACT_FIELDS[kind]),
                            at=now,
                            filter=json.dumps(filter or {}),
                        ),
                    )
            version = raw["school_fact_version"]

            def revalidate(**kw):
                with writer[0]["reader"].connect() as conn:
                    assert (
                        conn.scalar(
                            text("SELECT app.school_fact_current_version(:school)"),
                            {"school": actor.school_id},
                        )
                        == version
                    )

            return SimpleNamespace(
                records=raw["records"],
                school_fact_version=version,
                proof=SimpleNamespace(revalidate=revalidate),
            )

    repository = Repository()
    coordinator = EducationMaintenanceCoordinator(
        EducationProductAdapter(repository=repository, external=repository), family=None
    )
    offer = f"f4create:teaching_offering:{new['teaching_offering']}"
    changes = [
        dict(
            fact_kind=kinds[0],
            creation_ref=offer,
            fields={"course_ref": f"f4:teaching_course:{k('course')}", "offering_status": "active"},
        ),
        dict(
            fact_kind=kinds[1],
            creation_ref=f"f4create:{kinds[1]}:{new[kinds[1]]}",
            fields={
                "offering_ref": offer,
                "source_kind": "administrative_class",
                "class_ref": f"f4:education_class:{k('class-b')}",
            },
        ),
        dict(
            fact_kind=kinds[2],
            creation_ref=f"f4create:{kinds[2]}:{new[kinds[2]]}",
            fields={
                "offering_ref": offer,
                "assignment_ref": f"f3:assignment:{k('old-staff')}",
                "teaching_role": "lead",
            },
        ),
    ]
    changes = [
        {**c, "operation": "upsert", "reason": "owned M16完整创建", "effective_at": now.isoformat()}
        for c in changes
    ]
    request = EducationBatchInput.model_validate_json(
        json.dumps({**changes[0], "additional_changes": changes[1:]})
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=request, request_id=uuid4(), effective_at=now
    )
    assert len(result.summary.impacts) == 3
    assert result.summary.impacts[1]["after"]["offering_ref"] == offer
    result.summary_revalidate(writer[1])
    receipt = apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    for kind, key in new.items():
        assert read_rows(writer, kind, [key], now)[0]["version"] == 1
    print("F5_M16_OWNED_BATCH", receipt.batch_id)
