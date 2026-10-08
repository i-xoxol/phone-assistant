"""Read-only metadata check before restarting the single-worker bridge."""
import json
import sqlite3
from app.config import Settings

if __name__ == '__main__':
    settings=Settings()
    with sqlite3.connect(settings.database_path) as db:
        rows=db.execute('SELECT status,count(*) FROM calls GROUP BY status').fetchall()
    print(json.dumps({'states': dict(rows), 'live_tools_enabled': getattr(settings,'live_tools_enabled',False),
                      'webhook_configured': bool(getattr(settings,'webhook_url',''))}))
