from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.test_f2_directory_product import actor, prepare, setup

AT = datetime(2027, 9, 1, tzinfo=UTC)


class Proof:
    school_fact_version = 10
    denied = False

    def revalidate(self, **kwargs):
        if self.denied:
            raise PermissionError("label fields revoked")


def environment():
    proof = Proof()
    calls = []

    def read(**kw):
        calls.append(kw)
        assert kw["kind"] == "class_placement"
        assert kw["filter"] == {"class_source_key": ["a", "b"]}
        assert set(kw["fields"]) == {"class_source_key", "campus_source_key", "class_number"}
        rows = tuple(
            dict(
                fact_kind="class_placement",
                source_key="p:" + key,
                segments=[
                    dict(
                        effective_at="2026-09-01T00:00:00Z",
                        valid_to=None,
                        content=dict(
                            class_source_key=key, campus_source_key="campus:" + key, class_number=3
                        ),
                    )
                ],
            )
            for key in ("a", "b")
        )
        return SimpleNamespace(records=rows, proof=proof, as_of=AT, school_fact_version=10)

    def external(actor, kind, **kw):
        assert kind == "location" and kw["source_keys"] == ("campus:a", "campus:b")
        assert kw["fields"] == ("display_name",) and kw["as_of"] == AT
        return {
            "proof": proof,
            "records": [
                {
                    "source_key": "campus:" + key,
                    "content": {"display_name": "甲校区" if key == "a" else "乙校区"},
                }
                for key in ("a", "b")
            ],
        }

    return SimpleNamespace(read_records=read), SimpleNamespace(read_records=external), proof, calls


def lookup(repo, external):
    return import_module("app.composition.school_fact_education_labels").class_labels(
        repository=repo,
        external=external,
        actor=actor(),
        names={"a": "2028班", "b": "2028班"},
        as_of=AT,
        school_fact_version=10,
    )


def test_same_named_labels_use_only_exact_authorized_placement_and_campus_fields():
    repo, external, proof, calls = environment()
    result = lookup(repo, external)
    assert result.labels == {"a": "2028班 · 甲校区 · 3班", "b": "2028班 · 乙校区 · 3班"}
    assert len(calls) == 1
    proof.denied = True
    with pytest.raises(PermissionError):
        result.revalidate(actor=actor())


def test_missing_label_permission_does_not_expand_scope_or_invent_campus():
    repo, external, _, _ = environment()
    external.read_records = lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
    value = lookup(repo, external)
    assert all("未获准" in label and "甲校区" not in label for label in value.labels.values())


def test_label_sources_must_have_same_school_version():
    repo, external, proof, _ = environment()
    proof.school_fact_version = 11
    with pytest.raises(RuntimeError):
        lookup(repo, external)


@pytest.mark.parametrize("operation", ["proposal", "confirm"])
@pytest.mark.parametrize("reason", ["label grant expired", "label fact boundary changed"])
def test_optional_label_proof_blocks_later_preview_or_confirm_without_invoking_writer(
    operation, reason
):
    service, boundary, _, payload = setup()
    original = service._resolve
    valid = [True]

    def check(current):
        assert current == actor()
        if not valid[0]:
            raise PermissionError(reason)

    service._resolve = lambda **kw: replace(original(**kw), summary_revalidate=check)
    proposal = prepare(service, payload)
    valid[0] = False
    with pytest.raises(PermissionError, match=reason):
        method = getattr(service, operation)
        kw = dict(actor=actor(), session_binding="session-one", proposal_ref=proposal.proposal_ref)
        if operation == "confirm":
            kw["request_id"] = uuid4()
        method(**kw)
    assert not any(name == "confirm" for name, _ in boundary.calls)


def test_product_preview_consumes_label_and_retains_its_factual_time_proof(monkeypatch):
    from app.composition.school_fact_directory_product import FactChangeInput
    from app.composition.school_fact_education_labels import ClassLabels
    from app.composition.school_fact_education_product import EducationProductAdapter
    from tests.test_f4_education_product_adapter import Repository

    label_proof = Proof()
    seen = []

    def labels(**kwargs):
        seen.append(kwargs)
        return ClassLabels({"f4:class:a": "2023班 · 甲校区 · 3班"}, external=(label_proof,))

    monkeypatch.setattr("app.composition.school_fact_education_labels.class_labels", labels)
    adapter = EducationProductAdapter(repository=Repository())
    result = adapter.resolve_change(
        actor=actor(),
        request_id=uuid4(),
        effective_at=AT,
        payload=FactChangeInput(
            fact_kind="education_class",
            target_ref="f4:education_class:readable",
            fields={"display_name": "新班名"},
            reason="正式更名",
        ),
    )
    assert result.summary.label == "2023班 · 甲校区 · 3班"
    assert seen[0]["as_of"] == AT and seen[0]["names"] == {"f4:class:a": "2023班"}
    assert result.change_set.changes[0].content.display_name == "新班名"
    label_proof.denied = True
    with pytest.raises(PermissionError):
        result.summary_revalidate(actor())


