"""Owner-maintained office and mobile-reach schedules, evaluated in an IANA time zone."""
import json
from datetime import datetime,timezone
from typing import Literal
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator


class HoursWindow(BaseModel):
    model_config=ConfigDict(extra='forbid')
    days:list[int]=Field(min_length=1,max_length=7)
    start:str=Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end:str=Field(pattern=r'^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$')

    @model_validator(mode='after')
    def valid(self):
        if any(type(d) is not int or d not in range(7) for d in self.days) or len(set(self.days))!=len(self.days):
            raise ValueError('Use distinct weekday numbers 0=Monday through 6=Sunday')
        if self.start>=self.end: raise ValueError('End must be after start; split overnight windows at midnight')
        return self


class HoursSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool=False
    time_zone:str='America/New_York'
    office_name:str=Field(default='The office',min_length=1,max_length=120)
    office_hours:list[HoursWindow]=Field(default_factory=list,max_length=28)
    owner_reach_hours:list[HoursWindow]|None=Field(default=None,max_length=28)
    outside_hours:Literal['closed','messages']='closed'

    @field_validator('time_zone')
    @classmethod
    def zone(cls,value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('Use an IANA time zone') from None
        return value


class OperatingHours:
    def __init__(self,db):
        self.db=db
        self.clock=lambda:datetime.now(timezone.utc)
        with db.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS operating_hours (
                id INTEGER PRIMARY KEY CHECK(id=1),settings TEXT NOT NULL,updated_at TEXT NOT NULL)''')

    def policy(self):
        with self.db.connect() as conn:
            row=conn.execute('SELECT settings FROM operating_hours WHERE id=1').fetchone()
        return HoursSettings.model_validate_json(row['settings']) if row else HoursSettings()

    def within(self,windows,policy):
        local=self.clock().astimezone(ZoneInfo(policy.time_zone))
        minute=local.strftime('%H:%M')
        return any(local.weekday() in w.days and w.start<=minute<w.end for w in windows)

    def state(self):
        p=self.policy()
        return {'office_open':not p.enabled or self.within(p.office_hours,p),
            'owner_reachable':not p.enabled or self.within(p.office_hours if p.owner_reach_hours is None else p.owner_reach_hours,p)}

    def get(self):
        p=self.policy()
        return {**p.model_dump(),**self.state(),'office_hours_spoken':self.describe(p.office_hours),
            'note':'Weekday numbers: 0 Monday through 6 Sunday. Start inclusive, end exclusive. '
                'Null owner_reach_hours inherits office hours; [] means never ring the owner. '
                'Disabled means existing always-open behavior. No caller can override this policy.'}

    def set(self,body):
        with self.db.connect() as conn:
            conn.execute('INSERT INTO operating_hours VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                'settings=excluded.settings,updated_at=excluded.updated_at',
                (body.model_dump_json(),self.clock().isoformat()))
        return self.get()

    def describe(self,windows):
        days=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
        def time(value):
            h,m=map(int,value.split(':'))
            if h==24: return 'midnight'
            return str(h%12 or 12)+(':'+str(m).zfill(2) if m else '')+(' AM' if h<12 else ' PM')
        groups={}
        for w in windows:
            groups.setdefault((w.start,w.end),set()).update(w.days)
        return '; '.join(('Every day' if len(ds)==7 else ', '.join(days[d] for d in sorted(ds)))+': '+time(start)+' to '+time(end)
            for (start,end),ds in groups.items()) or 'No office hours are currently available. Please try again later.'

    def closed_announcement(self):
        p=self.policy()
        return p.office_name+' is currently closed. Please call during our office hours: '+self.describe(p.office_hours)+\
            '. All times are '+p.time_zone.rsplit('/',1)[-1].replace('_',' ')+' time.'

    def require_owner(self):
        if not self.state()['owner_reachable']:
            raise ValueError('The owner is outside the configured mobile reach hours. Take a message; do not ring or retry.')
