"""Public auth/SSE connectivity check. No dialing, no transcript output."""
import asyncio
import json
from app.config import Settings
from app.storage.database import Database
from app.web_auth import WebAuth
import httpx

async def check():
    settings=Settings()
    auth=WebAuth(Database(settings.database_path))
    async with httpx.AsyncClient(base_url=settings.public_base_url,timeout=10,follow_redirects=False) as client:
        response=await client.post('/live/login',data={'code':auth.create_code()},headers={'Origin':settings.public_base_url})
        print(json.dumps({'login_status':response.status_code,'location':response.headers.get('location'),'cookie_set':bool(response.headers.get('set-cookie'))}))
        response=await client.get('/live/bootstrap')
        response.raise_for_status()
        cursor=response.json()['cursor']
        async with client.stream('GET',f'/live/events?cursor={cursor}') as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if line=='event: heartbeat':
                    print(json.dumps({'public_sse_heartbeat':True}));break
        auth.logout(client.cookies.get('__Host-phone-owner',''))

if __name__=='__main__': asyncio.run(check())
