from app.config import Settings
from app.calling.lookup import cli_arguments
from app.calling.prompts import session_config


def test_fast_cli_keeps_subscription_auth_and_disabled_tools(tmp_path):
    args=cli_arguments('codex',tmp_path,tmp_path/'schema',fast=True)
    assert 'service_tier="fast"' in args and args[args.index('fast_mode')-1]=='--enable'
    assert 'forced_login_method="chatgpt"' in args and '--ignore-user-config' in args
    assert args[args.index('multi_agent')-1]=='--disable'
    standard=cli_arguments('codex',tmp_path,tmp_path/'schema')
    assert 'service_tier="fast"' not in standard and standard[standard.index('fast_mode')-1]=='--disable'


def test_fast_live_delegation_applies_to_inbound_and_outbound():
    settings=Settings(_env_file=None,openai_backend_fast=True,live_tools_enabled=True)
    outbound={'recipient':'Office','objective':'Check a time','context':'','constraints':[]}
    inbound={'direction':'inbound','phone_number':'+12025550101','inbound_mode':'messages'}
    for record in (outbound,inbound):
        responses=session_config(settings,record)['delegation']['responses']
        assert responses['model']=='gpt-6-luna' and responses['service_tier']=='priority'
        assert responses['reasoning']=={'effort':'low'}
    settings.openai_backend_fast=False
    assert 'service_tier' not in session_config(settings,outbound)['delegation']['responses']


def test_appointment_concierge_uses_priority_luna_with_the_general_fast_setting_off():
    settings=Settings(_env_file=None,openai_backend_fast=False)
    settings.appointments_enabled=True
    record={'direction':'inbound','phone_number':'+12025550101','inbound_mode':'concierge'}
    responses=session_config(settings,record)['delegation']['responses']
    assert responses['model']=='gpt-6-luna' and responses['service_tier']=='priority'
    assert responses['reasoning']=={'effort':'low'}
    record['inbound_mode']='messages'
    assert 'service_tier' not in session_config(settings,record)['delegation']['responses']
