from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 9, 22, tzinfo=UTC)


def api():
    from app.domains.students import residence_changes

    return residence_changes


def test_candidate_preflight_counts_peak_and_accepts_explicit_same_batch_resolution():
    from app.domains.students.education_versions import EducationSlice

    m = api()

    def span(payload, start=NOW, end=None):
        return (EducationSlice(start, end, payload),)

    rooms = {
        "profile:one": span(
            dict(room_source_key="room:one", authorized_bed_capacity=1, is_available=True)
        )
    }
    beds = {
        "bed:one": span(
            dict(room_source_key="room:one", availability="available"), end=NOW + timedelta(days=1)
        ),
        "bed:two": span(
            dict(room_source_key="room:one", availability="available"),
            start=NOW + timedelta(days=1),
        ),
    }
    ssr = {
        "ssr:one": span(
            dict(
                student_source_key="student:one",
                enrollment_status="enrolled",
                residence_mode="boarding",
            )
        )
    }
    stays = {
        "stay:one": span(
            dict(student_source_key="student:one", bed_source_key="bed:one"),
            end=NOW + timedelta(days=1),
        ),
        "stay:two": span(
            dict(student_source_key="student:one", bed_source_key="bed:two"),
            start=NOW + timedelta(days=1),
        ),
    }
    assert (
        m.residence_candidate_issues(rooms=rooms, beds=beds, enrollments=ssr, residences=stays)
        == ()
    )
    beds["bed:one"] = span(dict(room_source_key="room:one", availability="available"))
    assert "residence.capacity_requires_same_batch_resolution" in m.residence_candidate_issues(
        rooms=rooms, beds=beds, enrollments=ssr, residences=stays
    )
    rooms["profile:one"] = span(
        dict(room_source_key="room:one", authorized_bed_capacity=2, is_available=True)
    )
    stays["stay:one"] = span(dict(student_source_key="student:one", bed_source_key="bed:one"))
    assert (
        "residence.student_overlap_requires_same_batch_resolution"
        in m.residence_candidate_issues(rooms=rooms, beds=beds, enrollments=ssr, residences=stays)
    )
    del stays["stay:two"]
    ssr["ssr:one"] = span(
        dict(student_source_key="student:one", enrollment_status="enrolled", residence_mode=None)
    )
    assert "residence.eligibility_requires_same_batch_resolution" in m.residence_candidate_issues(
        rooms=rooms, beds=beds, enrollments=ssr, residences=stays
    )
    assert (
        m.residence_candidate_issues(rooms=rooms, beds=beds, enrollments=ssr, residences={}) == ()
    )


def test_move_explicitly_ends_old_and_publishes_new_without_mutating_snapshots():
    m = api()
    old = m.ResidenceFactSnapshot(
        "stay:old", 2, "student:one", "bed:old", NOW - timedelta(days=2), None
    )
    result = m.plan_residence_change(
        operation="move",
        student_source_key="student:one",
        at=NOW + timedelta(days=1),
        snapshots=(old,),
        target_source_key="stay:old",
        new_source_key="stay:new",
        bed_source_key="bed:new",
    )
    assert [(r.operation, r.source_key, r.expected_version) for r in result] == [
        ("end", "stay:old", 2),
        ("upsert", "stay:new", 0),
    ]
    assert old.valid_to is None


def test_departure_ends_current_and_revokes_future_without_negative_intervals():
    m = api()
    old = m.ResidenceFactSnapshot(
        "stay:old", 1, "student:one", "bed:old", NOW - timedelta(days=2), NOW + timedelta(days=2)
    )
    future = m.ResidenceFactSnapshot(
        "stay:future", 1, "student:one", "bed:new", NOW + timedelta(days=2), None
    )
    result = m.plan_residence_change(
        operation="lifecycle",
        student_source_key="student:one",
        at=NOW,
        snapshots=(old, future),
        enrollment_status="graduated",
        residence_mode="boarding",
    )
    assert [(r.operation, r.source_key) for r in result] == [
        ("end", "stay:old"),
        ("revoke", "stay:future"),
    ]


