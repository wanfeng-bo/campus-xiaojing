from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from tests.test_f3_product_identity import guardian

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def api():
    name = "app.domains.students.family_read"
    assert util.find_spec(name) is not None, "formal family Reader is not implemented"
    return import_module(name)


def setup(*, caregiver=False):
    mod = api()
    actor = guardian()
    basis = mod.ParentReadBasis(
        school_id=UUID(str(actor.school_id)),
        adult_person_id=UUID(str(actor.person_id)),
        relationship_id=UUID(str(actor.guardian_relationship_id)),
        relationship_version=1,
        student_id=UUID(int=101),
        student_version=1,
        relationship_kind="recognized_caregiver" if caregiver else "verified_guardian",
        valid_from=NOW - timedelta(days=3),
        valid_to=NOW + timedelta(days=3),
    )
    state = SimpleNamespace(
        basis=basis, active=True, unavailable=False, calls=0, changed=False, payload=None
    )

    class Repository:
        def authority(self, _actor):
            if state.unavailable:
                raise SQLAlchemyError("private diagnostic")
            return state.basis

        def read_profile(self, _actor, *, factual_at):
            state.calls += 1
            if state.payload is not None:
                return {"conflicting_fields": [], **state.payload}
            profile = {"name": "合成孩子", "enrollment_status": "enrolled", "campus": "合成校区"}
            if not caregiver:
                profile |= {"student_number": "S001", "residence_mode": "day", "region": None}
            if state.changed:
                state.basis = None
            return {
                "status": "available",
                "profile": profile,
                "sources": [],
                "missing": [] if caregiver else ["region"],
                "conflicting_fields": [],
            }

    service = mod.FamilyQueryService(
        repository=Repository(),
        current_actor=lambda a: state.active and a == actor,
        clock=lambda: NOW,
    )
    return service, actor, state


def test_formal_relation_required_before_any_profile_query():
    service, actor, state = setup()
    state.basis = None  # Includes legacy-only confirmed and unverified contact.
    result = service.query(actor=actor)
    assert result.status == "denied" and state.calls == 0
    assert result.public()["profile"] == {}


def test_current_relation_and_historical_interval_both_required():
    service, actor, state = setup()
    assert service.query(actor=actor, as_of=NOW - timedelta(days=4)).status == "denied"
    assert state.calls == 0
    assert service.query(actor=actor, as_of=NOW - timedelta(days=1)).status == "available"
    state.basis = replace(state.basis, valid_to=NOW)
    assert service.query(actor=actor, as_of=NOW - timedelta(days=1)).status == "denied"


def test_caregiver_does_not_gain_guardian_fields_or_history():
    service, actor, state = setup(caregiver=True)
    result = service.query(actor=actor)
    assert set(result.profile) == {"name", "enrollment_status", "campus"}
    assert service.query(actor=actor, as_of=NOW - timedelta(seconds=1)).status == "denied"
    assert state.calls == 1


def test_changed_relationship_after_read_discards_all_values():
    service, actor, state = setup()
    state.changed = True
    result = service.query(actor=actor)
    assert result.status == "denied" and result.profile == {}


def test_unavailable_is_distinct_from_denial_and_does_not_leak_diagnostic():
    service, actor, state = setup()
    state.unavailable = True
    result = service.query(actor=actor)
    assert result.status == "source_unavailable"
    assert "private diagnostic" not in str(result.public())


def test_history_evidence_checks_identity_child_and_current_result_again():
    service, actor, state = setup()
    result = service.query(actor=actor)
    assert result.evidence.valid(actor)
    assert not result.evidence.valid(guardian(2))
    state.active = False
    assert not result.evidence.valid(actor)


def test_source_coverage_is_not_inferred_from_profile_availability():
    service, actor, _ = setup()
    result = service.query(actor=actor)
    assert result.public()["coverage"]["status"] == "unknown"
    assert "region" in result.public()["missing"]


def test_source_trace_is_explicit_and_never_promoted_to_a_source_name():
    service, actor, state = setup()
    state.payload = {
        "status": "available",
        "profile": {"name": "合成孩子"},
        "sources": [
            {
                "fact": "person",
                "reference": "publication:approved",
                "published_at": NOW.isoformat(),
                "source_name": None,
                "source_trace_reference": "change-set:technical-trace",
                "channel": "人工正式发布",
            }
        ],
        "missing": [],
    }
    result = service.query(actor=actor)
    assert result.status == "available"
    assert result.public()["sources"][0]["source_name"] is None
    assert result.public()["sources"][0]["source_trace_reference"] == "change-set:technical-trace"


