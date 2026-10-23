"""Offline checks of isolation, wire pacing and truthful audio-result scoring."""
import asyncio
import base64
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from tests.test_calls import service
from scripts.simulate_appointment_lookup import (AudioPipe,LocalAudioTransport,NoTelephone,ReadOnlyRegistry,
    Scenario,caller_config,evaluate,heard_details,load_script,load_settings,seed_context)
from app.config import Settings
from app.storage.database import Database
from app.calling.call_manager import CallManager
from app.calling.context import ProfileUpdate


def test_audio_frames_preserve_order_codec_and_silence_and_bound_the_queue():
    pipe=AudioPipe();pipe.append(b'\x10'*200)
    assert pipe.frame()==b'\x10'*160
    assert pipe.frame()==b'\x10'*40+b'\xff'*120
    assert pipe.played==200 and pipe.enqueued==200
    assert pipe.frame()==b'\xff'*160
    with pytest.raises(RuntimeError):pipe.append(b'\0'*80001)


def test_local_playback_marks_wait_until_audio_reaches_the_other_model():
    async def check():
        wire=LocalAudioTransport();received=[]
        async def append(audio):received.append(base64.b64decode(audio))
        caller=SimpleNamespace(session=SimpleNamespace(input_audio=SimpleNamespace(append=append)))
        await wire.send_json({'event':'media','media':{'payload':base64.b64encode(b'\x20'*320).decode()}})
        await wire.send_json({'event':'mark','mark':{'name':'played'}})
        wire.to_assistant.append(b'\x30'*160)
        await wire.step(caller,0)
        first=await wire.receive_json();assert first['media']['timestamp']=='0'
        assert base64.b64decode(first['media']['payload'])==b'\x30'*160
        assert wire.incoming.empty() and received==[b'\x20'*160]
        await wire.step(caller,1)
        assert (await wire.receive_json())['media']['timestamp']=='20'
        assert (await wire.receive_json())=={'event':'mark','streamSid':wire.stream,'mark':{'name':'played'}}
        assert received==[b'\x20'*160]*2
    asyncio.run(check())


def test_read_only_registry_cannot_create_or_update_business_records(tmp_path):
    path=tmp_path/'source.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE records(value TEXT)')
    store=ReadOnlyRegistry(path)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM records').fetchone()[0]==0
        with pytest.raises(sqlite3.OperationalError):db.execute("INSERT INTO records VALUES('forbidden')")
    with pytest.raises(sqlite3.OperationalError):
        with ReadOnlyRegistry(tmp_path/'missing.sqlite3').connect():pass
    assert not (tmp_path/'missing.sqlite3').exists()


def test_no_telephone_adapter_blocks_every_telephone_action():
    async def check():
        for name in ('place','prepare_inbound','hangup','press_digits','fetch'):
            with pytest.raises(RuntimeError):await getattr(NoTelephone(),name)('synthetic')
    asyncio.run(check())


