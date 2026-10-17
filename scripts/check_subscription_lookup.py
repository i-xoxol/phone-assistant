"""Manual real ChatGPT-subscription probe using only synthetic facts. Never dials."""
import asyncio
import json
import time

from app.calling.lookup import select_with_codex


async def check():
    start=time.monotonic()
    result=await select_with_codex('What is the demonstration keyword?',
        [{'fact_id':'demo','topic':'Demonstration','text':'The demonstration keyword is lavender.'}],20)
    if result.selected_ids!=['demo'] or result.needs_supervisor or result.recommend_owner:
        raise RuntimeError('Evidence selection failed')
    print(json.dumps({'model':'gpt-6-luna','auth':'chatgpt_only','selected_expected_fact':True,
                      'seconds':round(time.monotonic()-start,2),'no_phone_dialed':True}))


if __name__=='__main__':
    try: asyncio.run(check())
    except Exception as exc:
        raise SystemExit(f'Subscription lookup unavailable ({type(exc).__name__}). No API fallback or phone call.') from None
