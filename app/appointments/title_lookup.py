"""Read-only caller-name matching against calendar titles, independent of booking."""
import asyncio
from datetime import timedelta
from app.appointments.contracts import Interval,ProviderFault
from app.appointments.name_match import caller_quotes_support,caller_text


class TitleLookup:
    def __init__(self,provider,resolver,clock):
        self.provider,self.resolver,self.clock=provider,resolver,clock

    async def find(self,turns):
        p=self.provider
        if not p.capabilities.title_lookup or not caller_text(turns).strip():return None
        try:
            async with asyncio.timeout(7):
                now=self.clock();window=Interval(now,now+timedelta(days=60),'America/New_York')
                coverage=await p.read_lookup_titles(window)
                if not (coverage.complete and coverage.interval.contains(window)
                        and coverage.checked_at<=self.clock()<coverage.valid_until):
                    return self.unavailable('title_search_incomplete')
                events=sorted((e for e in coverage.events if e.interval and e.interval.end>now
                    and e.state!='cancelled' and e.kind=='commitment' and e.owner_label and e.revision
                    and e.identity.calendar==p.identity),key=lambda e:e.interval.start)
                if len(events)>500 or sum(len(e.owner_label) for e in events)>64000:
                    return self.unavailable('title_search_limit')
                choice=await self.resolver(turns,[e.owner_label for e in events])
                if choice is None:return None
                if (choice.candidate is None or not 0<=choice.candidate<len(events)
                        or not caller_quotes_support(turns,choice.first_quote,choice.last_quote)):
                    return self.unavailable('title_name_unverified')
                selected=events[choice.candidate]
                event=await p.read_lookup_event(selected.identity)
                if (not event or event.identity!=selected.identity or event.state=='cancelled'
                        or event.kind!='commitment' or event.revision!=selected.revision
                        or event.interval!=selected.interval or event.location!=selected.location
                        or event.owner_label!=selected.owner_label or event.interval.end<=self.clock()
                        or not coverage.checked_at<=self.clock()<coverage.valid_until):
                    return self.unavailable('title_event_changed')
                # No title, event ID, participant metadata, body, links or other meetings leave this boundary.
                return {'status':'match','start':event.interval.start.isoformat(),
                    'end':event.interval.end.isoformat(),'zone':event.interval.zone,'location':event.location,
                    'duration_seconds':int((event.interval.end-event.interval.start).total_seconds())}
        except (ProviderFault,TimeoutError):return self.unavailable('title_provider_unavailable')

    @staticmethod
    def unavailable(reason):
        return {'status':'stale','reason':reason,
            'instruction':'The read-only calendar verification could not finish. Explain that once and offer owner review. '
                'Do not claim there is no meeting or repeat name/spelling questions.'}
