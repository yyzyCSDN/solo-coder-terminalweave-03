from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, ConflictError
from terminalweave.core.intervals import TimeInterval

@dataclass(frozen=True)
class Crane:
    crane_id: str
    rail_index: int
    moves_per_hour: float
    min_separation_bays: int = 2
    travel_minutes_per_bay: float = 0.0

    def __post_init__(self) -> None:
        if self.rail_index < 0 or self.moves_per_hour <= 0 or self.min_separation_bays < 0 or self.travel_minutes_per_bay < 0:
            raise ValidationError('invalid crane configuration')

@dataclass(frozen=True)
class CraneJob:
    job_id: str
    call_id: str
    bay: int
    moves: int
    earliest_start: datetime
    latest_finish: datetime
    priority: int = 0
    predecessor_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'earliest_start', ensure_utc(self.earliest_start))
        object.__setattr__(self, 'latest_finish', ensure_utc(self.latest_finish))
        object.__setattr__(self, 'predecessor_ids', frozenset(self.predecessor_ids))
        if self.bay < 1 or self.moves <= 0 or self.latest_finish <= self.earliest_start:
            raise ValidationError('invalid crane job')
        if self.job_id in self.predecessor_ids:
            raise ValidationError('job cannot be its own predecessor')

@dataclass(frozen=True)
class ScheduledCraneJob:
    crane_id: str
    rail_index: int
    job_id: str
    bay: int
    interval: TimeInterval
    moves: int

