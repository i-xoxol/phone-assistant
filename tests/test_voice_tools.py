import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.calling.live_session import MediaBridge
from app.calling.tools import KeypadArguments
from app.calling.prompts import session_config
from tests.test_calls import service, create
from tests.test_live import FakeConnection


def test_responses_configuration_keeps_voice_and_authorization(service):
    client, manager, _ = service
    cid=create(client).json()['call_id']
    manager.settings.live_tools_enabled=True
    config=session_config(manager.settings,manager.db.get(cid))
    assert config['model']=='gpt-live-1'
    backend=config['delegation']['responses']
    assert backend['model']=='gpt-6-luna'
    assert backend['parallel_tool_calls'] is False
    assert {t['name'] for t in backend['tools']}=={'press_digits','end_call'}
    assert 'OTP' in backend['instructions'] and 'approve charges' in config['instructions']


@pytest.mark.parametrize('value',['12345','1w','１２',''])
def test_voice_keypad_restricted(value):
    with pytest.raises(ValidationError): KeypadArguments(digits=value,reason='Navigation')


def test_nested_function_items_survive_empty_response_output_and_are_deduplicated(service):
    client, manager, gateway = service
    cid=create(client).json()['call_id']
    manager.transition(cid,'in_progress')
    async def exercise():
        connection=FakeConnection()
        bridge=MediaBridge(None,connection,manager,cid,'MZ_TOOLS')
        manager.attach_bridge(cid,bridge)
        sent=[]
        async def output(**kwargs): sent.append(kwargs['item'])
        async def resume(**kwargs): sent.append('continued')
        connection.response=SimpleNamespace(item=SimpleNamespace(create=output),create=resume)
        async def digits(sid,call_id,digit):
            assert digit=='6'
            manager.attach_bridge(call_id,SimpleNamespace(handoff=False))
        gateway.press_digits=digits
        def forward(event): bridge.tools.handle({'type':'response.event','delegation_id':'del_1','event':event})
        forward({'type':'response.created','response':{'id':'resp_1'}})
        item={'type':'function_call','call_id':'func_1','name':'press_digits','arguments':'{"digits":"6","reason":"Explicit navigation demonstration"}'}
        forward({'type':'response.function_call_arguments.done','arguments':item['arguments']})
        assert not manager.tool_tasks
        forward({'type':'response.output_item.done','item':item})
        forward({'type':'response.output_item.done','item':item})
        forward({'type':'response.completed','response':{'id':'resp_1','output':[]}})
        await asyncio.gather(*manager.tool_tasks)
        assert len(manager.db.get(cid)['keypad_events'])==1
        operations=manager.db.get(cid)['voice_tool_operations']
        assert len(operations)==1 and operations[0]['name']=='press_digits' and operations[0]['status']=='confirmed'
        assert not sent  # Old media socket retired; result is restored from durable state.
        assert not manager.db.claim_tool(cid,'func_1','press_digits')
    asyncio.run(exercise())


def test_voice_end_call_outputs_result_and_resumes_backend(service):
    client, manager, _ = service
    cid=create(client).json()['call_id']
    async def exercise():
        connection=FakeConnection()
        bridge=MediaBridge(None,connection,manager,cid,'MZ_END')
        manager.attach_bridge(cid,bridge)
        sent=[]
        async def output(**kwargs): sent.append(kwargs['item'])
        async def resume(**kwargs): sent.append('continued')
        connection.response=SimpleNamespace(item=SimpleNamespace(create=output),create=resume)
        state={'calls':[{'name':'end_call','call_id':'finish1','arguments':'{"reason":"Objective complete"}'}]}
        await bridge.tools.execute('del_1',state)
        assert bridge.end_requested.is_set()
        assert sent[0]['call_id']=='finish1' and sent[-1]=='continued'
        assert manager.db.get(cid)['voice_tool_operations'][0]['name']=='end_call'
        assert 'closing phrase' in connection.greeting['content']
    asyncio.run(exercise())
