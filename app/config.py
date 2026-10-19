from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')

    openai_api_key: SecretStr = SecretStr('')
    twilio_account_sid: str = ''
    twilio_auth_token: SecretStr = SecretStr('')
    twilio_phone_number: str = ''
    public_base_url: str = ''
    local_api_token: SecretStr = SecretStr('')
    openai_live_model: str = 'gpt-live-1'
    openai_voice: str = 'marin'
    caller_name: str = 'Alex'
    owner_pronouns: Literal['he/him', 'she/her', 'they/them'] = 'they/them'
    assistant_name: str = Field(default='Assistant',min_length=1,max_length=80)
    callback_number: str = ''
    database_path: Path = ROOT / 'data/calls.sqlite3'
    mcp_enabled: bool = False
    oauth_database_path: Path = ROOT / 'data/oauth.sqlite3'
    live_tools_enabled: bool = False
    openai_backend_model: str = 'gpt-6-luna'
    openai_backend_fast: bool = False
    subscription_lookup_fast: bool = False
    webhook_url: str = ''
    webhook_secret: SecretStr = SecretStr('')
    inbound_enabled: bool = False
    inbound_max_duration_minutes: int = Field(default=5, ge=1, le=15)
    inbound_max_calls_per_hour: int = Field(default=10, ge=1, le=100)
    inbound_mode: Literal['messages', 'concierge'] = 'messages'
    supervisor_wait_seconds: int = Field(default=45, ge=10, le=120)
    inbound_booking_allowed: bool = False
    subscription_lookup_enabled: bool = False
    subscription_lookup_timeout_seconds: int = Field(default=20, ge=5, le=40)
    owner_transfer_enabled: bool = False
    owner_transfer_number: str = Field(default='',pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    owner_transfer_ring_seconds: int = Field(default=20,ge=10,le=30)
    owner_observation_enabled: bool = False
    openai_transcription_model: str = 'gpt-live-transcribe'
    call_history_enabled: bool = False
    call_history_days: int = Field(default=90,ge=1,le=365)
    inbound_answer_delay_seconds: int = Field(default=0,ge=0,le=30)
    caller_id_trust_saved_contacts: bool = False
    owner_speech_enabled: bool = False
    owner_speech_model: str = 'gpt-realtime-2.1-mini'
    owner_speech_voice: str = 'cedar'
    call_recording_enabled: bool = False
    recording_notice_directory: Path = ROOT / 'data/recording-notices'
    inbound_missed_call_context: bool = False
    missed_call_notifications_enabled: bool = False


    def backend_options(self) -> dict:
        options = {'service_tier': 'priority'} if self.openai_backend_fast else {}
        if self.openai_backend_model == 'gpt-6-luna':
            options['reasoning'] = {'effort': 'low'}
        return options

    @field_validator('webhook_url')
    @classmethod
    def webhook_https(cls, value: str) -> str:
        if value:
            parts = urlsplit(value)
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
                raise ValueError('WEBHOOK_URL must be an HTTPS URL without userinfo or a fragment')
        return value

    @field_validator('database_path')
    @classmethod
    def absolute_database(cls, path: Path) -> Path:
        return path if path.is_absolute() else ROOT / path

    @field_validator('public_base_url')
    @classmethod
    def public_origin(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username
                or parts.password or parts.path not in ('', '/')
                or parts.query or parts.fragment):
            raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin without a path')
        return value.rstrip('/')

    def missing(self) -> list[str]:
        names = ['openai_api_key', 'twilio_account_sid', 'twilio_auth_token',
                 'twilio_phone_number', 'public_base_url', 'local_api_token']
        return [name.upper() for name in names if not (
            getattr(self, name).get_secret_value()
            if isinstance(getattr(self, name), SecretStr) else getattr(self, name)
        )]

    def public_url(self, path: str, *, websocket: bool = False) -> str:
        base = self.public_base_url
        return (base.replace('https://', 'wss://', 1) if websocket else base) + path
