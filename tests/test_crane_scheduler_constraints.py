from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import ConflictError, ValidationError
from terminalweave.core.intervals import TimeInterval
from terminalweave.cranes.scheduler import Crane, CraneJob, CraneScheduler


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


def make_crane(**kwargs):
    defaults = dict(crane_id="QC1", rail_index=0, moves_per_hour=60.0)
    defaults.update(kwargs)
    return Crane(**defaults)


def test_travel_time_applies_before_existing_job():
    scheduler = CraneScheduler([make_crane(travel_minutes_per_bay=2.0)])
    late = CraneJob("LATE", "CALL", 11, 30, D + timedelta(hours=2), D + timedelta(hours=4))
    scheduler.schedule(late)
    early = CraneJob("EARLY", "CALL", 1, 60, D, D + timedelta(hours=2))
    scheduled = scheduler.schedule(early)
    # 60 moves at 60 moves/hour = 1h; 10 bays * 2 min = 20 min travel before LATE
    assert scheduled.interval.end <= D + timedelta(hours=1, minutes=40)


def test_zero_travel_time_keeps_back_to_back_scheduling():
    scheduler = CraneScheduler([make_crane()])
    first = CraneJob("J1", "CALL", 1, 60, D, D + timedelta(hours=2))
    second = CraneJob("J2", "CALL", 11, 30, D, D + timedelta(hours=3))
    scheduler.schedule(first)
    scheduled = scheduler.schedule(second)
    assert scheduled.interval.start == D + timedelta(hours=1)


def test_maintenance_window_defers_job():
    window = TimeInterval(D + timedelta(minutes=30), D + timedelta(hours=2))
    scheduler = CraneScheduler([make_crane()], maintenance_windows={"QC1": [window]})
    job = CraneJob("J1", "CALL", 5, 60, D, D + timedelta(hours=4))
    scheduled = scheduler.schedule(job)
    assert scheduled.interval.start >= D + timedelta(hours=2)
    assert not scheduled.interval.overlaps(window)


def test_maintenance_window_can_make_job_infeasible():
    scheduler = CraneScheduler([make_crane()])
    scheduler.add_maintenance_window("QC1", TimeInterval(D, D + timedelta(hours=3)))
    job = CraneJob("J1", "CALL", 5, 60, D, D + timedelta(hours=2))
    with pytest.raises(ConflictError):
        scheduler.schedule(job)
    assert scheduler.scheduled == ()


def test_unknown_maintenance_crane_rejected():
    scheduler = CraneScheduler([make_crane()])
    with pytest.raises(ValidationError):
        scheduler.add_maintenance_window("GHOST", TimeInterval(D, D + timedelta(hours=1)))


def test_predecessor_must_finish_first():
    scheduler = CraneScheduler([make_crane()])
    first = CraneJob("J1", "CALL", 5, 60, D, D + timedelta(hours=2))
    dependent = CraneJob(
        "J2", "CALL", 5, 30, D, D + timedelta(hours=4), predecessor_ids=frozenset({"J1"})
    )
    scheduler.schedule(first)
    scheduled = scheduler.schedule(dependent)
    assert scheduled.interval.start >= D + timedelta(hours=1)


def test_unscheduled_predecessor_raises():
    scheduler = CraneScheduler([make_crane()])
    job = CraneJob("J2", "CALL", 5, 30, D, D + timedelta(hours=4), predecessor_ids=frozenset({"J1"}))
    with pytest.raises(ConflictError):
        scheduler.schedule(job)
    assert scheduler.scheduled == ()


def test_rail_interference_still_enforced():
    scheduler = CraneScheduler(
        [make_crane(), make_crane(crane_id="QC2", rail_index=1)]
    )
    first = CraneJob("J1", "CALL", 10, 120, D, D + timedelta(hours=4))
    crossing = CraneJob("J2", "CALL", 4, 120, D, D + timedelta(hours=4))
    a = scheduler.schedule(first)
    b = scheduler.schedule(crossing)
    # the crane behind (QC2) cannot work bays left of QC1 while QC1 works
    assert not a.interval.overlaps(b.interval) or a.crane_id == b.crane_id


def test_schedule_all_is_atomic_on_failure():
    scheduler = CraneScheduler([make_crane()])
    ok = CraneJob("OK", "CALL", 5, 60, D, D + timedelta(hours=2))
    impossible = CraneJob("BAD", "CALL", 5, 600, D, D + timedelta(hours=2))
    with pytest.raises(ConflictError):
        scheduler.schedule_all([ok, impossible])
    assert scheduler.scheduled == ()


def test_schedule_all_preserves_prior_plan_on_failure():
    scheduler = CraneScheduler([make_crane()])
    existing = scheduler.schedule(CraneJob("KEEP", "CALL", 5, 60, D, D + timedelta(hours=2)))
    ok = CraneJob("OK", "CALL", 6, 60, D + timedelta(hours=2), D + timedelta(hours=4))
    impossible = CraneJob("BAD", "CALL", 6, 600, D + timedelta(hours=2), D + timedelta(hours=4))
    with pytest.raises(ConflictError):
        scheduler.schedule_all([ok, impossible])
    assert [s.job_id for s in scheduler.scheduled] == ["KEEP"]
    assert scheduler.scheduled[0].interval == existing.interval


def test_schedule_all_orders_predecessors_within_batch():
    scheduler = CraneScheduler([make_crane()])
    dependent = CraneJob(
        "J2", "CALL", 5, 30, D, D + timedelta(hours=4), predecessor_ids=frozenset({"J1"})
    )
    first = CraneJob("J1", "CALL", 5, 60, D, D + timedelta(hours=2))
    placed = scheduler.schedule_all([dependent, first])
    by_id = {s.job_id: s for s in placed}
    assert by_id["J2"].interval.start >= by_id["J1"].interval.end


def test_schedule_all_rejects_cycle_without_polluting():
    scheduler = CraneScheduler([make_crane()])
    a = CraneJob("A", "CALL", 5, 30, D, D + timedelta(hours=4), predecessor_ids=frozenset({"B"}))
    b = CraneJob("B", "CALL", 5, 30, D, D + timedelta(hours=4), predecessor_ids=frozenset({"A"}))
    with pytest.raises(ConflictError):
        scheduler.schedule_all([a, b])
    assert scheduler.scheduled == ()


def test_schedule_all_rejects_duplicate_and_unknown():
    scheduler = CraneScheduler([make_crane()])
    job = CraneJob("J1", "CALL", 5, 30, D, D + timedelta(hours=2))
    with pytest.raises(ValidationError):
        scheduler.schedule_all([job, job])
    stranger = CraneJob("J2", "CALL", 5, 30, D, D + timedelta(hours=2), predecessor_ids=frozenset({"J9"}))
    with pytest.raises(ValidationError):
        scheduler.schedule_all([stranger])
    assert scheduler.scheduled == ()


def test_rescheduling_same_job_id_rejected():
    scheduler = CraneScheduler([make_crane()])
    job = CraneJob("J1", "CALL", 5, 30, D, D + timedelta(hours=2))
    scheduler.schedule(job)
    with pytest.raises(ValidationError):
        scheduler.schedule(job)
