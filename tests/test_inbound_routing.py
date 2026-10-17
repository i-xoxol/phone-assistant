from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import Settings
from scripts.configure_inbound import configure


def setup():
    settings=Settings(_env_file=None,openai_api_key='test',twilio_account_sid='AC_TEST',
        twilio_auth_token='test',twilio_phone_number='+12025550101',
        public_base_url='https://example.test',local_api_token='test',inbound_enabled=True)
    number=SimpleNamespace(sid='PN_TEST',phone_number=settings.twilio_phone_number,
        voice_url='https://demo.twilio.com/welcome/voice/',voice_method='POST',status_callback='',
        status_callback_method='POST',voice_application_sid='',voice_fallback_url='',voice_fallback_method='POST')
    def update(**fields):
        for key,value in fields.items(): setattr(number,key,value)
    number.update=Mock(side_effect=update)
    client=Mock()
    client.incoming_phone_numbers.list.return_value=[number]
    client.incoming_phone_numbers.return_value.fetch.return_value=number
    return settings,number,client


def test_routing_defaults_to_read_only_and_only_updates_voice_fields(tmp_path,monkeypatch):
    settings,number,client=setup()
    monkeypatch.setattr('scripts.configure_inbound.ROOT',tmp_path)
    assert configure(settings,client)['applied'] is False
    number.update.assert_not_called()
    result=configure(settings,client,apply=True)
    assert result['applied'] is True and result['sms_unchanged'] is True
    assert (tmp_path/'data/routing-backups').is_dir()
    assert set(number.update.call_args.kwargs)=={'voice_url','voice_method','status_callback','status_callback_method'}


def test_routing_refuses_custom_workflows_or_disabled_service(tmp_path,monkeypatch):
    settings,number,client=setup()
    monkeypatch.setattr('scripts.configure_inbound.ROOT',tmp_path)
    number.voice_url='https://other.example.test/answer'
    with pytest.raises(ValueError,match='Custom voice routing'): configure(settings,client,apply=True)
    number.voice_application_sid='AP_TEST'
    with pytest.raises(ValueError,match='TwiML Application'): configure(settings,client,apply=True,replace_existing=True)
    settings.inbound_enabled=False
    with pytest.raises(ValueError,match='Enable and test'): configure(settings,client,apply=True)
    number.update.assert_not_called()
