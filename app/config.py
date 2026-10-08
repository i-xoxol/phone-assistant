from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
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
    callback_number: str = ''
    database_path: Path = ROOT / 'data/calls.sqlite3'
    mcp_enabled: bool = False
    oauth_database_path: Path = ROOT / 'data/oauth.sqlite3'
    live_tools_enabled: bool = False
    openai_backend_model: str = 'gpt-6-luna'
    webhook_url: str = ''
    webhook_secret: SecretStr = SecretStr('')

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
