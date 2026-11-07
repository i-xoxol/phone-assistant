"""Private owner review/editing of a character's established fictional life."""
from uuid import UUID
from fastapi import APIRouter,Depends,Request
from fastapi.responses import Response
from app.api.calls import authenticated
from app.api.live import ASSETS,HEADERS,owner,write_owner
from app.api.personalities import checked
from app.calling.character_biography import BiographyUpdate

router=APIRouter()

@router.get('/live/character-biography.js')
def javascript():
    return Response((ASSETS/'character-biography.js').read_text(),media_type='application/javascript',headers=HEADERS)

@router.get('/live/personalities/{pid}/biography',dependencies=[Depends(owner)])
@router.get('/voice/personalities/{pid}/biography',dependencies=[Depends(authenticated)])
def read(pid:UUID,request:Request):
    return checked(request.app.state.manager.character_biographies.get,pid)

@router.put('/live/personalities/{pid}/biography',dependencies=[Depends(write_owner)])
@router.put('/voice/personalities/{pid}/biography',dependencies=[Depends(authenticated)])
def save(pid:UUID,body:BiographyUpdate,request:Request):
    return checked(request.app.state.manager.character_biographies.replace,pid,body)
