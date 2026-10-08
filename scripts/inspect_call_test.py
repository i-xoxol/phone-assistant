"""Read-only audit of the latest call's voice actions and event subscriptions."""
import json
from app.config import Settings
from app.storage.database import Database

if __name__ == '__main__':
    settings = Settings()
    database = Database(settings.database_path)
    with database.connect() as db:
        call = db.execute('SELECT call_id,created_at,status,objective,context,ivr_mode FROM calls ORDER BY created_at DESC LIMIT 1').fetchone()
        if not call:
            raise SystemExit('No calls')
        cid = call['call_id']
        tools = [dict(row) for row in db.execute('SELECT name,status,created_at FROM voice_tool_operations WHERE call_id=? ORDER BY created_at', (cid,))]
        keypad = [dict(row) for row in db.execute('SELECT digits,status,created_at FROM keypad_events WHERE call_id=? ORDER BY id', (cid,))]
        subscriptions = [dict(row) for row in db.execute('SELECT name,active,expires_at,cursor,failures FROM mcp_event_subscriptions WHERE call_id=?', (cid,))]
        print(json.dumps({'call':dict(call),'voice_tool_operations':tools,'keypad_events':keypad,
                         'subscriptions':subscriptions},ensure_ascii=False))
