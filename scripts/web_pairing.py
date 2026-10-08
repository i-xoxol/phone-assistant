"""Run on the service host over SSH to pair your mobile/browser. Never prints provider keys."""
from app.config import Settings
from app.storage.database import Database
from app.web_auth import WebAuth

if __name__ == '__main__':
    settings = Settings()
    print(WebAuth(Database(settings.database_path)).create_code())
