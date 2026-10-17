"""Refresh phone-safe free windows using an existing Google grant. No calendar writes or event titles."""
import argparse
import json
import os
from datetime import date,datetime,time,timedelta,timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings

UTC=timezone.utc
TOKEN_URL='https://oauth2.googleapis.com/token'
CALENDAR_URL='https://www.googleapis.com/calendar/v3/calendars/'


def access_token(path,client,current):
    path=Path(path)
    if path.is_symlink() or (os.name!='nt' and path.stat().st_mode & 0o077):
        raise ValueError('Use an owner-only, nonsymlink Google token file')
    saved=json.loads(path.read_text(encoding='utf-8'))
    expiry=datetime.fromisoformat(saved.get('expiry','1970-01-01T00:00:00Z').replace('Z','+00:00'))
    if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=UTC)
    if saved.get('token') and expiry>current+timedelta(minutes=1): return saved['token']
    # Refresh in memory only. Never modify the calendar bridge's credential cache.
    result=client.post(TOKEN_URL,data={'client_id':saved['client_id'],'client_secret':saved['client_secret'],
        'refresh_token':saved['refresh_token'],'grant_type':'refresh_token'})
    result.raise_for_status()
    return result.json()['access_token']


def event_time(value,calendar_zone):
    if value.get('dateTime'):
        result=datetime.fromisoformat(value['dateTime'].replace('Z','+00:00'))
        if result.tzinfo is None:
            if not value.get('timeZone'): raise ValueError('Event time has no time zone')
            result=result.replace(tzinfo=ZoneInfo(value['timeZone']))
        return result.astimezone(UTC)
    return datetime.combine(date.fromisoformat(value['date']),time.min,ZoneInfo(calendar_zone)).astimezone(UTC)


def busy_windows(client,token,calendar_ids,start,end):
    busy=[]
    for calendar_id in calendar_ids:
        params={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'singleEvents':'true','showDeleted':'false',
            'maxResults':2500,'fields':'nextPageToken,timeZone,items(status,transparency,eventType,start,end)'}
        for _ in range(10):
            result=client.get(CALENDAR_URL+quote(calendar_id,safe='')+'/events',params=params,
                              headers={'Authorization':'Bearer '+token})
            result.raise_for_status()
            data=result.json()
            zone=data.get('timeZone')
            if not zone: raise ValueError('Calendar response omitted its time zone')
            for event in data.get('items',[]):
                if event.get('status')=='cancelled' or event.get('transparency')=='transparent' or event.get('eventType')=='workingLocation': continue
                left,right=event_time(event['start'],zone),event_time(event['end'],zone)
                if right<=left: raise ValueError('Invalid busy interval')
                busy.append((left,right))
            if not data.get('nextPageToken'): break
            params['pageToken']=data['nextPageToken']
        else: raise ValueError('Calendar pagination limit; availability not refreshed')
    return busy


def free_windows(busy,current,*,zone='America/New_York',days=14,start_hour='09:00',end_hour='17:00',weekdays=(0,1,2,3,4),minimum_minutes=30,lead_minutes=30):
    tz=ZoneInfo(zone)
    first=current.astimezone(tz).date()
    begin,finish=time.fromisoformat(start_hour),time.fromisoformat(end_hour)
    if begin>=finish: raise ValueError('Working hours must start before they end')
    slots=[]
    for offset in range(days):
        day=first+timedelta(days=offset)
        if day.weekday() not in weekdays: continue
        left=max(datetime.combine(day,begin,tz).astimezone(UTC),current+timedelta(minutes=lead_minutes))
        right=datetime.combine(day,finish,tz).astimezone(UTC)
        if left>=right: continue
        cursor=left
        for busy_start,busy_end in sorted(busy):
            if busy_end<=cursor or busy_start>=right: continue
            gap_end=min(busy_start,right)
            if gap_end-cursor>=timedelta(minutes=minimum_minutes):
                slots.append({'start':cursor.isoformat(),'end':gap_end.isoformat()})
            cursor=max(cursor,busy_end)
            if cursor>=right: break
        if right-cursor>=timedelta(minutes=minimum_minutes):
            slots.append({'start':cursor.isoformat(),'end':right.isoformat()})
    return slots[:40]


def healthy_upstream(client,url,current):
    if not url: return
    response=client.get(url)
    response.raise_for_status()
    status=response.json()
    stamp=status.get('lastSuccess',{}).get('timestamp')
    if status.get('state') not in ('healthy','syncing') or not isinstance(stamp,(int,float)) or not 0<=current.timestamp()-stamp<=900:
        raise ValueError('Upstream calendar mirror is stale; availability not refreshed')


def synchronize(args,settings,client):
    current=datetime.now(UTC)
    healthy_upstream(client,args.upstream_health_url,current)
    token=access_token(args.token_file,client,current)
    zone=ZoneInfo(args.time_zone)
    start=datetime.combine(current.astimezone(zone).date(),time.min,zone).astimezone(UTC)
    end=start+timedelta(days=args.days+1)
    busy=busy_windows(client,token,args.calendar or ['primary'],start,end)
    slots=free_windows(busy,current,zone=args.time_zone,days=args.days,start_hour=args.start_hour,
        end_hour=args.end_hour,weekdays=tuple(int(n) for n in args.weekdays.split(',')),
        minimum_minutes=args.minimum_minutes,lead_minutes=args.lead_minutes)
    payload={'slots':slots,'time_zone':args.time_zone,'observed_at':current.isoformat(),
        'expires_at':(current+timedelta(minutes=10)).isoformat(),'source':'Calendar availability (routine hours; later options need supervisor approval)'}
    if not args.dry_run:
        if settings.missing(): raise ValueError('Configure the phone service .env first')
        response=client.put(settings.public_url('/concierge/availability'),json=payload,
            headers={'Authorization':'Bearer '+settings.local_api_token.get_secret_value()})
        response.raise_for_status()
    return {'updated':not args.dry_run,'free_windows':len(slots),'expires_in_minutes':10,
        'calendar_writes':False,'event_titles_read':False,'credential_file_modified':False}


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument('--token-file',type=Path,required=True)
    result.add_argument('--calendar',action='append',help='Repeat to union busy times; default primary')
    result.add_argument('--time-zone',default='America/New_York')
    result.add_argument('--days',type=int,choices=range(1,31),default=14)
    result.add_argument('--start-hour',default='09:00')
    result.add_argument('--end-hour',default='17:00')
    result.add_argument('--weekdays',default='0,1,2,3,4',help='Monday=0 through Sunday=6')
    result.add_argument('--minimum-minutes',type=int,choices=range(15,241),default=30)
    result.add_argument('--lead-minutes',type=int,choices=range(0,1441),default=30)
    result.add_argument('--upstream-health-url',default='',help='Optional trusted calendar-mirror status URL (state and lastSuccess.timestamp)')
    result.add_argument('--dry-run',action='store_true')
    return result


if __name__=='__main__':
    try:
        with httpx.Client(timeout=15,follow_redirects=False) as client:
            print(json.dumps(synchronize(parser().parse_args(),Settings(),client)))
    except Exception as exc:
        raise SystemExit(f'Availability refresh unconfirmed ({type(exc).__name__}); cached availability expires automatically.') from None
