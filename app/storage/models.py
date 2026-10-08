from enum import StrEnum

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CallStatus(StrEnum):
    queued = 'queued'
    dialing = 'dialing'
    ringing = 'ringing'
    in_progress = 'in_progress'
    completed = 'completed'
    failed = 'failed'
    busy = 'busy'
    no_answer = 'no_answer'
    cancelled = 'cancelled'


TERMINAL = {'completed', 'failed', 'busy', 'no_answer', 'cancelled'}
E164 = r'^\+[1-9][0-9]{7,14}$'


class CallRequest(BaseModel):
    phone_number: str = Field(pattern=E164)
    recipient: str = Field(default='', max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    context: str = Field(default='', max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    max_duration_minutes: int = Field(default=15, ge=1, le=15)
    ivr_mode: bool = False


class DigitsRequest(BaseModel):
    digits: str = Field(pattern=r'^[0-9*#wW]{1,20}$')
    reason: str = Field(min_length=1, max_length=500)


class CallUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=400)
    mode: Literal['context', 'say', 'instructions'] = 'context'
    update_id: UUID | None = None

    @field_validator('content')
    @classmethod
    def concise_content(cls, value):
        value = value.strip()
        # Conservative byte budget keeps each append below Live's 500-token
        # limit, including the policy prefix, without another tokenizer package.
        if not value or len(value.encode('utf-8')) > 400:
            raise ValueError('Send a short update of 1–400 UTF-8 bytes')
        return value
