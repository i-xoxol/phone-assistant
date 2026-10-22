"""Initial Google adapter. Only this module understands Google event shapes."""
import asyncio,hashlib,json,os
from dataclasses import replace
from datetime import date,datetime,time,timedelta,timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from app.appointments.contracts import (CalendarIdentity,EventIdentity,CalendarEvent,Interval,
    Capabilities,Coverage,ChangePage,ProviderFault,Appointment)
from app.appointments.details import calendar_title,calendar_description,require_arrangement

UTC=timezone.utc
SCOPE='https://www.googleapis.com/auth/calendar.events'
FIELDS='id,etag,status,transparency,eventType,start,end,recurringEventId,originalStartTime,recurrence,location,extendedProperties'


class OwnerFileToken:
    """Existing approved owner-only cache, on the established host; refresh in memory only."""
    def __init__(self,path,http,clock):
        self.path=Path(path);self.http=http;self.clock=clock
        self.cached=None;self.expires=None;self.lock=asyncio.Lock()
    async def __call__(self):
        async with self.lock:
            if self.cached and self.expires>self.clock()+timedelta(seconds=60):return self.cached
            try:
                if self.path.is_symlink() or (os.name!='nt' and self.path.stat().st_mode&0o077):raise ProviderFault('unauthorized')
                saved=json.loads(self.path.read_text())
                if SCOPE not in saved.get('scopes',[]):raise ProviderFault('unauthorized')
                expiry=datetime.fromisoformat(saved.get('expiry','1970-01-01T00:00:00Z').replace('Z','+00:00'))
                if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=UTC)
                if expiry>self.clock()+timedelta(seconds=60) and saved.get('token'):
                    self.cached,self.expires=saved['token'],expiry
                else:
                    response=await self.http.post('https://oauth2.googleapis.com/token',data={
                        'client_id':saved['client_id'],'client_secret':saved['client_secret'],
                        'refresh_token':saved['refresh_token'],'grant_type':'refresh_token'},timeout=5)
                    if response.status_code!=200:raise ProviderFault('unauthorized')
                    result=response.json();self.cached=result['access_token']
                    self.expires=self.clock()+timedelta(seconds=int(result.get('expires_in',3600)))
                return self.cached
            except (httpx.HTTPError,OSError,KeyError,ValueError,TypeError):raise ProviderFault('unauthorized') from None


class MirrorHealth:
    """Read the existing status API, never the default-writing reconciliation CLI."""
    def __init__(self,http,url,clock,*,verified_window=False,max_age_seconds=420):
        self.http,self.url,self.clock=http,url,clock
        self.verified_window,self.max_age=verified_window,max_age_seconds
    async def complete(self,interval):
        if not self.verified_window:return False,'mirror_coverage_not_attested'
        try:
            response=await self.http.get(self.url,timeout=2);response.raise_for_status();value=response.json()
            last=value.get('lastSuccess') or {};now=self.clock();timestamp=last.get('timestamp')
            if (value.get('state') not in ('healthy','syncing') or not value.get('timerActive')
                    or last.get('outcome')!='success' or not isinstance(timestamp,(int,float))
                    or not 0<=now.timestamp()-timestamp<=self.max_age):return False,'mirror_stale_or_failed'
            # Window attestation is an operator check against installed argv/source, not caller input.
            if value.get('windowDaysPast')!=30 or value.get('windowDaysFuture')!=365:return False,'mirror_window_changed'
            stamp=datetime.fromtimestamp(timestamp,UTC)
            if not (stamp-timedelta(days=30)<=interval.start and interval.end<=stamp+timedelta(days=365)):
                return False,'mirror_out_of_window'
            counts=last.get('counts') or {}
            if 'outlookEvents' not in counts or 'googleEventsScanned' not in counts:return False,'mirror_completeness_unknown'
            return True,'verified_with_mirror_lag'
        except (httpx.HTTPError,KeyError,ValueError,TypeError):return False,'mirror_unavailable'


