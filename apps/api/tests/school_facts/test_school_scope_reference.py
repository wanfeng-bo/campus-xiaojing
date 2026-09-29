from uuid import UUID

import pytest

from app.platform.school_facts.school_scope_reference import (
    decode_school_scope_reference,
    encode_school_scope_reference,
    is_school_scope_reference,
)

KEY = b"private-school-reference-key-32bytes"
SCHOOL = UUID(int=1)
ASSIGNMENT = UUID(int=2)


def issue(**overrides):
    values = dict(
        secret=KEY,
        family="f3",
        school_id=SCHOOL,
        assignment_id=ASSIGNMENT,
        fact_kind="student",
        source_key="student:未来甲",
        creation=False,
    )
    values.update(overrides)
    return encode_school_scope_reference(**values)


def resolve(reference, **overrides):
    values = dict(
        secret=KEY,
        family="f3",
        school_id=SCHOOL,
        assignment_id=ASSIGNMENT,
        fact_kind="student",
        creation=False,
    )
    values.update(overrides)
    return decode_school_scope_reference(reference, **values)


def test_school_reference_roundtrip_binds_exact_actor_and_action():
    reference = issue()
    assert is_school_scope_reference(reference)
    assert resolve(reference) == "student:未来甲"
    for changed in (
        {"school_id": UUID(int=9)},
        {"assignment_id": UUID(int=9)},
        {"family": "f4"},
        {"fact_kind": "person"},
        {"creation": True},
        {"secret": b"another-private-key-32bytes-xxxxx"},
    ):
        with pytest.raises(ValueError):
            resolve(reference, **changed)


def test_school_reference_rejects_noncanonical_and_modified_payload():
    reference = issue()
    assert not is_school_scope_reference("f3:student:old")
    for changed in (
        reference + "=",
        reference.replace("student:", "person:", 1),
        reference[:-1] + ("0" if reference[-1] != "0" else "1"),
        reference.replace(":o:", ":c:", 1),
    ):
        with pytest.raises(ValueError):
            resolve(changed)


def test_dispatch_coordinates_are_shape_only_and_reject_malformed_reference():
    from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

    assert school_scope_reference_coordinates(
        "schoolv1:f5:teaching_assignment:o:YQ:" + "a" * 64
    ) == ("f5", "teaching_assignment", False)
    assert school_scope_reference_coordinates("schoolv1:f9:person:o:YQ:" + "a" * 64) is None
    assert school_scope_reference_coordinates("schoolv1:f5:teaching_assignment:c:YQ:x") is None


def test_full_length_unicode_source_key_fits_formal_reference_contract():
    from pydantic import TypeAdapter

    from app.composition.school_fact_directory_product import Reference

    reference = issue(source_key="校" * 255)
    assert resolve(reference) == "校" * 255
    assert TypeAdapter(Reference).validate_python(reference) == reference
