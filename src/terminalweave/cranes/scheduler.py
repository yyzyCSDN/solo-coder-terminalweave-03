from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import heapq
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
        if not isinstance(self.predecessor_ids, frozenset):
            object.__setattr__(self, 'predecessor_ids', frozenset(self.predecessor_ids))
        if self.bay < 1 or self.moves <= 0 or self.latest_finish <= self.earliest_start:
            raise ValidationError('invalid crane job')
        if self.job_id in self.predecessor_ids:
            raise ValidationError('crane job cannot precede itself')

@dataclass(frozen=True)
class ScheduledCraneJob:
    crane_id: str
    rail_index: int
    job_id: str
    bay: int
    interval: TimeInterval
    moves: int

class CraneScheduler:

    def __init__(self, cranes: list[Crane], maintenance_windows: dict[str, list[TimeInterval]] | None = None):
        if not cranes:
            raise ValidationError('at least one crane is required')
        if len({c.crane_id for c in cranes}) != len(cranes):
            raise ValidationError('duplicate crane id')
        self.cranes = {c.crane_id: c for c in cranes}
        self._scheduled: list[ScheduledCraneJob] = []
        self._maintenance: dict[str, list[TimeInterval]] = {}
        if maintenance_windows:
            for crane_id, windows in maintenance_windows.items():
                if crane_id not in self.cranes:
                    raise ValidationError(f'unknown crane: {crane_id}')
                for window in windows:
                    self.add_maintenance_window(crane_id, window)

    @property
    def scheduled(self) -> tuple[ScheduledCraneJob, ...]:
        return tuple(sorted(self._scheduled, key=lambda x: (x.interval.start, x.rail_index)))

    def add_maintenance_window(self, crane_id: str, window: TimeInterval) -> None:
        if crane_id not in self.cranes:
            raise ValidationError(f'unknown crane: {crane_id}')
        self._maintenance.setdefault(crane_id, []).append(window)

    def _duration(self, crane: Crane, job: CraneJob) -> timedelta:
        return timedelta(hours=job.moves / crane.moves_per_hour)

    def _travel_time(self, crane: Crane, from_bay: int, to_bay: int) -> timedelta:
        return timedelta(minutes=crane.travel_minutes_per_bay * abs(from_bay - to_bay))

    def _scheduled_by_id(self) -> dict[str, ScheduledCraneJob]:
        return {s.job_id: s for s in self._scheduled}

    def _blocked_by_maintenance(self, crane_id: str, start: datetime, end: datetime) -> bool:
        # Half-open overlap: a job or travel leg touching a window boundary is fine.
        for window in self._maintenance.get(crane_id, ()):
            if start < window.end and window.start < end:
                return True
        return False

    def _travel_fits(self, crane: Crane, depart: datetime, arrive: datetime,
                     required: timedelta) -> bool:
        # The crane may walk early and wait, so any required-length slot inside
        # [depart, arrive] that avoids maintenance windows makes the move legal.
        if required.total_seconds() <= 0:
            return True
        if arrive - depart < required:
            return False
        windows = sorted(self._maintenance.get(crane.crane_id, ()), key=lambda w: w.start)
        cursor = depart
        for window in windows:
            if window.end <= cursor or window.start >= arrive:
                continue
            if window.start - cursor >= required:
                return True
            cursor = max(cursor, window.end)
            if cursor > arrive:
                return False
        return arrive - cursor >= required

    def _predecessor_ready(self, job: CraneJob, start: datetime,
                           scheduled_by_id: dict[str, ScheduledCraneJob]) -> bool:
        for predecessor_id in job.predecessor_ids:
            predecessor = scheduled_by_id.get(predecessor_id)
            if predecessor is None or predecessor.interval.end > start:
                return False
        return True

    def _same_crane_ok(self, crane: Crane, candidate: ScheduledCraneJob) -> bool:
        for existing in self._scheduled:
            if existing.crane_id != crane.crane_id:
                continue
            if candidate.interval.overlaps(existing.interval):
                return False
            required = self._travel_time(crane, candidate.bay, existing.bay)
            if required.total_seconds() <= 0:
                continue
            if existing.interval.end <= candidate.interval.start:
                depart, arrive = existing.interval.end, candidate.interval.start
            elif candidate.interval.end <= existing.interval.start:
                depart, arrive = candidate.interval.end, existing.interval.start
            else:
                return False
            if not self._travel_fits(crane, depart, arrive, required):
                return False
        return True

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

    def can_schedule(self, crane: Crane, job: CraneJob, start: datetime) -> bool:
        start = ensure_utc(start)
        finish = start + self._duration(crane, job)
        if start < job.earliest_start or finish > job.latest_finish:
            return False
        if not self._predecessor_ready(job, start, self._scheduled_by_id()):
            return False
        if self._blocked_by_maintenance(crane.crane_id, start, finish):
            return False
        candidate = ScheduledCraneJob(crane.crane_id, crane.rail_index, job.job_id, job.bay, TimeInterval(start, finish), job.moves)
        if not self._same_crane_ok(crane, candidate):
            return False
        for existing in self._scheduled:
            if existing.crane_id == crane.crane_id:
                continue
            if self._interference(candidate, existing):
                return False
        return True

    def _earliest_start(self, job: CraneJob) -> datetime:
        ready = job.earliest_start
        for predecessor in self._scheduled_by_id().values():
            if predecessor.job_id in job.predecessor_ids and predecessor.interval.end > ready:
                ready = predecessor.interval.end
        return ready

    def schedule(self, job: CraneJob, increment: timedelta=timedelta(minutes=5)) -> ScheduledCraneJob:
        if increment.total_seconds() <= 0:
            raise ValidationError('increment must be positive')
        if any(s.job_id == job.job_id for s in self._scheduled):
            raise ConflictError(f'job already scheduled: {job.job_id}')
        candidates: list[tuple[datetime, float, int, Crane]] = []
        cursor_start = self._earliest_start(job)
        for crane in self.cranes.values():
            cursor = cursor_start
            duration = self._duration(crane, job)
            while cursor + duration <= job.latest_finish:
                if self.can_schedule(crane, job, cursor):
                    candidates.append((cursor + duration, -crane.moves_per_hour, crane.rail_index, crane))
                    break
                cursor += increment
        if not candidates:
            raise ConflictError('no crane can execute job within its window')
        finish, _, _, crane = min(candidates, key=lambda x: (x[0], x[1], x[2]))
        start = finish - self._duration(crane, job)
        scheduled = ScheduledCraneJob(crane.crane_id, crane.rail_index, job.job_id, job.bay, TimeInterval(start, finish), job.moves)
        self._scheduled.append(scheduled)
        return scheduled

    def schedule_batch(self, jobs: list[CraneJob], increment: timedelta=timedelta(minutes=5)) -> tuple[ScheduledCraneJob, ...]:
        if increment.total_seconds() <= 0:
            raise ValidationError('increment must be positive')
        if not jobs:
            return ()
        batch = {job.job_id: job for job in jobs}
        if len(batch) != len(jobs):
            raise ValidationError('duplicate crane job in batch')
        known = set(batch) | {s.job_id for s in self._scheduled}
        for job in jobs:
            unknown = job.predecessor_ids - known
            if unknown:
                raise ValidationError(f'unknown job predecessors: {sorted(unknown)}')

        # Kahn ordering: predecessors are always placed first; ready jobs are
        # offered highest-priority / earliest-window first.
        remaining = set(batch)
        dependents: dict[str, set[str]] = {job_id: set() for job_id in batch}
        pending: dict[str, set[str]] = {}
        ready: list[tuple[int, datetime, str]] = []
        for job in jobs:
            blocked = job.predecessor_ids & remaining
            if blocked:
                pending[job.job_id] = set(blocked)
                for predecessor_id in blocked:
                    dependents[predecessor_id].add(job.job_id)
            else:
                heapq.heappush(ready, (-job.priority, job.earliest_start, job.job_id))
        order: list[CraneJob] = []
        while ready:
            _, _, job_id = heapq.heappop(ready)
            job = batch[job_id]
            order.append(job)
            remaining.discard(job_id)
            for dependent_id in dependents[job_id]:
                pending[dependent_id].discard(job_id)
                if not pending[dependent_id]:
                    dependent = batch[dependent_id]
                    heapq.heappush(ready, (-dependent.priority, dependent.earliest_start, dependent_id))
        if remaining:
            raise ConflictError('crane job predecessors contain a cycle')

        # Stage every placement against a snapshot; the live plan is only
        # touched once the whole batch fits, so a failure leaves no residue.
        snapshot = list(self._scheduled)
        placed: list[ScheduledCraneJob] = []
        try:
            for job in order:
                placed.append(self.schedule(job, increment))
        except BaseException:
            self._scheduled = snapshot
            raise
        return tuple(placed)

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
