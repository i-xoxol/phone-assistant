"""The caller agrees to heard audio, not a character-perfect assistant caption."""
import asyncio
import base64
import json
from dataclasses import replace

import pytest
from app.calling.live_session import MediaBridge
from app.appointments.registry import appointment
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.test_appointments_server_workflow import Captions,caption,replay,drain


class Playback:
    def __init__(self):self.queue=asyncio.Queue();self.messages=[];self.ms=20000;self.ack=True
    async def receive_json(self):return await self.queue.get()
    async def send_json(self,m):
        self.messages.append(m)
        if m['event']=='media':
            self.ms+=len(base64.b64decode(m['media']['payload']))//8
            await self.queue.put(dict(event='media',streamSid='MZ_PLAY',media=dict(track='inbound',timestamp=str(self.ms),payload=base64.b64encode(b'\xff'*160).decode())))
        if m['event']=='mark' and self.ack:await self.queue.put(m)


async def prepared(service,tmp_path,*,ack=True):
    client,m,p,g,cid,args=first_call(service,tmp_path)
    g.voice_workflow.settle_seconds=.01
    calls=[]
    async def synth(settings,text,voice,validate):
        assert validate(text);calls.append((text,voice))
        return b'\x10'*800
    g.voice_confirmation.synthesizer=synth
    ws=Playback();ws.ack=ack;con=Captions();b=MediaBridge(ws,con,m,cid,'MZ_PLAY',server_booking=True)
    async def input_audio(**_):pass
    con.session.input_audio.append=input_audio
    m.attach_bridge(cid,b);b.transcript_offset_ms=0
    receiver=asyncio.create_task(b.receive_twilio())
    result=await b.tools.operation(dict(name='find_first_available_slot',arguments=json.dumps(args)))
    await asyncio.gather(*list(b.booking_audio_tasks))
    return m,p,g,cid,b,con,ws,receiver,result,calls


async def finish(b,receiver):
    receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
    for t in list(b.booking_audio_tasks):t.cancel()
    await asyncio.gather(*list(b.booking_audio_tasks),return_exceptions=True)


@pytest.mark.parametrize('answer',['Yes.','That works for me.','Sure, go ahead.',"Yeah, that's would work for me, thank you."])
def test_short_once_then_auto_booking_is_independent_of_paraphrases_and_waiting_prefix(service,tmp_path,answer):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path)
        try:
            assert result['decision']=='available' and p.writes==0
            assert set(t for t,v in calls)=={'Monday, October 26th, 10:00 AM.','Does that work for you?','Great, see you Monday at 10:00 AM. Confirmed.'}
            assert not any('Confirmed' in t['text'] for t in m.db.get(cid)['transcript'])
            assert {v for t,v in calls}=={m.voice_record(cid)['voice_name']}
            # The main Live model may paraphrase, preface, repeat, or say an
            # unheard farewell. None becomes a second spoken proposal/proof.
            con.events=[caption('assistant',"Still checking. Casey Example, to discuss your course guidance. Thank you. Goodbye.",20500,20600),
                caption('callee',answer,21000,21500)]
            await replay(b,con)
            assert p.writes==1 and not b.end_requested.is_set()
            await asyncio.gather(*list(b.booking_audio_tasks))
            status=await b.tools.operation(dict(name='get_booking_status',arguments='{}'))
            assert status['state']=='confirmed' and status['delivery']['spoken_observed']
            assert not b.owned_booking
            for _ in range(2):assert (await b.tools.operation(dict(name='book_appointment',arguments='{}')))['state']=='confirmed'
            assert p.writes==1 and len(calls)==3
        finally:await finish(b,receiver)
    client.portal.call(exercise)


@pytest.mark.parametrize('answer',['No.','Yes, but eleven instead.','Maybe.','For my friend.','Yes, if it is online.'])
def test_correction_refusal_or_uncertainty_never_books(service,tmp_path,answer):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path)
        try:
            con.events=[caption('callee',answer,21000,21500)];await replay(b,con)
            assert not p.writes and not b.owned_booking
            if answer!='Maybe.':
                con.events=[caption('callee','Yes.',24000,24500)];await replay(b,con)
                assert not p.writes  # A later unrelated yes cannot revive a withdrawn offer.
        finally:await finish(b,receiver)
    client.portal.call(exercise)


def test_late_caller_captions_and_correction_during_io_preserve_dispatch_guard(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path)
        try:
            m.caller_activity[cid]={'epoch':1,'last_speech_ms':22000}
            con.events=[caption('callee','Yes',21000,21500)];await replay(b,con)
            assert not p.writes and b.owned_booking
            con.events=[caption('callee',', that works.',21600,22000)]
            p.read_hook=lambda:speech(m,cid,'callee','Actually, eleven instead.',23000)
            await replay(b,con)
            assert not p.writes
        finally:await finish(b,receiver)
    client.portal.call(exercise)


@pytest.mark.parametrize('guard',['session','revision','no_playback','expired','busy','incomplete','hangup'])
def test_audio_proof_does_not_bypass_binding_authority_or_availability(service,tmp_path,guard):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path)
        try:
            draft=g.service.bound(cid,result['draft_id']);row=g.voice_confirmation.proposal(draft)
            if guard=='session':b.session_id='different-session'
            if guard=='revision':
                with g.store.connect() as db:db.execute('UPDATE appointment_drafts SET revision=revision+1 WHERE draft_id=?',(draft['draft_id'],))
            if guard=='no_playback':g.voice_confirmation.update(row['update_id'],state='playing',payload_end_ms=None)
            if guard=='expired':
                with g.store.connect() as db:db.execute("UPDATE appointment_requests SET deadline='2000-01-01T00:00:00+00:00'")
            if guard=='busy':p.read_hook=lambda:p.events.update(busy=replace(p.office,kind='ordinary',state='busy'))
            if guard=='incomplete':p.complete=False
            if guard=='hangup':m.transition(cid,'completed')
            con.events=[caption('callee','Yes.',21000,21500)];await replay(b,con)
            assert p.writes==0
        finally:await finish(b,receiver)
    client.portal.call(exercise)


def test_failed_or_unacknowledged_audio_never_becomes_consent(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path,ack=False)
        try:
            draft=g.service.bound(cid,result['draft_id']);row=g.voice_confirmation.proposal(draft)
            assert row['state']=='failed' and row['payload_end_ms'] is None
            con.events=[caption('callee','Yes.',21000,21500)];await replay(b,con)
            assert not p.writes
        finally:await finish(b,receiver)
    client.portal.call(exercise)


def test_unheard_model_end_tool_cannot_cut_off_server_result(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,ws,receiver,result,calls=await prepared(service,tmp_path)
        try:
            result=await b.tools.operation({'name':'end_call','arguments':'{}','call_id':'unheard-end'})
            assert result['status']=='booking_pending' and not b.end_requested.is_set()
        finally:await finish(b,receiver)
    client.portal.call(exercise)
