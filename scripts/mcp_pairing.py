"""Generate a short-lived, single-use owner pairing code on the service host."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.oauth_provider import PairingOAuthProvider

if __name__ == '__main__':
    settings = Settings()
    provider = PairingOAuthProvider(settings.oauth_database_path, settings.public_base_url, database_url=settings.database_url, schema=settings.database_schema_prefix+'_oauth')
    print(provider.create_pairing_code())