class GoogleCalendarAdapter:
    capabilities=Capabilities(exact_reads=True,event_readback=True,create=True,deterministic_identity=True,
        suppress_invitations=True,private_events=True,change_cursors=True,recurrence_expansion=True,owner_agenda=True,guarded_write=True,
        availability_search=True,human_readable_details=True)
    def __init__(self,http,token,identity,mirror,clock,*,zone='America/New_York',office_ids=(),mirror_source=None,max_pages=10):
        if identity.provider!='google':raise ValueError('Google adapter requires its own provider namespace')
        self.http,self.token,self.identity,self.mirror,self.clock=http,token,identity,mirror,clock
        self.zone,self.office_ids,self.mirror_source,self.max_pages=zone,frozenset(office_ids),mirror_source,max_pages
        self.base='https://www.googleapis.com/calendar/v3/calendars/'+quote(identity.calendar,safe='')+'/events'

    async def request(self,method,url,authorize=None,**kwargs):
        try:
            token=await self.token()
            if authorize:authorize()  # Revalidate after credential/network preparation, immediately before POST dispatch.
            result=await self.http.request(method,url,headers={'Authorization':'Bearer '+token},timeout=5,**kwargs)
        except (httpx.HTTPError,OSError):raise ProviderFault('uncertain' if method!='GET' else 'unavailable') from None
        if result.status_code in (401,403):raise ProviderFault('unauthorized')
        if result.status_code==429:
            delay=result.headers.get('retry-after','')
            raise ProviderFault('rate_limited',min(60,int(delay)) if delay.isdecimal() else None)
        if result.status_code==410:raise ProviderFault('cursor_expired')
        if result.status_code==404:return None
        if result.status_code==409:raise ProviderFault('conflict')
        if result.status_code>=400:raise ProviderFault('uncertain' if method!='GET' else 'unavailable')
        try:return result.json()
        except ValueError:raise ProviderFault('uncertain' if method!='GET' else 'invalid') from None

    def instant(self,value):
        if 'dateTime' in value:
            d=datetime.fromisoformat(value['dateTime'].replace('Z','+00:00'))
            if d.tzinfo is None:raise ProviderFault('invalid')
            return d.astimezone(UTC)
        return datetime.combine(date.fromisoformat(value['date']),time.min,ZoneInfo(self.zone)).astimezone(UTC)

    def normalize(self,event):
        try:
            original=event.get('originalStartTime') or {};occurrence=original.get('dateTime') or original.get('date','')
            identity=EventIdentity(self.identity,event['id'],occurrence)
            if event.get('status')=='cancelled':return CalendarEvent(identity,event.get('etag',''),None,state='cancelled',series_id=event.get('recurringEventId',''))
            interval=Interval(self.instant(event['start']),self.instant(event['end']),event['start'].get('timeZone') or self.zone)
            private=(event.get('extendedProperties') or {}).get('private') or {};aliases=()
            if (self.mirror_source and private.get('bridgeName')=='shu-outlook' and private.get('outlookEventId')
                    and private.get('outlookICalUID') and private.get('outlookLastModified')):
                aliases=(EventIdentity(self.mirror_source,private['outlookEventId'],occurrence),)
            owned=private.get('phoneAgentOwner')=='phone-assistant'
            ordinary=owned and private.get('phoneDisclosure')=='ordinary'
            details_verified=False
            if ordinary and 'summary' in event and 'description' in event:
                try:
                    value=Appointment(private.get('phoneFirst',''),private.get('phoneLast',''),private.get('phonePurpose',''),
                        interval,event.get('location',''))
                    details_verified=(event['summary']==calendar_title(value) and event['description']==calendar_description(value)
                        and event.get('visibility')=='private' and not event.get('attendees'))
                except ValueError:pass  # Malformed metadata cannot verify a booking, but its busy interval still counts.
            availability=event['id'] in self.office_ids or event.get('recurringEventId') in self.office_ids
            free=event.get('transparency')=='transparent' or event.get('eventType')=='workingLocation'
            all_day=(date.fromisoformat(event['start']['date']),date.fromisoformat(event['end']['date'])) if 'date' in event['start'] else None
            return CalendarEvent(identity,event.get('etag',''),interval,state='free' if free else 'busy',
                kind='availability' if availability else ('series' if event.get('recurrence') else 'commitment'),
                aliases=aliases,alias_verified=bool(aliases),all_day=all_day,series_id=event.get('recurringEventId',''),
                recurrence=tuple(event.get('recurrence',())),location=event.get('location',''),
                participant_first=private.get('phoneFirst','') if ordinary else '',
                participant_last=private.get('phoneLast','') if ordinary else '',
                disclosure='ordinary' if ordinary else 'unknown',ownership='phone-assistant' if owned else private.get('bridgeName',''),
                purpose=private.get('phonePurpose','') if ordinary else '',booking_details_verified=details_verified)
        except (KeyError,ValueError,TypeError):raise ProviderFault('invalid') from None

    async def read_interval(self,interval,exclude=None):
        return await self._coverage(interval,exclude=exclude)

    async def read_agenda(self,interval):
        return await self._coverage(interval,agenda=True)

    async def read_availability(self,interval):
        # Search needs all conflicts, not the exact-check early exit. No private titles.
        return await self._coverage(interval,search=True)

    async def _coverage(self,interval,exclude=None,agenda=False,search=False):
        params={'timeMin':interval.start.isoformat(),'timeMax':interval.end.isoformat(),
            'singleEvents':'true','showDeleted':'true','maxResults':2500,
            'fields':f'nextPageToken,items({FIELDS+(",summary" if agenda else "")})'}
        events=[];checked=self.clock()
        for _ in range(self.max_pages):
            page=await self.request('GET',self.base,params=params)
            if page is None:raise ProviderFault('unauthorized')
            for raw in page.get('items',[]):
                event=self.normalize(raw)
                if agenda:event=replace(event,owner_label=str(raw.get('summary',''))[:160])
                events.append(event)
                if (not agenda and not search and event.identity!=exclude and not (event.alias_verified and exclude in event.aliases)
                        and event.kind!='availability' and event.state=='busy' and event.interval.overlaps(interval)):
                    return Coverage(interval,checked,checked+timedelta(seconds=30),False,tuple(events),'known_conflict')
            if not page.get('nextPageToken'):break
            params['pageToken']=page['nextPageToken']
        else:return Coverage(interval,checked,checked,False,tuple(events),'incomplete_pagination')
        complete,reason=await self.mirror.complete(interval)
        return Coverage(interval,checked,checked+timedelta(seconds=30),complete,tuple(events),reason)

    def intended_identity(self,operation_id):
        # Hex is a subset of Google's base32hex identifier alphabet. Namespace includes account/calendar.
        raw=json.dumps([self.identity.tenant,self.identity.calendar,operation_id]).encode()
        return EventIdentity(self.identity,'pa'+hashlib.sha256(raw).hexdigest())

    async def create(self,identity,value,*,authorize):
        if identity.calendar!=self.identity:raise ProviderFault('unauthorized')
        require_arrangement(value.location)
        body={'id':identity.event,'summary':calendar_title(value),'description':calendar_description(value),'visibility':'private',
            'start':{'dateTime':value.interval.start.isoformat(),'timeZone':value.interval.zone},
            'end':{'dateTime':value.interval.end.isoformat(),'timeZone':value.interval.zone},'location':value.location,
            # No attendees/conference creation: sendUpdates alone is not a no-invitation guarantee.
            'extendedProperties':{'private':{'phoneAgentOwner':'phone-assistant','phoneOperation':identity.event,
                'phoneDisclosure':'ordinary','phoneFirst':value.first_name,'phoneLast':value.last_name,'phonePurpose':value.purpose}},
            'reminders':{'useDefault':False}}
        await self.request('POST',self.base,params={'sendUpdates':'none'},json=body,authorize=authorize)

    async def read_event(self,identity):
        if identity.calendar!=self.identity:raise ProviderFault('unauthorized')
        value=await self.request('GET',self.base+'/'+quote(identity.event,safe=''),
            params={'fields':FIELDS+',summary,description,visibility,attendees'})
        return self.normalize(value) if value else None

    async def changes(self,cursor=None,*,interval=None):
        params={'singleEvents':'false','showDeleted':'true','maxResults':2500,
            'fields':f'nextPageToken,nextSyncToken,items({FIELDS})'}
        if cursor:params['syncToken']=cursor
        else:
            if (interval is None or interval.end-interval.start>timedelta(days=61)
                    or interval.start<self.clock()-timedelta(days=1) or interval.end>self.clock()+timedelta(days=60)):
                raise ProviderFault('unsupported')
            params.update(timeMin=interval.start.isoformat(),timeMax=interval.end.isoformat())
        result=[]
        for _ in range(self.max_pages):
            page=await self.request('GET',self.base,params=params)
            if page is None:raise ProviderFault('unauthorized')
            result.extend(self.normalize(e) for e in page.get('items',[]))
            if not page.get('nextPageToken'):
                if not page.get('nextSyncToken'):raise ProviderFault('invalid')
                return ChangePage(tuple(result),page['nextSyncToken'],True)
            params['pageToken']=page['nextPageToken']
        raise ProviderFault('unavailable')
