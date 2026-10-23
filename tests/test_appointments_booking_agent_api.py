import asyncio
import json
from types import SimpleNamespace

from app.appointments.booking_agent import BookingAction,choose_action,PROMPT
from app.config import Settings
from tests.appointment_fixtures import NOW,SLOT
from app.appointments.contracts import Appointment


def test_luna_reads_conversation_and_typed_proposal_in_priority_mode_without_action_tools(monkeypatch):
    seen={}
    class Client:
        def __init__(self,**kwargs):self.responses=SimpleNamespace(parse=self.parse)
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def close(self):pass
        async def parse(self,**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(output_parsed=BookingAction(action='book',offer_turn=0,answer_turn=1))
    monkeypatch.setattr('app.appointments.booking_agent.AsyncOpenAI',Client)
    turns=[dict(turn=0,speaker='assistant',text='Monday at ten works?',end=2),
        dict(turn=1,speaker='callee',text='Uh, I guess, yeah.',end=3)]
    result=asyncio.run(choose_action(Settings(_env_file=None),turns,Appointment('Casey','Example','course guidance',SLOT,'Office A'),NOW))
    assert result.action=='book' and seen['model']=='gpt-6-luna' and seen['service_tier']=='priority'
    assert seen['reasoning']=={'effort':'low'} and not seen['store'] and 'tools' not in seen
    assert json.loads(seen['input'])['conversation']==turns and seen['instructions']==PROMPT


def test_model_failure_returns_a_bounded_unavailable_result(monkeypatch):
    class Client:
        def __init__(self,**kwargs):self.responses=SimpleNamespace(parse=self.parse)
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def close(self):pass
        async def parse(self,**_):raise TimeoutError()
    monkeypatch.setattr('app.appointments.booking_agent.AsyncOpenAI',Client)
    assert asyncio.run(choose_action(Settings(_env_file=None),[],Appointment('Casey','Example','course guidance',SLOT,'Office A'),NOW)) is None