def test_suspension_does_not_create_extend_or_restore_a_bed():
    assert (
        api().plan_residence_change(
            operation="lifecycle",
            student_source_key="student:one",
            at=NOW,
            snapshots=(),
            enrollment_status="suspended",
            residence_mode="boarding",
        )
        == ()
    )


def test_unknown_requires_explicit_resolution_instead_of_assuming_departure():
    m = api()
    old = m.ResidenceFactSnapshot(
        "stay:old", 1, "student:one", "bed:old", NOW - timedelta(days=2), None
    )
    with pytest.raises(ValueError, match="未知"):
        m.plan_residence_change(
            operation="lifecycle",
            student_source_key="student:one",
            at=NOW,
            snapshots=(old,),
            enrollment_status="enrolled",
            residence_mode=None,
        )


def test_future_cancellation_never_invents_restoration_of_another_residence():
    m = api()
    future = m.ResidenceFactSnapshot(
        "stay:future", 1, "student:one", "bed:new", NOW + timedelta(days=2), None
    )
    result = m.plan_residence_change(
        operation="revoke",
        student_source_key="student:one",
        at=NOW,
        snapshots=(future,),
        target_source_key="stay:future",
    )
    assert len(result) == 1 and result[0].operation == "revoke"


def test_cross_student_and_already_revoked_snapshots_fail_closed():
    m = api()
    other = m.ResidenceFactSnapshot("stay:other", 1, "student:other", "bed:new", NOW, None)
    with pytest.raises(ValueError):
        m.plan_residence_change(
            operation="end",
            student_source_key="student:one",
            at=NOW,
            snapshots=(other,),
            target_source_key="stay:other",
        )


def test_facility_correction_preserves_old_rows_and_records_a_complete_new_revision(monkeypatch):
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.domains.campus.models import DormRoomProfileVersion
    from app.platform.school_facts.contracts import CanonicalFactChangeV4

    school, room, source, batch = (uuid4() for _ in range(4))
    old = DormRoomProfileVersion(
        id=uuid4(),
        school_id=school,
        room_location_id=room,
        source_key="room:profile",
        authorized_bed_capacity=2,
        accommodation_zone="boarding",
        is_available=True,
        accessibility_tags=[],
        valid_from=NOW - timedelta(days=5),
        valid_to=None,
        version=1,
        source_id=source,
    )
    session = Mock()
    session.execute.return_value.scalars.return_value = [old]
    monkeypatch.setattr(SqlAlchemySchoolFactPersistence, "_source_root_id", lambda *a, **k: room)
    change = CanonicalFactChangeV4.model_validate(
        dict(
            operation="correct",
            fact_kind="dorm_room",
            source_key="room:profile",
            expected_version=1,
            effective_at=NOW,
            origin={"sheet_code": "f6", "row_number": 1},
            correction_reason="核对容量",
            content=dict(
                room_source_key="location:room",
                authorized_bed_capacity=3,
                accommodation_zone="boarding",
                is_available=True,
                valid_to=NOW + timedelta(days=2),
            ),
        )
    )
    target, revision = SqlAlchemySchoolFactPersistence._apply_residence_facility(
        session=session,
        school_id=school,
        batch_id=batch,
        source_id=uuid4(),
        change=change,
        current_id=old.id,
        current_version=1,
    )
    added = [call.args[0] for call in session.add.call_args_list]
    assert len(added) == 3 and revision == 2
    assert old.superseded_at is not None and old.valid_to is None
    assert [row.authorized_bed_capacity for row in added] == [2, 3, 2]
    assert all(row.version == 2 and row.source_key == "room:profile" for row in added)
    assert target in {row.id for row in added}


