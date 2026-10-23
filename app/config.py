from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    owner_screen_speech_enabled: bool = False
    owner_screen_speech_model: str = 'gpt-realtime-2.1'
    owner_screen_speech_voice: str = 'cedar'
    owner_prompt_directory: Path = ROOT / 'data/owner-prompts'
    owner_coaching_strategy: Literal['tts','hybrid','realtime'] = 'tts'
    owner_realtime_transport: Literal['conference','bridge'] = 'conference'
    owner_coaching_application_sid: str = Field(default='',pattern=r'^(?:|AP[0-9a-fA-F]{32})$')
    owner_realtime_coaching_model: Literal['gpt-realtime-2.1'] = 'gpt-realtime-2.1'
    owner_realtime_coaching_voice: str = 'cedar'
    owner_realtime_coaching_max_hints: int = Field(default=40,ge=1,le=100)
    owner_live_coaching_idle_seconds: int = Field(default=20,ge=5,le=60)
    owner_live_coaching_max_seconds: int = Field(default=60,ge=10,le=120)
    owner_live_coaching_max_sessions: int = Field(default=2,ge=1,le=4)
    owner_live_coaching_max_utterance_seconds: int = Field(default=8,ge=3,le=12)
    call_recording_enabled: bool = False
    recording_notice_directory: Path = ROOT / 'data/recording-notices'
    inbound_missed_call_context: bool = False
    missed_call_notifications_enabled: bool = False
    appointments_enabled: bool = False
    owner_calendar_coaching_enabled: bool = False
    appointments_writes_enabled: bool = False
    appointment_sync_enabled: bool = False
    appointment_database_path: Path = ROOT / 'data/appointments.sqlite3'
    appointment_google_token_file: Path | None = None
    appointment_account_namespace: str = ''
    appointment_calendar_id: str = 'primary'
    appointment_mirror_status_url: str = ''
    appointment_mirror_window_verified: bool = False
    appointment_office_event_ids: list[str] = Field(default_factory=list)
    appointment_mirror_source_tenant: str = ''
    appointment_mirror_source_calendar: str = ''

    @model_validator(mode='after')
    def calendar_boundary(self):
        if (self.appointments_writes_enabled or self.appointment_sync_enabled) and not self.appointments_enabled:
            raise ValueError('Appointment writes/indexing require the separately enabled appointment feature')
        if self.appointments_enabled or self.owner_calendar_coaching_enabled:
            if not self.appointment_google_token_file or not self.appointment_account_namespace or not self.appointment_mirror_status_url:
                raise ValueError('Configure the existing Google credential boundary, account namespace and verified mirror status URL')
            if self.appointment_database_path.resolve()==self.database_path.resolve():
                raise ValueError('The private appointment registry must use a separate database')
        if bool(self.appointment_mirror_source_tenant)!=bool(self.appointment_mirror_source_calendar):
            raise ValueError('Mirror aliases require both verified source account and calendar namespaces')
        return self


    def backend_options(self,*,appointments=False) -> dict:
        options = {'service_tier': 'priority'} if (self.openai_backend_fast or
            (appointments and self.openai_backend_model=='gpt-6-luna')) else {}
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

    @field_validator('database_path','appointment_database_path')
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
