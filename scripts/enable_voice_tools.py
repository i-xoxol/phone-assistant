"""Enable verified voice delegation without touching provider credentials."""
from dotenv import set_key
from app.config import Settings, ROOT
from app.storage.database import Database
from app.storage.models import TERMINAL

if __name__=='__main__':
    settings=Settings()
    db=Database(settings.database_path)
    if any(call['status'] not in TERMINAL for call in db.recent_calls()):
        raise SystemExit('Active call exists; defer the service restart.')
    set_key(ROOT/'.env','LIVE_TOOLS_ENABLED','true')
    set_key(ROOT/'.env','OPENAI_BACKEND_MODEL','gpt-6-luna')
    print('Voice tools enabled in configuration; provider credentials unchanged.')
