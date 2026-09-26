from datetime import datetime, timedelta, timezone

from terminalweave.cranes.scheduler import Crane, CraneJob, CraneScheduler


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


def test_crane_travel_time_separates_consecutive_bay_jobs():
    scheduler = CraneScheduler(
        [Crane("QC1", 0, 60.0, min_separation_bays=2, travel_minutes_per_bay=2.0)]
    )
    first = CraneJob("J1", "CALL", 1, 60, D, D + timedelta(hours=2))
    second = CraneJob(
        "J2", "CALL", 11, 30, D + timedelta(hours=1), D + timedelta(hours=3)
    )
    scheduler.schedule(first)
    scheduled = scheduler.schedule(second)
    assert scheduled.interval.start >= D + timedelta(hours=1, minutes=20)