class CraneScheduler:

    def __init__(self, cranes: list[Crane], maintenance_windows: dict[str, Iterable[TimeInterval]] | None=None):
        if not cranes:
            raise ValidationError('at least one crane is required')
        if len({c.crane_id for c in cranes}) != len(cranes):
            raise ValidationError('duplicate crane id')
        self.cranes = {c.crane_id: c for c in cranes}
        self._scheduled: list[ScheduledCraneJob] = []
        self._maintenance: dict[str, list[TimeInterval]] = {c.crane_id: [] for c in cranes}
        for crane_id, windows in (maintenance_windows or {}).items():
            for window in windows:
                self.add_maintenance_window(crane_id, window)

    @property
    def scheduled(self) -> tuple[ScheduledCraneJob, ...]:
        return tuple(sorted(self._scheduled, key=lambda x: (x.interval.start, x.rail_index)))

    def add_maintenance_window(self, crane_id: str, window: TimeInterval) -> None:
        if crane_id not in self.cranes:
            raise ValidationError(f'unknown crane {crane_id}')
        if not isinstance(window, TimeInterval):
            raise ValidationError('maintenance window must be a TimeInterval')
        self._maintenance[crane_id].append(window)

    def maintenance_windows(self, crane_id: str) -> tuple[TimeInterval, ...]:
        if crane_id not in self.cranes:
            raise ValidationError(f'unknown crane {crane_id}')
        return tuple(sorted(self._maintenance[crane_id], key=lambda w: (w.start, w.end)))

    def _duration(self, crane: Crane, job: CraneJob) -> timedelta:
        return timedelta(hours=job.moves / crane.moves_per_hour)

    def _travel_gap(self, crane: Crane, bay_a: int, bay_b: int) -> timedelta:
        return timedelta(minutes=crane.travel_minutes_per_bay * abs(bay_a - bay_b))

    def _same_crane_conflict(self, candidate: ScheduledCraneJob, existing: ScheduledCraneJob) -> bool:
        if candidate.crane_id != existing.crane_id:
            return False
        gap = self._travel_gap(self.cranes[candidate.crane_id], candidate.bay, existing.bay)
        return not (candidate.interval.end + gap <= existing.interval.start or existing.interval.end + gap <= candidate.interval.start)

    def _interference(self, candidate: ScheduledCraneJob, existing: ScheduledCraneJob) -> bool:
        if not candidate.interval.overlaps(existing.interval):
            return False
        c = self.cranes[candidate.crane_id]
        e = self.cranes[existing.crane_id]
        if abs(candidate.bay - existing.bay) < max(c.min_separation_bays, e.min_separation_bays):
            return True
        if candidate.rail_index < existing.rail_index and candidate.bay > existing.bay:
            return True
        if candidate.rail_index > existing.rail_index and candidate.bay < existing.bay:
            return True
        return False

    def _maintenance_conflict(self, candidate: ScheduledCraneJob) -> bool:
        return any((candidate.interval.overlaps(window) for window in self._maintenance[candidate.crane_id]))

    def _predecessor_finish(self, job: CraneJob) -> datetime | None:
        finishes = []
        for predecessor_id in job.predecessor_ids:
            done = [s.interval.end for s in self._scheduled if s.job_id == predecessor_id]
            if not done:
                raise ConflictError(f'predecessor {predecessor_id} is not scheduled')
            finishes.append(max(done))
        return max(finishes) if finishes else None

    def can_schedule(self, crane: Crane, job: CraneJob, start: datetime) -> bool:
        start = ensure_utc(start)
        finish = start + self._duration(crane, job)
        if start < job.earliest_start or finish > job.latest_finish:
            return False
        predecessor_finish = self._predecessor_finish(job)
        if predecessor_finish and start < predecessor_finish:
            return False
        candidate = ScheduledCraneJob(crane.crane_id, crane.rail_index, job.job_id, job.bay, TimeInterval(start, finish), job.moves)
        if self._maintenance_conflict(candidate):
            return False
        for existing in self._scheduled:
            if self._same_crane_conflict(candidate, existing) or self._interference(candidate, existing):
                return False
        return True

    def schedule(self, job: CraneJob, increment: timedelta=timedelta(minutes=5)) -> ScheduledCraneJob:
        if increment.total_seconds() <= 0:
            raise ValidationError('increment must be positive')
        if any((s.job_id == job.job_id for s in self._scheduled)):
            raise ValidationError(f'job {job.job_id} is already scheduled')
        predecessor_finish = self._predecessor_finish(job)
        ready_at = max(job.earliest_start, predecessor_finish) if predecessor_finish else job.earliest_start
        candidates: list[tuple[datetime, float, Crane]] = []
        for crane in self.cranes.values():
            cursor = ready_at
            duration = self._duration(crane, job)
            while cursor + duration <= job.latest_finish:
                if self.can_schedule(crane, job, cursor):
                    candidates.append((cursor + duration, -crane.moves_per_hour, crane))
                    break
                cursor += increment
        if not candidates:
            raise ConflictError('no crane can execute job within its window')
        finish, _, crane = min(candidates, key=lambda x: (x[0], x[1], x[2].rail_index))
        start = finish - self._duration(crane, job)
        scheduled = ScheduledCraneJob(crane.crane_id, crane.rail_index, job.job_id, job.bay, TimeInterval(start, finish), job.moves)
        self._scheduled.append(scheduled)
        return scheduled

    def schedule_all(self, jobs: list[CraneJob], increment: timedelta=timedelta(minutes=5)) -> list[ScheduledCraneJob]:
        ids = [j.job_id for j in jobs]
        if len(set(ids)) != len(ids):
            raise ValidationError('duplicate job id')
        scheduled_ids = {s.job_id for s in self._scheduled}
        clash = set(ids) & scheduled_ids
        if clash:
            raise ValidationError(f'job already scheduled: {sorted(clash)}')
        known = set(ids) | scheduled_ids
        for job in jobs:
            unknown = job.predecessor_ids - known
            if unknown:
                raise ValidationError(f'unknown predecessors: {sorted(unknown)}')
        order = self._topological_order(jobs, scheduled_ids)
        snapshot = list(self._scheduled)
        placed: list[ScheduledCraneJob] = []
        try:
            for job in order:
                placed.append(self.schedule(job, increment))
        except Exception:
            self._scheduled = snapshot
            raise
        return placed

    def _topological_order(self, jobs: list[CraneJob], satisfied: set[str]) -> list[CraneJob]:
        remaining = {j.job_id: j for j in jobs}
        done = set(satisfied)
        order: list[CraneJob] = []
        while remaining:
            ready = [j for j in remaining.values() if j.predecessor_ids <= done]
            if not ready:
                raise ConflictError('job predecessors form a cycle')
            ready.sort(key=lambda j: (-j.priority, j.earliest_start, j.latest_finish, j.job_id))
            pick = ready[0]
            order.append(pick)
            done.add(pick.job_id)
            del remaining[pick.job_id]
        return order

    def remove_job(self, job_id: str) -> bool:
        before = len(self._scheduled)
        self._scheduled = [s for s in self._scheduled if s.job_id != job_id]
        return len(self._scheduled) != before

    def crane_workload(self, crane_id: str, interval: TimeInterval) -> float:
        seconds = 0.0
        for scheduled in self._scheduled:
            if scheduled.crane_id != crane_id:
                continue
            overlap = scheduled.interval.intersection(interval)
            if overlap:
                seconds += overlap.duration.total_seconds()
        return min(1.0, seconds / interval.duration.total_seconds())
