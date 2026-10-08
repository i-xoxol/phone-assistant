import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.storage.models import CallRequest, DigitsRequest, CallUpdateRequest

bearer = HTTPBearer(auto_error=False)


def authenticated(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = request.app.state.settings.local_api_token.get_secret_value()
    if not token or not credentials or not secrets.compare_digest(credentials.credentials, token):
        raise HTTPException(401, 'A local API bearer token is required')


router = APIRouter(prefix='/calls', dependencies=[Depends(authenticated)])


@router.post('', status_code=201)
async def place_call(body: CallRequest, request: Request):
    try:
        return await request.app.state.manager.place_call(body)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None


@router.get('/{call_id}')
async def get_call_status(call_id: UUID, request: Request, refresh: bool = False):
    try:
        manager = request.app.state.manager
        return await manager.refresh(str(call_id)) if refresh else manager.status(str(call_id))
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except Exception:
        raise HTTPException(502, 'Could not refresh from Twilio') from None


@router.get('/{call_id}/result')
def get_call_result(call_id: UUID, request: Request):
    try:
        return request.app.state.manager.db.get(str(call_id))
    except KeyError:
        raise HTTPException(404, 'Call not found') from None


@router.post('/{call_id}/hangup')
async def hangup_call(call_id: UUID, request: Request):
    try:
        return await request.app.state.manager.hangup_call(str(call_id))
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except Exception:
        raise HTTPException(502, 'Hangup unconfirmed; check Twilio') from None


@router.post('/{call_id}/digits')
async def press_digits(call_id: UUID, body: DigitsRequest, request: Request):
    try:
        return await request.app.state.manager.press_digits(str(call_id), body)
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(502, 'Keypad unconfirmed; check call status before continuing') from None


@router.post('/{call_id}/updates')
async def send_call_update(call_id: UUID, body: CallUpdateRequest, request: Request):
    try:
        return await request.app.state.manager.send_call_update(str(call_id), body)
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
