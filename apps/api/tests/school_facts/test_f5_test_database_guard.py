from importlib import util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def module():
    path = ROOT / "scripts/f5_engineering/prepare_test_database.py"
    assert path.exists(), "Owned F5 test database preparation is missing"
    spec = util.spec_from_file_location("f5_prepare_test", path)
    api = util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_owned_database_guard_rejects_live_or_wrong_cluster():
    api = module()
    valid = {
        "owner": api.OWNER,
        "database": "f3_" + "a" * 32 + "_test",
        "container": api.CONTAINER,
        "system_identifier": api.SYSID,
    }
    api.validate_target(valid)
    for field, value in [
        ("database", "campus_ai_school_facts_f1"),
        ("database", "f3_abc_test"),
        ("container", "other"),
        ("owner", "F4 persistence and reader controlled tests"),
        ("system_identifier", "wrong"),
    ]:
        with pytest.raises(ValueError):
            api.validate_target(valid | {field: value})


def test_fixture_anchor_copy_changes_only_new_database_coordinate():
    api = module()
    dump = (
        "COPY public.school_database_anchor (school_id, "
        "database_oid, database_name) FROM stdin;\nabc\t123\tf3_"
        + "b" * 32
        + "_test\n\\.\n"
    )
    new = "f3_" + "a" * 32 + "_test"
    value = api.adapt_fixture_anchor(dump, new, "456")
    assert "\nabc\t456\t" + new + "\n" in value
    with pytest.raises(ValueError):
        api.adapt_fixture_anchor("no anchor", new, "456")
    with pytest.raises(ValueError):
        api.adapt_fixture_anchor(dump, "original", "456")
