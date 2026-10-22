"""Offline synthetic timings only. No settings, credentials, phones or real calendars are loaded."""
import argparse,json,statistics,tempfile,time
from pathlib import Path
from tests.appointment_fixtures import setup


async def benchmark(iterations=30):
    samples={k:[] for k in ('exact_check','commit_with_recheck_readback','registry_persistence','synthetic_injection_ack')}
    with tempfile.TemporaryDirectory(prefix='phone-appointments-bench-') as folder:
        for n in range(iterations):
            s,db,p,d,state,now=setup(Path(folder)/str(n))
            started=time.perf_counter();check=await s.check('call-1',d['draft_id'])
            samples['exact_check'].append((time.perf_counter()-started)*1000)
            started=time.perf_counter();op=await s.book('call-1',d['draft_id'],check['check_id'],'yes','one')
            samples['commit_with_recheck_readback'].append((time.perf_counter()-started)*1000)
            assert op['state']=='confirmed' and p.writes==1
            with db.connect() as conn:
                samples['registry_persistence'].append(conn.execute("SELECT duration_ms FROM appointment_telemetry WHERE stage='registry_persistence'").fetchone()[0])
                outbox=conn.execute("SELECT outbox_id FROM booking_outbox WHERE operation_id=? AND kind='result' ORDER BY operation_revision DESC LIMIT 1",(op['operation_id'],)).fetchone()[0]
            started=time.perf_counter();db.delivery(outbox,'injection_ack')
            samples['synthetic_injection_ack'].append((time.perf_counter()-started)*1000)
    return {'kind':'offline_fake_provider_no_network_no_model_or_handset_latency','samples':iterations,
        'milliseconds':{key:{'p50':round(statistics.median(values),3),'p95':round(sorted(values)[int(.95*(len(values)-1))],3)} for key,values in samples.items()}}


if __name__=='__main__':
    import asyncio
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--iterations',type=int,default=30)
    args=parser.parse_args()
    if not 5<=args.iterations<=100:parser.error('Use 5–100 synthetic samples')
    print(json.dumps(asyncio.run(benchmark(args.iterations)),indent=2))
