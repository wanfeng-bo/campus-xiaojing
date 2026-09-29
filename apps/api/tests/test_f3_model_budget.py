"""A finite stage shares the original ledger; restart cannot erase its occupancy."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from app.composition.school_fact_current_model_budget import SqliteCurrentModelCallPermit
from app.platform.assistant.runtime import AssistantServiceError
from tests.test_f2_current_model_budget import current, ledger, row, usage


def stage(path, policy, cap=10000, reference="approved-f3"):
    from app.composition.school_fact_current_model_budget import ApprovedModelStageBudget

    base = current(path, policy, [datetime(2026, 9, 13, tzinfo=UTC)])
    return SqliteCurrentModelCallPermit(
        path=path,
        ledger_policy=policy,
        current_approval=base.current_approval,
        stage_budget=ApprovedModelStageBudget(
            stage_id="school-facts-f3", approval_reference=reference, cap_microyuan=cap
        ),
        clock=lambda: datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_stage_unknown_and_restart_preserve_original_occupancy(tmp_path):
    path, policy, old = ledger(tmp_path)
    historic = old.reserve(0, 360448)
    historic.unknown()
    before = row(path, historic.reference)
    permit = stage(path, policy)
    reservation = permit.reserve(1000, 1000)
    reservation.unknown()
    restarted = stage(path, policy)
    with pytest.raises(AssistantServiceError):
        restarted.reserve(1, 1)
    assert row(path, historic.reference) == before
    assert restarted.summary()["stage_occupied_microyuan"] == 10000
    assert restarted.summary()["global_occupied_microyuan"] == 3254032


def test_stage_settles_idempotently_and_older_permit_cannot_settle_it(tmp_path):
    path, policy, _ = ledger(tmp_path)
    permit = stage(path, policy)
    reservation = permit.reserve(1000, 1000)
    reservation.mark_transmission_started()
    old_permit = current(path, policy, [datetime(2026, 9, 13, tzinfo=UTC)])
    with pytest.raises(RuntimeError):
        old_permit.settle_reservation(reservation.reference, usage())
    reservation.settle(usage())
    before = row(path, reservation.reference)
    stage(path, policy).settle_reservation(reservation.reference, usage())
    assert row(path, reservation.reference) == before
    assert permit.summary()["stage_occupied_microyuan"] == 422
    permit.reserve(1, 1)


def test_stage_policy_change_cannot_restart_cap(tmp_path):
    path, policy, _ = ledger(tmp_path)
    stage(path, policy).reserve(10, 10)
    for changed in ({"cap": 20000}, {"reference": "replacement"}):
        with pytest.raises(RuntimeError):
            stage(path, policy, **changed)


def test_stage_limit_and_global_limit_are_atomic(tmp_path):
    path, policy, _ = ledger(tmp_path)
    permit = stage(path, policy)

    def reserve(_):
        try:
            permit.reserve(1000, 1000)
            return True
        except AssistantServiceError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1
    assert permit.summary()["stage_occupied_microyuan"] == 10000


def test_stage_also_respects_original_global_cap(tmp_path):
    path, policy, old = ledger(tmp_path)
    old.reserve(0, 5555555).unknown()
    permit = stage(path, policy)
    with pytest.raises(AssistantServiceError):
        permit.reserve(1, 1)
    assert permit.summary()["stage_occupied_microyuan"] == 0


def extended_stage(path, policy, **changes):
    from app.composition.school_fact_current_model_budget import ApprovedModelStageBudget

    fields = dict(
        stage_id="school-facts-f3",
        approval_reference="approved-extension",
        cap_microyuan=20000,
        previous_cap_microyuan=10000,
        previous_approval_reference="approved-f3",
    )
    fields.update(changes)
    return SqliteCurrentModelCallPermit(
        path=path,
        ledger_policy=policy,
        current_approval=current(
            path, policy, [datetime(2026, 9, 13, tzinfo=UTC)]
        ).current_approval,
        stage_budget=ApprovedModelStageBudget(**fields),
        clock=lambda: datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_explicit_extension_preserves_all_old_states_and_exhausts_combined_cap(tmp_path):
    path, policy, old = ledger(tmp_path)
    historic = old.reserve(0, 360448)
    historic.unknown()
    permit = stage(path, policy)
    settled = permit.reserve(100, 100)
    settled.mark_transmission_started()
    settled.settle(usage())
    unknown = permit.reserve(100, 100)
    unknown.unknown()
    inflight = permit.reserve(100, 100)
    refs = [historic.reference, settled.reference, unknown.reference, inflight.reference]
    before = [row(path, ref) for ref in refs]
    upgraded = extended_stage(path, policy)
    assert upgraded.summary()["stage_occupied_microyuan"] == 2422
    assert upgraded.summary()["global_occupied_microyuan"] == 3244032 + 2422
    added = upgraded.reserve(8789, 0)
    added.unknown()
    assert json.loads(row(path, added.reference)[3])["current_model_budget"]["stage"] == {
        "stage_id": "school-facts-f3",
        "approval_reference": "approved-extension",
        "cap_microyuan": 20000,
        "previous_cap_microyuan": 10000,
        "previous_approval_reference": "approved-f3",
    }
    assert extended_stage(path, policy).summary()["stage_occupied_microyuan"] == 20000
    with pytest.raises(AssistantServiceError, match="stage budget exhausted"):
        upgraded.reserve(1, 1)
    assert [row(path, ref) for ref in refs] == before


def test_extension_recovers_old_unknown_and_keeps_original_stage_metadata(tmp_path):
    path, policy, _ = ledger(tmp_path)
    permit = stage(path, policy)
    reservation = permit.reserve(100, 100)
    reservation.mark_transmission_started()
    reservation.unknown()
    before = row(path, reservation.reference)
    upgraded = extended_stage(path, policy)
    with pytest.raises(RuntimeError):
        upgraded.settle_reservation(reservation.reference, usage(completion_tokens=101))
    assert row(path, reservation.reference) == before
    upgraded.settle_reservation(reservation.reference, usage())
    upgraded.settle_reservation(reservation.reference, usage())
    after = row(path, reservation.reference)
    assert after[:3] == ("settled", 1000, 422)
    assert (
        json.loads(after[3])["current_model_budget"]["stage"]
        == json.loads(before[3])["current_model_budget"]["stage"]
    )


def test_extension_rejects_unapproved_history_and_downgrade_without_mutation(tmp_path):
    path, policy, _ = ledger(tmp_path)
    original = stage(path, policy)
    reservation = original.reserve(100, 100)
    for change in ({"previous_cap_microyuan": 9000}, {"previous_approval_reference": "other"}):
        with pytest.raises(RuntimeError, match="stage policy drift"):
            extended_stage(path, policy, **change)
    upgraded = extended_stage(path, policy)
    new = upgraded.reserve(100, 100)
    before = row(path, new.reference)
    with pytest.raises(RuntimeError, match="stage policy drift"):
        stage(path, policy)
    with pytest.raises(RuntimeError, match="stage policy drift"):
        reservation.unknown()
    assert row(path, new.reference) == before


def test_extension_does_not_increase_original_fifty_yuan_limit(tmp_path):
    path, policy, old = ledger(tmp_path)
    old.reserve(0, 5555555).unknown()
    with pytest.raises(AssistantServiceError, match="approved model budget exhausted"):
        extended_stage(path, policy).reserve(1, 1)


def test_extension_recovers_old_reserved_call_without_replacing_approval(tmp_path):
    path, policy, _ = ledger(tmp_path)
    reservation = stage(path, policy).reserve(100, 100)
    old_stage = json.loads(row(path, reservation.reference)[3])["current_model_budget"]["stage"]
    upgraded = extended_stage(path, policy)
    upgraded.mark_transmission_started(reservation.reference)
    upgraded.mark_unknown(reservation.reference)
    upgraded.settle_reservation(reservation.reference, usage())
    assert upgraded.summary()["stage_occupied_microyuan"] == 422
    assert (
        json.loads(row(path, reservation.reference)[3])["current_model_budget"]["stage"]
        == old_stage
    )


@pytest.mark.parametrize(
    "change",
    [
        {"previous_cap_microyuan": None},
        {"previous_approval_reference": None},
        {"previous_cap_microyuan": True},
        {"previous_cap_microyuan": 20000},
        {"previous_approval_reference": ""},
        {"previous_approval_reference": "approved-extension"},
    ],
)
def test_extension_requires_explicit_distinct_bounded_previous_approval(tmp_path, change):
    path, policy, _ = ledger(tmp_path)
    with pytest.raises(ValueError):
        extended_stage(path, policy, **change)



def f5_capacity_permit(path, policy, *, previous=False):
    from app.composition.school_fact_current_model_budget import ApprovedModelStageBudget

    fields = {
        "stage_id": "school-facts-f5",
        "approval_reference": "original-f5-budget" if previous else "approved-f5-extension",
        "cap_microyuan": 3000000 if previous else 6000000,
    }
    if not previous:
        fields.update(
            previous_cap_microyuan=3000000,
            previous_approval_reference="original-f5-budget",
        )
    return SqliteCurrentModelCallPermit(
        path=path,
        ledger_policy=policy,
        current_approval=current(
            path, policy, [datetime(2026, 9, 13, tzinfo=UTC)]
        ).current_approval,
        stage_budget=ApprovedModelStageBudget(**fields),
        clock=lambda: datetime(2026, 9, 13, tzinfo=UTC),
    )


def test_f5_96kb_reservation_preserves_old_settled_unknown_and_inflight_rows(tmp_path):
    path, policy, legacy = ledger(tmp_path)
    historical = legacy.reserve(0, 360448)
    historical.unknown()
    previous = f5_capacity_permit(path, policy, previous=True)
    settled = previous.reserve(65536, 16384)
    settled.mark_transmission_started()
    settled.settle(usage())
    unknown = previous.reserve(65536, 16384)
    unknown.mark_transmission_started()
    unknown.unknown()
    inflight = previous.reserve(65536, 16384)
    refs = [historical.reference, settled.reference, unknown.reference, inflight.reference]
    original_rows = [row(path, ref) for ref in refs]

    permit = f5_capacity_permit(path, policy)
    before = permit.summary()
    assert before["stage_cap_microyuan"] == 6000000
    assert before["global_cap_microyuan"] == 50000000
    assert before["stage_occupied_microyuan"] == 422 + 262144 * 2
    added = permit.reserve(98304, 16384)
    added.mark_transmission_started()
    added.unknown()
    assert row(path, added.reference)[:3] == ("unknown", 327680, 327680)
    metadata = json.loads(row(path, added.reference)[3])["current_model_budget"]
    assert (metadata["max_input_tokens"], metadata["max_output_tokens"]) == (98304, 16384)
    restarted = f5_capacity_permit(path, policy)
    assert restarted.summary()["global_occupied_microyuan"] == (
        before["global_occupied_microyuan"] + 327680
    )
    assert restarted.summary()["stage_occupied_microyuan"] == (
        before["stage_occupied_microyuan"] + 327680
    )
    assert [row(path, ref) for ref in refs] == original_rows
    # The new capacity does not rewrite the bound attached to an older unknown call.
    with pytest.raises(RuntimeError, match="exceeds bound"):
        restarted.settle_reservation(unknown.reference, usage(
            prompt_tokens=65537, total_tokens=65637,
            prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None,
        ))
    assert row(path, unknown.reference) == original_rows[2]
    old_metadata = json.loads(original_rows[2][3])["current_model_budget"]
    restarted.settle_reservation(unknown.reference, usage())
    recovered = json.loads(row(path, unknown.reference)[3])["current_model_budget"]
    assert recovered["stage"] == old_metadata["stage"]
    assert recovered["max_input_tokens"] == 65536
    restarted.settle_reservation(added.reference, usage(
        prompt_tokens=98304, completion_tokens=16384, total_tokens=114688,
        prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=98304,
    ))
    assert row(path, added.reference)[:3] == ("settled", 327680, 163840)
    assert row(path, historical.reference) == original_rows[0]
    assert row(path, settled.reference) == original_rows[1]
    assert row(path, inflight.reference) == original_rows[3]


def test_f5_96kb_reservation_rejects_stage_balance_that_only_covers_old_bound(tmp_path):
    path, policy, _ = ledger(tmp_path)
    permit = f5_capacity_permit(path, policy)
    for _ in range(21):
        permit.reserve(65536, 16384).unknown()
    permit.reserve(31952, 16384).unknown()
    before = permit.summary()
    assert before["stage_occupied_microyuan"] == 5700000
    with pytest.raises(AssistantServiceError, match="stage budget exhausted"):
        permit.reserve(98304, 16384)
    assert permit.summary() == before
    assert row(path, permit.reserve(65536, 16384).reference)[:3] == (
        "reserved", 262144, 262144
    )


def test_f5_96kb_reservation_keeps_original_global_fifty_yuan_cap(tmp_path):
    path, policy, legacy = ledger(tmp_path)
    historical = legacy.reserve(0, 5522222)
    historical.unknown()
    original = row(path, historical.reference)
    permit = f5_capacity_permit(path, policy)
    before = permit.summary()
    assert before["global_cap_microyuan"] - before["global_occupied_microyuan"] == 300002
    with pytest.raises(AssistantServiceError, match="approved model budget exhausted"):
        permit.reserve(98304, 16384)
    assert permit.summary() == before
    assert row(path, historical.reference) == original
    permit.reserve(65536, 16384)


def test_f7_three_to_four_yuan_continuation_keeps_old_rows_and_cumulative_occupancy(tmp_path):
    from app.composition.school_fact_current_model_budget import ApprovedModelStageBudget

    path, policy, legacy = ledger(tmp_path)
    historical = legacy.reserve(0, 360448)
    historical.unknown()
    now = datetime(2026, 9, 27, tzinfo=UTC)
    approval = current(path, policy, [now]).current_approval
    original = ApprovedModelStageBudget(
        stage_id="school-facts-f7", approval_reference="original-f7-three-yuan",
        cap_microyuan=3_000_000,
    )
    extended = ApprovedModelStageBudget(
        stage_id="school-facts-f7", approval_reference="approved-f7-four-yuan",
        cap_microyuan=4_000_000, previous_cap_microyuan=3_000_000,
        previous_approval_reference=original.approval_reference,
    )

    def permit(stage_policy):
        return SqliteCurrentModelCallPermit(
            path=path, ledger_policy=policy, current_approval=approval,
            stage_budget=stage_policy, clock=lambda: now,
        )

    old = permit(original)
    settled = old.reserve(100, 100)
    settled.mark_transmission_started()
    settled.settle(usage())
    unknown = old.reserve(98304, 16384)
    unknown.unknown()
    inflight = old.reserve(98304, 16384)
    refs = [historical.reference, settled.reference, unknown.reference, inflight.reference]
    before = [row(path, ref) for ref in refs]
    old_total = old.summary()["global_occupied_microyuan"]
    old_stage = old.summary()["stage_occupied_microyuan"]

    upgraded = permit(extended)
    assert upgraded.summary()["stage_cap_microyuan"] == 4_000_000
    assert upgraded.summary()["global_cap_microyuan"] == 50_000_000
    assert upgraded.summary()["global_occupied_microyuan"] == old_total
    assert upgraded.summary()["stage_occupied_microyuan"] == old_stage == 422 + 2 * 327680
    added = upgraded.reserve(98304, 16384)
    added.unknown()
    after_restart = permit(extended)
    assert after_restart.summary()["stage_occupied_microyuan"] == old_stage + 327680
    assert after_restart.summary()["global_occupied_microyuan"] == old_total + 327680
    assert [row(path, ref) for ref in refs] == before
    assert json.loads(row(path, added.reference)[3])["current_model_budget"]["stage"] == {
        "stage_id": "school-facts-f7", "approval_reference": "approved-f7-four-yuan",
        "cap_microyuan": 4_000_000, "previous_cap_microyuan": 3_000_000,
        "previous_approval_reference": "original-f7-three-yuan",
    }
    with pytest.raises(RuntimeError, match="stage policy drift"):
        permit(original)
    assert [row(path, ref) for ref in refs] == before
