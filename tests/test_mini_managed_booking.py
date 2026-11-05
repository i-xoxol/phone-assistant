"""Fictional new-booking replay: no provider or telephone traffic."""
import asyncio,json
import base64
from types import SimpleNamespace
import pytest
from app.appointments.lookup_intent import LookupIntent
from app.appointments.booking_agent import BookingAction
from app.calling.audio_timing import InputAudioTimeline
from app.calling.elevenlabs_session import MANAGED_CALENDAR_POLICY,realtime_config
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.test_voice_backends import connection,translated


def test_clock_can_skip_an_untranscribed_noise_span():
 c=InputAudioTimeline()
 c.observe(7000,800,True);c.observe(7100,4000,False)
 c.observe(7600,27200,False);c.observe(11000,12000,True);c.observe(12500,4000,False)
 assert c.native_span(4000/3,5700/3)==(11000,12500)
 assert not c.utterances[0]['transcribed']


def test_unmapped_native_caption_keeps_conversation_order_without_claiming_coverage():
 async def run():
  c=connection();c.observe_input_audio(20000,b'\xff'*8000)
  await translated(c,{'type':'input_audio_buffer.speech_started','item_id':'i','audio_start_ms':999999})
  c.observe_input_audio(21000,b'\xff'*8000)
  await translated(c,{'type':'input_audio_buffer.speech_stopped','item_id':'i','audio_end_ms':1000500})
  events=await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'i','transcript':'First available, please.'})
  assert events[0]['start_ms']==21000 and events[0]['end_ms']==22000
  assert 'input_audio_end_ms' not in events[0]
 asyncio.run(run())


def test_existing_zero_time_native_history_is_read_in_receipt_order_without_db_rewrite(service):
 from app.storage.models import CallRequest
 from tests.test_voice_backends import configure
 client,m,_=service;configure(m)
 cid=m.db.create(CallRequest(phone_number='+12025550101',objective='Fictional ordering test'))
 m.voice_backends.pin(m.db.get(cid),'elevenlabs')
 speech(m,cid,'callee','Hello, I need an appointment.',0)
 # Force the exact old failure shape, where both endpoints were zero.
 speech(m,cid,'assistant','What is your name?',17000)
 speech(m,cid,'callee','Casey Example.',0)
 with m.db.connect() as db:db.execute("UPDATE transcript_events SET end_ms=0 WHERE call_id=? AND speaker='callee'",(cid,))
 transcript=m.db.get(cid)['transcript']
 assert [r['speaker'] for r in transcript]==['callee','assistant','callee']
 assert transcript[-1]['timing_estimated'] and transcript[-1]['timestamp']>17
 with m.db.connect() as db:assert all(r[0]==0 for r in db.execute("SELECT start_ms FROM transcript_events WHERE call_id=? AND speaker='callee'",(cid,)))


def test_calendar_request_pauses_automatic_mini_answers_and_cancels_speculation():
 async def run():
  c=connection();c.calendar_managed=True;c.active_response='r'
  events=await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'i','transcript':'I want to book an appointment.'})
  assert c.calendar_controlled and any(e['type']=='session.output_audio.clear' for e in events)
  assert any(e['type']=='response.cancel' for e in c.socket.sent)
  assert not any(e['type']=='session.update' for e in c.socket.sent)
 asyncio.run(run())


def test_manual_calendar_commit_binds_exact_caption_coverage_and_ignores_silence_asr():
 async def run():
  c=connection();c.calendar_controlled=True;c.manual_mode=True
  for tick in range(50+33):
   raw=(b'\x80' if tick<50 else b'\xff')*160
   c.observe_input_audio(20000+tick*20,raw)
   await c.session.input_audio.append(audio=base64.b64encode(raw).decode())
  assert len(c.manual_pending)==1
  await translated(c,{'type':'input_audio_buffer.committed','item_id':'manual'})
  partial=await translated(c,{'type':'conversation.item.input_audio_transcription.delta','item_id':'manual','delta':'Yes, that works.'})
  assert 'input_audio_end_ms' not in partial[0]
  final=await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'manual','transcript':'Yes, that works.'})
  assert final[0]['start_ms']==20000 and final[0]['input_audio_end_ms']==21000
  assert await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'quiet','transcript':'...'})==[]
 asyncio.run(run())


def test_spoken_instruction_waits_for_matching_ack_and_is_bound_to_next_response():
 async def run():
  c=connection();await c.append_instruction('Ask only for the appointment purpose.','call_update_test',respond=True)
  assert not any(e['type']=='response.create' for e in c.socket.sent)
  await translated(c,{'type':'session.updated','session':{'instructions':'An unrelated update'}})
  assert not any(e['type']=='response.create' for e in c.socket.sent)
  update=c.socket.sent[0]
  await translated(c,{'type':'session.updated','session':update['session']})
  response=next(e['response'] for e in c.socket.sent if e['type']=='response.create')
  assert response['instructions'].endswith('Ask only for the appointment purpose.')
 asyncio.run(run())