def test_teaching_preview_names_are_exact_projected_and_revalidated():
    from app.composition.school_fact_directory_product import FactChangeSummary
    from app.composition.school_fact_education_labels import teaching_preview_labels

    proof = Proof()
    calls = []

    def read(actor, kind, **kw):
        calls.append((kind, kw))
        expected = ("person_source_key",) if kind == "assignment" else ("display_name",)
        assert kw["fields"] == expected
        assert kw["source_keys"] == (("job" if kind == "assignment" else "person"),)
        return {
            "proof": proof,
            "records": [
                {
                    "source_key": kw["source_keys"][0],
                    "content": {"person_source_key": "person"}
                    if kind == "assignment"
                    else {"display_name": "陈老师"},
                }
            ],
        }

    adapter = SimpleNamespace(
        external=SimpleNamespace(read_records=read), _source=lambda actor, kind, ref: "job"
    )
    summary = FactChangeSummary(
        fact_kind="teaching_assignment",
        label="任课",
        reference="target",
        operation="upsert",
        before={"assignment_ref": "signed:job"},
        after={"assignment_ref": "signed:job"},
        effective_at=AT,
        reason="核实",
        source_label="正式登记",
    )
    result = teaching_preview_labels(
        adapter=adapter, actor=actor(), summary=summary, as_of=AT, school_fact_version=10
    )
    assert result.labels == {"signed:job": "陈老师"}
    assert len(calls) == 2
    result.revalidate(actor=actor())
    proof.denied = True
    with pytest.raises(PermissionError):
        result.revalidate(actor=actor())


def test_teaching_preview_denial_preserves_reference_without_guessing():
    from app.composition.school_fact_education_labels import teaching_preview_labels

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    adapter = SimpleNamespace(
        external=SimpleNamespace(read_records=denied), _source=lambda *args: "job"
    )
    summary = SimpleNamespace(
        before={"assignment_ref": "signed:secret"},
        after=None,
        before_segments=(),
        after_segments=(),
    )
    result = teaching_preview_labels(
        adapter=adapter, actor=actor(), summary=summary, as_of=AT, school_fact_version=10
    )
    assert result.labels == {}


def test_teaching_preview_education_names_use_only_selected_current_name_fields():
    from app.composition.school_fact_education_labels import teaching_preview_labels

    proof = Proof()

    def read(actor, kind, **kw):
        assert kind == "teaching_offering" and kw["source_keys"] == ("math-b",)
        assert kw["fields"] == ("display_name",)
        return SimpleNamespace(
            school_fact_version=10,
            proof=proof,
            records=[
                {
                    "source_key": "math-b",
                    "segments": [
                        {
                            "effective_at": "2026-01-01T00:00:00Z",
                            "valid_to": None,
                            "content": {"display_name": "乙校区基础数学"},
                        }
                    ],
                }
            ],
        )

    adapter = SimpleNamespace(
        repository=SimpleNamespace(read_records=read), _source=lambda *args: "math-b"
    )
    summary = SimpleNamespace(
        before=None,
        after=None,
        before_segments=({"fields": {"offering_ref": "signed:math-b"}},),
        after_segments=(),
    )
    result = teaching_preview_labels(
        adapter=adapter, actor=actor(), summary=summary, as_of=AT, school_fact_version=10
    )
    assert result.labels == {"signed:math-b": "乙校区基础数学"}
    with pytest.raises(PermissionError):
        result.revalidate(actor=None)
    with pytest.raises(RuntimeError):
        teaching_preview_labels(
            adapter=adapter, actor=actor(), summary=summary, as_of=AT, school_fact_version=11
        )


def test_teaching_preview_rejects_unselected_external_label_rows():
    from app.composition.school_fact_education_labels import teaching_preview_labels

    adapter = SimpleNamespace(
        external=SimpleNamespace(
            read_records=lambda *args, **kw: {
                "proof": Proof(),
                "records": [{"source_key": "other", "content": {"person_source_key": "private"}}],
            }
        ),
        _source=lambda *args: "job",
    )
    summary = SimpleNamespace(
        before={"assignment_ref": "signed:job"}, after=None, before_segments=(), after_segments=()
    )
    assert (
        teaching_preview_labels(
            adapter=adapter, actor=actor(), summary=summary, as_of=AT, school_fact_version=10
        ).labels
        == {}
    )
