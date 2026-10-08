"""Owner-only browser pairing; no API keys or bearer tokens in browser URLs."""
import hashlib
import secrets
import time


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class WebAuth:
    def __init__(self, database):
        self.db = database
        self.failures = []
        with database.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_sessions (hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
            ''')

    def create_code(self, minutes: int = 15) -> str:
        code = secrets.token_hex(8)
        with self.db.connect() as db:
            db.execute('DELETE FROM web_pairing WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO web_pairing VALUES (?,?)', (digest(code), time.time() + minutes * 60))
        return code

    def login(self, code: str) -> str | None:
        stamp = time.time()
        self.failures = [t for t in self.failures if t > stamp - 60]
        if len(self.failures) >= 30:
            return None
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('DELETE FROM web_pairing WHERE hash=? AND expires>?',
                              (digest(code.strip().lower()), stamp))
            if not used.rowcount:
                self.failures.append(stamp)
                return None
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM web_sessions WHERE expires<?', (stamp,))
            db.execute('INSERT INTO web_sessions VALUES (?,?,?)',
                       (digest(token), secrets.token_urlsafe(32), stamp + 7 * 86400))
        return token

    def session(self, token: str) -> dict | None:
        with self.db.connect() as db:
            row = db.execute('SELECT csrf,expires FROM web_sessions WHERE hash=? AND expires>?',
                             (digest(token), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token: str):
        with self.db.connect() as db:
            db.execute('DELETE FROM web_sessions WHERE hash=?', (digest(token),))
