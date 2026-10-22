"""Bounded serial private-index ingestion; no calendar write, Graph read or native subscription change."""
import asyncio
from datetime import timedelta
from app.appointments.contracts import Interval,ProviderFault


class IndexSync:
    def __init__(self,store,provider,clock):
        self.store,self.provider,self.clock=store,provider,clock
        self.lock=asyncio.Lock()

    async def once(self,*,dry_run=True,resync=False):
        async with self.lock:
            p=self.provider;c=p.identity
            if not p.capabilities.change_cursors:return {'status':'unsupported','dry_run':dry_run}
            with self.store.connect() as db:row=db.execute('SELECT cursor,state FROM appointment_sync WHERE (provider,tenant,calendar_id)=(?,?,?)',
                (c.provider,c.tenant,c.calendar)).fetchone()
            if row and row['state']=='needs_resync' and not resync:return {'status':'needs_resync','dry_run':dry_run}
            cursor=None if resync else (row['cursor'] if row else None);now=self.clock()
            try:
                async with asyncio.timeout(15):
                    interval=Interval(now,now+timedelta(days=60),'America/New_York')
                    page=await p.changes(cursor,**({'interval':interval} if not cursor else {}))
                result=self.store.ingest(page,c,now,dry_run=dry_run,
                    expected_cursor=row['cursor'] if row else None,resync=resync)
                return {'status':'preview' if dry_run else 'current',**result}
            except ProviderFault as exc:
                if exc.code=='cursor_expired' and not dry_run:self.store.cursor_expired(c,now)
                return {'status':'needs_resync' if exc.code=='cursor_expired' else 'unavailable','reason':exc.code,'dry_run':dry_run}
            except (TimeoutError,ValueError):return {'status':'unavailable','reason':'budget_or_cursor_changed','dry_run':dry_run}
