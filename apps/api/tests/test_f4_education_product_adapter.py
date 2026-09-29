from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.composition.school_fact_directory_product import FactChangeInput
from tests.test_f2_directory_product import actor

AT = datetime(2026, 9, 13, tzinfo=UTC)


class Repository:
    revoked = False

    def revalidate(self, **kwargs):
        if self.revoked:
            raise PermissionError('read authority changed')

    def read_records(self, **kwargs):
        return SimpleNamespace(proof=self, school_fact_version=5, records=({
            'fact_kind': 'education_class', 'source_key': 'f4:class:a', 'version': 2,
            'status': 'available', 'segments': [{
                'effective_at': '2025-09-01T00:00:00+00:00', 'valid_to': None,
                'revision': 2, 'content': {'display_name': '2023班'},
            }],
        },))

    def source_for_reference(self, **kwargs):
        if kwargs['reference'] != 'f4:education_class:readable':
            raise PermissionError('not authorized')
        return 'f4:class:a'

    def reference_for(self, **kwargs):
        return 'f4:education_class:readable'


def test_education_form_uses_readable_field_names_and_finite_reference_types():
    from app.composition.school_fact_education_product import education_field_descriptor
    assert education_field_descriptor('class_number', 'class_placement')['label'] == '班序'
    assert education_field_descriptor('class_ref', 'class_placement')['reference_kind'] == 'education_class'
    with pytest.raises(ValueError):
        education_field_descriptor('actor', 'class_placement')


def test_simple_edit_reuses_canonical_v3_and_immutable_explicit_change_summary():
    from app.composition.school_fact_education_product import EducationProductAdapter
    adapter = EducationProductAdapter(repository=Repository())
    payload = FactChangeInput(fact_kind='education_class', target_ref='f4:education_class:readable',
                              fields={'display_name': '正式新班名'}, reason='学校确定更名')
    result = adapter.resolve_change(actor=actor(), payload=payload, request_id=uuid4(), effective_at=AT)
    assert result.change_set.schema_version == 3
    assert result.change_set.base_version == 5
    change = result.change_set.changes[0]
    assert change.source_key == 'f4:class:a' and change.expected_version == 2
    assert change.content.display_name == '正式新班名'
    assert result.summary.before == {'display_name': '2023班'}
    assert result.summary.after == {'display_name': '正式新班名'}


def test_education_edit_does_not_trust_model_supplied_source_coordinates():
    from app.composition.school_fact_education_product import EducationProductAdapter
    adapter = EducationProductAdapter(repository=Repository())
    payload = FactChangeInput(fact_kind='education_class', target_ref='f4:education_class:readable',
                              fields={'person_source_key': 'private:another'}, reason='不得越界')
    with pytest.raises(ValueError):
        adapter.resolve_change(actor=actor(), payload=payload, request_id=uuid4(), effective_at=AT)


def test_change_preview_rechecks_the_read_authority_before_releasing_old_fields():
    from app.composition.school_fact_education_product import EducationProductAdapter
    repo = Repository()
    repo.revoked = True
    adapter = EducationProductAdapter(repository=repo)
    payload = FactChangeInput(fact_kind='education_class', target_ref='f4:education_class:readable',
                              fields={'display_name': '正式新班名'}, reason='学校确定更名')
    with pytest.raises(PermissionError, match='read authority changed'):
        adapter.resolve_change(actor=actor(), payload=payload, request_id=uuid4(), effective_at=AT)


def test_describe_uses_current_bounded_creation_catalog_once():
    from app.composition.school_fact_education_product import EducationProductAdapter
    from tests.school_facts.test_f4_education_reader import repository, actor as read_actor
    repo, projection, authority = repository()
    authority.configuration = SimpleNamespace(creation_catalog={
        "education_class": {"f4:class:new": "New", "f4:class:a": "Existing", "other": "Other"}
    })
    result = EducationProductAdapter(repository=repo).describe(
        actor=read_actor(), fact_kind="education_class"
    )
    assert [item["label"] for item in result["creation_choices"]] == ["New"]
    assert authority.checks == [("education_class", "maintain")]
    assert projection.calls == []
    authority.allowed = False
    repeated = EducationProductAdapter(repository=repo).describe(
        actor=read_actor(), fact_kind="education_class"
    )
    assert repeated["creation_choices"] == []
