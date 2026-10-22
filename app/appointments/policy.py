"""Backend authority, independent of phone reach hours and provider-specific data."""
from dataclasses import dataclass,field
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from app.appointments.contracts import Interval,Coverage,EventIdentity


@dataclass(frozen=True)
class BookingPolicy:
    version: str = 'academic-office-v1'
    zone: str = 'America/New_York'
    # Owner instruction; not inferred from broad cached free windows or event titles.
    weekly: tuple = ((0,600,720),(3,600,720),(4,840,960))
    closed_dates: frozenset[date] = frozenset()
    buffer_before: int = 0
    buffer_after: int = 0
    check_ttl_seconds: int = 30
    require_occurrence: bool = True

    def office_hours(self, interval):
        a,b=interval.start.astimezone(ZoneInfo(self.zone)),interval.end.astimezone(ZoneInfo(self.zone))
        if a.date()!=b.date() or a.date() in self.closed_dates:return False
        return any(a.weekday()==day and a.hour*60+a.minute>=start and
            b.hour*60+b.minute+(bool(b.second or b.microsecond))<=end for day,start,end in self.weekly)

    def decide(self, interval:Interval, coverage:Coverage, now:datetime, exclude:EventIdentity|None=None):
        # A known conflict is useful even while another coverage gate is unknown.
        padded=Interval(interval.start-timedelta(minutes=self.buffer_before),
            interval.end+timedelta(minutes=self.buffer_after),interval.zone)
        for event in coverage.events:
            if event.identity==exclude or (exclude and event.alias_verified and exclude in event.aliases):continue
            if event.kind!='availability' and event.state=='busy' and event.interval and event.interval.overlaps(padded):
                return 'busy','overlapping_commitment'
        if interval.start<=now:return 'outside_hours','past_interval'
        if not self.office_hours(interval):return 'outside_hours','academic_office_hours'
        if not coverage.complete or not coverage.interval.contains(padded) or not coverage.checked_at<=now<coverage.valid_until:
            return 'unknown',coverage.reason or 'incomplete_coverage'
        if any(e.state=='unknown' for e in coverage.events):return 'unknown','unknown_event_state'
        if self.require_occurrence and not any(e.kind=='availability' and e.interval and e.interval.contains(interval)
                and e.state!='cancelled' for e in coverage.events):
            return 'unknown','office_occurrence_not_verified'
        return 'available','verified_with_mirror_lag'
