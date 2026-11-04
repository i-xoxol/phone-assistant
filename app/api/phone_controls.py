"""Owner controls under existing session/CSRF and bearer authentication boundaries."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from app.api.live import owner, write_owner, HEADERS
from app.api.calls import authenticated
from app.calling.recording_settings import RecordingChoice, NoticeText, MAX_UPLOAD
from app.calling.voice_backends import ContactVoiceChoice

router=APIRouter()

def preferences(request): return request.app.state.manager.recordings.preferences

def bad(exc): return HTTPException(422,str(exc))

@router.get('/live/recording-settings',dependencies=[Depends(owner)])
@router.get('/voice/recording-settings',dependencies=[Depends(authenticated)])
def recording_settings(request:Request): return preferences(request).get()

@router.put('/live/recording-settings',dependencies=[Depends(write_owner)])
@router.put('/voice/recording-settings',dependencies=[Depends(authenticated)])
def save_recording_settings(body:RecordingChoice,request:Request): return preferences(request).set(body)

@router.put('/live/contacts/{contact_id}/voice',dependencies=[Depends(write_owner)])
@router.put('/concierge/contacts/{contact_id}/voice',dependencies=[Depends(authenticated)])
def contact_voice(contact_id:UUID,body:ContactVoiceChoice,request:Request):
    try: return request.app.state.manager.voice_backends.set_contact(contact_id,body)
    except ValueError as exc: raise bad(exc) from None

@router.get('/live/recording-notices',dependencies=[Depends(owner)])
@router.get('/voice/recording-notices',dependencies=[Depends(authenticated)])
def notices(request:Request): return preferences(request).notices()

@router.post('/live/recording-notices/{language}/generate',dependencies=[Depends(write_owner)])
@router.post('/voice/recording-notices/{language}/generate',dependencies=[Depends(authenticated)])
async def generate(language:str,body:NoticeText,request:Request):
    try: return await preferences(request).generate(language,body.text)
    except ValueError as exc: raise bad(exc) from None
    except Exception: raise HTTPException(502,'Notice generation failed; previous audio preserved') from None

@router.post('/live/recording-notices/{language}/upload',dependencies=[Depends(write_owner)])
async def upload(language:str,request:Request):
    # Stream with a hard bound before parsing/decoding. No unbounded multipart file.
    audio=bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio)>MAX_UPLOAD: raise HTTPException(413,'Clip exceeds 2 MiB')
    try: return preferences(request).stage(language,request.query_params.get('text',''),bytes(audio))
    except ValueError as exc: raise bad(exc) from None

class Activate(BaseModel):
    asset_id:str=Field(pattern=r'^[0-9a-f]{64}$')

@router.post('/live/recording-notices/{language}/activate',dependencies=[Depends(write_owner)])
@router.post('/voice/recording-notices/{language}/activate',dependencies=[Depends(authenticated)])
def activate(language:str,body:Activate,request:Request):
    try: return preferences(request).activate(language,body.asset_id)
    except ValueError as exc: raise bad(exc) from None

@router.get('/live/recording-notices/{language}/audio',dependencies=[Depends(owner)])
def preview(language:str,request:Request,asset_id:str|None=None):
    try: path=preferences(request).asset_path(language,asset_id)
    except ValueError: raise HTTPException(404,'Notice not found') from None
    if not path.is_file(): raise HTTPException(404,'Notice not prepared')
    return Response(path.read_bytes(),media_type='audio/wav',headers=HEADERS)

@router.post('/live/voice/refresh-library',dependencies=[Depends(write_owner)])
@router.post('/voice/refresh-library',dependencies=[Depends(authenticated)])
async def refresh_library(request:Request):
    try: return await request.app.state.manager.voice_backends.refresh_elevenlabs()
    except Exception:
        raise HTTPException(502,'Could not refresh ElevenLabs library; previous catalog preserved') from None