def test_no_current_records_preserves_unknown_coverage_and_new_match_invalidates_evidence():
    service, actor, state = setup()
    state.payload = {"status": "no_current_records", "profile": {}, "sources": [], "missing": []}
    result = service.query(actor=actor)
    assert result.status == "no_current_records"
    assert result.public()["coverage"]["status"] == "unknown"
    assert result.evidence.valid(actor)
    state.payload = {
        "status": "available",
        "profile": {"name": "刚正式发布"},
        "sources": [],
        "missing": [],
    }
    assert not result.evidence.valid(actor)


def test_same_identity_changed_fact_invalidates_history_evidence():
    service, actor, state = setup()
    result = service.query(actor=actor)
    state.payload = {
        "status": "available",
        "profile": {"name": "已纠正姓名"},
        "sources": [],
        "missing": [],
    }
    assert not result.evidence.valid(actor)


def test_caregiver_overbroad_sql_payload_is_rejected_without_leaking_fields():
    service, actor, state = setup(caregiver=True)
    state.payload = {
        "status": "available",
        "profile": {"name": "合成孩子", "student_number": "private"},
        "sources": [],
        "missing": [],
    }
    result = service.query(actor=actor)
    assert result.status == "source_unavailable" and result.profile == {}
    assert "private" not in str(result.public())


def test_caregiver_current_read_marks_restricted_fields_as_denied():
    service, actor, _ = setup(caregiver=True)
    result = service.query(actor=actor)
    assert result.public()["authorized_fields"] == ["name", "enrollment_status", "campus"]
    assert result.public()["field_restrictions"] == dict.fromkeys(
        ["student_number", "residence_mode", "region", "history"], "denied"
    )
    assert service.query(actor=actor, as_of=NOW).status == "denied"


def test_conflicting_fields_are_not_mislabeled_missing():
    service, actor, state = setup()
    state.payload = {
        "status": "conflict",
        "profile": {"name": None, "campus": None},
        "sources": [],
        "missing": ["campus"],
        "conflicting_fields": ["name"],
    }
    result = service.query(actor=actor)
    assert result.status == "conflict"
    assert result.public()["missing"] == ["campus"]
    assert result.public()["conflicting_fields"] == ["name"]


def test_empty_status_cannot_hide_an_available_value_or_a_conflict():
    service, actor, state = setup()
    state.payload = {
        "status": "no_current_records",
        "profile": {"name": "already present"},
        "sources": [],
        "missing": [],
        "conflicting_fields": [],
    }
    assert service.query(actor=actor).status == "source_unavailable"
    state.payload = {
        "status": "no_current_records",
        "profile": {"name": None},
        "sources": [],
        "missing": [],
        "conflicting_fields": ["name"],
    }
    assert service.query(actor=actor).status == "source_unavailable"


def test_conflict_cannot_publish_a_chosen_value_or_claim_available():
    service, actor, state = setup()
    for status, name in (("available", None), ("conflict", "arbitrarily chosen")):
        state.payload = {
            "status": status,
            "profile": {"name": name},
            "sources": [],
            "missing": [],
            "conflicting_fields": ["name"],
        }
        assert service.query(actor=actor).status == "source_unavailable"


def test_multiple_publication_sources_do_not_imply_a_fact_conflict():
    service, actor, state = setup()
    sources = [
        {
            "fact": fact,
            "reference": "publication:" + fact,
            "published_at": NOW.isoformat(),
            "source_name": None,
            "source_trace_reference": "change-set:" + fact,
            "channel": "人工正式发布",
        }
        for fact in ("person", "student_school_relationship")
    ]
    state.payload = {
        "status": "available",
        "profile": {"name": "合成孩子"},
        "sources": sources,
        "missing": [],
    }
    result = service.query(actor=actor)
    assert result.status == "available" and len(result.sources) == 2


def test_historical_value_is_discarded_if_current_relationship_changes_during_read():
    service, actor, state = setup()
    state.changed = True
    result = service.query(actor=actor, as_of=NOW - timedelta(days=1))
    assert result.status == "denied" and result.profile == {}


def test_historical_evidence_rechecks_present_relationship_not_only_old_interval():
    service, actor, state = setup()
    result = service.query(actor=actor, as_of=NOW - timedelta(days=1))
    assert result.evidence.valid(actor)
    state.basis = replace(state.basis, relationship_version=2)
    assert not result.evidence.valid(actor)


def test_f7_guardian_quality_is_narrow_and_revalidated_with_family_result():
    service, actor, state = setup()
    service.quality_enabled = True
    quality = {
        "school_fact_version": 7,
        "claims": [{
            "field_name": "enrollment_status", "quality_kind": "dispute",
            "dispute_status": "open", "source_status": None,
            "coverage_status": None, "review_due_at": None,
        }],
    }
    calls = []

    def read_quality(_actor, *, factual_at, fields):
        calls.append((factual_at, fields))
        assert _actor == actor
        return quality

    service.repository.read_quality = read_quality
    result = service.query(actor=actor, quality_fields=("enrollment_status",))
    assert result.status == "available"
    assert result.public()["quality"]["enrollment_status"]["risks"] == [
        "学校登记了这项资料的争议"
    ]
    assert calls[0][1] == ("enrollment_status",)
    assert result.evidence.valid(actor)
    quality["school_fact_version"] = 8
    assert not result.evidence.valid(actor)
    state.active = False
    assert not result.evidence.valid(actor)


