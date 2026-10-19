"""Manual real ChatGPT-subscription probe using only synthetic facts. Never dials."""
import asyncio
import json
import time
import argparse
from app.config import Settings

from app.calling.lookup import select_with_codex


async def check(fast=False):
    start=time.monotonic()
    result=await select_with_codex('What is the demonstration keyword?',
        [{'fact_id':'demo','topic':'Demonstration','text':'The demonstration keyword is lavender.'}],20,fast=fast)
    if result.selected_ids!=['demo'] or result.needs_supervisor or result.recommend_owner:
        raise RuntimeError('Evidence selection failed')
    print(json.dumps({'model':'gpt-6-luna','auth':'chatgpt_only','selected_expected_fact':True,
                      'speed_requested':'fast' if fast else 'standard',
                      'seconds':round(time.monotonic()-start,2),'no_phone_dialed':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fast',action='store_true',help='Request Luna Fast using the existing ChatGPT subscription')
    parser.add_argument('--compare',action='store_true',help='Run one standard and one Fast synthetic selection; not a latency guarantee')
    args=parser.parse_args()
    async def run():
        for fast in ([False,True] if args.compare else [args.fast or Settings().subscription_lookup_fast]):
            await check(fast)
    try: asyncio.run(run())
    except Exception as exc:
        raise SystemExit(f'Subscription lookup unavailable ({type(exc).__name__}). No API fallback or phone call.') from None
