"""Read-only metadata check before restarting the single-worker bridge."""
import json
from app.storage.database import Database
from app.config import Settings

if __name__ == '__main__':
    settings=Settings()
    with Database(settings.database_path, database_url=settings.database_url, schema=settings.database_schema_prefix+'_calls').connect() as db:
        rows=db.execute('SELECT status,count(*) FROM calls GROUP BY status').fetchall()
    print(json.dumps({'states': dict(rows), 'live_tools_enabled': getattr(settings,'live_tools_enabled',False),
                      'webhook_configured': bool(getattr(settings,'webhook_url',''))}))