def test_quality_residence_target_requires_the_exact_students_person():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeV4

    school, target, owner = uuid4(), uuid4(), uuid4()
    change = CanonicalFactChangeV4.model_validate(
        dict(
            operation="upsert",
            fact_kind="fact_quality",
            source_key="quality:residence",
            expected_version=0,
            effective_at=NOW,
            origin={"sheet_code": "f6", "row_number": 1},
            content=dict(
                quality_kind="source",
                target_fact_kind="student_bed_residence",
                owner_kind="person",
                person_source_key="person:one",
                target_source_key="stay:one",
                source_status="available",
                basis_reference="正式核对",
                responsibility="资料维护",
                verified_at=NOW,
            ),
        )
    )
    session = Mock()
    session.execute.side_effect = [
        Mock(one_or_none=lambda: SimpleNamespace(target_id=target, operation="upsert")),
        Mock(scalar_one_or_none=lambda: owner),
    ]
    values = dict(
        target_fact_kind="student_bed_residence",
        target_source_key="stay:one",
        owner_kind="person",
        person_id=owner,
    )
    SqlAlchemySchoolFactPersistence._validate_quality_targets(session, school, change, values)
    session.execute.side_effect = [
        Mock(one_or_none=lambda: SimpleNamespace(target_id=target, operation="upsert")),
        Mock(scalar_one_or_none=lambda: uuid4()),
    ]
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._validate_quality_targets(session, school, change, values)


def test_v4_preflight_keeps_existing_education_impact_validation():
    from unittest.mock import Mock
    from uuid import uuid4

    from app.platform.school_facts.application import SchoolFactChangeService
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
    from tests.school_facts.test_f6_residence_contracts import ENROLLMENT, change
    from tests.school_facts.test_sf2_preflight import _Reader, _WriteTrap

    reader = _Reader()
    reader.education_preflight_codes = Mock(return_value=("education.impact_conflict",))
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    plan = CanonicalFactChangeSetV4(
        source_sha256="a" * 64,
        base_version=4,
        changes=(change("student_school_relationship", ENROLLMENT),),
    )
    report = service.preflight(school_id=uuid4(), change_set=plan)
    assert not report.is_clean
    assert report.issues[0].error_code == "education.impact_conflict"


def test_residence_preflight_reports_only_the_affected_submitted_row():
    from unittest.mock import Mock
    from uuid import uuid4

    from app.platform.school_facts.application import SchoolFactChangeService
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
    from tests.school_facts.test_f6_residence_contracts import change
    from tests.school_facts.test_sf2_preflight import _Reader, _WriteTrap

    reader = _Reader()
    first = change(source_key="profile:first")
    second = change(source_key="profile:second", origin=dict(sheet_code="f6", row_number=2))
    reader.residence_preflight_issues = Mock(
        return_value={
            (second.fact_kind, second.source_key): (
                "residence.capacity_requires_same_batch_resolution",
            )
        }
    )
    plan = CanonicalFactChangeSetV4(source_sha256="a" * 64, base_version=4, changes=(first, second))
    report = SchoolFactChangeService(reader=reader, writer=_WriteTrap()).preflight(
        school_id=uuid4(), change_set=plan
    )
    assert len(report.issues) == 1 and report.issues[0].row_number == 2


def test_residence_preflight_never_drops_an_unattributable_legacy_conflict():
    from contextlib import nullcontext
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from tests.school_facts.test_f6_residence_contracts import change

    submitted = change(
        "dorm_room",
        dict(
            room_source_key="room:one",
            authorized_bed_capacity=1,
            accommodation_zone="mixed",
            is_available=True,
        ),
    )

    def candidate(_session, _school, _changes, *, issue_targets, dependencies):
        code = "residence.capacity_requires_same_batch_resolution"
        issue_targets[code] = {("location", "legacy:unresolved")}
        dependencies[submitted.fact_kind, submitted.source_key] = {("location", "room:one")}
        return (code,)

    persistence = object.__new__(SqlAlchemySchoolFactPersistence)
    persistence._request_bound_session = Mock(return_value=nullcontext(object()))
    persistence._residence_candidate_codes = candidate
    assert persistence.residence_preflight_issues(uuid4(), (submitted,)) == {
        (submitted.fact_kind, submitted.source_key): (
            "residence.timeline_requires_explicit_resolution",
        )
    }


