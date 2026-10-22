from datetime import datetime,timedelta,timezone
from dataclasses import replace
from app.appointments.contracts import *
from app.appointments.registry import Registry
from app.appointments.service import BookingService,CallAuthority,ConsentEvidence

NOW=datetime(2026,10,26,13,0,tzinfo=timezone.utc)
SLOT=Interval(NOW+timedelta(hours=1),NOW+timedelta(hours=1,minutes=15),'America/New_York')

class FakeProvider:
    identity=CalendarIdentity('fake','tenant-two','opaque-calendar:🟢')
    capabilities=Capabilities(exact_reads=True,event_readback=True,create=True,deterministic_identity=True,
        suppress_invitations=True,private_events=True,recurrence_expansion=True,change_cursors=True,guarded_write=True,availability_search=True)
    def __init__(self,clock):
        self.clock=clock;self.events={};self.writes=0;self.complete=True;self.fail='';self.read_hook=None
        self.office=CalendarEvent(EventIdentity(self.identity,'office:α'),'v1',
            Interval(SLOT.start,SLOT.start+timedelta(hours=2),SLOT.zone),state='free',kind='availability')
    def intended_identity(self,key):return EventIdentity(self.identity,'opaque/request/'+key)
    async def read_interval(self,interval,exclude=None):
        if self.read_hook:self.read_hook()
        if self.fail=='read':raise ProviderFault('unavailable')
        return Coverage(interval,self.clock(),self.clock()+timedelta(minutes=1),self.complete,(self.office,*self.events.values()))
    async def read_event(self,identity):return self.events.get(identity.event)
    async def read_availability(self,interval):return await self.read_interval(interval)
    async def create(self,identity,value,*,authorize):
        authorize()
        self.writes+=1
        if self.fail=='before':raise ProviderFault('uncertain')
        self.events[identity.event]=CalendarEvent(identity,'different-revision:🔵',value.interval,
            location=value.location,participant_first=value.first_name,participant_last=value.last_name,
            disclosure='ordinary',ownership='phone-assistant',purpose=value.purpose)
        if self.fail=='after':raise ProviderFault('uncertain')
    async def changes(self,cursor=None,*,interval=None):return ChangePage(tuple(self.events.values()),'opaque-cursor',True)

def setup(tmp_path):
    now=[NOW];clock=lambda:now[0];state={'active':True,'revision':'turn-1','allowed':True}
    store=Registry(tmp_path/'appointments.sqlite3');provider=FakeProvider(clock)
    authority=lambda c,r:CallAuthority(state['active'],NOW+timedelta(seconds=90),state['revision'],state['allowed'])
    consent=lambda draft,c:ConsentEvidence(draft['payload_hash'],'turn-1',c=='yes',c=='yes')
    service=BookingService(store,provider,authority,consent,clock=clock)
    value=Appointment('Casey','Example','course guidance',SLOT,'Office A')
    draft=service.prepare('call-1','request-1',value)
    return service,store,provider,draft,state,now
