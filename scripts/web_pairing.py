"""Run on the service host over SSH to pair your mobile/browser. Never prints provider keys."""
from app.config import Settings
from app.storage.database import Database
from app.web_auth import WebAuth
import argparse

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-password', action='store_true',
                        help='Host-only recovery: clear the password, revoke browser sessions/codes, then issue a new code.')
    args = parser.parse_args()
    settings = Settings()
    auth = WebAuth(Database(settings.database_path))
    if args.reset_password:
        auth.reset_password()
    print(auth.create_code())
