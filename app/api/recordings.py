"""Owner-authenticated recording playback; provider credentials stay server-side."""
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Request,Response
from app.api.calls import authenticated

router=APIRouter(prefix='/calls',dependencies=[Depends(authenticated)])


async def audio_response(request,cid,rid):
    try: audio=await request.app.state.manager.recordings.audio(str(cid),rid)
    except KeyError: raise HTTPException(404,'Completed recording not found') from None
    except Exception: raise HTTPException(502,'Recording audio is temporarily unavailable') from None
    return Response(audio,media_type='audio/mpeg',headers={'Cache-Control':'no-store',
        'Content-Disposition':'inline; filename="call-recording.mp3"','X-Content-Type-Options':'nosniff'})


@router.get('/{call_id}/recordings')
def recordings(call_id:UUID,request:Request):
    try: return request.app.state.manager.recordings.snapshot(str(call_id))
    except KeyError: raise HTTPException(404,'Call not found') from None


@router.get('/{call_id}/recordings/{recording_id}/audio')
async def audio(call_id:UUID,recording_id:str,request:Request):
    return await audio_response(request,call_id,recording_id)


@router.post('/{call_id}/recordings/stop')
async def stop(call_id:UUID,request:Request):
    try: return await request.app.state.manager.recordings.stop(str(call_id))
    except KeyError: raise HTTPException(404,'Call not found') from None