def test_f7_missing_visible_field_can_carry_formal_source_failure():
    service, actor, _ = setup()
    service.quality_enabled = True
    selected = []

    def read_quality(_actor, *, factual_at, fields):
        selected.append(fields)
        return {
            "school_fact_version": 7,
            "claims": [{
                "field_name": "region", "quality_kind": "source",
                "source_status": "unavailable", "coverage_status": None,
                "dispute_status": None, "review_due_at": None,
            }],
        }

    service.repository.read_quality = read_quality
    result = service.query(actor=actor, quality_fields=("region",))
    assert selected == [("region",)]
    assert result.public()["profile"]["region"] is None
    assert result.public()["quality"]["region"]["risks"] == [
        "这项资料的来源目前无法核实"
    ]


def test_f7_caregiver_quality_never_reads_restricted_or_unasked_fields():
    service, actor, _ = setup(caregiver=True)
    service.quality_enabled = True
    seen = []

    def read_quality(_actor, *, factual_at, fields):
        seen.append(fields)
        return {"school_fact_version": 7, "claims": []}

    service.repository.read_quality = read_quality
    result = service.query(
        actor=actor, quality_fields=("enrollment_status", "student_number")
    )
    assert result.status == "available"
    assert seen == [("enrollment_status",)]
    assert "student_number" not in result.public()["quality"]


def test_f7_family_quality_overbroad_field_fails_closed():
    service, actor, _ = setup(caregiver=True)
    service.quality_enabled = True
    service.repository.read_quality = lambda *args, **kwargs: {
        "school_fact_version": 7,
        "claims": [{
            "field_name": "student_number", "quality_kind": "dispute",
            "dispute_status": "open", "source_status": None,
            "coverage_status": None, "review_due_at": None,
        }],
    }
    result = service.query(actor=actor, quality_fields=("name",))
    assert result.status == "source_unavailable"
    assert result.public()["profile"] == {}


def test_f7_family_quality_source_statement_is_not_promoted_to_independent_confirmation():
    service, actor, _ = setup()
    service.quality_enabled = True
    service.repository.read_quality = lambda *args, **kwargs: {
        "school_fact_version": 7,
        "claims": [
            {"field_name": "name", "quality_kind": "source", "source_status": "available"},
            {"field_name": "name", "quality_kind": "coverage", "coverage_status": "partial"},
        ],
    }
    result = service.query(actor=actor, quality_fields=("name",))
    assert result.status == "available"
    assert result.public()["quality"]["name"] == {
        "status": "claims", "risks": ["学校声明这项资料登记未覆盖完整范围"]
    }
    assert "已确认" not in str(result.public())


def test_f7_family_relation_revoked_during_quality_read_returns_no_value():
    service, actor, state = setup()
    service.quality_enabled = True

    def read_quality(*args, **kwargs):
        state.basis = None
        return {"school_fact_version": 7, "claims": []}

    service.repository.read_quality = read_quality
    result = service.query(actor=actor, quality_fields=("name",))
    assert result.status == "denied"
    assert result.public()["profile"] == {}
    assert "quality" not in result.public()


def test_f7_family_account_invalidated_during_quality_read_returns_no_value():
    service, actor, state = setup()
    service.quality_enabled = True

    def read_quality(*args, **kwargs):
        state.active = False
        return {"school_fact_version": 7, "claims": []}

    service.repository.read_quality = read_quality
    result = service.query(actor=actor, quality_fields=("name",))
    assert result.status == "denied"
    assert result.public()["profile"] == {}
    assert "quality" not in result.public()


def test_f7_family_sql_quality_reader_accepts_no_model_selected_target_key():
    from app.composition.school_fact_family_query import SqlFamilyReadRepository

    actor = guardian()
    seen = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, parameters):
            seen.append((str(statement), parameters))
            return SimpleNamespace(scalar_one=lambda: {"school_fact_version": 7, "claims": []})

    repository = SqlFamilyReadRepository(Session)
    assert repository.read_quality(actor, factual_at=None, fields=("name",)) == {
        "school_fact_version": 7, "claims": []
    }
    sql, parameters = seen[0]
    assert "app.school_fact_f7_family_profile_quality" in sql
    assert set(parameters) == {
        "school", "adult", "relation", "version", "factual_at", "fields"
    }
    assert parameters["fields"] == ["name"]