def test_environment_paths_belong_to_deployment_not_the_test_checkout(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_PATH',raising=False);monkeypatch.delenv('APPOINTMENT_DATABASE_PATH',raising=False)
    env=tmp_path/'.env';env.write_text('DATABASE_PATH=data/calls.sqlite3\nAPPOINTMENT_DATABASE_PATH=data/private.sqlite3\n')
    settings=load_settings(env)
    assert settings.database_path==tmp_path/'data/calls.sqlite3'
    assert settings.appointment_database_path==tmp_path/'data/private.sqlite3'


def test_real_caller_script_is_read_without_replaying_any_old_assistant_answers(service,tmp_path):
    client,m,_=service
    cid=m.db.create_inbound('CA_SOURCE_SCRIPT','+12025550101',3,10,'concierge')
    for i,(speaker,text) in enumerate([('assistant','Do not seed this answer.'),('callee','Hi, my name is Casey Example.'),
            ('assistant','An old appointment result must stay out of the caller prompt.'),('callee','Could you check again?')]):
        m.db.transcript(cid,dict(event_id='script-'+str(i),type='session.input_transcript.delta' if speaker=='callee' else 'session.output_transcript.delta',
            start_ms=i*1000,end_ms=i*1000+500,delta=text))
    scenario=Scenario(first_name='Casey',last_name='Example',source_call_id=cid)
    script,phone=load_script(m.settings,scenario)
    assert script==['Hi, my name is Casey Example.','Could you check again?']
    config=caller_config(m.settings,scenario,script,'marin')
    assert 'An old appointment result' not in config['instructions']
    assert config['delegation']=={'type':'client'} and config['store'] is False
    assert config['audio']['format']=={'type':'audio/pcmu','rate':8000}


def test_context_snapshot_never_copies_oauth_subscriptions_or_old_call_rows(service,tmp_path):
    client,m,_=service
    m.context.set_profile(ProfileUpdate(role='Office',context='Approved office information.'))
    m.db.create_inbound('CA_OLD_CALL','+12025550101',3,10,'concierge')
    local=m.settings.model_copy(update={'database_path':tmp_path/'scratch.sqlite3'})
    scratch=CallManager(local,Database(local.database_path),NoTelephone())
    seed_context(m.settings.database_path,scratch.db,'+12025550101')
    with scratch.db.connect() as db:
        assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==0
        assert db.execute('SELECT context FROM concierge_profile').fetchone()[0]=='Approved office information.'
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('oauth_refresh_tokens','mcp_event_subscriptions')").fetchall()


DETAILS={'status':'match','start':'2026-10-26T15:15:00-04:00','end':'2026-10-26T15:30:00-04:00',
    'zone':'America/New_York','location':'Office A'}
NOW=datetime(2026,10,25,16,tzinfo=timezone.utc)


@pytest.mark.parametrize('text,passed',[
    ('October 26th at 3:15 p.m., Office A.',True),
    ('Tomorrow at three fifteen in the afternoon, Office A.',True),
    ('October 26th at 3:15 a.m., Office A.',False),
    ('October 27th at 3:15 p.m., Office A.',False),
    ('October 26th at 3:30 p.m., Office A.',False),
    ('October 26th at 3:15 p.m., Office B.',False),
])
def test_success_requires_the_verified_date_time_and_location_to_be_spoken(text,passed):
    assert all(heard_details(text,DETAILS,NOW).values()) is passed


def test_a_tool_result_or_unheard_assistant_audio_alone_cannot_pass():
    report={'started_at':NOW.isoformat(),'transcript':[],'tools':[],'blocked_tools':[]}
    assert evaluate(report)['passed'] is False


    report['tools']=[{'result':DETAILS,'transcript_index':0}]
    text='October 26th at 3:15 p.m., Office A.'
    report['transcript']=[{'side':'assistant','direction':'output','text':text}]
    assert evaluate(report)['passed'] is False
    report['transcript'].append({'side':'caller','direction':'input','text':text})
    assert evaluate(report)['passed'] is True
    report['blocked_tools']=['book_appointment']
    assert evaluate(report)['passed'] is False


@pytest.mark.parametrize('mode',['done','unheard','no_closing','still_speaking','wrong_location','blocked_action'])
def test_simulated_caller_hangs_up_only_after_a_verified_heard_answer_and_closing(mode):
    from scripts.simulate_appointment_lookup import simulation_caller_done
    text='October 26th at 3:15 p.m., Office A.'
    events=[dict(side='assistant',direction='output',text=text,start_ms=1000,end_ms=2000)]
    if mode!='unheard':events.append(dict(side='caller',direction='input',text=text if mode!='wrong_location' else text.replace('A.','B.'),start_ms=2000,end_ms=3000))
    if mode!='no_closing':events.append(dict(side='caller',direction='output',text='Okay, thanks.',start_ms=3000,end_ms=4000))
    report=dict(started_at=NOW.isoformat(),transcript=events,tools=[dict(result=DETAILS,transcript_index=0)],blocked_tools=['book_appointment'] if mode=='blocked_action' else [])
    assert simulation_caller_done(report,5000 if mode=='still_speaking' else 7000)==(mode=='done')

@pytest.mark.parametrize('room,passed',[
    ('E 1066',True),('E, ten sixty-six',True),('E one zero six six',True),
    ('E one thousand sixty six',True),('E ten sixty-five',False),('E one sixty-six',False),
    ('E-10-66',True),('E-1-0-6-6',True),('E-10-65',False),
])
def test_location_scoring_accepts_equivalent_room_speech_only(room,passed):
    details={**DETAILS,'location':'East Building, West Campus, room E 1066'}
    text='October 26th, 3:15 p.m. East Building, West Campus, room '+room
    assert all(heard_details(text,details,NOW).values()) is passed
