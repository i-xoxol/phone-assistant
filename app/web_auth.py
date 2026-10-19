"""Single-owner browser access with pairing recovery and salted password hashes."""
import hashlib
import secrets
import time

SESSION_SECONDS = 7 * 86400
PASSWORD_MIN = 12
PASSWORD_MAX = 256


def password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1,
                          dklen=64, maxmem=64 * 1024 * 1024)


def valid_password(password: str) -> bool:
    return PASSWORD_MIN <= len(password) <= PASSWORD_MAX


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
                CREATE TABLE IF NOT EXISTS web_password (
                    id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL,
                    hash BLOB NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_login_attempts (bucket TEXT NOT NULL, attempted REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS web_login_attempts_bucket ON web_login_attempts(bucket,attempted);
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
            return self._new_session(db, stamp)

    @staticmethod
    def _new_session(db, stamp: float) -> str:
        token = secrets.token_urlsafe(32)
        db.execute('DELETE FROM web_sessions WHERE expires<?', (stamp,))
        db.execute('INSERT INTO web_sessions VALUES (?,?,?)',
                   (digest(token), secrets.token_urlsafe(32), stamp + SESSION_SECONDS))
        return token

    def password_enabled(self) -> bool:
        with self.db.connect() as db:
            return db.execute('SELECT 1 FROM web_password WHERE id=1').fetchone() is not None

    def _allow_password_attempt(self, bucket: str) -> bool:
        # Persist a short, aggregate budget across service restarts. Never trust X-Forwarded-For.
        stamp = time.time()
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM web_login_attempts WHERE attempted<=?', (stamp - 60,))
            count = db.execute('SELECT count(*) FROM web_login_attempts WHERE bucket=?', (bucket,)).fetchone()[0]
            if count >= 10:
                return False
            db.execute('INSERT INTO web_login_attempts VALUES (?,?)', (bucket, stamp))
        return True

    def password_login(self, username: str, password: str) -> str | None:
        if not self._allow_password_attempt('login'):
            return None
        if username != 'owner' or not valid_password(password):
            return None
        with self.db.connect() as db:
            row = db.execute('SELECT salt,hash FROM web_password WHERE id=1').fetchone()
        if not row or not secrets.compare_digest(password_hash(password, row['salt']), row['hash']):
            return None
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A concurrent password change/recovery must invalidate this login too.
            current = db.execute('SELECT hash FROM web_password WHERE id=1').fetchone()
            if not current or not secrets.compare_digest(current['hash'], row['hash']):
                return None
            return self._new_session(db, time.time())

    def set_password(self, token: str, current_password: str, new_password: str) -> str:
        """A paired owner sets the first password; changes require the current one."""
        if not self.session(token):
            raise ValueError('Sign in again before setting a password.')
        if not valid_password(new_password):
            raise ValueError(f'Use {PASSWORD_MIN} to {PASSWORD_MAX} characters for the new password.')
        if not self._allow_password_attempt('change'):
            raise ValueError('Too many attempts. Try again in a minute.')
        with self.db.connect() as db:
            previous = db.execute('SELECT salt,hash FROM web_password WHERE id=1').fetchone()
        if previous and (not valid_password(current_password) or not secrets.compare_digest(
                password_hash(current_password, previous['salt']), previous['hash'])):
            raise ValueError('Current password is incorrect.')
        salt = secrets.token_bytes(16)
        hashed = password_hash(new_password, salt)
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM web_sessions WHERE hash=? AND expires>?',
                              (digest(token), time.time())).fetchone():
                raise ValueError('Sign in again before setting a password.')
            current = db.execute('SELECT hash FROM web_password WHERE id=1').fetchone()
            if bool(current) != bool(previous) or (current and current['hash'] != previous['hash']):
                raise ValueError('Password changed in another browser. Try again.')
            db.execute('INSERT OR REPLACE INTO web_password VALUES (1,?,?,?)', (salt, hashed, time.time()))
            db.execute('DELETE FROM web_sessions')
            db.execute('DELETE FROM web_pairing')
            return self._new_session(db, time.time())

    def reset_password(self):
        """Host-only recovery: revoke credentials, sessions and pending pairing codes."""
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('web_password', 'web_sessions', 'web_pairing', 'web_login_attempts'):
                db.execute(f'DELETE FROM {table}')

    def session(self, token: str) -> dict | None:
        with self.db.connect() as db:
            row = db.execute('SELECT csrf,expires FROM web_sessions WHERE hash=? AND expires>?',
                             (digest(token), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token: str):
        with self.db.connect() as db:
            db.execute('DELETE FROM web_sessions WHERE hash=?', (digest(token),))