def test_managed_voice_waits_for_current_server_conversation_item_before_speaking():
 async def run():
  c=connection();c.calendar_controlled=True
  await c.append_instruction('Verified: the fictional booking is confirmed.','call_update_result',respond=True)
  update=c.socket.sent[0]
  await translated(c,{'type':'session.updated','session':update['session']})
  assert not any(e['type']=='response.create' for e in c.socket.sent)
  item=next(e['item'] for e in c.socket.sent if e['type']=='conversation.item.create')
  assert 'not telephone caller speech' in item['content'][0]['text']
  await translated(c,{'type':'conversation.item.created','item':item})
  await translated(c,{'type':'conversation.item.done','item':item})
  assert len([e for e in c.socket.sent if e['type']=='response.create'])==1
 asyncio.run(run())


def test_manual_noncalendar_caller_still_gets_a_normal_response():
 async def run():
  c=connection();c.manual_mode=True;c.calendar_managed=True
  await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'i','transcript':'Hello, are you there?'})
  assert not c.calendar_controlled and any(e['type']=='response.create' for e in c.socket.sent)
 asyncio.run(run())


def test_split_caller_sentence_does_not_start_a_stale_generic_response():
 async def run():
  c=connection();c.manual_mode=True;c.calendar_managed=True
  c.manual_items['name']=(7000,8000)
  c.observe_input_audio(7000,b'\x80'*8000);c.observe_input_audio(8000,b'\xff'*6000)
  c.observe_input_audio(8750,b'\x80'*8000)
  await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'name','transcript':'My name is Casey Example.'})
  assert not c.response_requested and not any(e['type']=='response.create' for e in c.socket.sent)
 asyncio.run(run())


def test_mini_calendar_tools_are_server_owned_while_max_retains_direct_tools(service,tmp_path):
 client,m,p,g,cid,args=first_call(service,tmp_path)
 record=m.voice_record(cid)
 mini=realtime_config(m.settings,record,{'model':'eleven_realtime_v1_mini','voice':'test'})
 maxi=realtime_config(m.settings,record,{'model':'eleven_realtime_v1_max','voice':'test'})
 assert MANAGED_CALENDAR_POLICY in mini['instructions']
 assert mini['audio']['input']['turn_detection'] is None
 assert 'find_first_available_slot' not in {t['name'] for t in mini['tools']}
 assert 'find_first_available_slot' in {t['name'] for t in maxi['tools']}
 assert 'save_message' in {t['name'] for t in mini['tools']}


@pytest.mark.parametrize('case',['book','changed_during_planning','refusal','write_disabled'])
def test_managed_first_available_reuses_real_proposal_consent_and_single_write(service,tmp_path,case):
 client,m,p,g,cid,args=first_call(service,tmp_path)
 c=UpdateConnection();c.config={'model':'eleven_realtime_v1_mini'};c.calendar_controlled=True
 b=MediaBridge(None,c,m,cid,'MZ_MANAGED',luna_booking=True);m.attach_bridge(cid,b)
 async def decide(context):
  assert context['now'] and context['time_zone']=='America/New_York' and context['server_proposal'] is None
  if case=='changed_during_planning':speech(m,cid,'callee','Actually, not today. Do not book.',15000)
  return LookupIntent(action='first_available',**args)
 g.lookup_intent.decide=decide
 if case=='write_disabled':
  with m.db.connect() as db:db.execute('UPDATE calls SET inbound_booking_allowed=0 WHERE call_id=?',(cid,))
 async def run():
  await g.lookup_intent.run(b)
  assert p.writes==0
  bound=g.voice_workflow.bound(cid)
  if case in ('changed_during_planning','write_disabled'):assert not bound;return
  assert bound and len(c.updates)==1
  offer=c.updates[0][1]['content'].split('\n')[-1]
  speech(m,cid,'assistant',offer,20000)
  speech(m,cid,'callee','No, do not book it.' if case=='refusal' else 'Well, yes, that works for me.',22000)
  async def consent(turns,*_):
   return BookingAction(action='decline' if case=='refusal' else 'book',offer_turn=len(turns)-2,answer_turn=len(turns)-1)
  g.booking_agent.decide=consent
  result=await g.voice_workflow.book(cid)
  if case=='refusal':assert p.writes==0 and result['state']!='confirmed'
  else:
   assert result['state']=='confirmed' and p.writes==1
   again=await g.voice_workflow.book(cid)
   assert again['state']=='confirmed' and p.writes==1 and len(c.updates)==2
  await g.voice_workflow.close()
 client.portal.call(run)