@pytest.mark.parametrize(
    "windows,expected",
    [
        (((0, 2, "boarding"),), [("end", "stay:old", 2), ("revoke", "stay:future", 3)]),
        ((), [("revoke", "stay:old", 0), ("revoke", "stay:future", 3)]),
        (((0, 1, "boarding"), (2, None, "boarding")), [("end", "stay:old", 1)]),
        (((0, 1, "boarding"), (1, None, "boarding")), []),
    ],
)
def test_complete_eligibility_covers_adjacent_sources_and_closes_only_conflicts(windows, expected):
    m = api()
    old = m.ResidenceFactSnapshot(
        "stay:old", 1, "student:one", "bed:old", NOW, NOW + timedelta(days=3)
    )
    future = m.ResidenceFactSnapshot(
        "stay:future", 1, "student:one", "bed:new", NOW + timedelta(days=3), None
    )
    slices = tuple(
        m.BoardingEligibilitySlice(
            NOW + timedelta(days=a),
            NOW + timedelta(days=b) if b is not None else None,
            "enrolled",
            mode,
        )
        for a, b, mode in windows
    )
    result = m.plan_residence_eligibility_change(
        student_source_key="student:one", snapshots=(old, future), eligibility=slices
    )
    assert [(c.operation, c.source_key, (c.effective_at - NOW).days) for c in result] == expected


def test_unknown_eligibility_refuses_to_guess_departure_and_never_restores_old_stay():
    m = api()
    old = m.ResidenceFactSnapshot("stay:old", 1, "student:one", "bed:old", NOW, None)
    windows = (
        m.BoardingEligibilitySlice(NOW, NOW + timedelta(days=1), "enrolled", "boarding"),
        m.BoardingEligibilitySlice(NOW + timedelta(days=1), None, "enrolled", None),
    )
    with pytest.raises(ValueError, match="未知"):
        m.plan_residence_eligibility_change(
            student_source_key="student:one", snapshots=(old,), eligibility=windows
        )
    windows = (
        windows[0],
        m.BoardingEligibilitySlice(
            NOW + timedelta(days=1), NOW + timedelta(days=2), "enrolled", "day"
        ),
        m.BoardingEligibilitySlice(NOW + timedelta(days=2), None, "enrolled", "boarding"),
    )
    result = m.plan_residence_eligibility_change(
        student_source_key="student:one", snapshots=(old,), eligibility=windows
    )
    assert [(c.operation, c.effective_at) for c in result] == [("end", NOW + timedelta(days=1))]


def test_eligibility_reader_requires_full_f3_authority_before_closed_set_projection():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from app.composition.school_fact_family_staff_reader import FamilyStaffRepository
    from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

    fields = tuple(F3_FACT_FIELDS["student_school_relationship"])
    actor = SimpleNamespace(school_id=uuid4())
    permit = SimpleNamespace(
        scope=SimpleNamespace(
            field_names=fields, object_keys=("ssr:one",), is_school_wide=False,
            allows_key=lambda key: key == "ssr:one",
        ), target=object()
    )
    projection = Mock()

    def keys(**kw):
        kw["before_read"]()
        assert kw["allowed_keys"]() == ("ssr:one",)
        kw["after_read"]()
        return dict(school_fact_version=5, source_keys=["ssr:one"])

    projection.student_relationship_keys.side_effect = keys
    repository = FamilyStaffRepository(
        projection=projection,
        configuration=SimpleNamespace(),
        reference_key=b"x" * 32,
        authorize_scope=Mock(return_value=permit),
        revalidate=Mock(),
        clock=lambda: NOW,
    )
    repository.read_education_timeline = Mock(
        return_value=dict(
            school_fact_version=5,
            source_key="ssr:one",
            version=1,
            segments=[
                dict(
                    effective_at=NOW.isoformat(),
                    valid_to=None,
                    content={"student_source_key": "student:one"},
                )
            ],
        )
    )
    result = repository.read_residence_eligibility(
        actor=actor, student_source_key="student:one", source_key="ssr:one"
    )
    assert len(result["timelines"]) == 1
    result["revalidate"](actor)
    permit.scope.field_names = ("student_source_key",)
    with pytest.raises(ValueError, match="全部"):
        repository.read_residence_eligibility(
            actor=actor, student_source_key="student:one", source_key="ssr:one"
        )


@pytest.mark.parametrize(
    "ending,with_adjacent,operation,terminal,with_new",
    [
        (2, False, "end", None, False),
        (None, False, "revoke", None, False),
        (2, True, None, None, False),
        (2, False, "end", "end", False),
        (2, False, "end", "revoke", False),
        (2, False, None, None, True),
        (2, False, None, "end", True),
    ],
)
def test_lifecycle_coordinator_uses_complete_f3_timelines_and_previews_named_old_bed(
    ending, with_adjacent, operation, terminal, with_new
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
    from app.composition.school_fact_residence_maintenance import ResidenceLifecycleCoordinator
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4, CanonicalFactChangeV4
    from tests.school_facts.test_f6_residence_contracts import ENROLLMENT

    content = dict(ENROLLMENT, residence_mode="boarding", student_source_key="student:one")
    old = dict(
        source_key="ssr:one",
        version=1,
        school_fact_version=5,
        segments=[
            dict(
                effective_at=NOW.isoformat(),
                valid_to=(NOW + timedelta(days=2)).isoformat() if with_adjacent else None,
                content=content,
            )
        ],
    )
    timelines = [old]
    if with_adjacent:
        timelines.append(
            dict(
                source_key="ssr:two",
                version=1,
                school_fact_version=5,
                segments=[
                    dict(
                        effective_at=(NOW + timedelta(days=2)).isoformat(),
                        valid_to=None,
                        content=content,
                    )
                ],
            )
        )
    replacement = (
        []
        if ending is None
        else [dict(effective_at=NOW, valid_to=NOW + timedelta(days=ending), content=content)]
    )
    root = CanonicalFactChangeV4.model_validate(
        dict(
            fact_kind="student_school_relationship",
            operation="correct",
            source_key="ssr:one",
            expected_version=1,
            effective_at=NOW,
            content=None,
            replacement_segments=tuple(replacement),
            correction_reason="在校期间核对",
            origin=dict(sheet_code="f6", row_number=1),
        )
    )
    if terminal:
        old["segments"] = [
            dict(
                effective_at=NOW.isoformat(),
                valid_to=(NOW + timedelta(days=1)).isoformat(),
                content=content,
            ),
            dict(
                effective_at=(NOW + timedelta(days=1)).isoformat(), valid_to=None, content=content
            ),
        ]
        root = CanonicalFactChangeV4.model_validate(
            dict(
                fact_kind="student_school_relationship",
                operation=terminal,
                source_key="ssr:one",
                expected_version=1,
                effective_at=NOW + timedelta(days=2),
                content=None,
                origin=dict(sheet_code="f6", row_number=1),
            )
        )
    submitted = (root,)
    if with_new:
        new = CanonicalFactChangeV4.model_validate(
            dict(
                fact_kind="student_school_relationship",
                operation="upsert",
                source_key="ssr:new",
                expected_version=0,
                effective_at=NOW + timedelta(days=2),
                content=content,
                origin=dict(sheet_code="f6", row_number=2),
            )
        )
        submitted = (new, root)
    plan = CanonicalFactChangeSetV4(source_sha256="a" * 64, base_version=5, changes=submitted)
    summary = FactChangeSummary(
        fact_kind="student_school_relationship",
        label="在校资料",
        reference="f3:ssr",
        operation="correct",
        before=None,
        after=None,
        effective_at=NOW,
        reason="期间核对",
        source_label="正式资料",
    )
    family = Mock()
    family.read_education_timeline.return_value = old
    family.public_field_references.side_effect = lambda **kw: kw["fields"]
    family.reference_for.return_value = "f3:ssr"
    family.read_residence_eligibility.return_value = dict(
        school_fact_version=5, timelines=timelines, revalidate=Mock()
    )
    repository = Mock()
    proof = Mock()
    repository.read_student_impacts.return_value = SimpleNamespace(
        school_fact_version=5,
        proof=proof,
        records=(
            dict(
                source_key="stay:one",
                version=1,
                bed_source_key="bed:one",
                valid_from=NOW.isoformat(),
                valid_to=None,
            ),
        ),
    )
    repository.read.return_value = SimpleNamespace(
        school_fact_version=5,
        proof=proof,
        records=(
            dict(fields={"student_name": "合成学生", "bed_name": "1号床", "room_name": "301室"}),
        ),
    )
    repository.reference_for.return_value = "f6:student_bed_residence:one"
    adapter = Mock()
    adapter.public_fields.side_effect = lambda actor, fields: {
        k.replace("_source_key", "_ref"): v for k, v in fields.items()
    }
    result = ResidenceLifecycleCoordinator(
        repository=repository, adapter=adapter, family_repository=family
    ).expand(actor=object(), resolved=ResolvedFactChange(plan, summary))
    assert len(result.change_set.changes) == len(submitted) + (1 if operation else 0)
    family.read_education_timeline.assert_called_once()
    assert family.read_education_timeline.call_args.kwargs["source_key"] == "ssr:one"
    assert family.read_residence_eligibility.call_args.kwargs["source_key"] == "ssr:one"
    if operation:
        assert result.change_set.changes[-1].operation == operation
        impact = result.summary.impacts[0]
        assert impact["before"]["student_name"] == "合成学生"
        assert impact["before"]["bed_name"] == "1号床"
        assert impact["before"]["valid_from"] == NOW.isoformat()
        assert (
            impact["after"]["valid_to"] == (NOW + timedelta(days=2)).isoformat()
            if operation == "end"
            else impact["after"] is None
        )
    assert repository.read_student_impacts.call_args.kwargs["as_of"] == NOW
    if terminal:
        normalized = next(c for c in result.change_set.changes if c.source_key == "ssr:one")
        assert normalized.operation == "correct" and len(normalized.replacement_segments) == 2
        assert normalized.replacement_segments[-1].valid_to == NOW + timedelta(days=2)
        assert result.summary.operation == terminal
        assert len(result.summary.before_segments) == len(result.summary.after_segments) == 2


def test_new_enrollment_does_not_infer_read_permission_or_create_residence_effects():
    from unittest.mock import Mock

    from app.composition.school_fact_residence_maintenance import ResidenceLifecycleCoordinator
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
    from tests.school_facts.test_f6_residence_contracts import ENROLLMENT, change

    plan = CanonicalFactChangeSetV4(
        source_sha256="a" * 64,
        base_version=5,
        changes=(
            change("student_school_relationship", dict(ENROLLMENT, residence_mode="boarding")),
        ),
    )
    resolved = Mock(change_set=plan)
    family, repository, adapter = Mock(), Mock(), Mock()
    family.read_education_timeline.side_effect = PermissionError("creation grants no read")
    coordinator = ResidenceLifecycleCoordinator(
        repository=repository,
        adapter=adapter,
        family_repository=family,
    )
    assert coordinator.expand(actor=object(), resolved=resolved) is resolved
    assert family.mock_calls == repository.mock_calls == adapter.mock_calls == []
